"""Kiro CLI sem terminal: instalar e entrar na conta a partir da introdução da Lunmi.

Por que existe: o caminho oficial é `irm https://cli.kiro.dev/install.ps1 | iex` e depois
`kiro-cli login` num terminal. Para quem nunca abriu um terminal isso não é plug and play.
Aqui a Lunmi faz os mesmos passos do script oficial (manifesto, MSI, SHA-256) e lança o login
com o fornecedor já escolhido, para o browser abrir direto na página certa.

* O MSI é por utilizador (sem `ALLUSERS`, "elevation not required" no SummaryInformation), por
  isso `/passive` instala em `%LOCALAPPDATA%\\Kiro-Cli` sem pedir administrador.
* Só se descarrega do anfitrião oficial e só se instala um ficheiro cujo tamanho e SHA-256
  batem com o manifesto.
* O estado público nunca leva o e-mail da conta: só o fornecedor ("Google", "GitHub", ...).
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from app.brain.cli.catalog import CLI_SPECS
from app.brain.cli.process import resolve_executable

BASE_URL = "https://prod.download.cli.kiro.dev/stable"
MANIFEST_URL = f"{BASE_URL}/latest/manifest.json"
# Caminho relativo do MSI no manifesto, ex. "2.35.1/kiro-cli-x86_64-pc-windows-msvc.msi".
_DOWNLOAD_PATH = re.compile(r"[0-9A-Za-z.]+/kiro-cli-x86_64-pc-windows-msvc\.msi")
_SHA256 = re.compile(r"[0-9a-f]{64}")
MAX_MSI_BYTES = 1024 * 1024 * 1024
# Windows 11 é requisito do Kiro CLI nativo (kiro.dev/docs/cli/installation).
MIN_WINDOWS_BUILD = 22000
LOGIN_TIMEOUT_SECONDS = 600.0
LOGIN_ARGS: dict[str, tuple[str, ...]] = {
    "google": ("--social", "google"),
    "github": ("--social", "github"),
    "builder": ("--license", "free"),
}
_LOGGED_IN = re.compile(r"logged in with ([^\r\n]+)", re.IGNORECASE)
_URL = re.compile(r"https://[^\s\"'<>]+")


def windows_supported() -> bool:
    if sys.platform != "win32":
        return False
    return sys.getwindowsversion().build >= MIN_WINDOWS_BUILD


def find_executable() -> str | None:
    spec = CLI_SPECS["kiro"]
    return resolve_executable(spec.executable, spec.search_paths)


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def account_of(whoami_output: str) -> str | None:
    """`Logged in with Google` -> "Google"; o e-mail da linha seguinte nunca sai daqui."""
    found = _LOGGED_IN.search(whoami_output)
    return found.group(1).strip()[:40] if found else None


def pick_msi(manifest: Any) -> tuple[str, str, int]:
    """(caminho relativo, sha256, tamanho) do MSI Windows x64, ou ValueError."""
    packages = manifest.get("packages") if isinstance(manifest, dict) else None
    for item in packages if isinstance(packages, list) else []:
        if not isinstance(item, dict):
            continue
        if (item.get("os"), item.get("architecture"), item.get("kind")) != ("windows", "x86_64", "msi"):
            continue
        download, sha, size = item.get("download"), item.get("sha256"), item.get("size")
        if (isinstance(download, str) and _DOWNLOAD_PATH.fullmatch(download) and ".." not in download
                and isinstance(sha, str) and _SHA256.fullmatch(sha.lower())
                and isinstance(size, int) and 0 < size <= MAX_MSI_BYTES):
            return download, sha.lower(), size
    raise ValueError("O manifesto do Kiro não tem um instalador Windows válido.")


class KiroSetup:
    """Uma operação de cada vez (instalar ou entrar), com estado público e cancelamento."""

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        self._emit = emit
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._login: subprocess.Popen[str] | None = None
        self._state: dict[str, Any] = {"phase": "idle", "progress": None, "error": None, "url": None}

    # ------------------------------------------------------------------ estado --
    def status(self) -> dict[str, Any]:
        """Estado completo; consulta o CLI (até ~25 s com o `whoami`)."""
        executable = find_executable()
        installed, version, account = executable is not None, None, None
        if executable:
            try:
                words = _run([executable, "--version"], 30).stdout.split()
                version = words[-1] if words else None
                account = account_of(_run([executable, "whoami"], 25).stdout)
            except (OSError, subprocess.TimeoutExpired):
                pass
        with self._lock:
            return {**self._state, "supported": windows_supported(), "installed": installed,
                    "version": version, "logged_in": account is not None, "account": account}

    def _set(self, **changes: Any) -> None:
        with self._lock:
            self._state.update(changes)
            snapshot = dict(self._state)
        self._emit(snapshot)

    def _start(self, target: Callable[[], None]) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("busy")
            self._cancel.clear()
            self._thread = threading.Thread(target=target, name="kiro-setup", daemon=True)
            self._thread.start()
        return {"started": True}

    def cancel(self) -> dict[str, Any]:
        self._cancel.set()
        with self._lock:
            login = self._login
        if login is not None and login.poll() is None:
            login.kill()
        return {"cancelled": True}

    # --------------------------------------------------------------- instalar --
    def install(self) -> dict[str, Any]:
        if not windows_supported():
            raise RuntimeError("unsupported")
        return self._start(self._install)

    def _install(self) -> None:
        workdir = Path(tempfile.mkdtemp(prefix="lunmi-kiro-"))
        msi = workdir / "kiro-cli.msi"
        try:
            self._set(phase="downloading", progress=0.0, error=None, url=None)
            with httpx.Client(timeout=httpx.Timeout(30.0, read=60.0), follow_redirects=False) as client:
                response = client.get(MANIFEST_URL)
                response.raise_for_status()
                download, expected_sha, expected_size = pick_msi(response.json())
                digest, received = hashlib.sha256(), 0
                with client.stream("GET", f"{BASE_URL}/{download}") as stream, msi.open("wb") as out:
                    stream.raise_for_status()
                    for chunk in stream.iter_bytes(1 << 20):
                        if self._cancel.is_set():
                            self._set(phase="idle", progress=None)
                            return
                        received += len(chunk)
                        if received > expected_size:
                            raise ValueError("O instalador do Kiro é maior do que o manifesto indica.")
                        digest.update(chunk)
                        out.write(chunk)
                        self._set(progress=round(received / expected_size, 3))
            if received != expected_size or digest.hexdigest() != expected_sha:
                raise ValueError("O instalador do Kiro não passou na verificação (tamanho ou SHA-256).")
            self._set(phase="installing", progress=None)
            # /passive: só a barra de progresso do Windows, sem perguntas; 3010 = pede reinício.
            code = _run(["msiexec", "/i", str(msi), "/passive", "/norestart"], 900).returncode
            if code == 1602:
                self._set(phase="idle")
                return
            if code not in (0, 3010) or find_executable() is None:
                raise RuntimeError(f"O instalador do Windows terminou com o código {code}.")
            self._set(phase="installed")
        except (httpx.HTTPError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self._set(phase="error", progress=None, error=_friendly(exc))
        finally:
            msi.unlink(missing_ok=True)
            try:
                workdir.rmdir()
            except OSError:
                pass

    # ------------------------------------------------------------------ entrar --
    def login(self, method: str) -> dict[str, Any]:
        if method not in LOGIN_ARGS:
            raise ValueError("method")
        if find_executable() is None:
            raise RuntimeError("not_installed")
        return self._start(lambda: self._run_login(method))

    def _run_login(self, method: str) -> None:
        executable = find_executable()
        if executable is None:
            self._set(phase="error", error="O Kiro CLI não está instalado.")
            return
        self._set(phase="waiting_login", progress=None, error=None, url=None)
        try:
            process = subprocess.Popen(
                [executable, "login", *LOGIN_ARGS[method]], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self._set(phase="error", error=_friendly(exc))
            return
        with self._lock:
            self._login = process
        timer = threading.Timer(LOGIN_TIMEOUT_SECONDS, process.kill)
        timer.start()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                # Se o browser não abrir sozinho, o CLI imprime o endereço: a introdução mostra-o.
                found = _URL.search(line)
                if found:
                    self._set(url=found.group(0))
            code = process.wait()
        finally:
            timer.cancel()
            with self._lock:
                self._login = None
        if self._cancel.is_set():
            self._set(phase="idle", url=None)
        elif code == 0:
            self._set(phase="logged_in", url=None)
        else:
            self._set(phase="error", url=None,
                      error="O login no Kiro não terminou. Tenta de novo ou escolhe outra forma de entrar.")


def _friendly(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPError):
        return "Não foi possível descarregar o Kiro CLI. Verifica a ligação à internet e tenta de novo."
    if isinstance(exc, subprocess.TimeoutExpired):
        return "A instalação do Kiro CLI demorou demasiado e foi interrompida."
    return str(exc) or "Não foi possível instalar o Kiro CLI."

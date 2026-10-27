"""Instalação e lifecycle Spotify (I1): deteção, consentimento, job, backup e rollback.

Regras (ver `.agents/SPOTIFY-INTEGRATION-V1-CONTRACT.md`):

* deteção é leitura pura — sem rede, sem invocar o CLI, sem mutar o Spotify;
* `plan` emite um `consent_id` opaco single-use ligado a plano/hash/epoch/alvo;
* `apply` só aceita `consent_id`, devolve `accepted/job_id/epoch` de imediato e corre o
  trabalho numa thread própria, fora do dispatch de 5 s, com progresso por evento;
* fechar o Spotify acontece antes do backup final e do apply; reabrir faz parte do mesmo
  job consentido e não inicia reprodução;
* `verify` só conclui com HMAC + **uma** leitura autenticada; ficheiro/ACK não contam;
* journal atómico, rollback com pós-condições e remoção seletiva quando houve edições
  posteriores — nunca repor um snapshot antigo por cima delas.

Nada aqui importa `output/` nem abre rede durante `get`/`plan`. O download do CLI pinado
só corre dentro do job, por uma função injetável (o gate I1 não faz rede real).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from app.integrations import spotify_contract as contract
from app.integrations.spotify import SpotifyBroker, SpotifyBrokerError

SPOTIFY_EXE_NAME = "Spotify.exe"
EXTENSION_NAME = "lunmi-spotify.js"
EXTENSIONS_DIR_NAME = "Extensions"
CONFIG_FILE_NAME = "config-xpui.ini"

CLI_URL = "https://github.com/spicetify/cli/releases/download/v2.46.2/spicetify-2.46.2-windows-x64.zip"
CLI_ZIP_SHA256 = "9c579fc0020b692d98d4a90b9ddc7ecbbce667a12d6990c57eebbeb361538ceb"
CLI_EXE_SHA256 = "85576d80b04ef8922a7f7fe3befbf9fcc56025dfb699b21339b546893742f0c1"

MAX_CLI_DOWNLOAD_BYTES = 32 * 1024 * 1024
# Distribuição runtime do CLI pinado: o `apply` nativo copia `jsHelper/*` para
# `Apps/xpui/helper` e lê `css-map.json` a partir da pasta do executável. Extrair
# só o .exe deixava o wrapper ausente e o `apply` terminava rc0 sem o injetar.
CLI_RUNTIME_REQUIRED = (
    "spicetify.exe",
    "jsHelper/spicetifyWrapper.js",
    "jsHelper/expFeatures.js",
    "jsHelper/homeConfig.js",
    "jsHelper/sidebarConfig.js",
    "css-map.json",
)
# Caches geradas pelo CLI/Spicetify dentro do perfil: nunca fazem parte do
# conjunto ativo, tal como `Backup`/`cache`/`logs`/`Extracted` no clone privado.
GENERATED_PROFILE_CACHE = frozenset({"backup", "cache", "logs", "extracted"})
MAX_CLI_RUNTIME_BYTES = 64 * 1024 * 1024
MAX_CLI_RUNTIME_FILES = 1024
# Marca escrita só pela extração segura: um `.exe` solto de cache antiga não a
# tem, pelo que a distribuição incompleta nunca é reutilizada em silêncio.
CLI_CACHE_MARKER = ".lunmi-cli-runtime.json"
MAX_PROFILE_BYTES = 16 * 1024 * 1024
MAX_BACKUP_FILES = 512
# Envelope já medido numa instalação real: 1904 ficheiros / 239 104 134 bytes.
# O perfil Spicetify e os alvos do Spotify têm limites distintos; ambos finitos.
MAX_SPOTIFY_APPS_BYTES = 512 * 1024 * 1024
MAX_SPOTIFY_APPS_FILES = 4096
CLI_APPLY_TIMEOUT_S = 180
CLI_CONFIG_TIMEOUT_S = 45
DETECT_TTL_S = 2.0


class SpotifyInstallError(RuntimeError):
    """Falha pública do lifecycle; `code` é o vocabulário fechado do contrato."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code if code in contract.SPOTIFY_ERROR_CODES else "unavailable"


@dataclass(frozen=True)
class Pins:
    spotify: str = contract.SPOTIFY_PINNED_SPOTIFY
    cli: str = contract.SPOTIFY_PINNED_CLI
    url: str = CLI_URL
    zip_sha256: str = CLI_ZIP_SHA256
    exe_sha256: str = CLI_EXE_SHA256


@dataclass
class Detected:
    app: str = "absent"
    version: str = "unknown"
    architecture: str = "unknown"
    cli: str = "missing"
    link: str = "unknown"
    app_running: bool = False
    spotify_dir: Path | None = None
    executable: Path | None = None
    config_root: Path | None = None
    ini: Path | None = None
    extensions_dir: Path | None = None
    writable: bool = False
    fingerprint: str = ""
    extensions: tuple[str, ...] = ()
    note: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "app": self.app,
            "version": self.version,
            "architecture": self.architecture,
            "cli": self.cli,
            "link": self.link,
            "app_running": self.app_running,
        }


@dataclass
class PrivateProfile:
    """Clone privado do perfil spicetify; nunca é o perfil pessoal."""

    root: Path
    config_root: Path
    ini: Path
    extensions_dir: Path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _is_reparse(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    if stat.S_ISLNK(path.lstat().st_mode):
        return True
    return bool(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) & attributes)


def _canonical_under(path: Path, root: Path) -> Path:
    """Recusa traversal/reparse: só um caminho real dentro da raiz canónica."""
    try:
        resolved = path.resolve(strict=True)
        base = root.resolve(strict=True)
    except OSError:
        raise SpotifyInstallError("write_denied") from None
    if resolved != base and base not in resolved.parents:
        raise SpotifyInstallError("invalid_payload")
    if _is_reparse(path) or _is_reparse(base):
        raise SpotifyInstallError("invalid_payload")
    return resolved


def _read_extensions(ini: Path) -> tuple[str, ...]:
    """Lê a lista de extensões do config-xpui.ini (chave `extensions`)."""
    try:
        text = ini.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section not in {"additionaloptions", "setting"} or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().lower() != "extensions":
            continue
        # O spicetify real separa com `|`; aceitar também `,` em configs antigas.
        return tuple(item.strip() for item in re.split(r"[|,]", value) if item.strip())
    return ()


def _parse_version(value: str) -> tuple[int, ...] | None:
    parts = value.strip().split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _version_fit(found: str, pinned: str) -> str:
    seen, pin = _parse_version(found), _parse_version(pinned)
    if seen is None or pin is None:
        return "unknown"
    if seen == pin:
        return "supported"
    return "newer" if seen > pin else "older"


def _default_file_version(path: Path) -> str | None:
    try:
        import win32api  # type: ignore[import-not-found]

        info = win32api.GetFileVersionInfo(str(path), "\\")
        ms, ls = int(info["FileVersionMS"]), int(info["FileVersionLS"])
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception:
        return None


def _spotify_running() -> bool:
    try:
        import psutil

        for process in psutil.process_iter(["name"]):
            if (process.info.get("name") or "").lower() == SPOTIFY_EXE_NAME.lower():
                return True
    except Exception:
        return False
    return False


def _post_close_windows(pid: int) -> bool:
    """Pede fecho gracioso (WM_CLOSE) às janelas top-level do PID exato."""
    if os.name != "nt" or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        posted = 0
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd: Any, _lparam: Any) -> bool:
            nonlocal posted
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid:
                user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
                posted += 1
            return True

        user32.EnumWindows(callback_type(visit), 0)
        return posted > 0
    except Exception:
        return False


def _music_cancelled(value: Callable[[], bool] | threading.Event | None) -> bool:
    if value is None:
        return False
    if callable(value):
        try:
            return bool(value())
        except Exception:
            return True
    try:
        return bool(value.is_set())
    except Exception:
        return True


def _default_close_spotify(executable: Path | None) -> bool:
    """Fecha graciosamente **apenas** o processo Spotify cujo exe é o validado.

    Sem `kill`/`terminate` por nome: identidade ausente é recusada (zero efeito) e
    uma instalação diferente nunca é fechada. Devolve True quando o alvo fechou.
    """
    if executable is None:
        return False
    target = os.path.normcase(str(Path(executable)))
    try:
        import psutil
    except Exception:
        return False
    try:
        for process in psutil.process_iter(["name", "exe", "pid"]):
            info = process.info
            if (info.get("name") or "").lower() != SPOTIFY_EXE_NAME.lower():
                continue
            exe = info.get("exe")
            if not exe:
                # Sem identidade não há prova de que é o alvo; nunca matar.
                return False
            if os.path.normcase(str(exe)) != target:
                continue
            if not _post_close_windows(info.get("pid")):
                return False
            try:
                process.wait(timeout=8.0)
            except Exception:
                return False
    except Exception:
        return False
    return True


def _default_open_spotify(executable: Path | None) -> bool:
    if executable is None or not Path(executable).is_file():
        return False
    try:
        subprocess.Popen([str(executable)], close_fds=True)
    except OSError:
        return False
    return True


def _default_fetch(url: str) -> bytes:
    if url != CLI_URL:
        raise SpotifyInstallError("offline")
    request = urllib.request.Request(url, headers={"User-Agent": "Lunmi/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - URL fixa
        payload = response.read(MAX_CLI_DOWNLOAD_BYTES + 1)
    if len(payload) > MAX_CLI_DOWNLOAD_BYTES:
        raise SpotifyInstallError("offline")
    return payload


class SpotifyInstaller:
    """Estado público do Spotify + transação de setup; um job por alvo."""

    def __init__(
        self,
        *,
        state_root: Path,
        broker: SpotifyBroker,
        session_id: str,
        emit: Callable[[dict[str, Any]], None],
        env: Mapping[str, str] | None = None,
        machine: str | None = None,
        version_reader: Callable[[Path], str | None] | None = None,
        process_reader: Callable[[], bool] | None = None,
        cli_runner: Callable[[list[str], dict[str, str]], Any] | None = None,
        fetcher: Callable[[str], bytes] | None = None,
        pins: Pins | None = None,
        extension_source: str | None = None,
        closer: Callable[[Path | None], bool] | None = None,
        opener: Callable[[Path | None], bool] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.state_root = Path(state_root)
        self.broker = broker
        self.session_id = session_id
        self._emit_cb = emit
        self._env = dict(os.environ if env is None else env)
        self._machine = machine or platform.machine()
        self._version_reader = version_reader or _default_file_version
        self._process_reader = process_reader or _spotify_running
        self._cli_runner = cli_runner
        self._fetcher = fetcher
        self._pins = pins or Pins()
        self._extension_source = extension_source
        self._closer = closer or _default_close_spotify
        self._opener = opener or _default_open_spotify
        self._clock = clock
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._job: dict[str, Any] | None = None
        self._job_thread: threading.Thread | None = None
        self._job_started = 0.0
        self._consents: dict[str, dict[str, Any]] = {}
        # Só digests em RAM: permite distinguir replay de expirado sem persistir o token.
        self._token_log: dict[str, tuple[str, float]] = {}
        self._journal_plan_hash = ""
        self._backup_digest = ""
        self._closed_for_job = False
        self._revision = 0
        self._epoch = 0
        self._connection = "disabled"
        self._device = "unknown"
        self._last_error: dict[str, Any] | None = None
        self._detected: Detected | None = None
        self._detected_at = 0.0
        self._verified = False
        self._enabled = False
        self._post_hashes: dict[str, str] = {}
        self._backup_dir: str = ""
        # Snapshot da transação atual: é o alvo do rollback do próprio job. Nunca
        # se reaproveita o baseline de uma instalação anterior.
        self._transaction_dir: str = ""
        # Filho CLI que a Lunmi lançou; `close`/`cancel` matam-no e esperam por ele.
        self._cli_process: subprocess.Popen[str] | None = None
        self._unsupported = ""
        self._closed = False
        self._detection_key: dict[str, Any] | None = None
        self._root = self.state_root / ".local" / "spotify"
        self._journal_path = self._root / "install.json"
        self._state_path = self._root / "state.json"
        self._load_state()

    # ------------------------------------------------------------ persistence --
    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        try:
            journal = json.loads(self._journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            journal = {}
        if isinstance(journal, dict) and isinstance(journal.get("backup_sha256"), str):
            self._backup_digest = journal["backup_sha256"]
        if isinstance(journal, dict) and journal.get("status") == "running":
            # Journal de um job interrompido: reportar honestamente, sem reidratar
            # estado antigo como verdade nem reexecutar escrita alguma.
            self._last_error = {"code": "interrupted", "retryable": True}
        self._enabled = data.get("enabled") is True
        self._verified = data.get("verified") is True
        self._epoch = int(data.get("epoch") or 0)
        self._backup_dir = str(data.get("backup_dir") or "")
        post = data.get("post_hashes")
        self._post_hashes = {str(k): str(v) for k, v in post.items()} if isinstance(post, dict) else {}
        if self._enabled:
            self._connection = "disconnected"

    def _save_state(self) -> None:
        payload = {
            "enabled": self._enabled,
            "verified": self._verified,
            "epoch": self._epoch,
            "backup_dir": self._backup_dir,
            "post_hashes": dict(self._post_hashes),
        }
        try:
            _atomic_write(self._state_path, json.dumps(payload, sort_keys=True).encode("utf-8"))
        except OSError:
            self._last_error = {"code": "write_denied", "retryable": True}

    def _journal_base(self) -> dict[str, Any]:
        base: dict[str, Any] = {"version": 1}
        if self._journal_plan_hash:
            base["plan_hash"] = self._journal_plan_hash
        if self._backup_digest:
            base["backup_sha256"] = self._backup_digest
        return base

    def _write_journal(self, data: dict[str, Any]) -> bool:
        payload = {
            **self._journal_base(),
            "updated": round(self._clock(), 3),
            **{key: value for key, value in data.items() if key != "job"},
            "job": data.get("job"),
        }
        try:
            _atomic_write(self._journal_path, json.dumps(payload, sort_keys=True).encode("utf-8"))
        except OSError:
            self._last_error = {"code": "write_denied", "retryable": True}
            return False
        return True

    # ---------------------------------------------------------------- detect --
    def _detect(self, *, force: bool = False) -> Detected:
        now = self._clock()
        if not force and self._detected is not None and now - self._detected_at < DETECT_TTL_S:
            return self._detected
        detected = self._detect_uncached()
        self._detected, self._detected_at = detected, now
        return detected

    def _detect_uncached(self) -> Detected:
        appdata = Path(self._env.get("APPDATA") or "")
        local = Path(self._env.get("LOCALAPPDATA") or "")
        detected = Detected(architecture=self._architecture())
        if not appdata.is_absolute():
            detected.note = "no_appdata"
            return detected

        spotify_dir = appdata / "Spotify"
        executable = spotify_dir / SPOTIFY_EXE_NAME
        if executable.is_file():
            detected.app, detected.spotify_dir, detected.executable = "win32", spotify_dir, executable
        elif (local / "Microsoft" / "WindowsApps" / SPOTIFY_EXE_NAME).is_file() or any(
            (local / "Packages").glob("SpotifyAB.SpotifyMusic_*")
        ):
            detected.app = "store"
            detected.spotify_dir = local / "Microsoft" / "WindowsApps"
        else:
            detected.app = "absent"

        if detected.executable is not None:
            try:
                detected.executable = _canonical_under(detected.executable, spotify_dir)
            except SpotifyInstallError:
                detected.app, detected.note = "unsupported", "reparse"
                detected.executable = None
            else:
                found = self._version_reader(detected.executable)
                detected.version = _version_fit(found or "", self._pins.spotify)
                detected.app_running = bool(self._process_reader())

        config_root = appdata / "spicetify"
        ini = config_root / CONFIG_FILE_NAME
        detected.config_root, detected.ini = config_root, ini
        detected.extensions_dir = config_root / EXTENSIONS_DIR_NAME
        if config_root.exists() and _is_reparse(config_root):
            detected.note = "reparse"
        elif detected.extensions_dir.exists() and _is_reparse(detected.extensions_dir):
            detected.note = "reparse"
        detected.extensions = _read_extensions(ini) if ini.is_file() else ()
        # O perfil pessoal é só leitura: a ligação vive no clone privado.
        private_config = self._private_config_root()
        private_ini = private_config / CONFIG_FILE_NAME
        private_extension = private_config / EXTENSIONS_DIR_NAME / EXTENSION_NAME
        detected.link = (
            "present"
            if detected.app != "absent"
            and private_ini.is_file()
            and private_extension.is_file()
            and EXTENSION_NAME in _read_extensions(private_ini)
            else "absent"
        )

        exe = self._cli_path()
        if not exe.is_file():
            detected.cli = "missing"
        else:
            detected.cli = "pinned" if _sha256_file(exe) == self._pins.exe_sha256 else "other"

        detected.writable = bool(
            config_root.parent.is_dir()
            and os.access(config_root.parent, os.W_OK)
            and (detected.spotify_dir is None or os.access(detected.spotify_dir, os.W_OK))
        )
        if detected.app == "win32" and not detected.writable:
            detected.note = detected.note or "write"
        detected.fingerprint = self._fingerprint(detected)
        return detected

    def _architecture(self) -> str:
        value = (self._machine or "").lower()
        if value in {"amd64", "x86_64", "x64"}:
            return "x64"
        if value in {"arm64", "aarch64"}:
            return "arm64"
        if value in {"x86", "i386", "i686"}:
            return "x86"
        return "unknown"

    def _fingerprint(self, detected: Detected) -> str:
        def digest(value: str) -> str:
            return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]

        # O alvo inclui o conteúdo do perfil (ini + ficheiros de extensão): uma edição
        # entre `plan` e `apply` obriga a um plano novo, nunca aplica sobre outro perfil.
        files: dict[str, str] = {}
        if detected.ini is not None and detected.ini.is_file():
            try:
                files[CONFIG_FILE_NAME] = _sha256_file(detected.ini)
            except OSError:
                files[CONFIG_FILE_NAME] = "unreadable"
        if detected.extensions_dir is not None and detected.extensions_dir.is_dir():
            for entry in sorted(detected.extensions_dir.glob("*")):
                if entry.is_file() and not _is_reparse(entry):
                    try:
                        files[f"{EXTENSIONS_DIR_NAME}/{entry.name}"] = _sha256_file(entry)
                    except OSError:
                        files[f"{EXTENSIONS_DIR_NAME}/{entry.name}"] = "unreadable"
        material = {
            "app": detected.app,
            "version": detected.version,
            "arch": detected.architecture,
            "exe": digest(str(detected.executable or "")),
            "ini": digest(str(detected.ini or "")),
            "ext": list(detected.extensions),
            "files": files,
        }
        return hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _cli_path(self) -> Path:
        return self._root / "cli" / "spicetify.exe"

    def _require_pinned_cli(self) -> Path:
        """Fail-closed: o remove precisa do CLI pin para restaurar o `Apps` real."""
        cli = self._cli_path()
        if not cli.is_file():
            raise SpotifyInstallError("cli_unavailable")
        if _sha256_file(cli) != self._pins.exe_sha256:
            raise SpotifyInstallError("hash_mismatch")
        return cli

    def _private_profile_root(self) -> Path:
        return self._root / "profile"

    def _private_config_root(self) -> Path:
        return self._private_profile_root() / "spicetify"

    def _copy_private_profile(self, source: Path, destination: Path) -> None:
        """Copia tema/opções/ordem/extensões/apps ativos, com limites e sem reparse.

        `Extracted` (como `Backup`/`cache`/`logs`) é cache gerada pelo CLI: não é
        ativo do perfil e não pode contar para o limite nem ir para o clone.
        """
        count = 0
        total = 0
        for entry in sorted(source.rglob("*")):
            relative = entry.relative_to(source)
            if relative.parts and relative.parts[0].lower() in GENERATED_PROFILE_CACHE:
                continue
            target = destination / relative
            if entry.is_dir():
                if _is_reparse(entry):
                    raise SpotifyInstallError("profile_unsafe")
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not entry.is_file() or _is_reparse(entry):
                raise SpotifyInstallError("profile_unsafe")
            count += 1
            if count > MAX_BACKUP_FILES:
                raise SpotifyInstallError("profile_unsafe")
            total += entry.stat().st_size
            if total > MAX_PROFILE_BYTES:
                raise SpotifyInstallError("profile_unsafe")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)

    @staticmethod
    def _patch_private_ini(text: str, spotify_path: Path, prefs_path: Path) -> str:
        """Preserva o INI e fixa spotify_path/prefs_path validados no clone."""
        lines = text.replace("\r\n", "\n").splitlines()
        wanted = {"spotify_path": str(spotify_path), "prefs_path": str(prefs_path)}
        seen = {key: False for key in wanted}
        section = ""
        patched: list[str] = []
        for raw in lines:
            stripped = raw.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1].strip().lower()
                patched.append(raw)
                continue
            if section == "setting" and "=" in raw:
                key = raw.split("=", 1)[0].strip().lower()
                if key in wanted:
                    patched.append(f"{key} = {wanted[key]}")
                    seen[key] = True
                    continue
            patched.append(raw)
        if not all(seen.values()):
            if "setting" not in {line.strip()[1:-1].strip().lower()
                                 for line in patched if line.strip().startswith("[")}:
                if patched and patched[-1].strip():
                    patched.append("")
                patched.append("[Setting]")
                section = "setting"
            insert_at = len(patched)
            for index, line in enumerate(patched):
                if line.strip().lower() == "[setting]":
                    insert_at = index + 1
                    for following in range(index + 1, len(patched)):
                        if patched[following].strip().startswith("["):
                            insert_at = following
                            break
                    break
            additions = [f"{key} = {wanted[key]}" for key, value in seen.items() if not value]
            patched[insert_at:insert_at] = additions
        return "\n".join(patched).rstrip("\n") + "\n"

    def _build_private_profile(self, detected: Detected) -> PrivateProfile:
        if detected.spotify_dir is None or detected.config_root is None:
            raise SpotifyInstallError("not_configured")
        spotify_dir = detected.spotify_dir.resolve()
        if not spotify_dir.is_dir() or _is_reparse(spotify_dir):
            raise SpotifyInstallError("write_denied")
        if spotify_dir.parent != Path(self._env.get("APPDATA") or "").resolve():
            raise SpotifyInstallError("profile_unsafe")
        prefs = spotify_dir / "prefs"
        if prefs.exists() and _is_reparse(prefs):
            raise SpotifyInstallError("profile_unsafe")
        root = self._private_profile_root()
        config = self._private_config_root()
        if root.exists():
            if _is_reparse(root) or self._root.resolve() not in root.resolve().parents:
                raise SpotifyInstallError("invalid_payload")
            shutil.rmtree(root)
        config.mkdir(parents=True, exist_ok=True)
        source = detected.config_root
        if source.is_dir():
            self._copy_private_profile(source, config)
        ini = config / CONFIG_FILE_NAME
        if ini.is_file():
            text = ini.read_text(encoding="utf-8", errors="replace")
        else:
            text = "[AdditionalOptions]\nextensions =\n\n[Setting]\n"
        _atomic_write(ini, self._patch_private_ini(text, spotify_dir, prefs).encode("utf-8"))
        return PrivateProfile(
            root=root,
            config_root=config,
            ini=ini,
            extensions_dir=config / EXTENSIONS_DIR_NAME,
        )

    # ------------------------------------------------------------- public DTO --
    def state(self) -> dict[str, Any]:
        detected = self._detect()
        with self._lock:
            # Deteção alterada é uma mudança efetiva do DTO: a UI só aplica
            # revisões estritamente maiores. Sem evento por leitura inalterada.
            public = detected.public()
            if self._detection_key != public:
                self._detection_key = dict(public)
                self._revision += 1
            job = dict(self._job) if self._job is not None else None
            connection = self._connection
            device = self._device
            error = dict(self._last_error) if self._last_error else None
            setup = self._setup_state(detected)
            revision = self._revision
            epoch = self._epoch
        info = self.broker.info()
        if info.get("authenticated"):
            connection = "connected"
            device = info.get("device_scope") or "unknown"
        elif self.broker.started and connection != "disabled":
            connection = "connecting"
        return {
            "setup": setup,
            "connection": connection,
            "device": device,
            "revision": revision,
            "epoch": epoch,
            "job": job,
            "detection": detected.public(),
            "error": error,
        }

    def _setup_state(self, detected: Detected) -> str:
        if self._unsupported or detected.note == "reparse":
            return "unsupported"
        if detected.app == "absent":
            return "absent"
        if detected.app == "store":
            return "unsupported"
        if detected.architecture != "x64":
            return "unsupported"
        if detected.version != "supported":
            return "unsupported"
        if detected.app_running and detected.link == "absent":
            return "not_configured"
        if detected.link == "present":
            return "configured" if self._verified else "needs_repair"
        return "not_configured"

    def payload(self) -> dict[str, Any]:
        return {"spotify": self.state()}

    def _emit(self) -> None:
        try:
            self._emit_cb({"spotify": self.state()})
        except Exception:
            # Um evento nunca pode parar a transação.
            pass

    def _bump(self, *, epoch: bool = False) -> None:
        self._revision += 1
        if epoch:
            self._epoch += 1
            self._consents.clear()
        self._save_state()

    # ------------------------------------------------------------------ plan ---
    def plan(self, action: str) -> dict[str, Any]:
        if action not in contract.SPOTIFY_ACTIONS:
            raise SpotifyInstallError("invalid_payload")
        detected = self._detect(force=True)
        self._unsupported = ""
        if detected.app == "absent":
            raise SpotifyInstallError("spotify_missing")
        if detected.app == "store":
            self._unsupported = "store_unsupported"
            raise SpotifyInstallError("store_unsupported")
        if detected.note == "reparse":
            raise SpotifyInstallError("policy_blocked")
        if detected.architecture != "x64":
            self._unsupported = "arm_unsupported"
            raise SpotifyInstallError("arm_unsupported")
        if not detected.writable:
            raise SpotifyInstallError("write_denied")
        if action in {"install", "repair"} and detected.version != "supported":
            raise SpotifyInstallError("version_unsupported")
        if action in {"repair", "remove"} and detected.link != "present":
            raise SpotifyInstallError("not_configured")
        if action == "remove":
            # Sem o CLI pin não há restauro do Spotify real: recusar antes de
            # emitir consentimento ou tocar em ficheiros.
            self._require_pinned_cli()

        backup_bytes = self._estimate_backup(detected)
        free = shutil.disk_usage(self.state_root).free
        if free < backup_bytes + 64 * 1024 * 1024:
            raise SpotifyInstallError("disk_space")

        steps = (
            ("detect", "obtain", "backup", "apply", "pair", "verify")
            if action in {"install", "repair"}
            else ("detect", "backup", "apply", "verify")
        )
        requires_network = action in {"install", "repair"} and detected.cli != "pinned"
        plan = contract.SpotifyPlan(
            action=action,
            plan_hash="0" * 64,
            steps=steps,
            adds_link=action != "remove",
            replaces_files=1,
            backup_bytes=backup_bytes,
            requires_close=True,
            requires_network=requires_network,
        )
        material = {
            "plan": plan.to_dict() | {"plan_hash": ""},
            "fingerprint": detected.fingerprint,
            "pins": {"spotify": self._pins.spotify, "cli": self._pins.cli},
        }
        plan_hash = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        plan = contract.SpotifyPlan(
            action=action,
            plan_hash=plan_hash,
            steps=steps,
            adds_link=action != "remove",
            replaces_files=1,
            backup_bytes=backup_bytes,
            requires_close=True,
            requires_network=requires_network,
        )
        token = "cns-" + secrets.token_hex(16)
        now = self._clock()
        with self._lock:
            self._consents = {
                key: value for key, value in self._consents.items() if value["expires"] > now
            }
            # O ledger só distingue replay de expirado; retém um histórico curto.
            self._token_log = {
                key: value for key, value in self._token_log.items()
                if value[1] + contract.SPOTIFY_CONSENT_TTL_SECONDS > now
            }
            for key, _ in list(self._token_log.items())[:-64]:
                self._token_log.pop(key, None)
            if len(self._consents) >= 4:
                self._consents.pop(next(iter(self._consents)))
            self._consents[token] = {
                "plan_hash": plan_hash,
                "action": action,
                "fingerprint": detected.fingerprint,
                "epoch": self._epoch,
                "expires": now + contract.SPOTIFY_CONSENT_TTL_SECONDS,
            }
            self._token_log[hashlib.sha256(token.encode()).hexdigest()] = (
                "pending", now + contract.SPOTIFY_CONSENT_TTL_SECONDS
            )
        consent = contract.SpotifyConsent(consent_id=token).to_dict()
        return {"plan": plan.to_dict(), "consent": consent}

    @staticmethod
    def _backup_limits(key: str) -> tuple[int, int]:
        """Limites separados: perfil Spicetify vs árvore `Spotify/Apps`."""
        if key == "spotify":
            return MAX_SPOTIFY_APPS_BYTES, MAX_SPOTIFY_APPS_FILES
        return MAX_PROFILE_BYTES, MAX_BACKUP_FILES

    def _estimate_backup(self, detected: Detected) -> int:
        total = 0
        roots: list[tuple[str, Path | None]] = [
            ("profile", detected.ini),
            ("profile", detected.extensions_dir),
        ]
        if detected.spotify_dir is not None:
            roots.append(("spotify", detected.spotify_dir / "Apps"))
        counts: dict[str, int] = {"profile": 0, "spotify": 0}
        totals: dict[str, int] = {"profile": 0, "spotify": 0}
        for key, root in roots:
            if root is None:
                continue
            limit_bytes, limit_files = self._backup_limits(key)
            entries = [root] if root.is_file() else (
                [p for p in root.rglob("*") if p.is_file()] if root.is_dir() else []
            )
            for entry in entries:
                if _is_reparse(entry):
                    raise SpotifyInstallError("profile_unsafe")
                counts[key] += 1
                if counts[key] > limit_files:
                    raise SpotifyInstallError("disk_space")
                totals[key] += entry.stat().st_size
                if totals[key] > limit_bytes:
                    raise SpotifyInstallError("disk_space")
                total += entry.stat().st_size
        return total

    # ----------------------------------------------------------------- apply ---
    def apply(self, consent_id: str) -> dict[str, Any]:
        if not isinstance(consent_id, str) or not contract.CONSENT_ID_PATTERN.fullmatch(consent_id):
            raise SpotifyInstallError("invalid_payload")
        now = self._clock()
        digest = hashlib.sha256(consent_id.encode()).hexdigest()
        with self._lock:
            if self._job is not None and self._job.get("status") == "running":
                raise SpotifyInstallError("job_busy")
            consent = self._consents.get(consent_id)
            if consent is None:
                state = self._token_log.get(digest)
                if state is None:
                    raise SpotifyInstallError("consent_invalid")
                raise SpotifyInstallError(
                    "consent_replayed" if state[0] == "consumed" else "consent_expired"
                )
            if consent["expires"] <= now:
                self._consents.pop(consent_id, None)
                self._token_log[digest] = ("expired", consent["expires"])
                raise SpotifyInstallError("consent_expired")
            if consent.get("epoch") != self._epoch:
                self._consents.pop(consent_id, None)
                self._token_log[digest] = ("consumed", now)
                raise SpotifyInstallError("consent_invalid")
            if consent["action"] not in contract.SPOTIFY_ACTIONS:
                self._consents.pop(consent_id, None)
                raise SpotifyInstallError("consent_invalid")
            # Consumido antes de qualquer revalidação/escrita: segundo uso nunca reabre o job.
            self._consents.pop(consent_id, None)
            self._token_log[digest] = ("consumed", now)
        detected = self._detect(force=True)
        if detected.fingerprint != consent["fingerprint"]:
            raise SpotifyInstallError("consent_invalid")

        job_id = "spj-" + secrets.token_hex(8)
        job = {
            "id": job_id,
            "action": consent["action"],
            "stage": "detect",
            "status": "running",
            "error_code": None,
        }
        with self._lock:
            self._journal_plan_hash = consent["plan_hash"]
            if consent["action"] != "remove":
                self._backup_digest = ""
            self._cancel.clear()
            self._last_error = None
            self._job_started = time.monotonic()
            # Journal durável é pré-condição: sem ele não há thread nem efeitos.
            durable = self._write_journal({
                "action": consent["action"],
                "stage": "detect",
                "status": "running",
                "epoch": self._epoch,
                "job": job,
            })
            if not durable:
                raise SpotifyInstallError("write_denied")
            self._job = job
            self._bump()
        self._emit()
        thread = threading.Thread(
            target=self._run_job, args=(dict(consent), job_id), name="spotify-setup", daemon=True
        )
        self._job_thread = thread
        thread.start()
        return {"accepted": True, "job_id": job_id, "epoch": self._epoch}

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._job
            if job is None or job.get("id") != job_id:
                return {"cancelled": False, "reason": "no_such_job"}
            if job.get("status") != "running":
                return {"cancelled": False, "reason": "already_finished"}
            # O flag é posto na mesma trava que publica o handle: o ACK nunca
            # chega antes de o filho estar sob controlo (ou de o fence recusar).
            self._cancel.set()
        self._terminate_cli()
        return {"cancelled": True, "reason": "cancelled"}

    def _set_stage(self, stage: str) -> None:
        with self._lock:
            if self._job is None:
                return
            self._job = {**self._job, "stage": stage}
            self._revision += 1
            durable = self._write_journal({
                "action": self._job["action"],
                "stage": stage,
                "status": self._job["status"],
                "epoch": self._epoch,
                "job": self._job,
            })
            if not durable:
                raise SpotifyInstallError("write_denied")
        self._emit()

    def _finish(self, status: str, error_code: str | None = None) -> None:
        with self._lock:
            if self._job is None:
                return
            # O perfil mudou durante o job: a próxima leitura de estado re-detecta.
            self._detected = None
            self._job = {**self._job, "status": status, "error_code": error_code}
            self._last_error = (
                {"code": error_code, "retryable": error_code in {"offline", "not_ready",
                                                                 "close_refused", "unavailable"}}
                if error_code else None
            )
            self._revision += 1
            durable = self._write_journal({
                "action": self._job["action"],
                "stage": self._job["stage"],
                "status": status,
                "error_code": error_code,
                "epoch": self._epoch,
                "job": self._job,
            })
            if not durable:
                self._job = {**self._job, "status": "failed", "error_code": "write_denied"}
                self._last_error = {"code": "write_denied", "retryable": True}
        self._emit()

    def _check_authority(self, consent: dict[str, Any], job_id: str) -> None:
        """Revalida cancel/epoch/fecho/alvo depois de cada bloqueio."""
        with self._lock:
            if self._closed:
                raise SpotifyInstallError("interrupted")
            if self._cancel.is_set():
                raise SpotifyInstallError("job_cancelled")
            if self._job is None or self._job.get("id") != job_id:
                raise SpotifyInstallError("interrupted")
            if consent.get("epoch") != self._epoch:
                raise SpotifyInstallError("interrupted")
        detected = self._detect(force=True)
        if detected.fingerprint != consent["fingerprint"] or detected.note == "reparse":
            raise SpotifyInstallError("interrupted")

    def _rotate_pairing(self) -> str | None:
        rotate = getattr(self.broker, "rotate_pairing", None)
        return rotate() if callable(rotate) else None

    def _restore_pairing(self, previous: str | None) -> None:
        restore = getattr(self.broker, "restore_pairing", None)
        if callable(restore):
            restore(previous)

    def _revoke_pairing(self) -> None:
        revoke = getattr(self.broker, "revoke_pairing", None)
        if not callable(revoke):
            return
        try:
            revoke()
        except SpotifyBrokerError as exc:
            raise SpotifyInstallError(exc.code) from None

    def _run_job(self, consent: dict[str, Any], job_id: str) -> None:
        dirty = False
        was_verified = self._verified
        detected: Detected | None = None
        previous_secret: str | None = None
        rotated = False
        with self._lock:
            # O rollback de um job falhado usa só o snapshot que ele próprio tira.
            self._transaction_dir = ""
        try:
            detected = self._detect(force=True)
            self._check_authority(consent, job_id)
            action = consent["action"]
            # O fecho gracioso antecede qualquer backup/escrita; reabrir faz parte
            # do mesmo job consentido e nunca inicia reprodução.
            self._close_spotify(detected)
            self._check_authority(consent, job_id)
            if action == "remove":
                dirty = True
                self._run_remove(detected, consent, job_id)
                self._check_authority(consent, job_id)
                with self._lock:
                    self._bump(epoch=True)
                self._finish("done")
                return
            self._set_stage("obtain")
            self._ensure_cli()
            self._check_authority(consent, job_id)
            self._set_stage("backup")
            self._snapshot(detected)
            dirty = True
            self._check_authority(consent, job_id)
            if action == "repair":
                # D11: repair roda o par; o bundle novo tem de nascer com ele.
                previous_secret = self._rotate_pairing()
                rotated = previous_secret is not None
                self._check_authority(consent, job_id)
            self._set_stage("apply")
            self._apply_link(detected)
            self._check_authority(consent, job_id)
            self._set_stage("pair")
            self._validate_pairing(detected)
            self._check_authority(consent, job_id)
            # Reabrir antes do `verify`: o handshake HMAC + leitura só existem com o
            # Spotify vivo e a extensão carregada; reabrir não inicia reprodução.
            # Numa instalação a frio o app nunca esteve aberto e o fecho não o
            # marcou: a abertura consentida tem de acontecer uma vez antes da
            # verificação. `get`/`plan`/`connect`/`state`/pausa/volume não abrem.
            if self._closed_for_job or not (detected.app_running or self._process_reader()):
                self._reopen_spotify(detected)
            self._set_stage("verify")
            ok, code = self._verify_link()
            if not ok:
                raise SpotifyInstallError(code)
            self._check_authority(consent, job_id)
            with self._lock:
                self._verified = True
                self._enabled = True
                self._connection = "connected"
                self._device = self.broker.info().get("device_scope") or "unknown"
                self._post_hashes = self._hash_profile(detected)
                self._unsupported = ""
                self._bump(epoch=True)
            self._finish("done")
        except SpotifyInstallError as exc:
            if rotated:
                try:
                    self._restore_pairing(previous_secret)
                except Exception:
                    # A transação não conseguiu recuperar o par anterior; o estado
                    # terminal abaixo fica deliberadamente desconectado.
                    pass
            self._recover_failed(dirty, exc.code, was_verified)
        except Exception:
            if rotated:
                try:
                    self._restore_pairing(previous_secret)
                except Exception:
                    pass
            self._recover_failed(dirty, "unavailable", was_verified)
        finally:
            if self._closed_for_job:
                self._reopen_spotify(detected)

    # --------------------------------------------------------- close/reopen ---
    def _close_spotify(self, detected: Detected) -> None:
        """Fecha só o Spotify validado; identidade do processo reconferida depois."""
        running = detected.app_running or bool(self._process_reader())
        if not running:
            return
        if not self._closer(detected.executable):
            raise SpotifyInstallError("close_refused")
        if self._process_reader():
            raise SpotifyInstallError("close_refused")
        self._closed_for_job = True
        detected.app_running = False

    def _reopen_spotify(self, detected: Detected | None) -> None:
        executable = detected.executable if detected is not None else None
        self._closed_for_job = False
        try:
            self._opener(executable)
        except Exception:
            # Reabrir falhar não desfaz um setup já verificado; fica no diagnóstico.
            pass

    def _recover_failed(self, dirty: bool, code: str, was_verified: bool) -> None:
        rolled_back = True
        if dirty:
            with self._lock:
                # `_transaction_dir` só existe depois de um snapshot completo. Se
                # ainda está vazio, o job falhou antes de qualquer escrita: não há
                # rollback a fazer e `rollback_failed` mascararia o erro original.
                snapshot = bool(self._transaction_dir)
            if snapshot:
                try:
                    self._set_stage("rollback")
                except SpotifyInstallError:
                    # Journal indisponível durante a recuperação: tentar o restauro
                    # mesmo assim e reportar honestamente no fim.
                    pass
                rolled_back = self._rollback()
        # Um setup por confirmar não pode continuar "connected": a ligação só é
        # declarada depois de HMAC + leitura autenticada (ver `_verify_link`).
        if dirty or code == "not_ready":
            self.broker.stop()
        with self._lock:
            if dirty or code == "not_ready":
                self._verified = False
                self._connection = "disconnected"
                self._device = "unknown"
            else:
                # Cancelado antes de qualquer escrita: a ligação anterior fica como estava.
                self._connection = "connected" if self._verified else "disconnected"
            self._bump(epoch=True)
        if not rolled_back:
            self._finish("failed", "rollback_failed")
            return
        self._finish("cancelled" if code == "job_cancelled" else "failed", code)

    def _run_remove(self, detected: Detected, consent: dict[str, Any], job_id: str) -> None:
        self._set_stage("backup")
        # Snapshot da transação (com as edições feitas depois da instalação): é
        # ele que o rollback usa. O baseline do journal (`_backup_dir`) não é
        # tocado nem usado por este job.
        self._snapshot(detected, update_baseline=False)
        self._check_authority(consent, job_id)
        # Fail-closed: sem o CLI pin não há restauro do `Apps` real. Recusar
        # antes de apagar o clone privado ou revogar o par (nunca `done`).
        self._require_pinned_cli()
        self._set_stage("apply")
        self.broker.stop()
        # Remoção seletiva: o clone é reconstruído a partir do perfil pessoal atual,
        # pelo que temas/extensões posteriores são preservados; só a nossa entrada sai.
        profile = self._build_private_profile(detected)
        self._remove_link_selectively(profile)
        self._run_cli(["--no-restart", "backup", "apply"],
                      timeout=CLI_APPLY_TIMEOUT_S, appdata=profile.root)
        self._check_authority(consent, job_id)
        self._set_stage("verify")
        if EXTENSION_NAME in _read_extensions(profile.ini):
            raise SpotifyInstallError("interrupted")
        if (profile.extensions_dir / EXTENSION_NAME).exists():
            raise SpotifyInstallError("interrupted")
        shutil.rmtree(self._private_profile_root(), ignore_errors=True)
        with self._lock:
            self._verified = False
            self._enabled = False
            self._connection = "disabled"
            self._device = "unknown"
            self._post_hashes = {}
            # O epoch terminal é incrementado pelo `_run_job` depois da última
            # revalidação; aqui só se persiste o novo estado do setup.
            self._bump()
            if not self._write_journal({
                "action": "remove",
                "stage": "verify",
                "status": "running",
                "epoch": self._epoch,
                "job": self._job,
            }):
                raise SpotifyInstallError("write_denied")
        # Só depois de a remoção estar durablemente registada é que o par é revogado.
        self._revoke_pairing()

    # ------------------------------------------------------------------ CLI ----
    def _cli_runtime_ready(self) -> bool:
        """Cache só conta se tiver a distribuição completa, não um .exe solto."""
        cli = self._cli_path()
        if not cli.is_file() or _is_reparse(cli):
            return False
        if _sha256_file(cli) != self._pins.exe_sha256:
            return False
        base = cli.parent
        marker = base / CLI_CACHE_MARKER
        if not marker.is_file() or _is_reparse(marker):
            return False
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(payload, dict) or payload.get("zip_sha256") != self._pins.zip_sha256:
            return False
        return all(
            (base / member).is_file() and not _is_reparse(base / member)
            for member in CLI_RUNTIME_REQUIRED
        )

    def _ensure_cli(self) -> Path:
        target = self._cli_path()
        if self._cli_runtime_ready():
            return target
        fetcher = self._fetcher or _default_fetch
        try:
            archive = fetcher(self._pins.url)
        except SpotifyInstallError:
            raise
        except Exception:
            raise SpotifyInstallError("offline") from None
        if len(archive) > MAX_CLI_DOWNLOAD_BYTES:
            raise SpotifyInstallError("offline")
        if _sha256_bytes(archive) != self._pins.zip_sha256:
            raise SpotifyInstallError("hash_mismatch")
        return self._extract_cli(archive, target.parent)

    @staticmethod
    def _cli_member(name: str) -> Path:
        """Nome do membro confinado à raiz: sem traversal, unidade ou reservados."""
        raw = name.replace("\\", "/")
        while raw.endswith("/"):
            raw = raw[:-1]
        if not raw or raw.startswith("/"):
            raise SpotifyInstallError("signature_invalid")
        parts = raw.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise SpotifyInstallError("signature_invalid")
        if any(char in '<>:"|?*' or ord(char) < 32 for char in raw):
            raise SpotifyInstallError("signature_invalid")
        if any(part[-1] in " ." for part in parts):
            raise SpotifyInstallError("signature_invalid")
        return Path(*parts)

    def _extract_cli(self, archive: bytes, destination: Path) -> Path:
        """Extrai a distribuição completa para `destination`, com limites finitos."""
        staging = destination.parent / (destination.name + ".staging")
        shutil.rmtree(staging, ignore_errors=True)
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                seen: set[str] = set()
                files = 0
                total = 0
                for info in bundle.infolist():
                    member = self._cli_member(info.filename)
                    key = member.as_posix().lower()
                    if key in seen:
                        raise SpotifyInstallError("signature_invalid")
                    seen.add(key)
                    # Symlink/char/block device e qualquer tipo que não seja pasta
                    # ou ficheiro regular não entram: nada é seguido nem reparseado.
                    if (info.external_attr >> 16) & 0o170000 not in (0, 0o040000, 0o100000):
                        raise SpotifyInstallError("signature_invalid")
                    if info.is_dir():
                        continue
                    files += 1
                    total += max(0, int(info.file_size))
                    if files > MAX_CLI_RUNTIME_FILES or total > MAX_CLI_RUNTIME_BYTES:
                        raise SpotifyInstallError("signature_invalid")
                    out = staging / member
                    out.parent.mkdir(parents=True, exist_ok=True)
                    written = 0
                    with bundle.open(info) as source, out.open("wb") as sink:
                        while True:
                            chunk = source.read(64 * 1024)
                            if not chunk:
                                break
                            written += len(chunk)
                            if written > MAX_CLI_RUNTIME_BYTES:
                                raise SpotifyInstallError("signature_invalid")
                            sink.write(chunk)
            exe = staging / "spicetify.exe"
            if not exe.is_file() or _sha256_file(exe) != self._pins.exe_sha256:
                raise SpotifyInstallError("signature_invalid")
            if not all((staging / member).is_file() for member in CLI_RUNTIME_REQUIRED):
                raise SpotifyInstallError("signature_invalid")
            (staging / CLI_CACHE_MARKER).write_text(
                json.dumps({"zip_sha256": self._pins.zip_sha256}), encoding="utf-8")
            shutil.rmtree(destination, ignore_errors=True)
            os.replace(staging, destination)
            return destination / "spicetify.exe"
        except zipfile.BadZipFile:
            raise SpotifyInstallError("signature_invalid") from None
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _run_cli(self, args: list[str], *, timeout: int, appdata: Path | None = None) -> None:
        # Fence antes da validação (que pode descarregar) e outra vez na mesma
        # trava que publica o handle: um cancelamento já confirmado nunca lança.
        self._fence_cli_dispatch()
        cli = self._ensure_cli()
        env = dict(self._env)
        if appdata is not None:
            # O CLI só vê o clone privado; o APPDATA pessoal nunca é transmitido.
            env["APPDATA"] = str(appdata)
        if self._cli_runner is not None:
            self._fence_cli_dispatch()
            try:
                result = self._cli_runner([str(cli), *args], env)
            except OSError:
                raise SpotifyInstallError("cli_unavailable") from None
            if int(getattr(result, "returncode", 1)) != 0:
                raise SpotifyInstallError("cli_unavailable")
            return
        # Processo real: a Lunmi é dona do filho. `close`/`cancel` matam-no e
        # esperam por ele antes de qualquer rollback/terminal, para que um CLI
        # bloqueado não escreva depois de o job ser dado como cancelado.
        with self._lock:
            self._fence_cli_dispatch()
            try:
                process = subprocess.Popen(
                    [str(cli), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env=env, shell=False,
                )
            except OSError:
                raise SpotifyInstallError("cli_unavailable") from None
            self._cli_process = process
        try:
            try:
                process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._terminate_cli(process)
                raise SpotifyInstallError("interrupted") from None
        finally:
            with self._lock:
                if self._cli_process is process:
                    self._cli_process = None
        if self._closed:
            raise SpotifyInstallError("interrupted")
        if self._cancel.is_set():
            raise SpotifyInstallError("job_cancelled")
        if int(getattr(process, "returncode", 1)) != 0:
            raise SpotifyInstallError("cli_unavailable")

    def _fence_cli_dispatch(self) -> None:
        """Cancelamento/fecho reconhecidos antes de lançar: nunca um Popen tardio."""
        with self._lock:
            if self._closed:
                raise SpotifyInstallError("interrupted")
            if self._cancel.is_set():
                raise SpotifyInstallError("job_cancelled")

    def _terminate_cli(self, process: subprocess.Popen[str] | None = None) -> None:
        """Mata e espera **só** o CLI que a Lunmi lançou; nunca toca no Spotify."""
        if process is None:
            with self._lock:
                process = self._cli_process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
        except OSError:
            pass
        finally:
            with self._lock:
                if self._cli_process is process:
                    self._cli_process = None

    def _apply_link(self, detected: Detected) -> None:
        profile = self._build_private_profile(detected)
        extension = self._extension_text()
        target = profile.extensions_dir / EXTENSION_NAME
        _atomic_write(target, extension.encode("utf-8"))
        self._run_cli(["config", "extensions", EXTENSION_NAME],
                      timeout=CLI_CONFIG_TIMEOUT_S, appdata=profile.root)
        self._run_cli(["--no-restart", "backup", "apply"],
                      timeout=CLI_APPLY_TIMEOUT_S, appdata=profile.root)
        if EXTENSION_NAME not in _read_extensions(profile.ini) or not target.is_file():
            raise SpotifyInstallError("not_ready")

    def _extension_text(self) -> str:
        source = self._extension_source
        if source is None:
            path = Path(__file__).with_name("spotify_assets") / EXTENSION_NAME
            source = path.read_text(encoding="utf-8")
        return self.broker.bundle(source)

    def _validate_pairing(self, detected: Detected) -> None:
        target = self._private_config_root() / EXTENSIONS_DIR_NAME / EXTENSION_NAME
        if not target.is_file() or target.stat().st_size == 0:
            raise SpotifyInstallError("not_ready")
        secret = self.broker.secret_hex()
        if len(secret) != 64:
            raise SpotifyInstallError("unavailable")
        text = target.read_text(encoding="utf-8", errors="replace")
        if "LUNMI_SPOTIFY_KIT_CONFIG" not in text or f'"secretHex":"{secret}"' not in text:
            raise SpotifyInstallError("not_ready")

    def _verify_link(self) -> tuple[bool, str]:
        try:
            self.broker.start()
        except SpotifyBrokerError as exc:
            return False, exc.code
        if not self.broker.started:
            return False, "not_ready"
        return self.broker.verify_read(timeout=8.0)

    # -------------------------------------------------------------- snapshot ---
    def _snapshot_roots(self, detected: Detected) -> dict[str, Path | None]:
        return {
            "spotify": detected.spotify_dir / "Apps" if detected.spotify_dir else None,
            "profile": self._private_config_root(),
        }

    def _snapshot(self, detected: Detected, *, update_baseline: bool = True) -> dict[str, Any]:
        directory = self._root / "backup" / f"plan-{int(time.time() * 1000)}"
        directory.mkdir(parents=True, exist_ok=True)
        files: list[dict[str, Any]] = []
        trees: dict[str, list[str]] = {}
        total = 0
        counts: dict[str, int] = {"profile": 0, "spotify": 0}
        totals: dict[str, int] = {"profile": 0, "spotify": 0}
        for key, root in self._snapshot_roots(detected).items():
            names: list[str] = []
            limit_bytes, limit_files = self._backup_limits(key)
            if root is not None and root.is_dir():
                for entry in sorted(root.rglob("*")):
                    relative = entry.relative_to(root)
                    # Caches geradas ficam fora do snapshot ativo (mesmo conjunto
                    # do clone privado): não contam para os limites nem entram no
                    # manifesto; `Themes`/`Extensions`/`CustomApps`/config ficam.
                    if key == "profile" and relative.parts and \
                            relative.parts[0].lower() in GENERATED_PROFILE_CACHE:
                        continue
                    if _is_reparse(entry):
                        raise SpotifyInstallError("profile_unsafe")
                    if not entry.is_file():
                        continue
                    relative_name = relative.as_posix()
                    counts[key] += 1
                    if counts[key] > limit_files:
                        raise SpotifyInstallError("disk_space")
                    totals[key] += entry.stat().st_size
                    if totals[key] > limit_bytes:
                        raise SpotifyInstallError("disk_space")
                    total += entry.stat().st_size
                    copy = directory / key / Path(relative_name)
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(entry, copy)
                    files.append({"root": key, "name": relative_name,
                                  "sha256": _sha256_file(copy),
                                  "bytes": copy.stat().st_size})
                    names.append(relative_name)
            trees[key] = names
        payload = {"version": 2, "created": round(self._clock(), 3),
                   "files": files, "trees": trees, "bytes": total}
        _atomic_write(directory / "manifest.json",
                      json.dumps(payload, sort_keys=True).encode("utf-8"))
        with self._lock:
            self._transaction_dir = str(directory)
            if update_baseline:
                self._backup_digest = _sha256_file(directory / "manifest.json")
                self._backup_dir = str(directory)
            self._save_state()
        return {"dir": str(directory), "manifest": files, "trees": trees}

    @staticmethod
    def _safe_name(name: str) -> Path | None:
        relative = Path(name)
        if not name or relative.is_absolute() or ".." in relative.parts:
            return None
        return relative

    def _restore_snapshot(self, directory: Path) -> bool:
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(manifest, dict) or manifest.get("version") != 2:
            return False
        roots = self._snapshot_roots(self._detect(force=True))
        trees = manifest.get("trees")
        if not isinstance(trees, dict) or set(trees) - set(roots):
            return False
        try:
            for item in manifest.get("files") or []:
                key = str(item.get("root") or "")
                relative = self._safe_name(str(item.get("name") or ""))
                root = roots.get(key)
                if root is None or relative is None:
                    return False
                source = directory / key / relative
                if not source.is_file() or _sha256_file(source) != item.get("sha256"):
                    return False
                destination = root / relative
                for part in [destination, *destination.parents]:
                    if part == root.parent:
                        break
                    if part.exists() and _is_reparse(part):
                        return False
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                if _sha256_file(destination) != item.get("sha256"):
                    return False
            for key, root in roots.items():
                if root is None:
                    continue
                baseline = set(trees.get(key) or [])
                if not root.exists():
                    continue
                if _is_reparse(root):
                    return False
                for entry in sorted(list(root.rglob("*")),
                                key=lambda part: len(part.parts), reverse=True):
                    relative = entry.relative_to(root)
                    # Caches geradas não são geridas pela transação: repor não as
                    # apaga (o `_snapshot` também não as inclui no baseline).
                    if key == "profile" and relative.parts and \
                            relative.parts[0].lower() in GENERATED_PROFILE_CACHE:
                        continue
                    if _is_reparse(entry):
                        return False
                    if entry.is_dir():
                        try:
                            if not any(entry.iterdir()):
                                entry.rmdir()
                        except OSError:
                            pass
                        continue
                    if entry.relative_to(root).as_posix() not in baseline:
                        entry.unlink()
            return True
        except (OSError, KeyError, ValueError):
            return False

    def _rollback(self) -> bool:
        # Só o snapshot da transação atual. Ausente => não repor nem apagar com
        # um baseline antigo; `_recover_failed` reporta `rollback_failed`.
        with self._lock:
            directory = Path(self._transaction_dir) if self._transaction_dir else None
        if directory is None or not (directory / "manifest.json").is_file():
            return False
        return self._restore_snapshot(directory)

    def _hash_profile(self, detected: Detected) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for key, root in self._snapshot_roots(detected).items():
            if root is None or not root.is_dir():
                continue
            for entry in sorted(root.rglob("*")):
                if entry.is_file() and not _is_reparse(entry):
                    hashes[f"{key}/{entry.relative_to(root).as_posix()}"] = _sha256_file(entry)
        return hashes

    def _has_external_edits(self, detected: Detected) -> bool:
        if not self._post_hashes:
            return True
        roots = self._snapshot_roots(detected)
        for name, digest in self._post_hashes.items():
            key, _, relative = name.partition("/")
            root = roots.get(key)
            if root is None or not relative:
                return True
            target = root / Path(relative)
            if not target.is_file() or _sha256_file(target) != digest:
                return True
        return False

    def _remove_link_selectively(self, profile: PrivateProfile) -> None:
        target = profile.extensions_dir / EXTENSION_NAME
        if target.is_file() and not _is_reparse(target):
            target.unlink()
        if profile.ini.is_file():
            text = profile.ini.read_text(encoding="utf-8", errors="replace")
            lines: list[str] = []
            for raw in text.splitlines():
                if "=" in raw and raw.split("=", 1)[0].strip().lower() == "extensions":
                    values = [item.strip() for item in re.split(r"[|,]", raw.split("=", 1)[1])
                              if item.strip() and item.strip() != EXTENSION_NAME]
                    lines.append(("extensions = " + "|".join(values)).rstrip())
                else:
                    lines.append(raw)
            _atomic_write(profile.ini, ("\n".join(lines) + "\n").encode("utf-8"))

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def job_running(self) -> bool:
        with self._lock:
            return bool(self._job is not None and self._job.get("status") == "running")

    def prepare_music(self, *, open_spotify: bool, timeout: float = 12.0,
                      cancelled: Callable[[], bool] | threading.Event | None = None) -> None:
        """Prova uma ligação viva antes de qualquer leitura/efeito musical.

        Só `search`/`play` podem abrir o Spotify; estado/pausa/volume com a app fechada
        devolvem `not_ready` sem lançar reprodução. A espera é limitada e cancelável e
        nunca chama `activate_local`, `play` ou outro efeito.
        """
        if self.job_running:
            raise SpotifyInstallError("job_busy")
        with self._lock:
            enabled = self._enabled
        if not enabled:
            # Desligar a ligação é uma decisão do utilizador: música não a repõe
            # silenciosamente; só `spotify.connect` volta a ligar.
            raise SpotifyInstallError("not_configured")
        detected = self._detect(force=True)
        setup = self._setup_state(detected)
        if setup == "needs_repair":
            raise SpotifyInstallError("not_ready")
        if setup != "configured" or detected.link != "present":
            raise SpotifyInstallError("not_configured")
        try:
            self.broker.start()
        except SpotifyBrokerError as exc:
            raise SpotifyInstallError(exc.code) from None
        if open_spotify and not detected.app_running:
            if detected.executable is None:
                raise SpotifyInstallError("spotify_missing")
            try:
                self.broker.open_app(detected.executable)
            except SpotifyBrokerError as exc:
                raise SpotifyInstallError(exc.code) from None
        deadline = time.monotonic() + max(0.0, float(timeout) if open_spotify else 0.75)
        while True:
            if _music_cancelled(cancelled):
                raise SpotifyInstallError("job_cancelled")
            if not open_spotify and not detected.app_running and not self._process_reader():
                raise SpotifyInstallError("not_ready")
            ok, _code = self.broker.verify_read(timeout=min(1.0, max(0.1, deadline - time.monotonic())))
            if ok:
                return
            if time.monotonic() >= deadline:
                raise SpotifyInstallError("not_ready")
            time.sleep(0.1)

    # -------------------------------------------------------------- lifecycle --
    def connect(self) -> dict[str, Any]:
        detected = self._detect(force=True)
        if detected.link != "present":
            raise SpotifyInstallError("not_configured")
        with self._lock:
            self._enabled = True
            self._connection = "connecting"
            self._bump(epoch=True)
        self._emit()
        try:
            self.broker.start()
        except SpotifyBrokerError as exc:
            with self._lock:
                self._connection = "failed"
                self._last_error = {"code": exc.code, "retryable": True}
                self._bump()
            self._emit()
            raise SpotifyInstallError(exc.code) from None
        return self.payload()

    def disconnect(self) -> dict[str, Any]:
        self.broker.stop()
        with self._lock:
            self._enabled = False
            # Desligar a ligação não desinstala nada: `_verified` mantém o setup
            # configurado para reconectar sem repair/reinstalação.
            self._connection = "disabled"
            self._device = "unknown"
            self._bump(epoch=True)
        self._emit()
        return self.payload()

    def autostart(self) -> None:
        """Liga o host se o utilizador já tinha a ligação ativa; sem efeitos musicais."""
        if not self._enabled or self._connection == "disabled":
            return
        if self._detect().link != "present":
            return
        try:
            self.broker.start()
        except SpotifyBrokerError:
            with self._lock:
                self._connection = "failed"
                self._bump()

    def on_broker_session(self, info: dict[str, Any]) -> None:
        with self._lock:
            if info.get("authenticated"):
                self._connection = "connected"
                self._device = info.get("device_scope") or "unknown"
            elif self._connection != "disabled":
                self._connection = "connecting"
                self._device = "unknown"
            self._bump()
        self._emit()

    def open_target(self, target: str) -> dict[str, Any]:
        if target not in contract.SPOTIFY_OPEN_TARGETS:
            raise SpotifyInstallError("invalid_payload")
        if target == "download":
            return {"opened": bool(self.broker.open_url().get("opened"))}
        detected = self._detect(force=True)
        if detected.app != "win32" or detected.executable is None:
            raise SpotifyInstallError("spotify_missing" if detected.app == "absent"
                                      else "not_configured")
        try:
            return {"opened": bool(self.broker.open_app(detected.executable).get("opened"))}
        except SpotifyBrokerError as exc:
            raise SpotifyInstallError(exc.code) from None

    def close(self) -> None:
        # Fechar a aplicação retira autoridade ANTES de esperar: uma thread que
        # regresse de um CLI bloqueado não pode continuar apply/reopen tardio.
        with self._lock:
            self._closed = True
        self._cancel.set()
        # Um CLI bloqueado não pode sobreviver ao fecho e escrever tarde.
        self._terminate_cli()
        thread = self._job_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=10.0)
        self.broker.close()
        with self._lock:
            self._closed = True

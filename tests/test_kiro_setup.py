"""Kiro CLI pela introdução (`app/brain/kiro_setup.py`), sem rede e sem instalar nada.

O que importa provar: só se instala um MSI que bate com o manifesto oficial, um cancelamento
ou um erro deixam um estado claro, e o e-mail da conta nunca sai do `whoami`.
"""
from __future__ import annotations

import hashlib
import subprocess
import threading
from typing import Any

import httpx
import pytest

from app.brain import kiro_setup

REAL_CLIENT = httpx.Client
PAYLOAD = b"msi-bytes" * 1000
PATH = "2.35.1/kiro-cli-x86_64-pc-windows-msvc.msi"


def _manifest(sha: str | None = None, size: int | None = None, download: str = PATH) -> dict[str, Any]:
    return {"version": "2.35.1", "packages": [
        {"os": "linux", "architecture": "x86_64", "kind": "deb", "download": "x", "sha256": "0" * 64, "size": 1},
        {"os": "windows", "architecture": "x86_64", "kind": "msi", "download": download,
         "sha256": sha or hashlib.sha256(PAYLOAD).hexdigest(), "size": size or len(PAYLOAD)}]}


def test_pick_msi_takes_the_windows_package_and_refuses_odd_paths() -> None:
    assert kiro_setup.pick_msi(_manifest()) == (PATH, hashlib.sha256(PAYLOAD).hexdigest(), len(PAYLOAD))
    for bad in ("../evil.msi", "2.35.1/other.msi", "https://evil.test/kiro-cli-x86_64-pc-windows-msvc.msi"):
        with pytest.raises(ValueError):
            kiro_setup.pick_msi(_manifest(download=bad))
    with pytest.raises(ValueError):
        kiro_setup.pick_msi({"packages": "nope"})


def test_account_is_the_provider_never_the_email() -> None:
    assert kiro_setup.account_of("Logged in with Google\nEmail: pessoa@example.com\n") == "Google"
    assert kiro_setup.account_of("error: not logged in") is None


class _Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, manifest: dict[str, Any], body: bytes = PAYLOAD,
                 msiexec_code: int = 0) -> None:
        self.events: list[dict[str, Any]] = []
        self.done = threading.Event()
        self.msiexec: list[list[str]] = []
        self.installed = False

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("manifest.json"):
                return httpx.Response(200, json=manifest)
            assert str(request.url) == f"{kiro_setup.BASE_URL}/{PATH}"
            return httpx.Response(200, content=body)

        monkeypatch.setattr(kiro_setup.httpx, "Client",
                            lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))

        def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            self.msiexec.append(argv)
            self.installed = msiexec_code in (0, 3010)
            return subprocess.CompletedProcess(argv, msiexec_code, "", "")

        monkeypatch.setattr(kiro_setup, "_run", run)
        monkeypatch.setattr(kiro_setup, "find_executable", lambda: "kiro-cli.exe" if self.installed else None)
        monkeypatch.setattr(kiro_setup, "windows_supported", lambda: True)

        def emit(state: dict[str, Any]) -> None:
            self.events.append(state)
            if state["phase"] in {"installed", "error", "idle"}:
                self.done.set()

        self.setup = kiro_setup.KiroSetup(emit)

    def install(self) -> dict[str, Any]:
        self.setup.install()
        assert self.done.wait(10)
        return self.events[-1]


def test_install_downloads_verifies_and_runs_msiexec_passively(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch, _manifest())
    final = h.install()
    assert final["phase"] == "installed" and final["error"] is None
    assert any(e["phase"] == "downloading" and e["progress"] == 1.0 for e in h.events)
    [argv] = h.msiexec
    assert argv[:2] == ["msiexec", "/i"] and argv[3:] == ["/passive", "/norestart"]


def test_tampered_download_is_never_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch, _manifest(), body=b"x" * len(PAYLOAD))
    final = h.install()
    assert final["phase"] == "error" and "SHA-256" in final["error"]
    assert h.msiexec == []


def test_download_larger_than_the_manifest_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch, _manifest(size=10))
    assert h.install()["phase"] == "error"
    assert h.msiexec == []


def test_windows_installer_failure_is_reported_and_user_cancel_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = _Harness(monkeypatch, _manifest(), msiexec_code=1603).install()
    assert failed["phase"] == "error" and "1603" in failed["error"]
    cancelled = _Harness(monkeypatch, _manifest(), msiexec_code=1602).install()
    assert cancelled["phase"] == "idle" and cancelled["error"] is None


def test_unknown_login_method_and_missing_cli_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    setup = kiro_setup.KiroSetup(lambda state: None)
    with pytest.raises(ValueError):
        setup.login("facebook")
    monkeypatch.setattr(kiro_setup, "find_executable", lambda: None)
    with pytest.raises(RuntimeError, match="not_installed"):
        setup.login("google")

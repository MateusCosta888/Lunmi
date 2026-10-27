"""O desinstalador chama o mesmo job de remoção e para quando ele falha."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.integrations import spotify_uninstall


class _Installer:
    created: list["_Installer"] = []

    def __init__(self, *, state_root: Path, broker: Any, session_id: str, emit: Any) -> None:
        self.root = state_root
        self.broker = broker
        self.session_id = session_id
        self.emit = emit
        self.calls: list[tuple[str, Any]] = []
        self.job: dict[str, Any] = {}
        _Installer.created.append(self)

    def state(self) -> dict[str, Any]:
        return {"setup": "configured", "job": dict(self.job) if self.job else None}

    def plan(self, action: str) -> dict[str, Any]:
        self.calls.append(("plan", action))
        return {"plan": {"action": action}, "consent": {"consent_id": "cns-" + "a" * 32}}

    def apply(self, consent_id: str) -> dict[str, Any]:
        self.calls.append(("apply", consent_id))
        self.job = {"id": "spj-test", "status": "done", "error_code": None}
        return {"accepted": True, "job_id": "spj-test", "epoch": 1}

    def close(self) -> None:
        self.calls.append(("close", None))


def test_uninstall_uses_the_same_remove_job(monkeypatch: pytest.MonkeyPatch,
                                            tmp_path: Path) -> None:
    _Installer.created.clear()
    monkeypatch.setattr(spotify_uninstall, "SpotifyBroker", lambda **kwargs: object())
    monkeypatch.setattr(spotify_uninstall, "SpotifyInstaller", _Installer)
    assert spotify_uninstall.main(["--state-root", str(tmp_path), "--approved"]) == 0
    installer = _Installer.created[-1]
    assert installer.calls[:2] == [("plan", "remove"), ("apply", "cns-" + "a" * 32)]
    assert ("close", None) in installer.calls


def test_uninstall_refuses_to_claim_success_when_the_job_fails(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Failed(_Installer):
        def apply(self, consent_id: str) -> dict[str, Any]:
            self.calls.append(("apply", consent_id))
            self.job = {"id": "spj-test", "status": "failed", "error_code": "not_ready"}
            return {"accepted": True, "job_id": "spj-test", "epoch": 1}

    _Installer.created.clear()
    monkeypatch.setattr(spotify_uninstall, "SpotifyBroker", lambda **kwargs: object())
    monkeypatch.setattr(spotify_uninstall, "SpotifyInstaller", Failed)
    assert spotify_uninstall.main(["--state-root", str(tmp_path), "--approved"]) == 2
    assert _Installer.created[-1].calls[0] == ("plan", "remove")


def test_uninstall_without_a_configured_link_is_a_noop(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Absent(_Installer):
        def state(self) -> dict[str, Any]:
            return {"setup": "absent", "job": None}

        def plan(self, action: str) -> dict[str, Any]:
            raise AssertionError("sem setup não há plano de remoção")

    _Installer.created.clear()
    monkeypatch.setattr(spotify_uninstall, "SpotifyBroker", lambda **kwargs: object())
    monkeypatch.setattr(spotify_uninstall, "SpotifyInstaller", Absent)
    assert spotify_uninstall.main(["--state-root", str(tmp_path)]) == 0
    assert not any(call[0] == "apply" for call in _Installer.created[-1].calls)

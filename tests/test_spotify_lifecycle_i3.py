"""I3: costuras de serviço — setup x tarefa, injeção e encerramento.

Só duplos: nenhuma porta, Spotify, CLI, rede ou provider. O teste observa a
guarda de busy nos dois sentidos e que o controller recebe o objeto Spotify.
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from app.bridge import protocol
from app.bridge.service import BridgeService
from app.core.controller import TaskResult


class _FakeController:
    def __init__(self, settings: Any, **_: Any) -> None:
        self.settings = settings
        self.started = threading.Event()
        self.release = threading.Event()
        self._task: asyncio.Task[Any] | None = None
        self.closed = False

    def set_auto_approve(self, enabled: bool) -> None:
        pass

    async def run(self, instruction: str, **_: Any) -> TaskResult:
        self.started.set()
        self._task = asyncio.current_task()
        try:
            while not self.release.is_set():
                await asyncio.sleep(0.01)
            return TaskResult(status="SUCCESS", message="ok", scope="end_to_end")
        except asyncio.CancelledError:
            return TaskResult(status="CANCELLED", failure_kind="CANCELLED",
                              message="cancelada", scope="end_to_end")

    def cancel(self) -> None:
        self.release.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def close(self) -> None:
        self.closed = True


class _FakeInstaller:
    def __init__(self) -> None:
        self.broker = object()
        self.job_running = False
        self.closed = False
        self.apply_calls: list[str] = []

    def apply(self, consent_id: str) -> dict[str, Any]:
        self.apply_calls.append(consent_id)
        return {"accepted": True, "job_id": "spj-test", "epoch": 1}

    def close(self) -> None:
        self.closed = True


def _service(tmp_path: Path, installer: _FakeInstaller) -> tuple[BridgeService, list[tuple[str, dict[str, Any]]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))
        if kind == "task.finished":
            pass

    svc = BridgeService(emit=emit, sandbox_dir=tmp_path,
                        controller_factory=_FakeController,
                        spotify_install=installer)
    return svc, events


def test_setup_is_deferred_while_a_computer_use_task_is_active(tmp_path: Path) -> None:
    installer = _FakeInstaller()
    svc, _events = _service(tmp_path, installer)
    try:
        assert svc.controller.spotify is installer
        svc.dispatch(protocol.Envelope(v=1, id="task", type="chat.send",
                                       payload={"text": "tarefa longa", "mode": "execute"}))
        assert svc.controller.started.wait(2.0)
        with pytest.raises(protocol.ProtocolError) as info:
            svc.dispatch(protocol.Envelope(v=1, id="apply", type="spotify.apply",
                                           payload={"consent_id": "cns-" + "a" * 32}))
        assert info.value.code == "task_busy"
        assert installer.apply_calls == []
        svc.dispatch(protocol.Envelope(v=1, id="cancel", type="task.cancel", payload={}))
        deadline = time.monotonic() + 2.0
        while svc._is_task_running() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not svc._is_task_running()
        svc.dispatch(protocol.Envelope(v=1, id="apply2", type="spotify.apply",
                                       payload={"consent_id": "cns-" + "b" * 32}))
        assert installer.apply_calls == ["cns-" + "b" * 32]
    finally:
        svc.close()


def test_chat_is_deferred_while_setup_job_runs_and_close_keeps_spotify(tmp_path: Path) -> None:
    installer = _FakeInstaller()
    installer.job_running = True
    svc, _events = _service(tmp_path, installer)
    try:
        with pytest.raises(protocol.ProtocolError) as info:
            svc.dispatch(protocol.Envelope(v=1, id="chat", type="chat.send",
                                           payload={"text": "toca música", "mode": "auto"}))
        assert info.value.code == "job_busy"
        assert not svc.controller.started.is_set()
    finally:
        svc.close()
    assert installer.closed is True
    assert svc.controller.closed is True

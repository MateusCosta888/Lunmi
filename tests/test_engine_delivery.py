from __future__ import annotations

import asyncio
import json

import pytest

from app.computer.ufo_engine import ComputerError, UFOEngine
from app.settings import Settings


class _Pipe:
    """A worker's stdin, enough to see what the engine wrote to it."""

    def __init__(self) -> None:
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        return None


class _Worker:
    def __init__(self) -> None:
        self.stdin = _Pipe()


def test_a_consent_event_mid_operation_is_answered_and_read_past(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker may ask for consent *inside* an operation; that line is not its answer.

    Measured live: the first launch of a swept application asks once, and the question
    arrives between the request and the reply. Reading it as a reply turned a launch that
    only needed a yes into a worker error.
    """
    replies = [{"event": "confirm", "message": "Abrir este aplicativo?"},
               {"ok": True, "data": {"outcome": "success"}}]

    async def start(self: UFOEngine) -> None:
        return None

    async def read(self: UFOEngine) -> dict:
        return replies.pop(0)

    monkeypatch.setattr(UFOEngine, "start", start)
    monkeypatch.setattr(UFOEngine, "_read", read)
    engine = UFOEngine(Settings())
    worker = _Worker()
    engine.process = worker  # type: ignore[assignment]
    asked: list[str] = []

    async def confirm(message: str) -> bool:
        asked.append(message)
        return True

    async def scenario() -> dict:
        return await engine.fast_path({"kind": "launch"}, confirm)

    assert asyncio.run(scenario()) == {"outcome": "success"}
    assert asked == ["Abrir este aplicativo?"]
    assert b'"approved": true' in bytes(worker.stdin.written)


def test_the_approval_mode_travels_with_each_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """The user's mode reaches the worker per task, so switching it needs no restart."""
    replies = [{"ok": True, "data": {"status": "SUCCESS", "evidence": []}}]

    async def start(self: UFOEngine) -> None:
        return None

    async def read(self: UFOEngine) -> dict:
        return replies.pop(0)

    monkeypatch.setattr(UFOEngine, "start", start)
    monkeypatch.setattr(UFOEngine, "_read", read)
    engine = UFOEngine(Settings())
    worker = _Worker()
    engine.process = worker  # type: ignore[assignment]

    async def completion(messages: list[dict], agent: str = "") -> str:
        return "{}"

    async def confirm(message: str) -> bool:
        return True

    asyncio.run(engine.run_task("tarefa", completion, confirm, approval_mode="auto"))
    request = json.loads(bytes(worker.stdin.written).decode("utf-8").splitlines()[0])
    assert request["operation"] == "run_task" and request["approval_mode"] == "auto"


def test_lost_reply_does_not_replay_a_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    stopped: list[bool] = []
    async def interrupted(self: UFOEngine, operation: str, **arguments: object) -> dict:
        calls.append(operation)
        raise ConnectionResetError("reply lost after mutation")
    async def stop(self: UFOEngine) -> None:
        stopped.append(True)
    monkeypatch.setattr(UFOEngine, "_exchange", interrupted)
    monkeypatch.setattr(UFOEngine, "stop", stop)
    async def scenario() -> None:
        with pytest.raises(ComputerError, match="não foi repetida"):
            await UFOEngine(Settings()).fast_path({"kind": "filesystem", "operation": "rename"})
    asyncio.run(scenario())
    assert calls == ["fast_path"] and stopped == [True]

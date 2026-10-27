"""VOICE REFINEMENTS V2 — readiness agregado de TTS+STT sem áudio nem microfone."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from app.voice.warmup import VoiceWarmup


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class _FakeTts:
    def __init__(self, result: tuple[str, str] = ("ready", ""), delay: float = 0.0) -> None:
        self.result = result
        self.delay = delay
        self.calls: list[float] = []

    def warm_up(self, timeout: float) -> tuple[str, str]:
        self.calls.append(timeout)
        if self.delay:
            time.sleep(self.delay)
        return self.result


class _FakeStt:
    def __init__(self, result: tuple[str, str] = ("ready", ""), delay: float = 0.0) -> None:
        self.result = result
        self.delay = delay
        self.calls: list[float] = []

    def prepare(self, timeout: float) -> tuple[str, str]:
        self.calls.append(timeout)
        if self.delay:
            time.sleep(self.delay)
        return self.result


def test_warmup_runs_both_legs_and_publishes_ready_without_audio() -> None:
    events: list[dict[str, Any]] = []
    tts = _FakeTts(delay=0.05)
    stt = _FakeStt(delay=0.05)
    coordinator = VoiceWarmup(tts=tts, stt=stt, emit=events.append, timeout_s=1.0)

    first = coordinator.start()
    assert first == {"state": "preparing", "ready": False,
                     "tts": "preparing", "stt": "preparing", "message": ""}
    assert _wait_for(lambda: coordinator.snapshot()["ready"])
    final = coordinator.snapshot()
    assert final == {"state": "ready", "ready": True, "tts": "ready", "stt": "ready",
                     "message": ""}
    assert tts.calls == [1.0] and stt.calls == [1.0]
    assert events
    assert all(set(item) == {"state", "ready", "tts", "stt", "message"} for item in events)
    assert events[0] == {"state": "preparing", "ready": False,
                         "tts": "preparing", "stt": "preparing", "message": ""}
    ready_at = next(index for index, item in enumerate(events) if item["ready"])
    assert all(item["ready"] is False for item in events[ready_at + 1:])
    assert events[-1]["ready"] is True


def test_warmup_is_idempotent_and_get_never_starts_a_leg() -> None:
    tts = _FakeTts(delay=0.05)
    stt = _FakeStt(delay=0.05)
    coordinator = VoiceWarmup(tts=tts, stt=stt, timeout_s=1.0)
    assert coordinator.snapshot()["state"] == "idle"
    assert tts.calls == [] and stt.calls == []

    coordinator.start()
    coordinator.start()
    assert _wait_for(lambda: coordinator.snapshot()["ready"])
    assert tts.calls == [1.0] and stt.calls == [1.0]


def test_unavailable_and_error_legs_are_reported_but_do_not_block_readiness() -> None:
    tts = _FakeTts(result=("unavailable", "Sem runtime de voz."))
    stt = _FakeStt(result=("error", "Sem modelo de ditado."))
    coordinator = VoiceWarmup(tts=tts, stt=stt, timeout_s=1.0)
    coordinator.start()
    assert _wait_for(lambda: coordinator.snapshot()["ready"])
    snapshot = coordinator.snapshot()
    assert snapshot["state"] == "ready" and snapshot["ready"] is True
    assert snapshot["tts"] == "unavailable" and snapshot["stt"] == "error"
    assert "runtime de voz" in snapshot["message"]
    assert "modelo de ditado" in snapshot["message"]


def test_timeout_of_an_injected_manager_is_finite_and_terminal() -> None:
    release = threading.Event()

    class _Stuck:
        def warm_up(self, timeout: float) -> tuple[str, str]:
            release.wait(5.0)
            return "ready", ""

    coordinator = VoiceWarmup(tts=_Stuck(), stt=_FakeStt(), timeout_s=0.05,
                             emit=lambda _payload: None)
    coordinator.start()
    assert _wait_for(lambda: coordinator.snapshot()["ready"], timeout=2.0)
    snapshot = coordinator.snapshot()
    assert snapshot["tts"] == "error"
    assert "demorou" in snapshot["message"]
    assert snapshot["ready"] is True
    release.set()
    # A perna bloqueada acorda tarde e devolve `ready`; o timeout continua terminal.
    time.sleep(0.1)
    late = coordinator.snapshot()
    assert late["tts"] == "error" and late["ready"] is True
    assert late["state"] == "ready"


class _FakeVoice:
    def __init__(self) -> None:
        self.warm_calls = 0
        self.closed = False

    def warm_up(self, timeout: float) -> tuple[str, str]:
        self.warm_calls += 1
        return "ready", ""

    def snapshot(self) -> dict[str, Any]:
        return {"enabled": False, "auto_speak": False, "available": True,
                "state": "disabled", "generation": 0, "can_speak_last": False,
                "capture_supported": False, "message": ""}

    def stop(self) -> dict[str, Any]:
        return {"accepted": True, "generation": 0, "state": "disabled", "reason": ""}

    def close(self) -> None:
        self.closed = True


class _FakeInput:
    def __init__(self) -> None:
        self.prepare_calls = 0
        self.closed = False

    def prepare(self, timeout: float) -> tuple[str, str]:
        self.prepare_calls += 1
        return "ready", ""

    def snapshot(self) -> dict[str, Any]:
        return {"available": True, "state": "idle", "generation": 0, "message": ""}

    def close(self) -> None:
        self.closed = True


class _FakeController:
    class _Engine:
        def set_host_pids(self, _pids: Any) -> None:
            pass

    engine = _Engine()

    def set_auto_approve(self, _enabled: bool) -> None:
        pass

    def cancel(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def run(self, _instruction: str, **_kwargs: Any) -> Any:
        raise AssertionError("warmup never runs a task")


def test_bridge_exposes_warmup_dto_and_readiness_event(tmp_path: Path) -> None:
    from app.bridge import protocol
    from app.bridge.service import BridgeService
    from app.settings import Settings

    events: list[tuple[str, dict[str, Any]]] = []
    voice = _FakeVoice()
    voice_input = _FakeInput()
    service = BridgeService(
        emit=lambda kind, payload: events.append((kind, payload)),
        sandbox_dir=tmp_path,
        settings=Settings(state_root=tmp_path, logs_path=tmp_path / "logs",
                          output_path=tmp_path / "output", prewarm=False),
        controller_factory=lambda *_args, **_kwargs: _FakeController(),
        voice=voice,
        voice_input=voice_input,
    )
    try:
        first = service.dispatch(protocol.Envelope(
            v=1, id="cmd-warmup", type="voice.warmup", payload={}))
        assert first == {"state": "preparing", "ready": False,
                         "tts": "preparing", "stt": "preparing", "message": ""}
        assert _wait_for(lambda: any(kind == "voice.readiness" and payload["ready"]
                                     for kind, payload in events))
        kind, payload = next((kind, payload) for kind, payload in events
                             if kind == "voice.readiness" and payload["ready"])
        assert kind == "voice.readiness"
        assert set(payload) == {"session_id", "state", "ready", "tts", "stt", "message"}
        assert payload["session_id"] == service.session_id
        assert payload["state"] == "ready" and payload["ready"] is True
        assert voice.warm_calls == 1 and voice_input.prepare_calls == 1

        snapshot = service.dispatch(protocol.Envelope(
            v=1, id="cmd-warmup-get", type="voice.warmup.get", payload={}))
        assert snapshot == {"state": "ready", "ready": True, "tts": "ready",
                            "stt": "ready", "message": ""}
        assert voice.warm_calls == 1  # get não inicia segunda preparação
    finally:
        service.close()
    assert voice.closed is True and voice_input.closed is True

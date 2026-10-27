"""VOICE INPUT V1 — comandos/eventos na ponte real, payloads fechados e half-duplex."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from app.bridge import protocol
from app.bridge.service import BridgeService
from app.core.controller import TaskResult
from app.settings import Settings
from app.voice.input_manager import VoiceInputManager
from app.voice.input_runtime import VoiceInputEnvironment
from app.voice.manager import VoiceEnvironment, VoiceManager

INPUT_EVENT_KEYS = {"session_id", "available", "state", "generation", "message"}
VOICE_STATE_KEYS = {
    "session_id", "enabled", "auto_speak", "available", "state", "generation",
    "can_speak_last", "capture_supported", "message",
}


class _FakeEngine:
    def set_host_pids(self, pids: Any) -> None:
        self.pids = set(pids)


class _FakeController:
    engine = _FakeEngine()

    def set_auto_approve(self, enabled: bool) -> None:
        pass

    def cancel(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def run(self, instruction: str, **kwargs: Any) -> TaskResult:
        del instruction, kwargs
        return TaskResult(status="SUCCESS", message="Resposta.", scope="conversation")


class _FakeInputManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.generation = 4
        self.state = "idle"
        self.closed = False

    def snapshot(self) -> dict[str, Any]:
        return {"available": True, "state": self.state,
                "generation": self.generation, "message": ""}

    def start(self, mode: str = "manual") -> dict[str, Any]:
        self.calls.append(("start", None if mode == "manual" else mode))
        self.state = "starting"
        return {"accepted": True, "generation": self.generation,
                "state": self.state, "reason": ""}

    def finish(self, generation: int) -> dict[str, Any]:
        self.calls.append(("finish", generation))
        self.state = "transcribing"
        return {"accepted": True, "generation": generation,
                "state": self.state, "reason": ""}

    def cancel(self) -> dict[str, Any]:
        self.calls.append(("cancel", None))
        self.state = "idle"
        return {"accepted": True, "generation": self.generation,
                "state": self.state, "reason": ""}

    def result(self, generation: int) -> dict[str, Any]:
        self.calls.append(("result", generation))
        if generation != self.generation:
            return {"status": "none", "generation": self.generation, "text": "",
                    "truncated": False}
        self.state = "idle"
        return {"status": "ready", "generation": generation,
                "text": "Pesquisa alpha", "truncated": False}

    def close(self) -> None:
        self.closed = True


TTS_WORKER = """\
import json, sys
for line in sys.stdin:
    try:
        data = json.loads(line)
    except Exception:
        continue
    kind = data.get("type")
    generation = data.get("generation")
    if kind == "speak":
        print(json.dumps({"type": "state", "generation": generation,
                          "state": "playing"}), flush=True)
        print(json.dumps({"type": "done", "generation": generation}), flush=True)
        print(json.dumps({"type": "ready", "generation": generation}), flush=True)
    elif kind == "shutdown":
        break
"""

INPUT_WORKER = """\
import json, sys, time
for line in sys.stdin:
    try:
        data = json.loads(line)
    except Exception:
        continue
    kind = data.get("type")
    generation = data.get("generation")
    if kind == "start":
        print(json.dumps({"type": "state", "generation": generation,
                          "state": "listening"}), flush=True)
        time.sleep(0.8)
        print(json.dumps({"type": "state", "generation": generation,
                          "state": "transcribing"}), flush=True)
        print(json.dumps({"type": "result", "generation": generation,
                          "text": "Pesquisa alpha no Spotify",
                          "truncated": False}), flush=True)
    elif kind == "cancel":
        print(json.dumps({"type": "stopped", "generation": generation}), flush=True)
    elif kind == "shutdown":
        break
"""


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _voice_env(tmp_path: Path, worker: Path) -> VoiceEnvironment:
    models = tmp_path / "tts-models"
    models.mkdir(exist_ok=True)
    return VoiceEnvironment(home=tmp_path, python=Path(sys.executable), models=models,
                            worker=worker)


def _input_env(tmp_path: Path, worker: Path) -> VoiceInputEnvironment:
    model = tmp_path / "stt-model"
    model.mkdir(exist_ok=True)
    silero = tmp_path / "silero.onnx"
    silero.write_bytes(b"onnx")
    return VoiceInputEnvironment(home=tmp_path, python=Path(sys.executable), model=model,
                                 silero=silero, worker=worker)


@pytest.fixture()
def fake_bridge(tmp_path: Path):
    events: list[tuple[str, dict[str, Any]]] = []
    manager = _FakeInputManager()
    service = BridgeService(
        emit=lambda kind, payload: events.append((kind, payload)),
        sandbox_dir=tmp_path,
        settings=Settings(state_root=tmp_path, logs_path=tmp_path / "logs",
                          output_path=tmp_path / "output", prewarm=False),
        controller_factory=lambda *args, **kwargs: _FakeController(),
        voice_input=manager,
    )

    def command(kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return service.dispatch(protocol.Envelope(v=1, id="cmd-input-1", type=kind,
                                                 payload=payload or {}))

    yield {"service": service, "manager": manager, "events": events, "command": command,
           "tmp": tmp_path}
    service.close()


def test_closed_payloads_for_input_commands(fake_bridge) -> None:
    command = fake_bridge["command"]
    with pytest.raises(protocol.ProtocolError) as excinfo:
        command("voice.input.start", {"text": "PRIVATE"})
    assert excinfo.value.code == "invalid_payload"
    with pytest.raises(protocol.ProtocolError) as excinfo:
        command("voice.input.start", {"mode": "other"})
    assert excinfo.value.code == "invalid_payload"
    for kind in ("voice.input.get", "voice.input.cancel"):
        with pytest.raises(protocol.ProtocolError) as excinfo:
            command(kind, {"path": "C:/fake"})
        assert excinfo.value.code == "invalid_payload"

    for kind in ("voice.input.result", "voice.input.finish"):
        for payload in ({}, {"generation": True}, {"generation": "3"}, {"generation": -1},
                        {"generation": 3, "text": "PRIVATE"}):
            with pytest.raises(protocol.ProtocolError) as excinfo:
                command(kind, payload)
            assert excinfo.value.code == "invalid_payload"


def test_commands_and_capability_are_honest(fake_bridge) -> None:
    service = fake_bridge["service"]
    assert service.capabilities()["voice_input"] is True
    assert "voice.input.finish" in protocol.VOICE_INPUT_COMMAND_TYPES
    assert protocol.capabilities_for_commands(protocol.COMMAND_TYPES)["voice_input"] is True

    state = fake_bridge["command"]("voice.input.get")
    assert state == {"available": True, "state": "idle", "generation": 4, "message": ""}
    assert "session_id" not in state

    ack = fake_bridge["command"]("voice.input.start")
    assert ack["accepted"] is True and ack["state"] == "starting"
    assert fake_bridge["manager"].calls == [("start", None)]

    hold = fake_bridge["command"]("voice.input.start", {"mode": "hold"})
    assert hold["accepted"] is True
    assert fake_bridge["manager"].calls[-1] == ("start", "hold")
    finished = fake_bridge["command"]("voice.input.finish", {"generation": 4})
    assert finished["accepted"] is True
    assert fake_bridge["manager"].calls[-1] == ("finish", 4)

    result = fake_bridge["command"]("voice.input.result", {"generation": 4})
    assert result == {"status": "ready", "generation": 4,
                      "text": "Pesquisa alpha", "truncated": False}
    assert fake_bridge["command"]("voice.input.result", {"generation": 3})["status"] == "none"


def test_input_state_event_shape_and_no_text(fake_bridge) -> None:
    service = fake_bridge["service"]
    service.voice_input.state = "listening"
    service._emit_voice_input({**service.voice_input.snapshot(), "session_id": service.session_id})
    kind, payload = fake_bridge["events"][-1]
    assert kind == "voice.input.state"
    assert set(payload) == INPUT_EVENT_KEYS
    assert payload["session_id"] == service.session_id


def test_legacy_tts_dto_is_unchanged(fake_bridge) -> None:
    state = fake_bridge["command"]("voice.get")
    assert set(state) == VOICE_STATE_KEYS - {"session_id"}
    assert state["capture_supported"] is False


@pytest.fixture()
def real_bridge(tmp_path: Path):
    events: list[tuple[str, dict[str, Any]]] = []
    tts_worker = _script(tmp_path, "tts_worker.py", TTS_WORKER)
    input_worker = _script(tmp_path, "input_worker.py", INPUT_WORKER)
    holder: dict[str, Any] = {}

    voice = VoiceManager(
        emit=lambda payload: holder["service"]._emit_voice(payload),
        root_dir=tmp_path,
        state_root=tmp_path,
        session_id="sess-input-bridge",
        environment=_voice_env(tmp_path, tts_worker),
    )
    manager = VoiceInputManager(
        emit=lambda payload: holder["service"]._emit_voice_input(payload),
        state_root=tmp_path,
        session_id="sess-input-bridge",
        environment=_input_env(tmp_path, input_worker),
        on_speech_interrupt=voice.stop,
        on_active_changed=voice.set_input_active,
    )
    service = BridgeService(
        emit=lambda kind, payload: events.append((kind, payload)),
        sandbox_dir=tmp_path,
        settings=Settings(state_root=tmp_path, logs_path=tmp_path / "logs",
                          output_path=tmp_path / "output", prewarm=False),
        controller_factory=lambda *args, **kwargs: _FakeController(),
        voice=voice,
        voice_input=manager,
    )
    holder["service"] = service

    def command(kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return service.dispatch(protocol.Envelope(v=1, id="cmd-real-1", type=kind,
                                                 payload=payload or {}))

    yield {"service": service, "voice": voice, "manager": manager, "events": events,
           "command": command}
    service.close()


def test_start_interrupts_tts_and_blocks_new_speech_while_mic_is_open(real_bridge) -> None:
    command = real_bridge["command"]
    assert command("voice.set", {"enabled": True})["enabled"] is True

    ack = command("voice.input.start")
    assert ack["accepted"] is True
    # A fala é recusada enquanto a captura está ativa (gate aplicado antes de abrir o mic).
    refused = command("voice.test")
    assert refused["accepted"] is False and refused["reason"] == "input_active"

    # Mesmo já com o microfone aberto (listening/transcribing), a fala continua recusada.
    assert _wait_for(lambda: command("voice.input.get")["state"] in
                     {"listening", "transcribing"})
    during = command("voice.test")
    assert during["accepted"] is False and during["reason"] == "input_active"

    assert _wait_for(lambda: command("voice.input.get")["state"] == "ready")
    state = command("voice.input.get")
    delivered = command("voice.input.result", {"generation": state["generation"]})
    assert delivered["status"] == "ready"
    assert delivered["text"] == "Pesquisa alpha no Spotify"
    assert command("voice.input.result", {"generation": state["generation"]})["status"] == "none"

    # Resultado consumido: o gate fecha e a fala volta a ser aceite.
    assert _wait_for(lambda: real_bridge["manager"].active is False)
    allowed = command("voice.test")
    assert allowed["accepted"] is True


def test_private_transcript_never_appears_in_any_event(real_bridge) -> None:
    command = real_bridge["command"]
    command("voice.input.start")
    assert _wait_for(lambda: command("voice.input.get")["state"] == "ready")
    generation = command("voice.input.get")["generation"]
    assert command("voice.input.result", {"generation": generation})["status"] == "ready"
    for kind, payload in real_bridge["events"]:
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "Pesquisa alpha no Spotify" not in serialized
        if kind == "voice.input.state":
            assert set(payload) == INPUT_EVENT_KEYS
        if kind == "voice.state":
            assert set(payload) == VOICE_STATE_KEYS
            assert payload["capture_supported"] is False


def test_service_close_terminates_both_workers(real_bridge) -> None:
    command = real_bridge["command"]
    command("voice.set", {"enabled": True})
    command("voice.input.start")
    assert _wait_for(lambda: real_bridge["manager"].worker_pid is not None)
    real_bridge["service"].close()
    assert real_bridge["manager"].worker_pid is None
    assert real_bridge["voice"].worker_pid is None

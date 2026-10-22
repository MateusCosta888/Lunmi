"""VOICE V1 — comandos/eventos de voz na ponte real, com worker falso e sandbox."""
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
from app.voice.manager import VoiceEnvironment, VoiceManager
from app.voice.prefs import prefs_path

EVENT_KEYS = {
    "session_id", "enabled", "auto_speak", "available", "state", "generation",
    "can_speak_last", "capture_supported", "message",
}


class _FakeEngine:
    def set_host_pids(self, pids: Any) -> None:
        self.pids = set(pids)


class _FakeController:
    def __init__(self, result: TaskResult | None = None) -> None:
        self.result = result or TaskResult(status="SUCCESS", message="Resposta.",
                                           scope="conversation")
        self.engine = _FakeEngine()

    def set_auto_approve(self, enabled: bool) -> None:
        pass

    def cancel(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def run(self, instruction: str, **kwargs: Any) -> TaskResult:
        del instruction, kwargs
        return self.result


def _worker(tmp_path: Path) -> Path:
    path = tmp_path / "fake_voice_worker.py"
    path.write_text(
        "import json, sys, time\n"
        "for line in sys.stdin:\n"
        "    try:\n"
        "        data = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    if data.get('type') == 'speak':\n"
        "        generation = data.get('generation')\n"
        "        print(json.dumps({'type': 'state', 'generation': generation,\n"
        "                          'state': 'playing'}), flush=True)\n"
        "        print(json.dumps({'type': 'done', 'generation': generation}), flush=True)\n"
        "        print(json.dumps({'type': 'ready', 'generation': generation}), flush=True)\n"
        "    elif data.get('type') == 'shutdown':\n"
        "        break\n",
        encoding="utf-8",
    )
    return path


def _env(tmp_path: Path) -> VoiceEnvironment:
    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    return VoiceEnvironment(home=tmp_path, python=Path(sys.executable), models=models,
                            worker=_worker(tmp_path))


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def bridge(tmp_path: Path):
    events: list[tuple[str, dict[str, Any]]] = []

    class _Controller(_FakeController):
        async def run(self, instruction: str, **kwargs: Any) -> TaskResult:
            del instruction, kwargs
            return self.result or TaskResult(status="SUCCESS", message="Resposta.",
                                             scope="conversation")

    controller = _Controller()
    holder: dict[str, Any] = {}
    voice = VoiceManager(
        # O emit do gestor entra no mesmo caminho público do serviço (`voice.state`).
        emit=lambda payload: holder["service"]._emit_voice(payload),
        root_dir=tmp_path,
        state_root=tmp_path,
        session_id="sess-voice",
        environment=_env(tmp_path),
    )
    service = BridgeService(
        emit=lambda kind, payload: events.append((kind, payload)),
        sandbox_dir=tmp_path,
        settings=Settings(state_root=tmp_path, logs_path=tmp_path / "logs",
                          output_path=tmp_path / "output", prewarm=False),
        controller_factory=lambda *args, **kwargs: controller,
        voice=voice,
    )
    holder["service"] = service

    def dispatch(kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        env = protocol.Envelope(v=1, id="cmd-voice-1", type=kind, payload=payload or {})
        return service.dispatch(env)

    def send_chat(text: str = "Olá", *, mode: str = "converse",
                  attachments: list[dict[str, Any]] | None = None) -> None:
        payload: dict[str, Any] = {"mode": mode, "text": text}
        if attachments:
            payload["attachments"] = attachments
        dispatch("chat.send", payload)

    yield {
        "service": service,
        "voice": voice,
        "events": events,
        "controller": controller,
        "dispatch": dispatch,
        "send_chat": send_chat,
        "tmp": tmp_path,
    }
    service.close()


def test_voice_get_returns_the_frozen_dto(bridge) -> None:
    dto = bridge["dispatch"]("voice.get")
    assert dto == {
        "enabled": False,
        "auto_speak": False,
        "available": True,
        "state": "disabled",
        "generation": 0,
        "can_speak_last": False,
        "capture_supported": False,
        "message": "",
    }
    assert "session_id" not in dto


def test_voice_set_persists_in_the_sandbox_and_emits_state(bridge) -> None:
    dto = bridge["dispatch"]("voice.set", {"enabled": True, "auto_speak": True})
    assert dto["enabled"] is True and dto["auto_speak"] is True
    assert dto["state"] == "ready"

    path = prefs_path(bridge["tmp"])
    assert path == bridge["tmp"] / ".local" / "voice-prefs.json"
    assert json.loads(path.read_text(encoding="utf-8"))["enabled"] is True

    voice_events = [payload for kind, payload in bridge["events"] if kind == "voice.state"]
    assert voice_events
    assert set(voice_events[-1]) == EVENT_KEYS
    assert voice_events[-1]["session_id"] == "sess-voice"
    assert voice_events[-1]["capture_supported"] is False


@pytest.mark.parametrize("payload", [{"extra": True}, {"enabled": "true"}, {"enabled": 1},
                                     {"auto_speak": None}, {}])
def test_voice_set_rejects_invalid_payloads(bridge, payload: dict[str, Any]) -> None:
    with pytest.raises(protocol.ProtocolError) as excinfo:
        bridge["dispatch"]("voice.set", payload)
    assert excinfo.value.code == "invalid_payload"
    assert bridge["voice"].snapshot()["enabled"] is False


@pytest.mark.parametrize("kind", ["voice.get", "voice.test", "voice.speak_last", "voice.stop"])
def test_voice_commands_reject_extra_fields(bridge, kind: str) -> None:
    with pytest.raises(protocol.ProtocolError) as excinfo:
        bridge["dispatch"](kind, {"text": "fala isto"})
    assert excinfo.value.code == "invalid_payload"


def test_successful_conversation_sets_can_speak_last_and_speaks_on_demand(bridge) -> None:
    bridge["dispatch"]("voice.set", {"enabled": True})
    bridge["send_chat"]("Pergunta simples")
    assert _wait_for(lambda: bridge["voice"].snapshot()["can_speak_last"] is True)

    ack = bridge["dispatch"]("voice.speak_last")
    assert ack["accepted"] is True
    assert _wait_for(lambda: any(
        payload["state"] == "playing" for kind, payload in bridge["events"]
        if kind == "voice.state"))
    assert bridge["voice"].worker_pid is not None


def test_failed_reply_clears_the_previous_spoken_text(bridge) -> None:
    bridge["voice"].set(enabled=True, auto_speak=False)
    bridge["send_chat"]("Primeira")
    assert _wait_for(lambda: bridge["voice"].snapshot()["can_speak_last"] is True)

    bridge["controller"].result = TaskResult(status="FAILED", message="Falhou.",
                                             scope="conversation", failure_kind="TIMEOUT")
    bridge["send_chat"]("Segunda")
    assert _wait_for(lambda: bridge["voice"].snapshot()["can_speak_last"] is False)
    ack = bridge["dispatch"]("voice.speak_last")
    assert ack["accepted"] is False
    assert ack["reason"] == "no_last_response"


def test_reply_with_attachment_is_not_spoken(bridge) -> None:
    bridge["voice"].set(enabled=True, auto_speak=True)
    bridge["send_chat"]("Resumo do anexo", attachments=[
        {"name": "nota.txt", "text": "conteúdo externo", "mime": "text/plain"}])
    assert _wait_for(lambda: any(
        kind == "task.finished" for kind, _ in bridge["events"]))
    assert bridge["voice"].snapshot()["can_speak_last"] is False
    assert bridge["voice"].worker_pid is None


def test_operations_edges_block_speech_at_the_bridge_boundary(bridge) -> None:
    bridge["voice"].set(enabled=True)

    def finished_count() -> int:
        return sum(1 for kind, _ in bridge["events"] if kind == "task.finished")

    bridge["send_chat"]("Pergunta normal")
    assert _wait_for(lambda: bridge["voice"].snapshot()["can_speak_last"] is True)

    # Scope conversa + operação por verificar: nunca vira fala.
    bridge["controller"].result = TaskResult(
        status="SUCCESS", scope="conversation", message="Já fiz a alteração.",
        operation_states=[{"state": "completed_unverified", "category": "mutation"}])
    bridge["send_chat"]("Altera isto")
    assert _wait_for(lambda: finished_count() >= 2)
    assert _wait_for(lambda: bridge["voice"].snapshot()["can_speak_last"] is False)

    # Consulta verificada sozinha volta a autorizar apenas o template.
    bridge["controller"].result = TaskResult(
        status="SUCCESS", scope="end_to_end", message="Conteúdo lido.",
        evidence=[{"verified": True, "operation": "read", "read_only": True}],
        operation_states=[{"state": "completed_verified", "category": "read_only"}])
    bridge["send_chat"]("Lê o ficheiro")
    assert _wait_for(lambda: finished_count() >= 3)
    assert _wait_for(lambda: bridge["voice"].snapshot()["can_speak_last"] is True)

    # Prova verificada ao lado de uma operação incerta: o turno inteiro falha fechado.
    bridge["controller"].result = TaskResult(
        status="SUCCESS", scope="end_to_end", message="Resumo não comprovado.",
        evidence=[{"verified": True, "operation": "read", "read_only": True}],
        operation_states=[{"state": "completed_verified", "category": "read_only"},
                          {"state": "uncertain", "category": "mutation"}])
    bridge["send_chat"]("Tenta de novo")
    assert _wait_for(lambda: finished_count() >= 4)
    assert _wait_for(lambda: bridge["voice"].snapshot()["can_speak_last"] is False)
    assert bridge["voice"].worker_pid is None


def test_voice_state_events_never_carry_the_reply_text(bridge) -> None:
    bridge["voice"].set(enabled=True, auto_speak=True)
    bridge["send_chat"]("Conteúdo privado do utilizador")
    assert _wait_for(lambda: any(
        kind == "task.finished" for kind, _ in bridge["events"]))
    for kind, payload in bridge["events"]:
        if kind == "voice.state":
            assert set(payload) == EVENT_KEYS
            assert "Conteúdo privado" not in json.dumps(payload, ensure_ascii=False)


def test_voice_stop_is_accepted_while_speech_is_in_flight(bridge) -> None:
    bridge["voice"].set(enabled=True)
    bridge["voice"].test()
    ack = bridge["dispatch"]("voice.stop")
    assert ack["accepted"] is True
    assert ack["state"] in {"ready", "error", "disabled"}


def test_service_close_terminates_the_voice_worker(bridge) -> None:
    bridge["voice"].set(enabled=True)
    bridge["voice"].test()
    assert _wait_for(lambda: bridge["voice"].worker_pid is not None)
    bridge["service"].close()
    assert bridge["voice"].worker_pid is None

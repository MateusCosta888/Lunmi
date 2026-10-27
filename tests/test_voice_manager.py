"""VOICE V1 — gestor de voz com worker falso: geração, stop, prefs, EOF e privacidade.

O worker falso é um processo real (`sys.executable`) para exercitar supervisão, EOF e kill de
árvore. Nenhum destes testes abre dispositivo de áudio, carrega modelos ou usa microfone.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import psutil
import pytest

from app.voice.manager import (
    MESSAGE_UNAVAILABLE,
    MESSAGE_WORKER_LOST,
    VoiceEnvironment,
    VoiceManager,
    VoicePrefsError,
)
from app.voice.prefs import prefs_path

EVENT_KEYS = {
    "session_id", "enabled", "auto_speak", "available", "state", "generation",
    "can_speak_last", "capture_supported", "message",
}


def _worker_script(tmp_path: Path, *, delay: float = 0.05, name: str = "fake_worker.py") -> Path:
    path = tmp_path / name
    path.write_text(
        "import json, sys, time\n"
        "for line in sys.stdin:\n"
        "    try:\n"
        "        data = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    kind = data.get('type')\n"
        "    if kind == 'speak':\n"
        "        generation = data.get('generation')\n"
        "        def emit(payload):\n"
        "            payload['generation'] = generation\n"
        "            sys.stdout.write(json.dumps(payload) + '\\n')\n"
        "            sys.stdout.flush()\n"
        "        emit({'type': 'state', 'state': 'synthesizing'})\n"
        f"        time.sleep({delay!r})\n"
        "        emit({'type': 'state', 'state': 'playing'})\n"
        "        time.sleep(0.05)\n"
        "        emit({'type': 'done'})\n"
        "        emit({'type': 'ready'})\n"
        "    elif kind == 'stop':\n"
        "        continue\n"
        "    elif kind == 'shutdown':\n"
        "        break\n",
        encoding="utf-8",
    )
    return path


def _script_with_body(tmp_path: Path, body: str, name: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def make_manager(tmp_path: Path, worker: Path, *, available: bool = True,
                 emits: list[dict[str, Any]] | None = None) -> tuple[VoiceManager, list[dict[str, Any]]]:
    events = emits if emits is not None else []
    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    env = VoiceEnvironment(
        home=tmp_path,
        python=Path(sys.executable),
        models=models,
        worker=worker,
        reason="" if available else "runtime_missing",
    )
    manager = VoiceManager(
        emit=events.append,
        root_dir=tmp_path / "root",
        state_root=tmp_path / "root",
        session_id="sess-test",
        environment=env,
    )
    return manager, events


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def states(events: list[dict[str, Any]]) -> list[str]:
    return [item["state"] for item in events]


def test_defaults_are_disabled_and_spawn_nothing(tmp_path: Path) -> None:
    manager, events = make_manager(tmp_path, _worker_script(tmp_path))
    snapshot = manager.snapshot()
    assert snapshot == {
        "enabled": False,
        "auto_speak": False,
        "available": True,
        "state": "disabled",
        "generation": 0,
        "can_speak_last": False,
        "capture_supported": False,
        "message": "",
    }
    assert manager.worker_pid is None
    assert not prefs_path(tmp_path / "root").exists()
    assert events == []
    manager.close()


def test_set_persists_atomic_prefs_and_reloads(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path, _worker_script(tmp_path))
    dto = manager.set(enabled=True, auto_speak=True)
    assert dto["enabled"] is True and dto["auto_speak"] is True
    assert dto["state"] == "ready"

    path = prefs_path(tmp_path / "root")
    assert path == tmp_path / "root" / ".local" / "voice-prefs.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"version": 1, "enabled": True, "auto_speak": True}
    assert list(path.parent.glob(".*tmp")) == []

    reopened, _ = make_manager(tmp_path, _worker_script(tmp_path))
    assert reopened.snapshot()["enabled"] is True
    assert reopened.snapshot()["auto_speak"] is True
    manager.close()
    reopened.close()


def test_prefs_write_failure_is_typed_and_leaves_state_unchanged(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path, _worker_script(tmp_path))
    target = prefs_path(tmp_path / "root")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()  # replace sobre um diretório falha de forma determinística
    with pytest.raises(VoicePrefsError):
        manager.set(enabled=True)
    assert manager.snapshot()["enabled"] is False
    assert manager.snapshot()["state"] == "disabled"
    manager.close()


def test_unavailable_runtime_is_honest_and_never_spawns(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path, _worker_script(tmp_path), available=False)
    manager.set(enabled=True)
    snapshot = manager.snapshot()
    assert snapshot["available"] is False
    assert snapshot["state"] == "error"
    assert snapshot["message"] == MESSAGE_UNAVAILABLE
    ack = manager.test()
    assert ack["accepted"] is False
    assert ack["reason"] == "unavailable"
    assert manager.worker_pid is None
    manager.close()


def test_test_speaks_and_emits_state_transitions(tmp_path: Path) -> None:
    manager, events = make_manager(tmp_path, _worker_script(tmp_path))
    manager.set(enabled=True)
    ack = manager.test()
    assert ack["accepted"] is True
    assert ack["generation"] == 1
    assert ack["state"] == "synthesizing"
    assert ack["reason"] == ""

    assert wait_for(lambda: "playing" in states(events))
    assert wait_for(lambda: manager.snapshot()["state"] == "ready")
    assert wait_for(lambda: manager.worker_pid is not None)
    manager.close()


def test_stop_invalidates_a_slow_generation_without_waiting(tmp_path: Path) -> None:
    manager, events = make_manager(tmp_path, _worker_script(tmp_path, delay=0.7, name="slow.py"))
    manager.set(enabled=True)
    first = manager.test()
    assert first["accepted"] is True

    started = time.monotonic()
    ack = manager.stop()
    elapsed = time.monotonic() - started
    assert elapsed < 0.5  # não espera a síntese
    assert ack["accepted"] is True
    assert ack["generation"] > first["generation"]
    assert ack["state"] == "ready"

    time.sleep(0.9)
    stale = [item for item in events
             if item["generation"] == first["generation"] and item["state"] == "playing"]
    assert stale == []  # resultado tardio da geração antiga foi descartado
    assert manager.snapshot()["state"] == "ready"
    manager.close()


def test_observe_reply_only_keeps_the_last_eligible_answer(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path, _worker_script(tmp_path))
    manager.set(enabled=True)
    manager.observe_reply("Resposta elegível.")
    assert manager.snapshot()["can_speak_last"] is True
    assert manager.worker_pid is None  # auto desligado: nada é sintetizado

    manager.observe_reply("")  # resposta inelegível limpa a anterior
    assert manager.snapshot()["can_speak_last"] is False
    ack = manager.speak_last()
    assert ack["accepted"] is False
    assert ack["reason"] == "no_last_response"
    manager.close()


def test_speak_last_speaks_the_stored_backend_text(tmp_path: Path) -> None:
    manager, events = make_manager(tmp_path, _worker_script(tmp_path))
    manager.set(enabled=True)
    manager.observe_reply("Resposta final verificada.")
    ack = manager.speak_last()
    assert ack["accepted"] is True
    assert wait_for(lambda: "playing" in states(events))
    manager.close()


def test_auto_speak_requires_both_flags(tmp_path: Path) -> None:
    manager, events = make_manager(tmp_path, _worker_script(tmp_path))
    manager.set(enabled=False, auto_speak=True)
    manager.observe_reply("Sem voz ligada.")
    assert manager.worker_pid is None

    manager.set(enabled=True)
    manager.observe_reply("Agora sim.")
    assert wait_for(lambda: "playing" in states(events))
    manager.close()


def test_disable_stops_audio_and_clears_last_text(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path, _worker_script(tmp_path, delay=0.4, name="slow2.py"))
    manager.set(enabled=True, auto_speak=True)
    manager.observe_reply("Frase que estava a tocar.")
    assert wait_for(lambda: manager.worker_pid is not None)

    dto = manager.set(enabled=False)
    assert dto["state"] == "disabled"
    assert dto["can_speak_last"] is False
    assert manager.worker_pid is None
    ack = manager.speak_last()
    assert ack["accepted"] is False
    assert ack["reason"] == "disabled"
    manager.close()


def test_worker_crash_is_reported_and_the_next_request_recovers(tmp_path: Path) -> None:
    marker = tmp_path / "started.marker"
    crash_once = _script_with_body(
        tmp_path,
        "import os, sys\n"
        f"marker = {str(marker)!r}\n"
        "if not os.path.exists(marker):\n"
        "    open(marker, 'w').close()\n"
        "    sys.exit(3)\n"
        "import json, time\n"
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
        "    elif data.get('type') == 'shutdown':\n"
        "        break\n",
        "crash_once.py",
    )
    manager, events = make_manager(tmp_path, crash_once)
    manager.set(enabled=True)
    manager.test()
    assert wait_for(lambda: manager.snapshot()["state"] == "error")
    assert manager.snapshot()["message"] == MESSAGE_WORKER_LOST
    assert wait_for(lambda: manager.worker_pid is None)

    ack = manager.test()
    assert ack["accepted"] is True
    assert wait_for(lambda: "playing" in states(events))
    manager.close()


def test_model_unavailable_kills_the_worker_and_reports(tmp_path: Path) -> None:
    script = _script_with_body(
        tmp_path,
        "import json, sys, time\n"
        "for line in sys.stdin:\n"
        "    try:\n"
        "        data = json.loads(line)\n"
        "    except Exception:\n"
        "        continue\n"
        "    if data.get('type') == 'speak':\n"
        "        print(json.dumps({'type': 'error',\n"
        "                          'generation': data.get('generation'),\n"
        "                          'code': 'model_unavailable'}), flush=True)\n"
        "        time.sleep(5)\n"
        "    elif data.get('type') == 'shutdown':\n"
        "        break\n",
        "no_model.py",
    )
    manager, events = make_manager(tmp_path, script)
    manager.set(enabled=True)
    manager.test()
    assert wait_for(lambda: manager.snapshot()["state"] == "error")
    assert manager.snapshot()["message"] == "Modelo de voz indisponível."
    assert wait_for(lambda: manager.worker_pid is None)
    manager.close()


def test_worker_environment_excludes_provider_secrets(tmp_path: Path) -> None:
    manager, _ = make_manager(tmp_path, _worker_script(tmp_path))
    secret_vars = {
        "LLM_API_KEY": "sk-secret",
        "OPENAI_API_KEY": "sk-secret",
        "LUNMI_STATE_ROOT": "C:\\outro",
        "LUNMI_VOICE_HOME": "C:\\outro",
        "COMPOSIO_API_KEY": "secret",
    }
    with pytest.MonkeyPatch.context() as patch:
        for key, value in secret_vars.items():
            patch.setenv(key, value)
        env = manager._worker_environment()
    for key, value in secret_vars.items():
        assert env.get(key) != value
    assert not any(key.startswith("LLM_") for key in env)
    manager.close()


def test_events_never_carry_speech_text_and_close_kills_the_worker(tmp_path: Path) -> None:
    manager, events = make_manager(tmp_path, _worker_script(tmp_path, delay=0.3, name="slow3.py"))
    manager.set(enabled=True)
    manager.observe_reply("Texto privado que nunca deve sair em eventos.")
    manager.test()
    assert wait_for(lambda: manager.worker_pid is not None)
    pid = manager.worker_pid
    assert pid is not None

    manager.close()
    assert manager.worker_pid is None
    assert wait_for(lambda: not psutil.pid_exists(pid), timeout=3.0)
    for event in events:
        assert set(event) == EVENT_KEYS
        assert "Texto privado" not in json.dumps(event, ensure_ascii=False)
    assert os.path.exists(prefs_path(tmp_path / "root"))


WARMUP_WORKER = """\
import json, sys
for line in sys.stdin:
    try:
        data = json.loads(line)
    except Exception:
        continue
    kind = data.get("type")
    if kind == "warmup":
        print(json.dumps({"type": "warmup_done"}), flush=True)
    elif kind == "shutdown":
        break
"""

SILENT_WARMUP_WORKER = """\
import json, sys
for line in sys.stdin:
    try:
        json.loads(line)
    except Exception:
        pass
"""


def test_warmup_loads_tts_without_audio_or_state_events(tmp_path: Path) -> None:
    worker = _script_with_body(tmp_path, WARMUP_WORKER, "warmup_worker.py")
    manager, events = make_manager(tmp_path, worker)
    assert manager.warm_up(timeout=2.0) == ("ready", "")
    assert manager.worker_pid is not None
    assert events == []  # warmup não publica voz nem muda enabled/auto_speak
    assert manager.snapshot()["state"] == "disabled"
    manager.close()


def test_warmup_timeout_is_finite_and_terminates_the_worker(tmp_path: Path) -> None:
    worker = _script_with_body(tmp_path, SILENT_WARMUP_WORKER, "silent_warmup.py")
    manager, events = make_manager(tmp_path, worker)
    state, message = manager.warm_up(timeout=0.15)
    assert state == "error"
    assert "demorou" in message
    assert manager.worker_pid is None
    assert events == []
    manager.close()


def test_warmup_unavailable_runtime_never_spawns(tmp_path: Path) -> None:
    worker = _script_with_body(tmp_path, WARMUP_WORKER, "warmup_worker2.py")
    manager, _ = make_manager(tmp_path, worker, available=False)
    assert manager.warm_up(timeout=0.2) == ("unavailable", MESSAGE_UNAVAILABLE)
    assert manager.worker_pid is None
    manager.close()

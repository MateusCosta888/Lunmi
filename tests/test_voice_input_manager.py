"""VOICE INPUT V1 — manager real sobre um worker falso (Popen), sem microfone/STT reais."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from app.voice.input_manager import MAX_WORKER_LINE_BYTES, VoiceInputManager
from app.voice.input_runtime import VoiceInputEnvironment

EVENT_KEYS = {"session_id", "available", "state", "generation", "message"}


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


RESULT_WORKER = """\
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
        time.sleep(0.05)
        print(json.dumps({"type": "state", "generation": generation,
                          "state": "transcribing"}), flush=True)
        print(json.dumps({"type": "result", "generation": generation,
                          "text": "Pesquisa alpha", "truncated": False}), flush=True)
    elif kind == "cancel":
        print(json.dumps({"type": "stopped", "generation": generation}), flush=True)
    elif kind == "shutdown":
        break
"""

SLOW_WORKER = """\
import json, sys
for line in sys.stdin:
    try:
        data = json.loads(line)
    except Exception:
        continue
    kind = data.get("type")
    if kind == "start":
        print(json.dumps({"type": "state", "generation": data.get("generation"),
                          "state": "listening"}), flush=True)
    elif kind == "cancel":
        print(json.dumps({"type": "stopped", "generation": data.get("generation")}),
              flush=True)
    elif kind == "shutdown":
        break
"""

TRANSCRIBING_WORKER = """\
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
        time.sleep(0.05)
        print(json.dumps({"type": "state", "generation": generation,
                          "state": "transcribing"}), flush=True)
        time.sleep(1.5)
        print(json.dumps({"type": "result", "generation": generation,
                          "text": "texto tardio", "truncated": False}), flush=True)
    elif kind == "cancel":
        time.sleep(0.5)
        print(json.dumps({"type": "stopped", "generation": generation}), flush=True)
    elif kind == "shutdown":
        break
"""

BLOCKED_CANCEL_WORKER = """\
import json, sys, threading, time
from pathlib import Path
marker = Path(__file__).with_name("late-write.txt")
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
    elif kind == "cancel":
        def late():
            time.sleep(0.6)
            marker.write_text("late", encoding="utf-8")
        threading.Thread(target=late, daemon=True).start()
    elif kind == "shutdown":
        break
"""

START_ERROR_WORKER = """\
import json, sys
print(json.dumps({"type": "error", "code": "model_unavailable"}), flush=True)
sys.stdin.read()
"""

SILENT_START_WORKER = """\
import json, sys
for line in sys.stdin:
    try:
        json.loads(line)
    except Exception:
        pass
"""

CRASH_WORKER = """\
import json, sys
for line in sys.stdin:
    try:
        data = json.loads(line)
    except Exception:
        continue
    if data.get("type") == "start":
        print(json.dumps({"type": "state", "generation": data.get("generation"),
                          "state": "listening"}), flush=True)
        break
"""


def _script(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _env(tmp_path: Path, worker: Path) -> VoiceInputEnvironment:
    model = tmp_path / "model"
    model.mkdir(exist_ok=True)
    silero = tmp_path / "silero.onnx"
    silero.write_bytes(b"onnx")
    return VoiceInputEnvironment(home=tmp_path, python=Path(sys.executable), model=model,
                                 silero=silero, worker=worker)


def _manager(tmp_path: Path, worker: Path, **kwargs: Any) -> tuple[VoiceInputManager,
                                                                   list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    manager = VoiceInputManager(
        emit=events.append,
        state_root=tmp_path,
        session_id="sess-input",
        environment=_env(tmp_path, worker),
        **kwargs,
    )
    return manager, events


def _ready(manager: VoiceInputManager) -> dict[str, Any]:
    assert _wait_for(lambda: manager.snapshot()["state"] == "ready")
    return manager.snapshot()


def test_result_is_one_shot_bound_to_generation_and_never_in_events(tmp_path: Path) -> None:
    worker = _script(tmp_path, "result_worker.py", RESULT_WORKER)
    manager, events = _manager(tmp_path, worker)
    try:
        ack = manager.start()
        assert ack["accepted"] is True and ack["state"] == "starting"
        snapshot = _ready(manager)
        generation = snapshot["generation"]
        assert generation >= 1

        assert manager.result(generation - 1) == {
            "status": "none", "generation": generation, "text": "", "truncated": False}
        delivered = manager.result(generation)
        assert delivered == {"status": "ready", "generation": generation,
                             "text": "Pesquisa alpha", "truncated": False}
        assert manager.result(generation)["status"] == "none"
        assert manager.snapshot()["state"] == "idle"
        states = [event["state"] for event in events]
        assert states[:4] == ["starting", "listening", "transcribing", "ready"]
        assert states[-1] == "idle"

        for event in events:
            assert set(event) == EVENT_KEYS
            assert event["session_id"] == "sess-input"
            assert "Pesquisa alpha" not in json.dumps(event, ensure_ascii=False)
            assert "text" not in event
    finally:
        manager.close()


def test_new_start_invalidates_the_unconsumed_result(tmp_path: Path) -> None:
    worker = _script(tmp_path, "result_worker.py", RESULT_WORKER)
    manager, _events = _manager(tmp_path, worker)
    try:
        manager.start()
        first = _ready(manager)["generation"]
        manager.start()
        assert manager.result(first)["status"] == "none"
        second = _ready(manager)["generation"]
        assert second > first
        assert manager.result(second)["status"] == "ready"
    finally:
        manager.close()


def test_cancel_waits_for_stopped_and_discards_without_result(tmp_path: Path) -> None:
    worker = _script(tmp_path, "slow_worker.py", SLOW_WORKER)
    manager, events = _manager(tmp_path, worker)
    try:
        ack = manager.start()
        generation = ack["generation"]
        assert _wait_for(lambda: manager.snapshot()["state"] == "listening")
        started = time.monotonic()
        cancelled = manager.cancel()
        elapsed = time.monotonic() - started
        assert cancelled["accepted"] is True and cancelled["state"] == "idle"
        assert elapsed < 2.0
        assert manager.result(generation)["status"] == "none"
        assert all(set(event) == EVENT_KEYS for event in events)
        assert "Pesquisa alpha" not in json.dumps(events, ensure_ascii=False)
        assert manager.snapshot()["state"] == "idle"
    finally:
        manager.close()


def test_cancel_during_stt_kills_the_worker_and_discards_the_result(tmp_path: Path) -> None:
    worker = _script(tmp_path, "transcribing_worker.py", TRANSCRIBING_WORKER)
    manager, events = _manager(tmp_path, worker)
    try:
        ack = manager.start()
        assert _wait_for(lambda: manager.snapshot()["state"] == "transcribing")
        started = time.monotonic()
        cancelled = manager.cancel()
        elapsed = time.monotonic() - started
        assert cancelled["accepted"] is True and cancelled["state"] == "idle"
        assert elapsed < 2.0
        assert manager.worker_pid is None, "STT nativo não pode continuar após cancelar"
        assert manager.result(ack["generation"])["status"] == "none"
        assert "texto tardio" not in json.dumps(events, ensure_ascii=False)
    finally:
        manager.close()


def test_cancel_without_capture_is_not_active(tmp_path: Path) -> None:
    worker = _script(tmp_path, "result_worker.py", RESULT_WORKER)
    manager, _events = _manager(tmp_path, worker)
    try:
        ack = manager.cancel()
        assert ack == {"accepted": False, "generation": 0, "state": "idle",
                       "reason": "not_active"}
    finally:
        manager.close()


def test_start_interrupts_tts_before_spawn_and_closes_the_gate(tmp_path: Path) -> None:
    worker = _script(tmp_path, "result_worker.py", RESULT_WORKER)
    active: list[bool] = []
    interrupted: list[int | None] = []
    manager = VoiceInputManager(
        emit=lambda payload: None, state_root=tmp_path, session_id="sess-input",
        environment=_env(tmp_path, worker),
        on_speech_interrupt=lambda: interrupted.append(manager.worker_pid),
        on_active_changed=active.append,
    )
    try:
        manager.start()
        assert interrupted == [None], "fala tem de parar antes de abrir o microfone"
        assert active[:1] == [True]
        _ready(manager)
        assert _wait_for(lambda: active and active[-1] is False)
        assert "Pesquisa alpha" not in json.dumps(active)
    finally:
        manager.close()


def test_start_timeout_kills_the_worker_without_transcribing(tmp_path: Path) -> None:
    worker = _script(tmp_path, "silent_worker.py", SILENT_START_WORKER)
    manager, events = _manager(tmp_path, worker, start_timeout_s=0.25)
    try:
        manager.start()
        assert _wait_for(lambda: manager.snapshot()["state"] == "error")
        snapshot = manager.snapshot()
        assert "não arrancou" in snapshot["message"]
        assert manager.worker_pid is None
        assert all(set(event) == EVENT_KEYS for event in events)
        assert "Pesquisa alpha" not in json.dumps(events, ensure_ascii=False)
        assert manager.result(snapshot["generation"])["status"] == "none"
    finally:
        manager.close()


def test_prepare_timeout_is_finite_and_kills_worker_without_state_event(
        tmp_path: Path) -> None:
    worker = _script(tmp_path, "silent_worker.py", SILENT_START_WORKER)
    manager, events = _manager(tmp_path, worker)
    try:
        state, message = manager.prepare(0.25)
        assert state == "error" and "preparação" in message
        assert manager.worker_pid is None
        assert events == []
        assert manager.snapshot()["state"] == "idle"
    finally:
        manager.close()


def test_worker_crash_invalidates_generation_and_reports_error(tmp_path: Path) -> None:
    worker = _script(tmp_path, "crash_worker.py", CRASH_WORKER)
    manager, events = _manager(tmp_path, worker)
    try:
        ack = manager.start()
        assert _wait_for(lambda: manager.snapshot()["state"] == "error")
        snapshot = manager.snapshot()
        assert snapshot["message"] == "Motor de ditado encerrou inesperadamente."
        assert snapshot["generation"] > ack["generation"]
        assert manager.result(ack["generation"])["status"] == "none"
        assert all(set(event) == EVENT_KEYS for event in events)
        assert "Pesquisa alpha" not in json.dumps(events, ensure_ascii=False)
    finally:
        manager.close()


def test_startup_error_is_reported_and_process_is_gone(tmp_path: Path) -> None:
    worker = _script(tmp_path, "error_worker.py", START_ERROR_WORKER)
    manager, _events = _manager(tmp_path, worker)
    try:
        manager.start()
        assert _wait_for(lambda: manager.snapshot()["state"] == "error")
        assert manager.snapshot()["message"] == "Modelo de ditado indisponível."
        assert manager.worker_pid is None
    finally:
        manager.close()


def test_cancel_timeout_kills_the_owned_tree_before_a_late_write(tmp_path: Path) -> None:
    worker = _script(tmp_path, "blocked_worker.py", BLOCKED_CANCEL_WORKER)
    manager, _events = _manager(tmp_path, worker, cancel_timeout_s=0.2)
    try:
        manager.start()
        assert _wait_for(lambda: manager.snapshot()["state"] == "listening")
        started = time.monotonic()
        cancelled = manager.cancel()
        elapsed = time.monotonic() - started
        assert cancelled["accepted"] is True
        assert elapsed < 1.5
        assert manager.worker_pid is None
        time.sleep(0.7)
        assert not (tmp_path / "late-write.txt").exists(), \
            "cancel/close cannot return while the owned child can still write"
    finally:
        manager.close()


def test_close_terminates_the_worker_before_returning(tmp_path: Path) -> None:
    worker = _script(tmp_path, "slow_worker.py", SLOW_WORKER)
    manager, _events = _manager(tmp_path, worker)
    manager.start()
    assert _wait_for(lambda: manager.snapshot()["state"] == "listening")
    pid = manager.worker_pid
    assert pid is not None
    manager.close()
    assert manager.worker_pid is None
    assert _wait_for(lambda: not _pid_alive(pid), timeout=3.0)


def test_unavailable_runtime_never_spawns_a_process(tmp_path: Path) -> None:
    worker = _script(tmp_path, "result_worker.py", RESULT_WORKER)
    env = VoiceInputEnvironment(home=tmp_path, python=Path(sys.executable),
                                model=tmp_path / "model", silero=tmp_path / "vad.onnx",
                                worker=worker, reason="runtime_missing")
    events: list[dict[str, Any]] = []
    manager = VoiceInputManager(emit=events.append, state_root=tmp_path, environment=env)
    try:
        ack = manager.start()
        assert ack["accepted"] is False and ack["reason"] == "unavailable"
        assert manager.snapshot()["state"] == "unavailable"
        assert manager.worker_pid is None
        assert events == []
    finally:
        manager.close()


@pytest.mark.parametrize("newline", [False, True])
def test_oversized_worker_line_terminates_owned_process_before_error(
        tmp_path: Path, newline: bool) -> None:
    suffix = 'b"\\n"' if newline else 'b"xxxxxxxxxx"'
    body = (
        "import json, sys, time\n"
        "data = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'type': 'state', 'generation': data['generation'], "
        "'state': 'listening'}), flush=True)\n"
        f"sys.stdout.buffer.write(b'x' * {MAX_WORKER_LINE_BYTES} + {suffix})\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    worker = _script(tmp_path, f"oversized_{newline}.py", body)
    processes: list[Any] = []

    def popen(*args: Any, **kwargs: Any) -> Any:
        proc = subprocess.Popen(*args, **kwargs)
        processes.append(proc)
        return proc

    manager, events = _manager(tmp_path, worker, popen=popen)
    try:
        assert manager.start()["accepted"] is True
        assert _wait_for(lambda: manager.snapshot()["state"] == "error")
        proc = processes[0]
        assert proc.poll() is not None, "error visible while owned process is alive"
        assert manager.worker_pid is None
        assert manager.snapshot()["message"] == "Motor de ditado encerrou inesperadamente."
        manager.close()
        assert proc.poll() is not None
        assert not _pid_alive(proc.pid)
        assert events[-1]["state"] == "error"
    finally:
        manager.close()
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)


def test_stdout_eof_with_live_worker_terminates_before_error(tmp_path: Path) -> None:
    # O Windows não entrega EOF só por o filho fechar o fd 1; a stream do proxy simula o
    # protocolo a fechar stdout enquanto o processo real continua vivo.
    worker = _script(tmp_path, "eof_alive.py", "import time\ntime.sleep(30)\n")
    processes: list[Any] = []

    class _EofStream:
        def __init__(self) -> None:
            self._line = (b'{"type":"state","generation":1,"state":"listening"}\n')
            self._sent = False

        def readline(self, _limit: int = -1) -> bytes:
            if self._sent:
                return b""
            self._sent = True
            return self._line

    class _Proxy:
        def __init__(self, proc: Any) -> None:
            self._proc = proc
            self.stdout = _EofStream()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._proc, name)

    def popen(*args: Any, **kwargs: Any) -> Any:
        proc = subprocess.Popen(*args, **kwargs)
        processes.append(proc)
        return _Proxy(proc)

    manager, _events = _manager(tmp_path, worker, popen=popen)
    try:
        assert manager.start()["accepted"] is True
        assert _wait_for(lambda: manager.snapshot()["state"] == "error")
        proc = processes[0]
        assert proc.poll() is not None, "EOF with live child published error first"
        assert manager.worker_pid is None
        manager.close()
        assert proc.poll() is not None
    finally:
        manager.close()
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)


def _pid_alive(pid: int) -> bool:
    import psutil

    try:
        return psutil.Process(pid).is_running()
    except (psutil.Error, OSError):
        return False


HOLD_WORKER = """\
import json, sys, time
from pathlib import Path
marker = Path(__file__).with_name("hold-events.txt")
def note(text):
    with marker.open("a", encoding="utf-8") as handle:
        handle.write(text + "\\n")
print(json.dumps({"type": "ready"}), flush=True)
for line in sys.stdin:
    try:
        data = json.loads(line)
    except Exception:
        continue
    kind = data.get("type")
    generation = data.get("generation")
    if kind == "start":
        note("start")
        print(json.dumps({"type": "state", "generation": generation,
                          "state": "listening"}), flush=True)
    elif kind == "finish":
        note("finish")
        print(json.dumps({"type": "state", "generation": generation,
                          "state": "transcribing"}), flush=True)
        time.sleep(0.05)
        print(json.dumps({"type": "result", "generation": generation,
                          "text": "Pesquisa alpha", "truncated": False,
                          "reason": "release"}), flush=True)
    elif kind == "cancel":
        note("cancel")
        print(json.dumps({"type": "stopped", "generation": generation}), flush=True)
    elif kind == "shutdown":
        break
"""

EARLY_HOLD_WORKER = """\
import json, sys, time
from pathlib import Path
marker = Path(__file__).with_name("early-events.txt")
def note(text):
    with marker.open("a", encoding="utf-8") as handle:
        handle.write(text + "\\n")
time.sleep(0.4)
print(json.dumps({"type": "ready"}), flush=True)
for line in sys.stdin:
    try:
        data = json.loads(line)
    except Exception:
        continue
    kind = data.get("type")
    if kind == "start":
        note("start")
        print(json.dumps({"type": "state", "generation": data.get("generation"),
                          "state": "listening"}), flush=True)
    elif kind == "shutdown":
        break
"""

CAP_HOLD_WORKER = """\
import json, sys
print(json.dumps({"type": "ready"}), flush=True)
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
        print(json.dumps({"type": "state", "generation": generation,
                          "state": "transcribing"}), flush=True)
        print(json.dumps({"type": "result", "generation": generation,
                          "text": "Texto no teto", "truncated": False,
                          "reason": "max_capture"}), flush=True)
    elif kind == "shutdown":
        break
"""


def test_hold_finish_is_idempotent_and_auto_sends_only_on_release(tmp_path: Path) -> None:
    worker = _script(tmp_path, "hold_worker.py", HOLD_WORKER)
    manager, events = _manager(tmp_path, worker)
    try:
        assert manager.prepare(2.0) == ("ready", "")
        ack = manager.start(mode="hold")
        assert ack["accepted"] is True and ack["state"] == "starting"
        generation = ack["generation"]
        assert _wait_for(lambda: manager.snapshot()["state"] == "listening")
        assert manager.finish(generation)["accepted"] is True
        assert manager.finish(generation)["accepted"] is True
        assert _wait_for(lambda: manager.snapshot()["state"] == "ready")
        delivered = manager.result(generation)
        assert delivered == {"status": "ready", "generation": generation,
                             "text": "Pesquisa alpha", "truncated": False,
                             "auto_send": True}
        marker = (tmp_path / "hold-events.txt").read_text(encoding="utf-8")
        assert marker.splitlines() == ["start", "finish"]
        assert all(set(event) == EVENT_KEYS for event in events)
    finally:
        manager.close()


def test_hold_finish_during_starting_never_opens_the_microphone(tmp_path: Path) -> None:
    worker = _script(tmp_path, "early_worker.py", EARLY_HOLD_WORKER)
    manager, events = _manager(tmp_path, worker)
    try:
        ack = manager.start(mode="hold")
        generation = ack["generation"]
        assert ack["accepted"] is True and ack["state"] == "starting"
        assert manager.finish(generation)["accepted"] is True
        assert manager.snapshot()["state"] == "idle"
        assert _wait_for(lambda: manager.snapshot()["message"] != "", timeout=2.0)
        time.sleep(0.6)
        assert manager.result(generation)["status"] == "none"
        assert not (tmp_path / "early-events.txt").exists()
        assert all(set(event) == EVENT_KEYS for event in events)
    finally:
        manager.close()


def test_hold_manual_turns_reject_finish_and_cap_cancels_auto_send(tmp_path: Path) -> None:
    worker = _script(tmp_path, "hold_worker.py", HOLD_WORKER)
    manager, _events = _manager(tmp_path, worker)
    try:
        assert manager.prepare(2.0) == ("ready", "")
        manual = manager.start()
        assert _wait_for(lambda: manager.snapshot()["state"] == "listening")
        refused = manager.finish(manual["generation"])
        assert refused["accepted"] is False and refused["reason"] == "not_active"
        manager.cancel()
    finally:
        manager.close()

    cap_worker = _script(tmp_path, "cap_worker.py", CAP_HOLD_WORKER)
    cap_manager, _events = _manager(tmp_path, cap_worker)
    try:
        assert cap_manager.prepare(2.0) == ("ready", "")
        ack = cap_manager.start(mode="hold")
        generation = ack["generation"]
        assert _wait_for(lambda: cap_manager.snapshot()["state"] == "ready")
        delivered = cap_manager.result(generation)
        assert delivered["auto_send"] is False
        assert delivered["text"] == "Texto no teto"
        assert cap_manager.finish(generation)["accepted"] is False
    finally:
        cap_manager.close()

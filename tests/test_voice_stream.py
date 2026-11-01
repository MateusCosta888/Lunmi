"""Speaking a plain answer while it is written (01/11): policy, response hook and manager.

No test opens an audio device or loads a model: the worker is a real process that only logs.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from app.bridge.responses import PublicResponse
from app.voice.eligibility import SpeechStream, lead_text, speakable_reply
from app.voice.filter import MAX_SPEECH_CHARS
from app.voice.manager import VoiceEnvironment, VoiceManager

SUMMARY = "A capital da Austrália é Camberra."
DETAILS = " ".join(["Foi escolhida como meio-termo entre Sydney e Melbourne."] * 14)


class FakeVoice:
    def __init__(self, ready: bool = True) -> None:
        self.ready = ready
        self.spoken: list[str] = []
        self.closed: list[str] = []

    def stream_ready(self) -> bool:
        return self.ready

    def stream_speak(self, text: str) -> bool:
        self.spoken.append(text)
        return True

    def stream_close(self, text: str) -> None:
        self.closed.append(text)


def test_a_long_answer_speaks_its_first_paragraph_and_a_short_one_all_of_it() -> None:
    long_answer = SUMMARY + "\n\n" + DETAILS
    assert len(long_answer) > MAX_SPEECH_CHARS
    assert lead_text(long_answer) == SUMMARY
    assert lead_text("Olá.\n\nTudo bem.") == "Olá.\n\nTudo bem."
    assert speakable_reply(message=long_answer, status="SUCCESS", scope="conversation") == SUMMARY


def test_the_stream_speaks_the_summary_then_stops_once_the_answer_is_long() -> None:
    voice = FakeVoice()
    stream = SpeechStream(voice, attachments=False)
    answer = SUMMARY + " "
    stream.stable(SUMMARY + " ", 0, answer)
    answer += "\n\n" + DETAILS
    stream.stable("\n\n" + DETAILS, len(SUMMARY) + 1, answer)
    assert voice.spoken == [SUMMARY]
    assert stream.finish(SUMMARY) is True and voice.closed == [SUMMARY]


def test_a_short_answer_streams_every_paragraph() -> None:
    voice = FakeVoice()
    stream = SpeechStream(voice, attachments=False)
    stream.stable("Olá! ", 0, "Olá! ")
    stream.stable("\n\nTudo pronto.", 5, "Olá! \n\nTudo pronto.")
    assert voice.spoken == ["Olá!", "Tudo pronto."]


def test_operations_attachments_and_disabled_voice_never_stream() -> None:
    voice = FakeVoice()
    stream = SpeechStream(voice, attachments=False)
    stream.stable("A abrir. ", 0, "A abrir. ")
    stream.cancel()
    stream.stable("Mais texto. ", 9, "A abrir. Mais texto. ")
    assert voice.spoken == ["A abrir."] and voice.closed == [""]  # silenced at the operation
    for blocked in (SpeechStream(FakeVoice(), attachments=True), SpeechStream(FakeVoice(ready=False), attachments=False)):
        blocked.stable("Olá. ", 0, "Olá. ")
        assert blocked.voice.spoken == [] and blocked.finish("Olá.") is False


def test_the_public_response_feeds_speech_and_an_operation_cancels_it() -> None:
    voice = FakeVoice()
    response = PublicResponse(lambda kind, data: None, "s", "t")
    response.speech = SpeechStream(voice, attachments=False)
    response.finish("Feito. Até já.", "SUCCESS")
    assert voice.spoken == ["Feito. Até já."]
    other = FakeVoice()
    response = PublicResponse(lambda kind, data: None, "s", "t")
    response.speech = SpeechStream(other, attachments=False)
    response.speech.started = True
    response.receive("operation.started", {"operation_id": "op", "state": "started"})
    assert other.closed == [""] and response.speech.active is False


def _logging_worker(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "received.jsonl"
    worker = tmp_path / "logging_worker.py"
    worker.write_text(
        "import sys\n"
        f"log = open({str(log)!r}, 'a', encoding='utf-8')\n"
        "for line in sys.stdin:\n"
        "    log.write(line); log.flush()\n", encoding="utf-8")
    return worker, log


def test_the_manager_opens_feeds_and_closes_one_streamed_utterance(tmp_path: Path) -> None:
    worker, log = _logging_worker(tmp_path)
    (tmp_path / "models").mkdir()
    manager = VoiceManager(emit=lambda event: None, root_dir=tmp_path / "root", state_root=tmp_path / "root",
                           session_id="s", environment=VoiceEnvironment(
                               home=tmp_path, python=Path(sys.executable), models=tmp_path / "models", worker=worker))
    try:
        assert manager.stream_ready() is False and manager.stream_speak("Olá.") is False  # voice off
        manager.set(enabled=True, auto_speak=True)
        assert manager.stream_ready() is True
        assert manager.stream_speak("Primeira.") and manager.stream_speak("Segunda.")
        manager.stream_close("Primeira. Segunda.")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (not log.exists() or log.read_text(encoding="utf-8").count("\n") < 4):
            time.sleep(0.02)
        sent = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        generation = sent[1]["generation"]
        assert sent == [{"type": "stop"},
                        {"type": "stream", "generation": generation, "text": "Primeira.", "final": False},
                        {"type": "stream", "generation": generation, "text": "Segunda.", "final": False},
                        {"type": "stream", "generation": generation, "text": "", "final": True}]
        assert manager.snapshot()["can_speak_last"] is True
    finally:
        manager.close()


def test_a_streamed_answer_is_closed_not_spoken_again_at_the_end() -> None:
    """Measured 01/11: the bridge cleared its response before deciding speech, so a streamed
    answer was spoken a second time and the restart delayed the voice by ~1.5 s."""
    from types import SimpleNamespace

    from app.bridge.service import BridgeService
    from app.core.controller import TaskResult

    voice = FakeVoice()
    voice.observed = []  # type: ignore[attr-defined]
    voice.observe_reply = voice.observed.append  # type: ignore[attr-defined]
    stream = SpeechStream(voice, attachments=False)
    stream.stable("Olá. ", 0, "Olá. ")
    owner = SimpleNamespace(voice=voice, _public_response=None)
    result = TaskResult(task_id="t", status="SUCCESS", message="Olá.", scope="conversation")
    BridgeService._observe_voice(owner, result, attachments=False, verified_prefix="", speech=stream)  # type: ignore[arg-type]
    assert voice.closed == ["Olá."] and voice.observed == []  # type: ignore[attr-defined]


def test_a_streamed_answer_reaches_the_worker_through_the_real_backend_path(tmp_path: Path) -> None:
    """Real path, only the audio process is fake: response -> SpeechStream -> VoiceManager ->
    worker pipe. The parts were tested alone; this is how they are wired in the bridge."""
    worker, log = _logging_worker(tmp_path)
    (tmp_path / "models").mkdir()
    manager = VoiceManager(emit=lambda event: None, root_dir=tmp_path / "root", state_root=tmp_path / "root",
                           session_id="s", environment=VoiceEnvironment(
                               home=tmp_path, python=Path(sys.executable), models=tmp_path / "models", worker=worker))
    try:
        manager.set(enabled=True, auto_speak=True)
        response = PublicResponse(lambda kind, data: None, "s", "t")
        response.speech = SpeechStream(manager, attachments=False)
        answer = SUMMARY + "\n\n" + DETAILS
        response.finish(answer, "SUCCESS")
        eligible = speakable_reply(message=answer, status="SUCCESS", scope="conversation")
        assert response.speech.finish(eligible) is True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (not log.exists() or log.read_text(encoding="utf-8").count("\n") < 3):
            time.sleep(0.02)
        sent = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        spoken = [item["text"] for item in sent if item["type"] == "stream" and item["text"]]
        assert spoken == [SUMMARY]  # the long details are never read aloud
        assert sent[-1]["final"] is True and manager.snapshot()["can_speak_last"] is True
    finally:
        manager.close()

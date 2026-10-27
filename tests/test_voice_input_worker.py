"""VOICE INPUT V1 — worker externo: endpoint, DSP, silêncio, cancelamento e resultado.

Nenhum teste abre microfone/modelo reais: stream, VAD e STT são falsos e o áudio é sintético.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

import numpy as np

from app.voice.input_worker import (
    EndpointDetector,
    FrameBuffer,
    HighPass70,
    InputWorker,
    StreamResampler,
    clean_transcript,
    prepare_for_stt,
    rms_dbfs,
)


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class _Vad:
    """Probabilidade 0.9 quando o frame tem energia; reset por turno."""

    def __init__(self, threshold: float = 0.01) -> None:
        self.threshold = threshold
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def probability(self, frame: Any) -> float:
        return 0.9 if float(np.sqrt(np.mean(np.square(frame)))) >= self.threshold else 0.0


class _Model:
    def __init__(self, text: str = "Pesquisa alpha no Spotify") -> None:
        self.text = text
        self.calls: list[Any] = []

    def transcribe(self, samples: Any) -> str:
        self.calls.append(np.asarray(samples).copy())
        return self.text


class _Stream:
    """Stream 48 kHz falso: blocos de fala/silêncio, abort fecha e read falha."""

    def __init__(self, blocks: list[np.ndarray], *, endless_silence: bool = False) -> None:
        self._blocks = deque(blocks)
        self._endless = endless_silence
        self.aborted = False
        self.closed = False
        self.closed_at: float | None = None
        self.reads = 0

    def read(self, frames: int) -> tuple[np.ndarray, bool]:
        if self.aborted or self.closed:
            raise RuntimeError("stream closed")
        if self._blocks:
            self.reads += 1
            return np.asarray(self._blocks.popleft(), dtype=np.float32), False
        if self._endless:
            self.reads += 1
            return np.zeros(frames, dtype=np.float32), False
        time.sleep(0.01)
        self.reads += 1
        return np.zeros(frames, dtype=np.float32), False

    def abort(self) -> None:
        self.aborted = True

    def close(self) -> None:
        self.closed = True
        self.closed_at = time.monotonic()


def _tone(seconds: float, amplitude: float = 0.2, frequency: float = 220.0,
          rate: int = 48_000) -> np.ndarray:
    t = np.arange(int(seconds * rate), dtype=np.float32) / rate
    return (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.float32)


def _silence(seconds: float, rate: int = 48_000) -> np.ndarray:
    return np.zeros(int(seconds * rate), dtype=np.float32)


def _blocks(*parts: np.ndarray, block: int = 4_800) -> list[np.ndarray]:
    audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    return [audio[i:i + block] for i in range(0, audio.size, block)]


# --------------------------------------------------------------------- endpoint


def test_endpoint_rejects_silence_without_transcribing() -> None:
    detector = EndpointDetector(onset_timeout_ms=500, max_capture_ms=5_000,
                                min_speech_ms=64, endpoint_silence_ms=50)
    steps = [detector.push(0.0) for _ in range(30)]
    assert steps[-1].finished is True
    assert steps[-1].reason == "onset_timeout"
    assert detector.triggered is False


def test_endpoint_waits_for_min_speech_and_then_one_second_of_silence() -> None:
    detector = EndpointDetector(onset_timeout_ms=5_000, max_capture_ms=30_000,
                                min_speech_ms=250, endpoint_silence_ms=1_000)
    triggered = False
    finished = None
    for _ in range(30):
        step = detector.push(0.9)
        triggered = triggered or step.triggered_now
        assert step.finished is False
    assert triggered is True
    for _ in range(40):
        step = detector.push(0.0)
        if step.finished:
            finished = step
            break
    assert finished is not None and finished.reason == "endpoint"


def test_endpoint_caps_capture_even_while_speech_continues() -> None:
    detector = EndpointDetector(onset_timeout_ms=5_000, max_capture_ms=320,
                                min_speech_ms=32, endpoint_silence_ms=1_000)
    finished = None
    for _ in range(20):
        step = detector.push(0.9)
        if step.finished:
            finished = step
            break
    assert finished is not None and finished.reason == "max_capture"


# ------------------------------------------------------------------------- DSP


def test_frame_buffer_preserves_samples_across_odd_blocks() -> None:
    frames = FrameBuffer(512)
    output = []
    output.extend(frames.push(np.arange(300, dtype=np.float32)))
    output.extend(frames.push(np.arange(300, 900, dtype=np.float32)))
    assert len(output) == 1
    assert output[0].size == 512
    output.extend(frames.push(np.arange(900, 1_024, dtype=np.float32)))
    assert len(output) == 2
    assert output[1].tolist() == list(range(512, 1_024))


def test_resampler_halves_nothing_and_preserves_a_1khz_tone() -> None:
    resampler = StreamResampler(48_000, 16_000)
    source = _tone(0.5, frequency=1_000)
    out = np.concatenate([resampler.process(source[:4_800]),
                          resampler.process(source[4_800:])])
    assert abs(out.size - source.size // 3) <= 1
    assert 0.12 < float(np.sqrt(np.mean(np.square(out)))) <= 0.2
    crossings = np.count_nonzero(np.diff(np.signbit(out)))
    assert 950 <= crossings / (out.size / 16_000) / 2 <= 1_050


def test_highpass_attenuates_20hz_and_keeps_1khz() -> None:
    low = HighPass70()
    low_out = low.process(_tone(1.0, frequency=20.0, amplitude=0.5))
    high = HighPass70()
    high_out = high.process(_tone(1.0, frequency=1_000.0, amplitude=0.5))
    assert rms_dbfs(low_out) < rms_dbfs(high_out) - 10.0


def test_gain_is_bounded_and_silence_is_never_amplified() -> None:
    quiet = (_tone(0.5, amplitude=0.01)).astype(np.float32)
    boosted, gain_db = prepare_for_stt(quiet)
    assert 0.0 <= gain_db <= 6.0
    assert float(np.max(np.abs(boosted))) <= 0.89
    assert rms_dbfs(boosted) < -20.0

    loud = (_tone(0.5, amplitude=0.9)).astype(np.float32)
    limited, _ = prepare_for_stt(loud)
    assert float(np.max(np.abs(limited))) <= 0.89 + 1e-6

    silence = np.zeros(16_000, dtype=np.float32)
    unchanged, gain = prepare_for_stt(silence)
    assert gain == 0.0 and np.array_equal(unchanged, silence)


def test_transcript_is_normalised_and_truncated_on_a_word_boundary() -> None:
    text, truncated = clean_transcript("  Olá   mundo \n disto  ", max_chars=100)
    assert text == "Olá mundo disto" and truncated is False
    long = "palavra " * 400
    cut, truncated = clean_transcript(long, max_chars=40)
    assert truncated is True
    assert len(cut) <= 40 and not cut.endswith(" ")


# ---------------------------------------------------------------------- turnos


def _worker(tmp_path: Path, *, blocks: list[np.ndarray], text: str = "Pesquisa alpha",
            endless: bool = False, onset_timeout_ms: int = 1_500,
            endpoint_silence_ms: int = 300, max_capture_ms: int = 5_000
            ) -> tuple[InputWorker, list[dict[str, Any]], _Stream, _Model]:
    events: list[dict[str, Any]] = []
    stream = _Stream(blocks, endless_silence=endless)
    model = _Model(text)
    vad = _Vad()
    worker = InputWorker(
        sample_rate=48_000, target_rate=16_000,
        onset_timeout_ms=onset_timeout_ms, endpoint_silence_ms=endpoint_silence_ms,
        min_speech_ms=64, max_capture_ms=max_capture_ms,
        emit=events.append,
        load_model=lambda: model,
        load_vad=lambda: vad,
        open_stream=lambda *_args: stream,
    )
    worker.prepare()
    return worker, events, stream, model


def test_turn_emits_result_only_after_microphone_is_closed(tmp_path: Path) -> None:
    blocks = _blocks(_silence(0.2), _tone(0.4), _silence(0.6))
    worker, events, stream, model = _worker(tmp_path, blocks=blocks)
    worker.start(7)
    assert worker.wait_idle(5) is True
    assert stream.closed is True
    kinds = [event["type"] for event in events]
    assert "result" in kinds and "no_speech" not in kinds
    result = next(event for event in events if event["type"] == "result")
    assert result == {"type": "result", "generation": 7,
                      "text": "Pesquisa alpha", "truncated": False}
    assert model.calls and model.calls[0].size
    states = [event.get("state") for event in events if event["type"] == "state"]
    assert states[:2] == ["listening", "transcribing"]
    transcribing = next(index for index, event in enumerate(events)
                        if event["type"] == "state" and event["state"] == "transcribing")
    stopped = next(index for index, event in enumerate(events)
                   if event["type"] == "stopped")
    assert stopped < transcribing, "mic must be closed before STT starts"
    assert kinds.count("result") == 1


def test_silence_only_turn_never_reaches_stt(tmp_path: Path) -> None:
    worker, events, stream, model = _worker(
        tmp_path, blocks=_blocks(_silence(1.0)), onset_timeout_ms=200)
    worker.start(3)
    assert worker.wait_idle(5) is True
    assert stream.closed is True
    assert model.calls == []
    assert [event["type"] for event in events].count("no_speech") == 1
    assert all(event["type"] != "result" for event in events)


def test_max_capture_is_bounded_and_still_transcribes(tmp_path: Path) -> None:
    worker, events, _stream, model = _worker(
        tmp_path, blocks=_blocks(_tone(1.2)), max_capture_ms=400)
    worker.start(5)
    assert worker.wait_idle(5) is True
    assert any(event["type"] == "result" for event in events)
    assert model.calls
    assert model.calls[0].size <= int(16_000 * 0.85), \
        "captured audio must stay within the published ceiling"


def test_cancel_discards_capture_without_transcribing(tmp_path: Path) -> None:
    worker, events, stream, model = _worker(tmp_path, blocks=[], endless=True)
    worker.start(11)
    assert _wait_for(lambda: any(event["type"] == "state" for event in events))
    worker.cancel(11)
    assert worker.wait_idle(5) is True
    assert stream.aborted is True and stream.closed is True
    assert model.calls == []
    assert any(event["type"] == "stopped" for event in events)
    assert all(event["type"] != "result" for event in events)


def test_empty_or_oversized_transcript_becomes_no_speech_or_truncated(tmp_path: Path) -> None:
    blocks = _blocks(_silence(0.2), _tone(0.4), _silence(0.6))
    worker, events, _stream, _model = _worker(tmp_path, blocks=blocks, text="   ")
    worker.start(1)
    assert worker.wait_idle(5) is True
    assert any(event["type"] == "no_speech" for event in events)

    long_text = "palavra " * 400
    worker2, events2, _s2, _m2 = _worker(tmp_path, blocks=blocks, text=long_text)
    worker2.start(2)
    assert worker2.wait_idle(5) is True
    result = next(event for event in events2 if event["type"] == "result")
    assert result["truncated"] is True
    assert len(result["text"]) <= 2_000


def test_unprepared_worker_fails_closed_without_opening_microphone(tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []
    opened: list[bool] = []
    worker = InputWorker(
        sample_rate=48_000, target_rate=16_000, emit=events.append,
        open_stream=lambda *_args: opened.append(True),
    )
    worker.start(1)
    assert worker.wait_idle(5) is True
    assert opened == []
    assert any(event.get("code") == "audio_failed" for event in events)


def test_hold_ignores_silence_and_finishes_only_on_release(tmp_path: Path) -> None:
    blocks = _blocks(_silence(0.2), _tone(0.4), _silence(1.2))
    worker, events, stream, model = _worker(
        tmp_path, blocks=blocks, endpoint_silence_ms=100, max_capture_ms=5_000)
    worker.start(9, "hold")
    assert _wait_for(lambda: any(event.get("state") == "listening" for event in events))
    time.sleep(0.2)
    worker.finish(9)
    assert worker.wait_idle(5) is True
    assert stream.closed is True
    result = next(event for event in events if event["type"] == "result")
    assert result["reason"] == "release"
    assert model.calls


def test_hold_finish_before_speech_never_transcribes_or_reports_release(
        tmp_path: Path) -> None:
    worker, events, stream, model = _worker(tmp_path, blocks=[])
    worker.start(12, "hold")
    worker.finish(12)
    assert worker.wait_idle(5) is True
    assert stream.closed is True
    assert model.calls == []
    assert all(event["type"] != "result" for event in events)
    assert any(event["type"] == "no_speech" for event in events)


def test_hold_finish_while_the_microphone_is_opening_never_emits_listening(
        tmp_path: Path) -> None:
    events: list[dict[str, Any]] = []
    opened: list[bool] = []
    gate = threading.Event()

    def open_stream(*_args: Any) -> _Stream:
        opened.append(True)
        gate.wait(2.0)
        return _Stream([], endless_silence=True)

    worker = InputWorker(
        sample_rate=48_000, target_rate=16_000, emit=events.append,
        load_model=lambda: _Model(), load_vad=lambda: _Vad(), open_stream=open_stream,
    )
    worker.prepare()
    worker.start(8, "hold")
    assert _wait_for(lambda: bool(opened))
    worker.finish(8)
    gate.set()
    assert worker.wait_idle(5) is True
    assert all(event.get("state") != "listening" for event in events)
    assert all(event["type"] != "result" for event in events)
    assert any(event["type"] == "no_speech" for event in events)


def test_hold_silence_still_hits_the_finite_capture_ceiling() -> None:
    detector = EndpointDetector(onset_timeout_ms=0, endpoint_silence_ms=0,
                                min_speech_ms=64, max_capture_ms=320)
    finished = None
    for _ in range(20):
        step = detector.push(0.0)
        if step.finished:
            finished = step
            break
    assert finished is not None and finished.reason == "max_capture"

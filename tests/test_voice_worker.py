"""VOICE V1 — worker externo: protocolo JSONL, chunking, cancelamento e falhas sanitizadas.

Nenhum teste abre dispositivo de áudio nem carrega Supertonic: o motor e a reprodução são falsos.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np
import pytest

from app.voice.worker_main import (
    DEFAULT_SAMPLE_RATE,
    Cancelled,
    VoiceWorker,
    chunk_text,
    parse_args,
)


class _FakePlayback:
    def __init__(self) -> None:
        self.writes: list[Any] = []
        self.begun = 0
        self.stopped = 0
        self.drained = 0

    def begin(self) -> None:
        self.begun += 1

    def write(self, samples: Any) -> None:
        self.writes.append(samples)

    def stop(self) -> None:
        self.stopped += 1

    def drain(self) -> None:
        self.drained += 1

    def close(self) -> None:
        self.stop()


class _FakeEngine:
    sample_rate = DEFAULT_SAMPLE_RATE

    def __init__(self, hook=None) -> None:
        self.texts: list[str] = []
        self.hook = hook

    def synthesize(self, text, style, **kwargs):
        del style, kwargs
        self.texts.append(text)
        if self.hook is not None:
            self.hook()
        return np.zeros(int(DEFAULT_SAMPLE_RATE * 0.02), dtype=np.float32), 0.02


def render(worker: VoiceWorker, generation: int, text: str) -> None:
    worker._active_generation = generation
    worker._render(generation, text)


class _NativeStream:
    """Modela o contrato do sounddevice real: abort não fecha; close anula o ponteiro."""

    def __init__(self, log: list[str], *, block_until_abort: bool = True,
                 abort_delay_s: float = 0.0, ignore_abort: bool = False,
                 block_timeout_s: float = 2.0) -> None:
        self.log = log
        self.block_until_abort = block_until_abort
        self.abort_delay_s = abort_delay_s
        self.ignore_abort = ignore_abort
        self.block_timeout_s = block_timeout_s
        self.release = threading.Event()
        self.write_active = False
        self.closed = False
        self.ptr = "stream"
        self.written: list[int] = []

    def start(self) -> None:
        self.log.append("start")

    def write(self, data: Any) -> None:
        if self.ptr is None:
            self.log.append("WRITE_AFTER_CLOSE")
        self.write_active = True
        self.written.append(len(data))
        self.log.append("write-enter")
        try:
            if self.block_until_abort:
                self.release.wait(self.block_timeout_s)
            elif self.abort_delay_s:
                self.release.wait(self.abort_delay_s)
            if self.closed:
                self.log.append("WRITE_AFTER_CLOSE")
        finally:
            self.write_active = False
            self.log.append("write-leave")

    def abort(self) -> None:
        self.log.append("abort")
        if self.closed:
            self.log.append("ABORT_AFTER_CLOSE")
        if self.ignore_abort:
            return
        if self.abort_delay_s:
            threading.Timer(self.abort_delay_s, self.release.set).start()
        else:
            self.release.set()

    def close(self) -> None:
        self.log.append("close")
        if self.write_active:
            self.log.append("CLOSE_DURING_WRITE")
        if self.ptr is None:
            self.log.append("DOUBLE_CLOSE")
        self.closed = True
        self.ptr = None


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return False


def _playback_with_streams(tmp_path, *, block_ms: int = 50,
                           **stream_kwargs) -> tuple[Any, list[str], list[_NativeStream]]:
    from app.voice.worker_main import Playback

    log: list[str] = []
    streams: list[_NativeStream] = []
    playback = Playback(sample_rate=8_000, block_ms=block_ms)

    def open_stream() -> _NativeStream:
        stream = _NativeStream(log, **stream_kwargs)
        streams.append(stream)
        playback._stream = stream
        return stream

    playback._open_locked = open_stream  # type: ignore[method-assign]
    return playback, log, streams


def test_stop_never_closes_while_the_native_write_is_in_flight() -> None:
    playback, log, streams = _playback_with_streams(None)
    samples = np.zeros(8_000, dtype=np.float32)  # 1 s / blocos de 50 ms
    playback.begin()
    writer = threading.Thread(target=lambda: _ignore_cancel(playback, samples), daemon=True)
    writer.start()
    assert _wait_until(lambda: streams and streams[0].write_active)

    started = time.perf_counter()
    playback.stop()
    stop_ms = (time.perf_counter() - started) * 1000.0
    writer.join(timeout=3.0)

    assert "CLOSE_DURING_WRITE" not in log
    assert "WRITE_AFTER_CLOSE" not in log
    assert "DOUBLE_CLOSE" not in log
    assert log.index("abort") < log.index("close")
    assert log.index("write-leave") < log.index("close")
    assert stop_ms < 1_000.0
    assert streams[0].closed is True


def test_close_never_frees_a_parked_stream_while_the_write_is_persistent() -> None:
    playback, log, streams = _playback_with_streams(None, block_ms=50, ignore_abort=True,
                                                    block_timeout_s=10.0)
    samples = np.zeros(800, dtype=np.float32)
    playback.begin()
    writer = threading.Thread(target=lambda: _ignore_cancel(playback, samples), daemon=True)
    writer.start()
    assert _wait_until(lambda: streams and streams[0].write_active)

    started = time.monotonic()
    playback.stop()  # write não volta após abort: fica estacionado, não fechado
    assert time.monotonic() - started < 2.0
    playback.close()

    assert "close" not in log
    assert streams[0].closed is False

    # O write nativo finalmente volta; agora fechar é seguro e obrigatório.
    streams[0].release.set()
    writer.join(timeout=3.0)
    assert "CLOSE_DURING_WRITE" not in log
    playback.close()
    assert streams[0].closed is True
    assert "CLOSE_DURING_WRITE" not in log
    assert "WRITE_AFTER_CLOSE" not in log


def test_stop_waits_for_a_slow_abort_before_closing() -> None:
    playback, log, streams = _playback_with_streams(None, abort_delay_s=0.15)
    samples = np.zeros(1_600, dtype=np.float32)
    playback.begin()
    writer = threading.Thread(target=lambda: _ignore_cancel(playback, samples), daemon=True)
    writer.start()
    assert _wait_until(lambda: streams and streams[0].write_active)

    playback.stop()
    writer.join(timeout=3.0)

    assert "CLOSE_DURING_WRITE" not in log
    assert log.index("write-leave") < log.index("close")


def test_new_utterance_after_stop_opens_a_fresh_stream() -> None:
    playback, log, streams = _playback_with_streams(None, block_ms=100)
    first = np.zeros(1_600, dtype=np.float32)
    playback.begin()
    writer = threading.Thread(target=lambda: _ignore_cancel(playback, first), daemon=True)
    writer.start()
    assert _wait_until(lambda: streams and streams[0].write_active)
    playback.stop()
    writer.join(timeout=3.0)
    assert streams[0].closed is True

    # Segunda fala (geração nova): stream novo, sem reutilizar o fechado.
    with playback._lock:
        playback._stream = None
    playback._open_locked = lambda: _new_stream(playback, streams, log)  # type: ignore[method-assign]
    playback.begin()
    playback.write(np.zeros(1_600, dtype=np.float32))

    assert len(streams) == 2
    assert streams[1].closed is False
    assert streams[1].written == [800, 800]
    assert "WRITE_AFTER_CLOSE" not in log
    assert "CLOSE_DURING_WRITE" not in log


def _new_stream(playback, streams: list[_NativeStream], log: list[str]) -> _NativeStream:
    stream = _NativeStream(log, block_until_abort=False)
    streams.append(stream)
    playback._stream = stream
    return stream


def _ignore_cancel(playback, samples) -> None:
    try:
        playback.write(samples)
    except Cancelled:
        pass


def test_stop_before_the_first_block_writes_nothing() -> None:
    playback, log, streams = _playback_with_streams(None)
    playback.begin()
    playback.stop()
    with pytest.raises(Cancelled):
        playback.write(np.zeros(800, dtype=np.float32))
    assert streams == []
    assert "write-enter" not in log


def test_write_is_split_into_blocks_bounded_in_milliseconds() -> None:
    playback, _log, streams = _playback_with_streams(None, block_ms=100,
                                                     block_until_abort=False)
    playback.begin()
    playback.write(np.zeros(8_000, dtype=np.float32))  # 1 s a 8 kHz
    assert streams[0].written == [800] * 10
    assert max(streams[0].written) / 8_000 * 1000.0 <= 150.0


def make_worker(tmp_path, engine=None) -> tuple[VoiceWorker, list[dict], _FakePlayback]:
    worker = VoiceWorker(model_dir=str(tmp_path))
    events: list[dict] = []
    worker.emit = events.append
    worker.playback = _FakePlayback()
    if engine is not None:
        worker._engine = engine
        worker._voice_style = "style"
    return worker, events, worker.playback


def test_chunk_text_groups_sentences_and_respects_bounds() -> None:
    assert chunk_text("Olá. Tudo bem? Sim!") == ["Olá. Tudo bem? Sim!"]
    long_sentence = "palavra " * 100
    chunks = chunk_text(long_sentence, max_chars=120, max_chunks=3)
    assert len(chunks) == 3
    assert all(0 < len(chunk) <= 120 for chunk in chunks)
    assert chunk_text("   ") == []
    assert chunk_text("a. b. c.", max_chunks=2) == ["a. b. c."]
    text = "Uma frase curta. Outra frase clara. Mais uma frase."
    grouped = chunk_text(text, max_chars=35, max_chunks=2)
    assert " ".join(grouped) == text
    assert len(grouped) <= 2 and all(len(chunk) <= 35 for chunk in grouped)


def test_parse_args_requires_a_model_dir_and_keeps_pinned_defaults(tmp_path) -> None:
    args = parse_args(["--model-dir", str(tmp_path)])
    assert args.model_dir == str(tmp_path)
    assert args.sample_rate == DEFAULT_SAMPLE_RATE
    assert args.voice == "F4"
    assert args.total_steps == 10
    assert args.speed == pytest.approx(1.0)
    assert args.lang == "pt"
    with pytest.raises(SystemExit):
        parse_args([])


def test_handle_command_validates_speak_and_recognizes_stop_and_shutdown(tmp_path) -> None:
    worker, _events, _playback = make_worker(tmp_path)
    assert worker.handle_command({"type": "speak", "generation": 1, "text": "Olá."}) is True
    assert worker.handle_command({"type": "speak", "generation": "1", "text": "x"}) is True
    assert worker.handle_command({"type": "speak", "generation": 3, "text": "  "}) is True
    assert worker._pending == (1, "Olá.")
    assert worker.handle_command({"type": "stop"}) is True
    assert worker._cancelled(1) is True
    assert worker.handle_command({"type": "shutdown"}) is False
    assert worker.handle_command("não é objeto") is True


def test_render_speaks_chunks_and_emits_playing_then_done(tmp_path) -> None:
    engine = _FakeEngine()
    worker, events, playback = make_worker(tmp_path, engine)
    first = "Primeira frase " + "longa " * 25 + "termina."
    second = "Segunda frase " + "longa " * 25 + "termina."
    render(worker, 4, first + " " + second)

    assert engine.texts == [first, second]
    assert playback.begun == 1
    assert playback.drained == 1
    assert len(playback.writes) == 2
    assert events == [
        {"type": "state", "generation": 4, "state": "synthesizing"},
        {"type": "state", "generation": 4, "state": "playing"},
        {"type": "state", "generation": 4, "state": "synthesizing"},
        {"type": "done", "generation": 4},
        {"type": "ready", "generation": 4},
    ]
    assert all("text" not in event for event in events)


def test_short_sentences_render_with_one_prosody_context(tmp_path) -> None:
    engine = _FakeEngine()
    worker, events, playback = make_worker(tmp_path, engine)
    text = "Primeira frase. Segunda frase."
    render(worker, 5, text)
    assert engine.texts == [text]
    assert len(playback.writes) == 1
    assert events[-2:] == [{"type": "done", "generation": 5},
                           {"type": "ready", "generation": 5}]


def test_playback_drain_waits_for_reported_latency_and_is_stop_aware() -> None:
    from app.voice.worker_main import Playback

    class Stream(_NativeStream):
        @property
        def latency(self) -> float:
            entered.set()
            return 0.05

    entered = threading.Event()
    playback = Playback()
    stream = Stream([], block_until_abort=False)
    playback._stream = stream
    started = time.monotonic()
    playback.drain()
    assert time.monotonic() - started >= 0.05
    assert not stream.closed

    entered.clear()
    outcomes: list[str] = []

    def drain() -> None:
        try:
            playback.drain()
        except Cancelled:
            outcomes.append("cancelled")

    thread = threading.Thread(target=drain)
    thread.start()
    assert entered.wait(0.5)
    playback.stop()
    thread.join(0.5)
    assert not thread.is_alive() and outcomes == ["cancelled"]


def test_stop_during_drain_never_announces_done_or_ready(tmp_path) -> None:
    worker, events, playback = make_worker(tmp_path, _FakeEngine())
    playback.drain = worker.stop
    render(worker, 6, "Uma frase completa.")
    assert len(playback.writes) == 1
    assert not any(event["type"] in {"done", "ready"} for event in events)


def test_stop_during_synthesis_prevents_playback_and_done(tmp_path) -> None:
    worker_holder: dict[str, VoiceWorker] = {}

    def hook() -> None:
        worker_holder["worker"].stop()

    engine = _FakeEngine(hook=hook)
    worker, events, playback = make_worker(tmp_path, engine)
    worker_holder["worker"] = worker
    render(worker, 2, "Frase que será cancelada.")
    assert playback.writes == []
    assert {"type": "done", "generation": 2} not in events
    assert worker._cancelled(2) is True


def test_model_failure_is_a_sanitized_code_and_never_the_exception(tmp_path) -> None:
    worker, events, _playback = make_worker(tmp_path)

    def boom() -> None:
        raise RuntimeError("c:\\caminho\\secreto do modelo")

    worker._ensure_engine = boom  # type: ignore[method-assign]
    render(worker, 7, "Olá.")
    assert events == [{"type": "state", "generation": 7, "state": "loading"},
                      {"type": "error", "generation": 7, "code": "model_unavailable"}]


def test_preload_failure_is_a_sanitized_startup_error(tmp_path, capsys) -> None:
    from app.voice import worker_main

    def broken(self) -> bool:
        return False

    original = worker_main.VoiceWorker.preload
    worker_main.VoiceWorker.preload = broken  # type: ignore[method-assign]
    try:
        rc = worker_main.main(["--model-dir", str(tmp_path)])
    finally:
        worker_main.VoiceWorker.preload = original  # type: ignore[method-assign]
    assert rc == 1
    out = capsys.readouterr().out.strip()
    assert out == '{"type":"error","code":"model_unavailable"}'
    assert "generation" not in out


def test_synthesis_failure_is_sanitized(tmp_path) -> None:
    class _Broken(_FakeEngine):
        def synthesize(self, text, style, **kwargs):
            raise RuntimeError("ort falhou com C:\\Users\\dono")

    worker, events, playback = make_worker(tmp_path, _Broken())
    render(worker, 5, "Olá.")
    assert events[-1] == {"type": "error", "generation": 5, "code": "synthesis_failed"}
    assert playback.writes == []
    assert "Users" not in repr(events)


def test_warmup_synthesizes_fixed_text_without_playback() -> None:
    from app.voice.worker_main import WARMUP_TEXT

    worker = VoiceWorker(model_dir="missing")
    engine = _FakeEngine()
    worker._engine = engine
    worker.playback = _FakePlayback()
    events: list[dict[str, Any]] = []
    worker.emit = events.append  # type: ignore[method-assign]
    worker.warm_up()
    assert engine.texts == [WARMUP_TEXT]
    assert len(WARMUP_TEXT) <= 32
    assert worker.playback.writes == []  # nada chega ao dispositivo de saída
    assert events == [{"type": "warmup_done"}]
    worker.warm_up()
    assert engine.texts == [WARMUP_TEXT]  # idempotente, sem segunda inferência
    assert events == [{"type": "warmup_done"}, {"type": "warmup_done"}]


def test_warmup_failure_is_sanitized_and_does_not_touch_playback() -> None:
    worker = VoiceWorker(model_dir="missing")
    worker.playback = _FakePlayback()
    events: list[dict[str, Any]] = []
    worker.emit = events.append  # type: ignore[method-assign]

    def fail() -> Any:
        raise RuntimeError("driver detail")

    worker._ensure_engine = fail  # type: ignore[method-assign]
    worker.warm_up()
    assert events == [{"type": "warmup_failed", "code": "model_unavailable"}]
    assert worker.playback.writes == []

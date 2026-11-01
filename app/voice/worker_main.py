"""Worker de voz da Lunmi — síntese e reprodução, fora do Python do produto.

Executado pelo runtime externo preparado pelo Root:

    <state_root>/.local/voice/runtime/Scripts/python.exe app/voice/worker_main.py \
        --model-dir <state_root>/.local/voice/models/supertonic-3

Contrato JSONL (stdin/stdout, UTF-8, uma mensagem por linha):

  entrada  {"type":"speak","generation":N,"text":"..."}
           {"type":"warmup"}
           {"type":"stop"}
           {"type":"shutdown"}
  saída    {"type":"state","generation":N,"state":"loading|synthesizing|playing"}
           {"type":"warmup_done"}
           {"type":"warmup_failed","code":"model_unavailable|synthesis_failed"}
           {"type":"ready","generation":N}
           {"type":"done","generation":N}
           {"type":"error","generation":N,"code":"model_unavailable|synthesis_failed|audio_failed"}

Regras: nada de texto, caminhos ou stack em `stdout`/`stderr`; só códigos sanitizados. `stop` aborta
o áudio e invalida a geração sem esperar a síntese. Este ficheiro só usa a stdlib no topo — numpy,
sounddevice e supertonic são importados quando há fala, para o produto poder testar os helpers sem
esses pacotes.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

MAX_TEXT_CHARS = 2_000
MAX_CHUNK_CHARS = 220
# Cada bloco de escrita nativa fica limitado a <=150 ms para que `stop` tenha uma janela curta.
MAX_WRITE_BLOCK_MS = 150
WRITE_BLOCK_MS = 100
# Se um write nativo não voltar após abort, o stream não é fechado (fechar libertaria memória em uso).
STOP_DRAIN_TIMEOUT_S = 1.0
# Enough blocks for MAX_TEXT_CHARS (6 cut long answers short).
MAX_CHUNKS = 10
# The first block is short and rendered with half the diffusion steps, so speech starts in
# ~0.5 s instead of 1.7 s (measured 01/11, F5 at 1.1x); later blocks keep full quality.
FIRST_CHUNK_CHARS = 60
# A breath between blocks: back to back they ran together, synthesized one by one they had
# ~1 s gaps (measured 01/11 with Supertonic F4).
SENTENCE_PAUSE_S = 0.12
# A streamed answer whose writer sent nothing for this long is treated as finished.
STREAM_IDLE_S = 30.0
MAX_OUTPUT_MS = 20_000
DEFAULT_SAMPLE_RATE = 44_100
DEFAULT_TOTAL_STEPS = 10
# Chosen by the user on 01/11 from recorded samples: voice F5 at 1.1x.
DEFAULT_SPEED = 1.1
DEFAULT_VOICE = "F5"
# Shipped since Alpha 5; used only when the chosen style file is missing on an older install.
FALLBACK_VOICES = ("F5", "F4")
# Texto fixo, curto e descartado: paga a primeira inferência sem play, ficheiro ou áudio audível.
WARMUP_TEXT = "Olá."

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


class Cancelled(Exception):
    """A geração atual foi interrompida por `stop`."""


def _sentences(text: str) -> list[str]:
    cleaned = " ".join((text or "").split())
    return [sentence.strip() for sentence in _SENTENCE_SPLIT.split(cleaned) if sentence.strip()]


def _block_limit(index: int, max_chars: int = MAX_CHUNK_CHARS, first_chars: int = FIRST_CHUNK_CHARS) -> int:
    # Growing blocks: a short first one starts speech, and each next one is rendered while
    # the previous plays, so it must not take longer than that one lasts.
    return min(max_chars, (first_chars, first_chars * 2)[index] if index < 2 else max_chars)


def _take_block(queue: deque[str], limit: int) -> str:
    """The next block from the sentences already available; never waits for more."""
    piece = queue.popleft()
    if len(piece) > limit:
        window = piece[:limit]
        cut = max(window.rfind(", "), window.rfind("; "), window.rfind(" "))
        if cut < limit // 3:
            cut = limit
        queue.appendleft(piece[cut:].strip())
        return piece[:cut].strip()
    while queue and len(piece) + 1 + len(queue[0]) <= limit:
        piece += " " + queue.popleft()
    return piece


def chunk_text(text: str, *, max_chars: int = MAX_CHUNK_CHARS,
               max_chunks: int = MAX_CHUNKS, first_chars: int = FIRST_CHUNK_CHARS) -> list[str]:
    """Agrupa frases para manter prosódia; respeita tamanho e número máximos de blocos."""
    queue = deque(_sentences(text))
    chunks: list[str] = []
    while queue and len(chunks) < max_chunks:
        piece = _take_block(queue, _block_limit(len(chunks), max_chars, first_chars))
        if piece:
            chunks.append(piece)
    return chunks


class _Feed:
    """The text of one utterance: whole ("speak") or sentence by sentence ("stream")."""

    def __init__(self, text: str, *, closed: bool) -> None:
        self.sentences: deque[str] = deque()
        self.closed = closed
        self.chars = 0
        self.updated = time.monotonic()
        self.add(text)

    def add(self, text: str) -> None:
        cleaned = " ".join((text or "").split())[:max(0, MAX_TEXT_CHARS - self.chars)]
        self.chars += len(cleaned)
        self.sentences.extend(_sentences(cleaned))
        self.updated = time.monotonic()


class Playback:
    """Saída de áudio mono float32 com contrato PortAudio seguro para `stop` concorrente.

    O crash nativo P1 (access violation em `Pa_WriteStream`, sounddevice `_raw_write`) vinha de
    `stop()` fechar o stream na thread principal enquanto a thread de render estava dentro do
    write nativo: `close()` faz `Pa_CloseStream` e anula `_ptr` imediatamente. Aqui:

    - o áudio é escrito em blocos de <=150 ms, para limitar a janela de paragem;
    - `stop()` só chama `abort()` (Pa_AbortStream) enquanto há write em voo — nunca `close()`;
    - `close()` só acontece depois de `_write_in_flight` voltar a falso, sob a mesma trava que
      impede um novo bloco de começar;
    - uma geração parada nunca abre um write novo (`_stopped` é verificado sob a trava);
    - um stream novo é aberto no primeiro write depois de `begin()`.

    Latência de `stop`: no máximo um bloco (`block_ms`, 100 ms por default) mais o abort.
    """

    def __init__(self, sample_rate: int = DEFAULT_SAMPLE_RATE,
                 block_ms: int = WRITE_BLOCK_MS) -> None:
        if not 20 <= int(block_ms) <= MAX_WRITE_BLOCK_MS:
            raise ValueError("block_ms must be between 20 and 150")
        self.sample_rate = int(sample_rate)
        self.block_ms = int(block_ms)
        self._lock = threading.RLock()
        self._space = threading.Condition(self._lock)
        self._stream: Any = None
        self._stopped = False
        self._write_in_flight = False
        self._current_write_stream: Any = None
        # Handles que `stop()` não pôde fechar porque o write nativo ainda estava em voo.
        # Cada entrada guarda se o write daquele stream ainda não voltou.
        self._parked: list[dict[str, Any]] = []

    def begin(self) -> None:
        with self._lock:
            self._stopped = False

    def _open_locked(self) -> Any:
        import sounddevice as sd

        try:
            # "low": the default "high" device latency added ~180 ms before the first sound.
            stream = sd.OutputStream(samplerate=self.sample_rate, channels=1, dtype="float32",
                                     latency="low")
            stream.start()
        except Exception as exc:  # nunca expor o detalhe do driver
            raise AudioUnavailable() from exc
        self._stream = stream
        return stream

    def _block_frames(self) -> int:
        return max(1, int(self.sample_rate * self.block_ms / 1000))

    def write(self, samples: Any) -> None:
        total = len(samples)
        if total == 0:
            return
        block = self._block_frames()
        for start in range(0, total, block):
            piece = samples[start:start + block]
            with self._lock:
                if self._stopped:
                    raise Cancelled()
                stream = self._stream
                if stream is None:
                    stream = self._open_locked()
                # Marcado na mesma secção crítica que entrega o stream: `stop()` não pode
                # fechar entre esta verificação e a chamada nativa.
                self._current_write_stream = stream
                self._write_in_flight = True
            try:
                stream.write(piece)
            except Exception as exc:
                with self._lock:
                    self._finish_write_locked(stream)
                    stopped = self._stopped
                if stopped:
                    raise Cancelled() from exc
                raise AudioUnavailable() from exc
            with self._lock:
                self._finish_write_locked(stream)
                stopped = self._stopped
            if stopped:
                raise Cancelled()

    def _finish_write_locked(self, stream: Any) -> None:
        self._write_in_flight = False
        if self._current_write_stream is stream:
            self._current_write_stream = None
        for entry in self._parked:
            if entry["stream"] is stream:
                entry["active"] = False
        self._space.notify_all()

    def drain(self) -> None:
        """Espera a cauda enfileirada sem tocar no handle nativo; Parar continua imediato."""
        with self._lock:
            if self._stopped:
                raise Cancelled()
            stream = self._stream
            if stream is None:
                return
            try:
                latency = float(stream.latency)
            except (AttributeError, TypeError, ValueError):
                latency = 0.1
        if not math.isfinite(latency) or latency < 0:
            latency = 0.1
        # shortcut: usa latência reportada, limitada a 1 s; revalidar drivers de alta latência.
        deadline = time.monotonic() + min(1.0, latency + 0.02)
        while True:
            with self._lock:
                if self._stopped or self._stream is not stream:
                    raise Cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.01, remaining))

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            stream = self._stream
        if stream is not None:
            try:
                # Abort interrompe o write em voo sem libertar o stream (close faria crash).
                stream.abort()
            except Exception:
                pass
        with self._lock:
            deadline = time.monotonic() + STOP_DRAIN_TIMEOUT_S
            while self._write_in_flight and time.monotonic() < deadline:
                self._space.wait(0.05)
            stream, self._stream = self._stream, None
            in_flight = self._write_in_flight
        if stream is None:
            return
        if in_flight:
            # Memória nativa ainda em uso: fechar agora repetiria o crash. O handle fica
            # estacionado e só é fechado quando o write daquele stream terminar.
            with self._lock:
                still_active = (self._current_write_stream is stream
                                and self._write_in_flight)
                self._parked.append({"stream": stream, "active": still_active})
            return
        try:
            stream.close()
        except Exception:
            pass

    def close(self) -> None:
        """Fecha o que é seguro fechar; handles com write nativo em voo ficam para o teardown.

        Um write que não voltou após o timeout fica estacionado: fechá-lo aqui repetiria o access
        violation P1 (o processo a terminar liberta os handles; não há use-after-free).
        """
        with self._lock:
            needs_stop = not self._stopped or self._stream is not None
        if needs_stop:
            self.stop()
        with self._lock:
            parked = list(self._parked)
            # Mantém os ainda activos: um `close()` posterior (ou o teardown) fecha-os quando o
            # write nativo voltar; descartar a referência aqui perderia a única oportunidade.
            self._parked = [entry for entry in parked if entry["active"]]
            closable = [entry["stream"] for entry in parked if not entry["active"]]
        for stream in closable:
            try:
                stream.close()
            except Exception:
                pass


class AudioUnavailable(RuntimeError):
    """Dispositivo de saída indisponível."""


class VoiceWorker:
    def __init__(self, *, model_dir: str, sample_rate: int = DEFAULT_SAMPLE_RATE,
                 voice: str = DEFAULT_VOICE, total_steps: int = DEFAULT_TOTAL_STEPS,
                 speed: float = DEFAULT_SPEED,
                 lang: str = "pt") -> None:
        self.model_dir = model_dir
        self.sample_rate = int(sample_rate)
        self.voice = voice
        self.total_steps = 2 if total_steps < 2 else total_steps
        self.speed = speed
        self.lang = lang
        self._engine: Any = None
        self._voice_style: Any = None
        self._engine_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._condition = threading.Condition(self._state_lock)
        self._pending: tuple[int, _Feed] | None = None
        self._feed: _Feed | None = None
        self._active_generation = 0
        self._stop_generation = -1
        self._shutdown = False
        self._warmed_up = False
        self.playback = Playback(sample_rate=self.sample_rate)
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- saída --
    def emit(self, payload: dict[str, Any]) -> None:
        try:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False,
                                        separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    def _emit_state(self, generation: int, state: str) -> None:
        self.emit({"type": "state", "generation": generation, "state": state})

    def _cancelled(self, generation: int) -> bool:
        with self._state_lock:
            return (self._shutdown or generation != self._active_generation
                    or generation <= self._stop_generation)

    # ------------------------------------------------------------- comandos --
    def submit(self, generation: int, text: str) -> None:
        with self._condition:
            self._active_generation = generation
            self._feed = _Feed(text, closed=True)
            self._pending = (generation, self._feed)
            self._condition.notify_all()

    def stream(self, generation: int, text: str, *, final: bool) -> None:
        """More sentences of the answer being written; `final` closes the utterance."""
        with self._condition:
            if generation <= self._stop_generation or generation < self._active_generation:
                return  # a stopped or superseded answer never resumes
            feed = self._feed
            if generation == self._active_generation and feed is not None and not feed.closed:
                feed.add(text)
                feed.closed = feed.closed or final
            else:
                self._active_generation = generation
                self._feed = _Feed(text, closed=final)
                self._pending = (generation, self._feed)
            self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            if self._active_generation > self._stop_generation:
                self._stop_generation = self._active_generation
            self._pending = None
            self._condition.notify_all()
        self.playback.stop()

    def shutdown(self) -> None:
        with self._condition:
            self._shutdown = True
            self._pending = None
            self._condition.notify_all()
        self.playback.close()

    # ---------------------------------------------------------------- motor --
    def preload(self) -> bool:
        """Carrega as extensões nativas na thread principal antes de bloquear no stdin.

        Medido no gates V1: no Windows, importar uma DLL de extensão numa thread secundária
        enquanto a thread principal está parada num `readline()` de stdin bloqueia o import
        até chegar outra linha. As importações ficam em cache; o modelo/pesos continuam a ser
        carregados a pedido, na thread de render, e só no processo externo.
        """
        try:
            import numpy  # noqa: F401
            import sounddevice  # noqa: F401
            import supertonic  # noqa: F401
        except Exception:
            return False
        return True

    def warm_up(self) -> None:
        """Carrega o modelo e paga a primeira inferência com texto fixo, sem reproduzir.

        A síntese descartada não abre dispositivo de saída nem guarda áudio. Repetir o pedido
        depois de pronto só confirma o estado; o motor/pesos continuam em cache.
        """
        if self._warmed_up:
            self.emit({"type": "warmup_done"})
            return
        try:
            engine = self._ensure_engine()
        except Exception:
            self.emit({"type": "warmup_failed", "code": "model_unavailable"})
            return
        try:
            samples = self._synthesize(engine, WARMUP_TEXT)
        except Exception:
            self.emit({"type": "warmup_failed", "code": "synthesis_failed"})
            return
        # As amostras vivem só nesta frame e são descartadas; nunca chegam a Playback.
        del samples
        self._warmed_up = True
        self.emit({"type": "warmup_done"})

    def _ensure_engine(self) -> Any:
        with self._engine_lock:
            if self._engine is not None:
                return self._engine
            from supertonic import TTS

            engine = TTS(model="supertonic-3", model_dir=self.model_dir,
                         auto_download=False, intra_op_num_threads=2,
                         inter_op_num_threads=1)
            if int(engine.sample_rate) != self.sample_rate:
                raise RuntimeError("unexpected sample rate")
            self._voice_style = self._style(engine)
            self._engine = engine
            return engine

    def _style(self, engine: Any) -> Any:
        """The chosen voice, or a shipped one: an install updated before F5 shipped has only F4."""
        for voice in dict.fromkeys((self.voice, *FALLBACK_VOICES)):
            try:
                return engine.get_voice_style(voice)
            except Exception:  # noqa: BLE001 - that style file is absent; try the next shipped one
                continue
        raise RuntimeError("no voice style")

    def _synthesize(self, engine: Any, text: str, steps: int | None = None) -> Any:
        import numpy as np

        wav, _duration = engine.synthesize(text, self._voice_style,
                                           total_steps=steps or self.total_steps,
                                           speed=self.speed, lang=self.lang, verbose=False)
        array = np.asarray(wav, dtype=np.float32)
        if array.ndim == 2:
            if array.shape[0] != 1:
                raise RuntimeError("unexpected waveform shape")
            array = array[0]
        if array.ndim != 1 or array.size == 0:
            raise RuntimeError("empty waveform")
        if array.size * 1000.0 / self.sample_rate > MAX_OUTPUT_MS:
            raise RuntimeError("audio too long")
        return np.ascontiguousarray(array, dtype=np.float32)

    def _next_block(self, generation: int, feed: _Feed, index: int) -> str | None:
        """Waits for the next block of a streamed answer; None when it ended or was stopped."""
        with self._condition:
            while True:
                if self._cancelled(generation) or index >= MAX_CHUNKS:
                    return None
                if feed.sentences:
                    return _take_block(feed.sentences, _block_limit(index)) or None
                if feed.closed or time.monotonic() - feed.updated > STREAM_IDLE_S:
                    return None  # the answer ended, or its writer went away
                self._condition.wait(0.1)

    def _produce(self, engine: Any, generation: int, feed: _Feed, index: int) -> Any:
        block = self._next_block(generation, feed, index)
        if block is None:
            return None
        # Half the diffusion steps on the first block only: speech starts in ~0.3-0.8 s.
        return self._synthesize(engine, block, max(2, self.total_steps // 2) if index == 0 else None)

    def _with_pause(self, samples: Any) -> Any:
        import numpy as np
        return np.concatenate([samples, np.zeros(int(self.sample_rate * SENTENCE_PAUSE_S), np.float32)])

    # ------------------------------------------------------------- reprodução --
    def _render(self, generation: int, feed: _Feed) -> None:
        if self._cancelled(generation):
            return
        if self._engine is None:
            self._emit_state(generation, "loading")
        try:
            engine = self._ensure_engine()
        except Exception:
            self.emit({"type": "error", "generation": generation, "code": "model_unavailable"})
            return
        playing = False
        # The next block renders (and, when streamed, waits for its text) while the current
        # one plays: no silence while it computes.
        synth = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lunmi-tts")
        try:
            pending = synth.submit(self._produce, engine, generation, feed, 0)
            index = 0
            while True:
                if self._cancelled(generation):
                    return
                if not playing:
                    self._emit_state(generation, "synthesizing")
                try:
                    samples = pending.result()
                except Cancelled:
                    return
                except Exception:
                    if self._cancelled(generation):
                        return
                    self.emit({"type": "error", "generation": generation,
                               "code": "synthesis_failed"})
                    return
                if samples is None:
                    break
                index += 1
                pending = synth.submit(self._produce, engine, generation, feed, index)
                samples = self._with_pause(samples)
                if self._cancelled(generation):
                    return
                if not playing:
                    playing = True
                    self.playback.begin()
                    self._emit_state(generation, "playing")
                try:
                    self.playback.write(samples)
                except Cancelled:
                    return
                except AudioUnavailable:
                    self.emit({"type": "error", "generation": generation, "code": "audio_failed"})
                    return
        finally:
            # A stop must not wait for a block nobody will hear.
            synth.shutdown(wait=False, cancel_futures=True)
        if self._cancelled(generation):
            return
        if not playing:
            self.emit({"type": "done", "generation": generation})
            return
        try:
            self.playback.drain()
        except Cancelled:
            return
        except Exception:
            if not self._cancelled(generation):
                self.emit({"type": "error", "generation": generation, "code": "audio_failed"})
            return
        if self._cancelled(generation):
            return
        self.emit({"type": "done", "generation": generation})
        self.emit({"type": "ready", "generation": generation})

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._shutdown:
                    self._condition.wait()
                if self._shutdown:
                    return
                item, self._pending = self._pending, None
            if item is None:
                continue
            self._render(*item)

    def handle_command(self, data: Any) -> bool:
        """Aplica uma mensagem já desserializada; `False` significa encerrar."""
        if not isinstance(data, dict):
            return True
        kind = data.get("type")
        if kind == "speak":
            generation = data.get("generation")
            text = data.get("text")
            if type(generation) is not int or not isinstance(text, str):
                return True
            cleaned = " ".join(text.split())[:MAX_TEXT_CHARS]
            if cleaned:
                self.submit(generation, cleaned)
            return True
        if kind == "stream":
            generation, text, final = data.get("generation"), data.get("text"), data.get("final")
            if type(generation) is not int or not isinstance(text, str) or not isinstance(final, bool):
                return True
            self.stream(generation, text, final=final)
            return True
        if kind == "warmup":
            self.warm_up()
            return True
        if kind == "stop":
            self.stop()
            return True
        if kind == "shutdown":
            return False
        return True

    def serve(self, stream: Any = None) -> int:
        source = stream if stream is not None else sys.stdin
        self._thread = threading.Thread(target=self._run, name="voice-render", daemon=True)
        self._thread.start()
        while True:
            raw = source.readline()
            if not raw:
                break
            try:
                data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8", "strict"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not self.handle_command(data):
                break
        self.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lunmi voice worker (external runtime)")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE)
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--total-steps", type=int, default=DEFAULT_TOTAL_STEPS)
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    parser.add_argument("--lang", default="pt")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    worker = VoiceWorker(model_dir=args.model_dir, sample_rate=args.sample_rate,
                         voice=args.voice, total_steps=args.total_steps,
                         speed=args.speed, lang=args.lang)
    if not worker.preload():
        # Sem geração: o gestor trata isto como falha de arranque, não como fala atrasada.
        worker.emit({"type": "error", "code": "model_unavailable"})
        return 1
    return worker.serve()


if __name__ == "__main__":
    raise SystemExit(main())

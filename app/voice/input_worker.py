"""Worker de entrada de voz (ditado) — captura, VAD e STT fora do Python do produto.

Executado **apenas** no runtime externo separado, pelo manager:

    <voice_home>/input-runtime/Scripts/python.exe app/voice/input_worker.py \
        --model-dir <voice_home>/models/whisper-large-v3-turbo \
        --vad <voice_home>/models/silero/silero_vad.onnx

Contrato: `.agents/VOICE-INPUT-V1-CONTRACT.md` §8. Protocolo JSONL (stdin/stdout, UTF-8):

  entrada  {"type":"start","generation":N,"mode":"manual|hold"}
           {"type":"finish","generation":N}
           {"type":"cancel","generation":N}
           {"type":"shutdown"}
  saída    {"type":"ready"}
           {"type":"state","generation":N,"state":"listening|transcribing"}
           {"type":"result","generation":N,"text":"...","truncated":bool}
           {"type":"no_speech","generation":N,"reason":"silence|empty_transcript"}
           {"type":"stopped","generation":N}
           {"type":"error","generation":N,"code":"model_unavailable|audio_failed|stt_failed"}

Regras: nada de texto, caminhos, modelo ou stack em `stderr`; só códigos sanitizados. O
microfone só abre em `start`, fecha antes de `transcribing`, e `cancel`/EOF/`shutdown` fecham a
captura. PCM e transcrição vivem só em RAM; nenhum WAV/temporário/histórico. O topo do módulo é
stdlib puro — numpy/scipy/onnxruntime/sounddevice/faster_whisper só são importados no processo
externo, quando há captura/modelo.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from dataclasses import dataclass
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable

INPUT_SAMPLE_RATE = 48_000
TARGET_SAMPLE_RATE = 16_000
VAD_FRAME_SAMPLES = 512
VAD_CONTEXT_SAMPLES = 64
VAD_STATE_SHAPE = (2, 1, 128)
DEFAULT_BLOCK_MS = 100
PREROLL_MS = 300
MAX_WORKER_LINE_BYTES = 64 * 1024
MAX_TRANSCRIPT_CHARS = 2_000

# Mesma ordem usada no preflight de Root (RUNTIME-PREFLIGHT.json, PASS CUDA float16).
_NVIDIA_DLLS = (
    "cuda_nvrtc/bin/nvrtc64_120_0.dll",
    "cublas/bin/cublasLt64_12.dll",
    "cublas/bin/cublas64_12.dll",
    "cuda_nvrtc/bin/nvrtc-builtins64_129.dll",
    "cudnn/bin/cudnn_ops64_9.dll",
    "cudnn/bin/cudnn_graph64_9.dll",
    "cudnn/bin/cudnn_adv64_9.dll",
    "cudnn/bin/cudnn_cnn64_9.dll",
    "cudnn/bin/cudnn_engines_runtime_compiled64_9.dll",
    "cudnn/bin/cudnn_engines_precompiled64_9.dll",
    "cudnn/bin/cudnn_heuristic64_9.dll",
    "cudnn/bin/cudnn64_9.dll",
)
_DLL_HANDLES: list[Any] = []


class AudioUnavailable(RuntimeError):
    """Microfone indisponível (nunca expõe o detalhe do driver)."""


@dataclass(frozen=True)
class EndpointStep:
    triggered_now: bool = False
    finished: bool = False
    reason: str = ""


class EndpointDetector:
    """Detetor determinístico de fala/endpoint, em frames de VAD (sem numpy).

    `reason`: `""` enquanto decorre; `"endpoint"` após silêncio suficiente; `"onset_timeout"`
    sem fala no prazo; `"max_capture"` no teto rígido desde a abertura do microfone.
    """

    def __init__(self, *, threshold: float = 0.5, min_speech_ms: int = 250,
                 endpoint_silence_ms: int = 1_000, onset_timeout_ms: int = 5_000,
                 max_capture_ms: int = 30_000, frame_ms: float = 32.0) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if min_speech_ms < 0 or endpoint_silence_ms < 0 or onset_timeout_ms < 0:
            raise ValueError("invalid endpoint timings")
        if max_capture_ms <= 0:
            raise ValueError("max_capture_ms must be positive")
        self.threshold = float(threshold)
        self.frame_ms = float(frame_ms)
        self.min_speech_frames = max(1, int(round(min_speech_ms / self.frame_ms)))
        # `None` desativa o timer: o hold só termina por release, teto ou erro.
        self.endpoint_frames = (None if endpoint_silence_ms <= 0 else
                                max(1, int(round(endpoint_silence_ms / self.frame_ms))))
        self.onset_frames = (None if onset_timeout_ms <= 0 else
                             max(1, int(round(onset_timeout_ms / self.frame_ms))))
        self.max_frames = max(1, int(round(max_capture_ms / self.frame_ms)))
        self._frames = 0
        self._speech_run = 0
        self._silence_run = 0
        self._triggered = False
        self._trigger_frame = 0

    @property
    def triggered(self) -> bool:
        return self._triggered

    def push(self, probability: float) -> EndpointStep:
        self._frames += 1
        if not self._triggered:
            if probability >= self.threshold:
                self._speech_run += 1
                if self._speech_run >= self.min_speech_frames:
                    self._triggered = True
                    self._trigger_frame = self._frames
                    self._silence_run = 0
                    return EndpointStep(triggered_now=True)
            else:
                self._speech_run = 0
            if self._frames >= self.max_frames:
                # O teto vale também antes da primeira fala (hold desativa o onset).
                return EndpointStep(finished=True, reason="max_capture")
            if self.onset_frames is not None and self._frames >= self.onset_frames:
                return EndpointStep(finished=True, reason="onset_timeout")
            return EndpointStep()

        if self._frames >= self.max_frames:
            return EndpointStep(finished=True, reason="max_capture")
        if probability >= self.threshold:
            self._silence_run = 0
        else:
            self._silence_run += 1
            if (self.endpoint_frames is not None
                    and self._silence_run >= self.endpoint_frames):
                return EndpointStep(finished=True, reason="endpoint")
        return EndpointStep()


class FrameBuffer:
    """Acumula amostras 16 kHz e devolve apenas frames completos de 512 amostras."""

    def __init__(self, frame_samples: int = VAD_FRAME_SAMPLES) -> None:
        self.frame_samples = int(frame_samples)
        self._pending: Any = None

    def push(self, block: Any) -> list[Any]:
        import numpy as np

        array = np.asarray(block, dtype=np.float32).reshape(-1)
        if self._pending is not None and self._pending.size:
            array = np.concatenate([self._pending, array])
        usable = (array.size // self.frame_samples) * self.frame_samples
        self._pending = array[usable:].copy()
        return [array[i:i + self.frame_samples].copy()
                for i in range(0, usable, self.frame_samples)]


def _design_lowpass_fir(cutoff_hz: float, sample_rate: int, taps: int) -> Any:
    """Sinc janelado (Kaiser) — anti-alias determinístico, sem scipy."""
    import numpy as np

    if taps % 2 == 0 or taps < 31:
        raise ValueError("FIR tap count must be odd and >= 31")
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2.0
    normalized = 2.0 * float(cutoff_hz) / float(sample_rate)
    kernel = normalized * np.sinc(normalized * n)
    kernel *= np.kaiser(taps, 6.0)
    return (kernel / kernel.sum()).astype(np.float32)


class _FirFilter:
    """FIR linear com estado contínuo (overlap-save por convolução)."""

    def __init__(self, coeffs: Any) -> None:
        import numpy as np

        self._coeffs = np.asarray(coeffs, dtype=np.float32)
        self._tail = np.zeros(self._coeffs.size - 1, dtype=np.float32)

    def process(self, block: Any) -> Any:
        import numpy as np

        array = np.asarray(block, dtype=np.float32).reshape(-1)
        if not array.size:
            return np.zeros(0, dtype=np.float32)
        padded = np.concatenate([self._tail, array])
        out = np.convolve(padded, self._coeffs, mode="valid")
        self._tail = padded[-(self._coeffs.size - 1):].copy()
        return np.ascontiguousarray(out, dtype=np.float32)


class StreamResampler:
    """Decimador contínuo 48 kHz -> 16 kHz com anti-alias FIR e fase preservada.

    Só aceita uma razão inteira (`source_rate % target_rate == 0`), que é o caso 48k/16k do
    contrato. Sem scipy: o produto não instala dependências de áudio.
    """

    def __init__(self, source_rate: int = INPUT_SAMPLE_RATE,
                 target_rate: int = TARGET_SAMPLE_RATE, *, taps: int = 127) -> None:
        if source_rate <= 0 or target_rate <= 0 or source_rate % target_rate:
            raise ValueError("StreamResampler requires an integer decimation ratio")
        self.down = source_rate // target_rate
        self.target_rate = target_rate
        self._phase = 0
        self._filter = _FirFilter(_design_lowpass_fir(
            target_rate * 0.45, source_rate, taps))

    def process(self, block: Any) -> Any:
        import numpy as np

        filtered = self._filter.process(block)
        if not filtered.size:
            return np.zeros(0, dtype=np.float32)
        usable = filtered.size - ((self._phase + filtered.size) % self.down)
        if usable <= 0:
            self._phase = (self._phase + filtered.size) % self.down
            return np.zeros(0, dtype=np.float32)
        start = (self.down - self._phase) % self.down
        out = filtered[start:start + usable:self.down]
        self._phase = (self._phase + filtered.size) % self.down
        return np.ascontiguousarray(out, dtype=np.float32)


class HighPass70:
    """Passa-altos Butterworth de 2.ª ordem a 70 Hz, direto II transposto.

    Coeficientes RBJ (Q=1/sqrt(2)) e estado contínuo entre blocos; implementado sem scipy
    para o runtime de teste do produto não precisar de dependências de áudio.
    """

    def __init__(self, sample_rate: int = TARGET_SAMPLE_RATE, cutoff_hz: float = 70.0,
                 q: float = 0.7071067811865476) -> None:
        import math

        if sample_rate <= 0 or not 0 < cutoff_hz < sample_rate / 2:
            raise ValueError("invalid high-pass cutoff")
        omega = 2.0 * math.pi * float(cutoff_hz) / float(sample_rate)
        alpha = math.sin(omega) / (2.0 * q)
        cos_omega = math.cos(omega)
        a0 = 1.0 + alpha
        self._b0 = ((1.0 + cos_omega) / 2.0) / a0
        self._b1 = (-(1.0 + cos_omega)) / a0
        self._b2 = ((1.0 + cos_omega) / 2.0) / a0
        self._a1 = (-2.0 * cos_omega) / a0
        self._a2 = (1.0 - alpha) / a0
        self._z1 = 0.0
        self._z2 = 0.0

    def process(self, block: Any) -> Any:
        import numpy as np

        array = np.asarray(block, dtype=np.float32).reshape(-1)
        if not array.size:
            return np.zeros(0, dtype=np.float32)
        b0, b1, b2 = self._b0, self._b1, self._b2
        a1, a2 = self._a1, self._a2
        z1, z2 = self._z1, self._z2
        out = np.empty(array.size, dtype=np.float32)
        for index, value in enumerate(array.tolist()):
            x = float(value)
            y = b0 * x + z1
            z1 = b1 * x - a1 * y + z2
            z2 = b2 * x - a2 * y
            out[index] = y
        self._z1, self._z2 = z1, z2
        return out


def rms_dbfs(samples: Any) -> float:
    import numpy as np

    array = np.asarray(samples, dtype=np.float32).reshape(-1)
    if not array.size:
        return float("-inf")
    rms = float(np.sqrt(np.mean(np.square(array, dtype=np.float64))))
    if rms <= 0.0:
        return float("-inf")
    return 20.0 * float(np.log10(rms))


def prepare_for_stt(samples: Any, *, target_dbfs: float = -26.0,
                    max_gain_db: float = 6.0, peak_limit: float = 0.89,
                    silence_dbfs: float = -50.0) -> tuple[Any, float]:
    """Ganho constante do bloco final: alvo -26 dBFS, +6 dB máx, pico <=0.89, silêncio intacto."""
    import numpy as np

    array = np.asarray(samples, dtype=np.float32).reshape(-1)
    if not array.size:
        return array, 0.0
    level = rms_dbfs(array)
    if level <= silence_dbfs:
        return array, 0.0
    target_rms = 10.0 ** (target_dbfs / 20.0)
    current_rms = 10.0 ** (level / 20.0)
    gain = target_rms / current_rms if current_rms > 0 else 1.0
    gain = min(gain, 10.0 ** (max_gain_db / 20.0))
    peak = float(np.max(np.abs(array)))
    if peak > 0.0:
        gain = min(gain, peak_limit / peak)
    gain = max(gain, 0.0)
    out = np.ascontiguousarray(array * np.float32(gain), dtype=np.float32)
    if not out.size:
        return out, 0.0
    return out, 20.0 * float(np.log10(gain)) if gain > 0 else 0.0


def clean_transcript(text: str, max_chars: int = MAX_TRANSCRIPT_CHARS) -> tuple[str, bool]:
    """Normaliza espaços e corta no limite, preferindo fronteira de palavra."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= max_chars:
        return cleaned, False
    window = cleaned[:max_chars]
    space = window.rfind(" ")
    return (window[:space] if space > 0 else window).strip(), True


class SileroVad:
    """Silero ONNX (512 amostras + 64 contexto + estado recorrente), CPU, reset por turno."""

    def __init__(self, session: Any) -> None:
        import numpy as np

        self.session = session
        names = {item.name for item in session.get_inputs()}
        if not {"input", "state", "sr"} <= names:
            raise ValueError("unexpected Silero inputs")
        self._np = np
        self.reset()

    def reset(self) -> None:
        self._state = self._np.zeros(VAD_STATE_SHAPE, dtype=self._np.float32)
        self._context = self._np.zeros((1, VAD_CONTEXT_SAMPLES), dtype=self._np.float32)

    def probability(self, frame: Any) -> float:
        np = self._np
        samples = np.asarray(frame, dtype=np.float32).reshape(1, -1)
        if samples.shape[1] != VAD_FRAME_SAMPLES:
            raise ValueError("Silero frame must have 512 samples")
        model_input = np.concatenate([self._context, samples], axis=1)
        output, state = self.session.run(
            ["output", "stateN"],
            {"input": model_input, "state": self._state,
             "sr": np.array(TARGET_SAMPLE_RATE, dtype=np.int64)},
        )
        self._state = np.asarray(state, dtype=np.float32)
        self._context = model_input[:, -VAD_CONTEXT_SAMPLES:]
        return float(np.asarray(output, dtype=np.float32).reshape(-1)[0])


def prepare_native_runtime() -> None:
    """Resolve DLLs CUDA **só neste processo**, antes de importar ctranslate2.

    Repete a ordem validada no preflight de Root; nunca altera `PATH`, `sys.path` do produto
    nem o runtime do TTS.
    """
    import ctypes

    if os.name != "nt":
        return
    site = Path(sys.prefix) / "Lib" / "site-packages"
    nvidia = site / "nvidia"
    directories = {str((nvidia / name).parent) for name in _NVIDIA_DLLS}
    for directory in sorted(directories):
        try:
            _DLL_HANDLES.append(os.add_dll_directory(directory))
        except (OSError, AttributeError):
            pass
    for name in _NVIDIA_DLLS:
        path = nvidia / name
        if path.is_file():
            try:
                ctypes.WinDLL(str(path))
            except OSError:
                # Um DLL em falta falha no import/load do modelo, que devolve
                # `model_unavailable`; não se inventa um fallback.
                pass
    # Mesma ordem do preflight: `av` antes de ctranslate2, ambos só neste processo externo.
    import av  # noqa: F401
    import ctranslate2  # noqa: F401


class WhisperAdapter:
    """Adaptador mínimo e local do faster-whisper; recusa device/compute diferentes."""

    def __init__(self, model: Any, *, language: str = "pt", beam_size: int = 5) -> None:
        self.model = model
        self.language = language
        self.beam_size = beam_size

    def transcribe(self, samples: Any) -> str:
        segments, _info = self.model.transcribe(
            samples,
            language=self.language,
            task="transcribe",
            beam_size=self.beam_size,
            best_of=self.beam_size,
            temperature=0.0,
            vad_filter=False,
            condition_on_previous_text=False,
            word_timestamps=False,
            log_progress=False,
        )
        return " ".join(segment.text.strip() for segment in segments if segment.text).strip()


def load_whisper(model_dir: str | Path, *, device: str = "cuda",
                 compute_type: str = "float16", language: str = "pt",
                 beam_size: int = 5) -> WhisperAdapter:
    """Carrega o modelo local pinado; nunca descarrega nem altera o dispositivo pedido."""
    from faster_whisper import WhisperModel

    model = WhisperModel(
        str(model_dir), device=device, compute_type=compute_type,
        cpu_threads=4, num_workers=1, local_files_only=True,
    )
    actual_device = str(getattr(model.model, "device", ""))
    actual_compute = str(getattr(model.model, "compute_type", ""))
    if actual_device != device or actual_compute != compute_type:
        raise RuntimeError("unexpected STT device/compute type")
    return WhisperAdapter(model, language=language, beam_size=beam_size)


def load_silero(path: str | Path) -> SileroVad:
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(path), sess_options=options,
                                   providers=["CPUExecutionProvider"])
    return SileroVad(session)


def open_microphone(sample_rate: int = INPUT_SAMPLE_RATE,
                    block_ms: int = DEFAULT_BLOCK_MS) -> Any:
    import sounddevice as sd

    block = max(1, int(sample_rate * block_ms / 1000))
    stream = sd.InputStream(samplerate=sample_rate, channels=1, dtype="float32",
                            blocksize=block)
    stream.start()
    return stream


class InputWorker:
    """Um turno de captura por vez; modelo/VAD carregados no arranque, mic só em `start`."""

    def __init__(
        self,
        *,
        model_dir: str | Path = "",
        vad_path: str | Path = "",
        sample_rate: int = INPUT_SAMPLE_RATE,
        target_rate: int = TARGET_SAMPLE_RATE,
        language: str = "pt",
        beam_size: int = 5,
        device: str = 'cuda',
        max_capture_ms: int = 30_000,
        onset_timeout_ms: int = 5_000,
        endpoint_silence_ms: int = 1_000,
        min_speech_ms: int = 250,
        max_transcript_chars: int = MAX_TRANSCRIPT_CHARS,
        emit: Callable[[dict[str, Any]], None] | None = None,
        load_model: Callable[[], Any] | None = None,
        load_vad: Callable[[], Any] | None = None,
        open_stream: Callable[[int, int], Any] | None = None,
    ) -> None:
        self.model_dir = str(model_dir)
        self.vad_path = str(vad_path)
        self.sample_rate = int(sample_rate)
        self.target_rate = int(target_rate)
        self.language = language
        self.beam_size = int(beam_size)
        if device not in {'cpu', 'cuda'}:
            raise ValueError('unsupported STT device')
        self.device = device
        self.max_capture_ms = int(max_capture_ms)
        self.onset_timeout_ms = int(onset_timeout_ms)
        self.endpoint_silence_ms = int(endpoint_silence_ms)
        self.min_speech_ms = int(min_speech_ms)
        self.max_transcript_chars = int(max_transcript_chars)
        self._emit_external = emit
        self._load_model = load_model or (
            lambda: load_whisper(self.model_dir, device=self.device,
                                 compute_type='int8_float32' if self.device == 'cpu' else 'float16', language=self.language,
                                 beam_size=self.beam_size))
        self._load_vad = load_vad or (lambda: load_silero(self.vad_path))
        self._open_stream = open_stream or open_microphone
        self.block_frames = max(1, int(self.sample_rate * DEFAULT_BLOCK_MS / 1000))
        self._model: Any = None
        self._vad: Any = None
        self._lock = threading.RLock()
        self._active_generation: int | None = None
        self._stop_generation = -1
        self._shutdown = False
        self._mode = "manual"
        self._finish_generation = -1
        self._stream: Any = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- protocolo --
    def emit(self, payload: dict[str, Any]) -> None:
        if self._emit_external is not None:
            try:
                self._emit_external(payload)
            except Exception:
                pass
            return
        try:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False,
                                        separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    def prepare(self) -> None:
        """Carrega modelo e VAD antes de emitir `ready`; falha fechada, sem fallback."""
        self._model = self._load_model()
        self._vad = self._load_vad()

    def _cancelled(self, generation: int) -> bool:
        with self._lock:
            return (self._shutdown or generation != self._active_generation
                    or generation <= self._stop_generation)

    def _turn_cancelled(self, generation: int) -> bool:
        """Cancelamento depois de o turno limpar `_active_generation` (mic já fechado)."""
        with self._lock:
            return self._shutdown or generation <= self._stop_generation

    def _finished(self, generation: int) -> bool:
        """Release recebido para este turno; distinto de cancelamento e nunca descarta PCM."""
        with self._lock:
            return self._shutdown or generation <= self._finish_generation

    def handle_command(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return True
        kind = data.get("type")
        if kind == "start":
            generation = data.get("generation")
            mode = data.get("mode", "manual")
            if type(generation) is int and mode in ("manual", "hold"):
                self.start(generation, mode)
            return True
        if kind == "finish":
            generation = data.get("generation")
            if type(generation) is int:
                self.finish(generation)
            return True
        if kind == "cancel":
            generation = data.get("generation")
            if type(generation) is int:
                self.cancel(generation)
            return True
        if kind == "shutdown":
            return False
        return True

    def start(self, generation: int, mode: str = "manual") -> None:
        if mode not in ("manual", "hold"):
            return
        with self._lock:
            if self._shutdown:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._active_generation = generation
            self._stop_generation = -1
            self._finish_generation = -1
            self._mode = mode
            self._thread = threading.Thread(target=self._run_turn, args=(generation,),
                                            name="voice-input-capture", daemon=True)
            thread = self._thread
        thread.start()

    def finish(self, generation: int) -> None:
        """Fecha a captura no release e deixa o STT correr; nunca aborta nem descarta o PCM."""
        with self._lock:
            if generation > self._finish_generation:
                self._finish_generation = generation

    def cancel(self, generation: int) -> None:
        with self._lock:
            if generation > self._stop_generation:
                self._stop_generation = generation
            stream = self._stream
            thread = self._thread
            active = self._active_generation
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
        if thread is None or not thread.is_alive() or active != generation:
            # Nenhum microfone aberto para esta geração: confirmar já o fecho.
            with self._lock:
                self._stream = None
            self.emit({"type": "stopped", "generation": generation})

    def wait_idle(self, timeout: float = 10.0) -> bool:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            stream = self._stream
            thread = self._thread
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=3.0)

    # ---------------------------------------------------------------- turno --
    def _emit_state(self, generation: int, state: str) -> None:
        self.emit({"type": "state", "generation": generation, "state": state})

    def _run_turn(self, generation: int) -> None:
        try:
            samples, cancelled, failure, reason = self._capture(generation)
        except Exception:
            self.emit({"type": "error", "generation": generation, "code": "audio_failed"})
            return
        if cancelled:
            return
        if failure == "audio_failed":
            self.emit({"type": "error", "generation": generation, "code": "audio_failed"})
            return
        if failure == "no_speech" or samples is None:
            self.emit({"type": "no_speech", "generation": generation, "reason": "silence"})
            return
        if self._turn_cancelled(generation):
            return
        self._emit_state(generation, "transcribing")
        try:
            text = self._model.transcribe(samples)
        except Exception:
            self.emit({"type": "error", "generation": generation, "code": "stt_failed"})
            return
        if self._turn_cancelled(generation):
            return
        cleaned, truncated = clean_transcript(text, self.max_transcript_chars)
        if not cleaned:
            self.emit({"type": "no_speech", "generation": generation,
                       "reason": "empty_transcript"})
            return
        payload = {"type": "result", "generation": generation, "text": cleaned,
                   "truncated": truncated}
        if self._mode == "hold" and reason:
            # Interno (manager → auto_send); o IPC público não recebe este campo. Sem reason
            # (ex.: EOF do stream), o manager nunca autoriza autoenvio.
            payload["reason"] = reason
        self.emit(payload)

    def _capture(self, generation: int) -> tuple[Any, bool, str, str]:
        import numpy as np

        if self._vad is None or self._model is None:
            # Nunca abre o microfone sem modelo/VAD carregados.
            raise RuntimeError("worker not prepared")
        with self._lock:
            hold = self._mode == "hold"
            if self._finish_generation >= generation:
                return None, False, "no_speech", "release"
        stream = self._open_stream(self.sample_rate, self.block_frames)
        with self._lock:
            self._stream = stream
            if self._finish_generation >= generation:
                finished_before_open = True
            else:
                finished_before_open = False
        if finished_before_open:
            try:
                stream.close()
            except Exception:
                pass
            with self._lock:
                self._stream = None
                if self._active_generation == generation:
                    self._active_generation = None
            self.emit({"type": "stopped", "generation": generation})
            return None, False, "no_speech", "release"
        detector = EndpointDetector(
            min_speech_ms=self.min_speech_ms,
            endpoint_silence_ms=0 if hold else self.endpoint_silence_ms,
            onset_timeout_ms=0 if hold else self.onset_timeout_ms,
            max_capture_ms=self.max_capture_ms,
        )
        resampler = StreamResampler(self.sample_rate, self.target_rate)
        highpass = HighPass70(self.target_rate)
        frames = FrameBuffer(VAD_FRAME_SAMPLES)
        recent: deque[Any] = deque()
        preroll_samples = int(self.target_rate * PREROLL_MS / 1000)
        recent_samples = 0
        speech_chunks: list[Any] = []
        speech_started = False
        failure = ""
        stopped = ""
        try:
            self._vad.reset()
            self._emit_state(generation, "listening")
            while not self._cancelled(generation) and not self._finished(generation):
                result = stream.read(self.block_frames)
                if isinstance(result, tuple):
                    data = result[0]
                else:
                    data = result
                if data is None:
                    break
                block = np.asarray(data, dtype=np.float32)
                if block.ndim == 2:
                    block = block[:, 0]
                block = block.reshape(-1)
                if not block.size:
                    continue
                raw16 = resampler.process(block)
                filtered = highpass.process(raw16)
                for frame in frames.push(raw16):
                    step = detector.push(self._vad.probability(frame))
                    if step.triggered_now and not speech_started:
                        speech_started = True
                        speech_chunks.extend(recent)
                    if step.finished:
                        stopped = step.reason
                        break
                if speech_started:
                    speech_chunks.append(filtered)
                recent.append(filtered)
                recent_samples += int(filtered.size)
                while recent and recent_samples - int(recent[0].size) >= preroll_samples:
                    recent_samples -= int(recent.popleft().size)
                if stopped:
                    if stopped == "onset_timeout" and not speech_started:
                        failure = "no_speech"
                    break
            if not stopped and self._finished(generation):
                stopped = "release"
        except Exception:
            failure = "audio_failed"
        finally:
            with self._lock:
                self._stream = None
                if self._active_generation == generation:
                    self._active_generation = None
            try:
                stream.close()
            except Exception:
                pass
            self.emit({"type": "stopped", "generation": generation})

        if self._turn_cancelled(generation):
            return None, True, "", ""
        if failure:
            return None, False, failure, ""
        if not speech_started or not speech_chunks:
            return None, False, "no_speech", stopped
        samples = np.concatenate(speech_chunks)
        minimum = int(self.target_rate * self.min_speech_ms / 1000)
        if samples.size < max(1, minimum):
            return None, False, "no_speech", stopped
        prepared, _gain = prepare_for_stt(samples)
        return prepared, False, "", stopped

    def serve(self, stream: Any = None, *, emit_ready: bool = True) -> int:
        source = stream if stream is not None else sys.stdin
        if emit_ready:
            self.emit({"type": "ready"})
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
        return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lunmi voice input worker (external runtime)")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--vad", required=True)
    parser.add_argument("--sample-rate", type=int, default=INPUT_SAMPLE_RATE)
    parser.add_argument("--language", default="pt")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument('--device', choices=('cpu', 'cuda'), default='cuda')
    parser.add_argument("--max-capture-ms", type=int, default=30_000)
    parser.add_argument("--onset-timeout-ms", type=int, default=5_000)
    parser.add_argument("--endpoint-silence-ms", type=int, default=1_000)
    parser.add_argument("--min-speech-ms", type=int, default=250)
    parser.add_argument("--max-transcript-chars", type=int, default=MAX_TRANSCRIPT_CHARS)
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    worker = InputWorker(
        model_dir=args.model_dir,
        vad_path=args.vad,
        sample_rate=args.sample_rate,
        language=args.language,
        beam_size=args.beam_size,
        device=args.device,
        max_capture_ms=args.max_capture_ms,
        onset_timeout_ms=args.onset_timeout_ms,
        endpoint_silence_ms=args.endpoint_silence_ms,
        min_speech_ms=args.min_speech_ms,
        max_transcript_chars=args.max_transcript_chars,
    )
    try:
        if args.device == 'cuda':
            prepare_native_runtime()
        worker.prepare()
    except Exception:
        # Sem ready: o manager mata a árvore e marca erro; nunca há fallback CPU/download.
        worker.emit({"type": "error", "code": "model_unavailable"})
        return 1
    return worker.serve()


if __name__ == "__main__":
    raise SystemExit(main())

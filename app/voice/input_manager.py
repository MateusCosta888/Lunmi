"""Gestor de entrada de voz (ditado) — supervisão do worker externo e estado público.

Regras (`.agents/VOICE-INPUT-V1-CONTRACT.md`):

- O microfone só abre depois de `start` explícito; nada de escuta contínua.
- O worker é um processo próprio no runtime externo de entrada; nenhum módulo nativo é
  importado pela bridge.
- A transcrição vive **só em RAM**, ligada à sessão+geração, e é entregue uma única vez por
  `voice.input.result`. Eventos nunca carregam texto, áudio, paths, modelo ou device.
- `cancel` só responde depois de o worker confirmar `stopped` (microfone fechado) ou de a
  árvore ser terminada; `cancel` nunca transcreve.
- `close`, EOF, crash e timeouts fecham microfone/processos; nada é retomado sozinho.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

import psutil

from app.voice.input_runtime import (
    VoiceInputEnvironment,
    resolve_input_environment,
)

MAX_WORKER_LINE_BYTES = 64 * 1024
WORKER_EXIT_TIMEOUT_SECONDS = 2.0
STOP_GRACE_SECONDS = 0.5
START_TIMEOUT_SECONDS = 30.0
STT_TIMEOUT_SECONDS = 30.0
CANCEL_TIMEOUT_SECONDS = 2.0
WARMUP_TIMEOUT_SECONDS = 30.0
MAX_TRANSCRIPT_CHARS = 2_000

VOICE_INPUT_MODES = ("manual", "hold")

STATES = ("unavailable", "idle", "starting", "listening", "transcribing", "ready", "error")
ACTIVE_STATES = frozenset({"starting", "listening", "transcribing"})

MESSAGE_WORKER = "Motor de ditado indisponível."
MESSAGE_WORKER_LOST = "Motor de ditado encerrou inesperadamente."
MESSAGE_START_TIMEOUT = "O ditado não arrancou a tempo."
MESSAGE_STT_TIMEOUT = "A transcrição demorou demasiado."
MESSAGE_NO_SPEECH = "Não foi detetada fala."
MESSAGE_WARMUP_TIMEOUT = "A preparação do ditado demorou demasiado."

_WORKER_ERROR_MESSAGES = {
    "model_unavailable": "Modelo de ditado indisponível.",
    "audio_failed": "Microfone indisponível.",
    "stt_failed": "Não foi possível transcrever o áudio.",
    "stt_timeout": MESSAGE_STT_TIMEOUT,
}

# Ambiente mínimo do worker: nada de `LLM_*`, chaves, `.env` ou PATH de credenciais.
_WORKER_ENV_ALLOWLIST = (
    "SystemRoot", "SYSTEMROOT", "PATH", "PATHEXT", "TEMP", "TMP", "WINDIR",
    "COMSPEC", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
)


class VoiceInputError(RuntimeError):
    """Falha pública e tipada do gestor de entrada (nunca inclui stack/stderr do worker)."""


def _bound_text(text: str, max_chars: int = MAX_TRANSCRIPT_CHARS) -> tuple[str, bool]:
    """Normaliza e corta no limite, preferindo fronteira de palavra; devolve (texto, cortado)."""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= max_chars:
        return cleaned, False
    window = cleaned[:max_chars]
    space = window.rfind(" ")
    return (window[:space] if space > 0 else window).strip(), True


class VoiceInputManager:
    def __init__(
        self,
        *,
        emit: Callable[[dict[str, Any]], None],
        state_root: str | Path,
        session_id: str = "",
        environment: VoiceInputEnvironment | None = None,
        worker_path: str | Path | None = None,
        runtime_python: str | Path | None = None,
        models_dir: str | Path | None = None,
        silero_path: str | Path | None = None,
        popen: Callable[..., Any] | None = None,
        on_speech_interrupt: Callable[[], Any] | None = None,
        on_active_changed: Callable[[bool], Any] | None = None,
        start_timeout_s: float = START_TIMEOUT_SECONDS,
        stt_timeout_s: float = STT_TIMEOUT_SECONDS,
        cancel_timeout_s: float = CANCEL_TIMEOUT_SECONDS,
    ) -> None:
        self._emit = emit
        self.session_id = session_id
        self.state_root = Path(state_root)
        self._fixed_environment = environment
        self._worker_path = worker_path
        self._runtime_python = runtime_python
        self._models_dir = models_dir
        self._silero_path = silero_path
        self._popen = popen or subprocess.Popen
        self._on_speech_interrupt = on_speech_interrupt
        self._on_active_changed = on_active_changed
        self.start_timeout_s = float(start_timeout_s)
        self.stt_timeout_s = float(stt_timeout_s)
        self.cancel_timeout_s = float(cancel_timeout_s)

        self._lock = threading.RLock()
        self._termination_lock = threading.Lock()
        self._state = "idle"
        self._message = ""
        self._generation = 0
        self._result_text = ""
        self._result_truncated = False
        self._proc: Any = None
        self._stdin: Any = None
        self._terminating: Any = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._closing = False
        self._start_timer: threading.Timer | None = None
        self._stt_timer: threading.Timer | None = None
        self._stopped = threading.Event()
        self._active = False
        # Preparação (loading): worker/modelo/VAD carregados sem `start` e sem abrir microfone.
        self._ready_event = threading.Event()
        self._prepared = False
        self._preparing = False
        self._prepare_message = ""
        # Hold: a geração recorda o modo, o release e o fim já pedido. Nada disto sai no DTO.
        self._mode = "manual"
        self._hold_generation: int | None = None
        self._released_generation = -1
        self._finish_generation = -1
        self._pending_start: tuple[int, str] | None = None
        self._result_auto_send = False

    # ------------------------------------------------------------- ambiente --
    def environment(self) -> VoiceInputEnvironment:
        if self._fixed_environment is not None:
            return self._fixed_environment
        return resolve_input_environment(
            self.state_root,
            worker_path=self._worker_path,
            runtime_python=self._runtime_python,
            models_dir=self._models_dir,
            silero_path=self._silero_path,
        )

    def _state_locked(self, env: VoiceInputEnvironment) -> tuple[str, str]:
        if not env.available and self._state not in ACTIVE_STATES:
            return "unavailable", env.message
        return self._state, self._message

    def _snapshot_locked(self, env: VoiceInputEnvironment) -> dict[str, Any]:
        state, message = self._state_locked(env)
        return {
            "available": bool(env.available),
            "state": state,
            "generation": int(self._generation),
            "message": message,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot_locked(self.environment()))

    def prepare(self, timeout: float = WARMUP_TIMEOUT_SECONDS) -> tuple[str, str]:
        """Carrega o worker/modelo/VAD e espera `ready`, sem `start` nem microfone.

        Devolve `(estado, mensagem)` em `ready|unavailable|error`. Não altera o estado público de
        ditado nem cria geração; um erro de arranque é reportado honestamente e fica terminal até
        ao próximo gesto manual.
        """
        with self._lock:
            if self._closing:
                return "unavailable", MESSAGE_WORKER
            env = self.environment()
            if not env.available:
                return "unavailable", env.message
            if self._prepared:
                return "ready", ""
            if self._preparing:
                event = self._ready_event
            else:
                self._preparing = True
                self._prepare_message = ""
                self._ready_event.clear()
                if not self._ensure_worker_locked(env):
                    self._preparing = False
                    self._ready_event.set()
                    return "error", MESSAGE_WORKER
                event = self._ready_event
        if not event.wait(max(0.01, float(timeout))):
            with self._lock:
                pending = self._preparing
                if pending:
                    self._preparing = False
                    self._prepare_message = MESSAGE_WARMUP_TIMEOUT
            if pending:
                self._kill_worker()
            return "error", MESSAGE_WARMUP_TIMEOUT
        with self._lock:
            if self._prepared:
                return "ready", ""
            return "error", self._prepare_message or MESSAGE_WORKER

    @property
    def worker_pid(self) -> int | None:
        with self._lock:
            proc = self._proc
            if proc is None or self._terminating is proc or proc.poll() is not None:
                return None
            return int(proc.pid)

    @property
    def active(self) -> bool:
        with self._lock:
            return self._state in ACTIVE_STATES

    def _ack_locked(self, accepted: bool, reason: str = "", *,
                    env: VoiceInputEnvironment | None = None) -> dict[str, Any]:
        environment = env if env is not None else self.environment()
        state, _ = self._state_locked(environment)
        return {
            "accepted": bool(accepted),
            "generation": int(self._generation),
            "state": state,
            "reason": reason,
        }

    def _publish(self) -> None:
        with self._lock:
            if self._closing:
                return
            payload = self._snapshot_locked(self.environment())
            payload["session_id"] = self.session_id
        try:
            self._emit(payload)
        except Exception:
            # Um cliente que já fechou stdout não pode derrubar o ditado.
            pass

    def _notify_active(self, active: bool) -> None:
        with self._lock:
            if active == self._active:
                return
            self._active = active
        if self._on_active_changed is not None:
            try:
                self._on_active_changed(active)
            except Exception:
                pass

    # ---------------------------------------------------------------- start --
    def start(self, mode: str = "manual") -> dict[str, Any]:
        if mode not in VOICE_INPUT_MODES:
            with self._lock:
                return self._ack_locked(False, "not_active")
        with self._lock:
            env = self.environment()
            if self._closing:
                return self._ack_locked(False, "unavailable", env=env)
            if not env.available:
                return self._ack_locked(False, "unavailable", env=env)
            if self._state in ACTIVE_STATES:
                return self._ack_locked(False, "busy", env=env)
            self._cancel_timers_locked()
            self._generation += 1
            generation = self._generation
            self._clear_result_locked()
            self._reset_turn_flags_locked(mode, generation)
            self._message = ""
            self._state = "starting"
            interrupt = self._on_speech_interrupt

        # Half-duplex: interrompe a fala antes de abrir o microfone. Fora da nossa trava para
        # o `voice.stop` do TTS nunca poder bloquear um `cancel`/`close` concorrente.
        self._notify_active(True)
        if interrupt is not None:
            try:
                interrupt()
            except Exception:
                pass

        kill: Any = None
        with self._lock:
            if self._closing or self._generation != generation or self._state != "starting":
                # Cancelado durante a interrupção da fala: não abre microfone nenhum.
                return self._ack_locked(False, "not_active")
            if not self._ensure_worker_locked(env):
                self._state = "error"
                self._message = MESSAGE_WORKER
                self._cancel_timers_locked()
                result = self._ack_locked(False, "unavailable", env=env)
            elif mode == "hold" and not self._prepared:
                # O hold não manda `start` antes de o worker carregar o modelo: se o release
                # chegar entretanto, o microfone nunca abre tardiamente.
                self._pending_start = (generation, "hold")
                self._arm_start_timer_locked(generation)
                result = self._ack_locked(True, "", env=env)
            elif not self._send_start_locked(generation, mode):
                kill = True
                self._state = "error"
                self._message = MESSAGE_WORKER
                self._cancel_timers_locked()
                result = self._ack_locked(False, "unavailable", env=env)
            else:
                self._arm_start_timer_locked(generation)
                result = self._ack_locked(True, "", env=env)
        if kill:
            self._kill_worker()
        if not result["accepted"]:
            self._notify_active(False)
        self._publish()
        return result

    def _send_start_locked(self, generation: int, mode: str) -> bool:
        payload: dict[str, Any] = {"type": "start", "generation": generation}
        if mode == "hold":
            payload["mode"] = "hold"
        return self._send_locked(payload)

    def _reset_turn_flags_locked(self, mode: str, generation: int) -> None:
        self._mode = mode
        self._hold_generation = generation if mode == "hold" else None
        self._released_generation = -1
        self._finish_generation = -1
        self._pending_start = None
        self._result_auto_send = False

    # --------------------------------------------------------------- finish --
    def finish(self, generation: int) -> dict[str, Any]:
        """Regista o release de um turno hold; não descarta áudio nem se confunde com cancel."""
        kill = False
        finalized = False
        with self._lock:
            env = self.environment()
            if (generation != self._generation or self._state not in ACTIVE_STATES
                    or self._mode != "hold" or self._hold_generation != generation):
                return self._ack_locked(False, "not_active", env=env)
            if self._finish_generation == generation:
                # Idempotente: o release repetido não manda segundo finish ao worker.
                return self._ack_locked(True, "", env=env)
            pending = (self._pending_start is not None
                       and self._pending_start[0] == generation)
            self._finish_generation = generation
            self._released_generation = generation
            if self._state == "transcribing":
                result = self._ack_locked(True, "", env=env)
            elif pending:
                # O worker ainda não recebeu `start`: finaliza o mesmo turno sem abrir mic.
                self._pending_start = None
                self._cancel_timers_locked()
                self._clear_result_locked()
                self._state = "idle"
                self._message = MESSAGE_NO_SPEECH
                finalized = True
                result = self._ack_locked(True, "", env=env)
            elif not self._send_locked({"type": "finish", "generation": generation}):
                self._state = "error"
                self._message = MESSAGE_WORKER
                self._cancel_timers_locked()
                kill = True
                result = self._ack_locked(False, "unavailable", env=env)
            else:
                result = self._ack_locked(True, "", env=env)
        if finalized:
            self._notify_active(False)
            self._publish()
        if kill:
            self._kill_worker()
            self._notify_active(False)
            self._publish()
        return result

    # --------------------------------------------------------------- cancel --
    def cancel(self) -> dict[str, Any]:
        with self._lock:
            env = self.environment()
            state = self._state
            if state in {"idle", "error"}:
                return self._ack_locked(False, "not_active", env=env)
            if state == "ready":
                self._cancel_timers_locked()
                self._clear_result_locked()
                self._reset_turn_flags_locked("manual", self._generation)
                self._generation += 1
                self._state = "idle"
                self._message = ""
                result = self._ack_locked(True, "", env=env)
        if state == "ready":
            self._publish()
            self._notify_active(False)
            return result

        with self._lock:
            generation = self._generation
            proc = self._proc
            self._cancel_timers_locked()
            self._stopped.clear()
        if proc is not None:
            if state in {"starting", "transcribing"}:
                # No arranque, o microfone ainda não abriu e terminar evita que o `start`
                # enfileirado o abra. Em `transcribing`, o PCM já foi entregue ao STT: terminar
                # a árvore impede trabalho nativo a continuar e qualquer resultado tardio.
                self._kill_worker()
            else:
                stopped = False
                if self._send_locked({"type": "cancel", "generation": generation}):
                    stopped = self._stopped.wait(self.cancel_timeout_s)
                if not stopped:
                    # Não se declara cancelado enquanto o filho pode escrever no microfone:
                    # terminar a árvore owned e esperar pela saída antes de responder.
                    self._kill_worker()
        with self._lock:
            if self._generation != generation:
                # O worker morreu/entrou em erro durante o cancelamento: preservar o estado
                # honesto em vez de o maquilhar como `idle`.
                return self._ack_locked(False, "not_active")
            self._generation += 1
            self._cancel_timers_locked()
            self._clear_result_locked()
            self._reset_turn_flags_locked("manual", self._generation)
            self._state = "idle"
            self._message = ""
            result = self._ack_locked(True)
        self._publish()
        self._notify_active(False)
        return result

    # --------------------------------------------------------------- result --
    def result(self, generation: int) -> dict[str, Any]:
        """Entrega a transcrição uma única vez, ligada à geração pedida; nunca envia chat."""
        with self._lock:
            current = self._generation
            if (generation != current or self._state != "ready" or not self._result_text):
                return {"status": "none", "generation": current, "text": "",
                        "truncated": False}
            text = self._result_text
            truncated = self._result_truncated
            hold = self._mode == "hold" and self._hold_generation == current
            auto_send = bool(self._result_auto_send)
            self._clear_result_locked()
            self._hold_generation = None
            self._released_generation = -1
            self._finish_generation = -1
            self._result_auto_send = False
            self._state = "idle"
            self._message = ""
        self._publish()
        payload: dict[str, Any] = {"status": "ready", "generation": current, "text": text,
                                   "truncated": truncated}
        if hold:
            # Só o turno hold leva esta decisão; o manual V1 mantém exatamente quatro campos.
            payload["auto_send"] = auto_send
        return payload

    # --------------------------------------------------------------- worker --
    def _worker_environment(self) -> dict[str, str]:
        env = {key: os.environ[key] for key in _WORKER_ENV_ALLOWLIST if os.environ.get(key)}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # O worker decide o offline explicitamente antes de importar faster_whisper; aqui só
        # garantimos que nenhuma credencial/HF token do produto entra no processo externo.
        env["HF_HUB_OFFLINE"] = "1"
        env["HF_HUB_DISABLE_TELEMETRY"] = "1"
        return env

    def _worker_argv(self, env: VoiceInputEnvironment) -> list[str]:
        return [
            str(env.python), str(env.worker),
            "--model-dir", str(env.model),
            "--vad", str(env.silero),
        ]

    def _ensure_worker_locked(self, env: VoiceInputEnvironment) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        self._proc = None
        self._stdin = None
        kwargs: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": self._worker_environment(),
            "cwd": str(env.worker.parent),
            "bufsize": 0,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = self._popen(self._worker_argv(env), **kwargs)
        except (OSError, ValueError):
            return False
        self._proc = proc
        self._stdin = proc.stdin
        self._prepared = False
        self._ready_event.clear()
        self._reader = threading.Thread(target=self._read_loop, args=(proc,),
                                        name="voice-input-reader", daemon=True)
        self._stderr_reader = threading.Thread(target=self._drain_stderr, args=(proc,),
                                               name="voice-input-stderr", daemon=True)
        self._reader.start()
        self._stderr_reader.start()
        return True

    def _send_locked(self, payload: dict[str, Any]) -> bool:
        stream = self._stdin
        if stream is None:
            return False
        try:
            stream.write((json.dumps(payload, ensure_ascii=False,
                                     separators=(",", ":")) + "\n").encode("utf-8"))
            stream.flush()
            return True
        except (OSError, ValueError):
            return False

    def _drain_stderr(self, proc: Any) -> None:
        """Drena stderr sem interpretar: nada do worker vaza para eventos, logs ou erros."""
        stream = proc.stderr
        if stream is None:
            return
        try:
            while stream.read(4096):
                pass
        except (OSError, ValueError):
            return

    def _read_loop(self, proc: Any) -> None:
        stream = proc.stdout
        broken = False
        try:
            if stream is not None:
                while True:
                    raw = stream.readline(MAX_WORKER_LINE_BYTES + 1)
                    if not raw:
                        # EOF com o filho ainda vivo é a mesma falha de protocolo/captura.
                        broken = proc.poll() is None
                        break
                    if len(raw) > MAX_WORKER_LINE_BYTES:
                        # O limite vale para a linha inteira, com ou sem newline.
                        broken = True
                        break
                    line = raw.strip()
                    if line:
                        self._handle_line(proc, line)
        except (OSError, ValueError):
            broken = proc.poll() is None
        if broken:
            self._on_reader_failure(proc)
        else:
            self._on_worker_exit(proc)

    def _handle_line(self, proc: Any, raw: bytes) -> None:
        try:
            data = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type")
        generation = data.get("generation")
        publish = False
        kill = False
        with self._lock:
            if self._closing or self._proc is not proc:
                return
            if kind == "ready":
                self._prepared = True
                self._preparing = False
                self._prepare_message = ""
                self._ready_event.set()
                pending = self._pending_start
                self._pending_start = None
                if (pending is not None and self._state == "starting"
                        and pending[0] == self._generation):
                    if not self._send_start_locked(pending[0], pending[1]):
                        self._cancel_timers_locked()
                        self._state = "error"
                        self._message = MESSAGE_WORKER
                        publish = True
                if publish:
                    self._notify_active(self._state in ACTIVE_STATES)
                    self._publish()
                return
            if kind == "stopped":
                # Só a geração atual confirma o fecho; um `stopped` atrasado de outro turno
                # não pode libertar um cancelamento em curso.
                if type(generation) is int and generation == self._generation:
                    self._stopped.set()
                return
            if kind == "cancelled":
                return
            if type(generation) is not int:
                # Só o erro de arranque (modelo indisponível) vem sem geração.
                if kind != "error" or (self._state != "starting" and not self._preparing):
                    return
            elif generation != self._generation:
                return

            if kind == "state":
                value = data.get("state")
                if value == "listening" and self._state == "starting":
                    self._cancel_start_timer_locked()
                    self._state = "listening"
                    self._message = ""
                    publish = True
                elif value == "transcribing" and self._state in {"listening", "starting"}:
                    self._cancel_start_timer_locked()
                    self._arm_stt_timer_locked(self._generation)
                    self._state = "transcribing"
                    self._message = ""
                    publish = True

            elif kind == "result" and self._state in {"listening", "transcribing"}:
                text = data.get("text")
                if not isinstance(text, str):
                    return
                cleaned, clipped = _bound_text(text)
                self._cancel_timers_locked()
                if not cleaned:
                    self._state = "idle"
                    self._message = MESSAGE_NO_SPEECH
                    publish = True
                else:
                    self._result_text = cleaned
                    self._result_truncated = bool(clipped or data.get("truncated") is True)
                    reason = data.get("reason")
                    self._result_auto_send = bool(
                        self._mode == "hold"
                        and self._hold_generation == self._generation
                        and self._released_generation == self._generation
                        and reason == "release"
                    )
                    self._state = "ready"
                    self._message = ""
                    publish = True

            elif kind == "no_speech" and self._state in {"listening", "transcribing", "starting"}:
                self._cancel_timers_locked()
                self._clear_result_locked()
                self._reset_turn_flags_locked("manual", self._generation)
                self._state = "idle"
                self._message = MESSAGE_NO_SPEECH
                publish = True

            elif kind == "error":
                code = str(data.get("code") or "")
                self._cancel_timers_locked()
                self._clear_result_locked()
                self._reset_turn_flags_locked("manual", self._generation)
                self._state = "error"
                self._message = _WORKER_ERROR_MESSAGES.get(code, MESSAGE_WORKER)
                if self._preparing:
                    self._preparing = False
                    self._prepare_message = self._message
                    self._ready_event.set()
                publish = True
                kill = code == "model_unavailable"
                if kill:
                    # O pid deixa de ser utilizável enquanto a árvore termina; o handle
                    # continua em `_proc` para close/cancel concorrentes.
                    self._terminating = self._proc
        if publish:
            with self._lock:
                active = self._state in ACTIVE_STATES
            self._notify_active(active)
            self._publish()
        if kill:
            self._kill_worker()

    def _on_worker_exit(self, proc: Any) -> None:
        with self._lock:
            if self._proc is not proc:
                return
            self._proc = None
            self._stdin = None
            if self._closing or self._terminating is proc:
                return
            self._cancel_timers_locked()
            self._generation += 1
            self._clear_result_locked()
            self._reset_turn_flags_locked("manual", self._generation)
            self._prepared = False
            if self._preparing:
                self._preparing = False
                self._prepare_message = MESSAGE_WORKER_LOST
                self._ready_event.set()
            self._state = "error"
            self._message = MESSAGE_WORKER_LOST
        self._notify_active(False)
        self._publish()

    def _on_reader_failure(self, proc: Any) -> None:
        """Linha acima do limite/EOF inesperado: termina a árvore e espera antes do erro.

        O handle do filho só é libertado depois de `_terminate_proc` confirmar a saída, para
        `close`/`cancel` concorrentes nunca perderem acesso ao processo ainda vivo.
        """
        with self._lock:
            if self._proc is not proc:
                return
        with self._termination_lock:
            with self._lock:
                if self._proc is not proc:
                    return
                self._stdin = None
                self._terminating = proc
            try:
                self._terminate_proc(proc)
            finally:
                with self._lock:
                    if self._terminating is proc:
                        self._terminating = None
            with self._lock:
                if self._proc is not proc:
                    return
                self._proc = None
                if self._closing:
                    return
                self._cancel_timers_locked()
                self._clear_result_locked()
                self._reset_turn_flags_locked("manual", self._generation)
                self._prepared = False
                if self._preparing:
                    self._preparing = False
                    self._prepare_message = MESSAGE_WORKER_LOST
                    self._ready_event.set()
                self._state = "error"
                self._message = MESSAGE_WORKER_LOST
                self._generation += 1
                self._stopped.set()
        self._notify_active(False)
        self._publish()

    def _kill_worker(self) -> None:
        """Pede a saída e espera; só liberta o handle depois de a árvore terminar."""
        with self._termination_lock:
            with self._lock:
                proc = self._proc
                self._prepared = False
                if self._preparing:
                    self._preparing = False
                    self._prepare_message = MESSAGE_WORKER_LOST
                    self._ready_event.set()
                if proc is None:
                    return
                self._stdin = None
                self._terminating = proc
            try:
                self._terminate_proc(proc)
            finally:
                with self._lock:
                    if self._terminating is proc:
                        self._terminating = None
                    if self._proc is proc:
                        self._proc = None

    def _terminate_proc(self, proc: Any) -> None:
        """Pede `shutdown`, espera, e termina a árvore owned; nunca deixa filho a escrever."""
        stream = getattr(proc, "stdin", None)
        if stream is not None:
            try:
                stream.write(b'{"type":"shutdown"}\n')
                stream.flush()
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            proc.wait(timeout=STOP_GRACE_SECONDS)
            return
        except Exception:
            pass
        self._terminate_tree(proc)

    @staticmethod
    def _terminate_tree(proc: Any) -> None:
        children: list[Any] = []
        try:
            children = psutil.Process(proc.pid).children(recursive=True)
        except (psutil.Error, OSError):
            children = []
        for child in children:
            try:
                child.kill()
            except (psutil.Error, OSError):
                pass
        try:
            proc.kill()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=WORKER_EXIT_TIMEOUT_SECONDS)
        except Exception:
            pass

    # --------------------------------------------------------------- timers --
    def _arm_start_timer_locked(self, generation: int) -> None:
        self._cancel_start_timer_locked()
        timer = threading.Timer(self.start_timeout_s, self._on_timeout,
                                args=(generation, "start"))
        timer.daemon = True
        self._start_timer = timer
        timer.start()

    def _arm_stt_timer_locked(self, generation: int) -> None:
        self._cancel_stt_timer_locked()
        timer = threading.Timer(self.stt_timeout_s, self._on_timeout,
                                args=(generation, "stt"))
        timer.daemon = True
        self._stt_timer = timer
        timer.start()

    def _cancel_start_timer_locked(self) -> None:
        timer, self._start_timer = self._start_timer, None
        if timer is not None:
            timer.cancel()

    def _cancel_stt_timer_locked(self) -> None:
        timer, self._stt_timer = self._stt_timer, None
        if timer is not None:
            timer.cancel()

    def _cancel_timers_locked(self) -> None:
        self._cancel_start_timer_locked()
        self._cancel_stt_timer_locked()

    def _on_timeout(self, generation: int, kind: str) -> None:
        with self._lock:
            if self._closing or generation != self._generation:
                return
            if kind == "start" and self._state != "starting":
                return
            if kind == "stt" and self._state != "transcribing":
                return
            self._cancel_timers_locked()
            self._clear_result_locked()
            self._reset_turn_flags_locked("manual", self._generation)
            self._state = "error"
            self._message = MESSAGE_START_TIMEOUT if kind == "start" else MESSAGE_STT_TIMEOUT
            self._terminating = self._proc
        # Um worker preso no arranque/STT pode manter o microfone ou o modelo; terminar a
        # árvore owned e esperar pela saída antes de publicar o estado terminal.
        self._kill_worker()
        self._notify_active(False)
        self._publish()

    # -------------------------------------------------------------- limpeza --
    def _clear_result_locked(self) -> None:
        self._result_text = ""
        self._result_truncated = False

    def close(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
            self._generation += 1
            self._cancel_timers_locked()
            self._clear_result_locked()
            self._reset_turn_flags_locked("manual", self._generation)
            self._prepared = False
            self._state = "idle"
            self._message = ""
            self._stopped.clear()
        self._kill_worker()
        self._notify_active(False)

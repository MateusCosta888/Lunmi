"""Gestor de voz da Lunmi — supervisão do worker externo e estado público.

Regras:
- Nada de áudio na bridge: o worker é um processo próprio no runtime externo (`.local/voice/runtime`).
- `enabled`/`auto_speak` começam desligados e vivem em `voice-prefs.json` atómico.
- `voice.stop` (e `enabled=false`, desligar a app) invalida a geração, descarta a fila e aborta o
  áudio, sem esperar a síntese nem cancelar a tarefa Windows em curso.
- O texto falável vive **só em RAM**; uma resposta final inelegível/falhada/anexada limpa-o.
- Eventos `voice.state` nunca carregam texto de conversa, caminhos ou stack.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

import psutil

from app.voice.prefs import VoicePrefs, load_prefs, prefs_path, save_prefs
from app.voice.runtime import VoiceEnvironment, resolve_environment

MAX_WORKER_LINE_BYTES = 64 * 1024
WORKER_EXIT_TIMEOUT_SECONDS = 2.0
TEST_PHRASE = "Olá. A voz da Lunmi está a funcionar."

STATES = ("disabled", "loading", "ready", "synthesizing", "playing", "error")

MESSAGE_UNAVAILABLE = "Voz indisponível: runtime ou modelos de voz não instalados."
MESSAGE_WORKER = "Motor de voz indisponível."
MESSAGE_WORKER_LOST = "Motor de voz encerrou inesperadamente."
MESSAGE_SYNTHESIS = "Falha ao sintetizar a resposta."
MESSAGE_AUDIO = "Áudio de saída indisponível."
MESSAGE_MODEL = "Modelo de voz indisponível."

_WORKER_ERROR_MESSAGES = {
    "synthesis_failed": MESSAGE_SYNTHESIS,
    "audio_failed": MESSAGE_AUDIO,
    "model_unavailable": MESSAGE_MODEL,
}

# Ambiente mínimo do worker: nada de `LLM_*`, chaves, `.env` ou PATH de credenciais.
_WORKER_ENV_ALLOWLIST = (
    "SystemRoot", "SYSTEMROOT", "PATH", "PATHEXT", "TEMP", "TMP", "WINDIR",
    "COMSPEC", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
)


class VoiceError(RuntimeError):
    """Falha pública e tipada do gestor de voz (nunca inclui stack ou stderr do worker)."""


class VoicePrefsError(VoiceError):
    """Não foi possível persistir as preferências de voz."""


class VoiceManager:
    def __init__(
        self,
        *,
        emit: Callable[[dict[str, Any]], None],
        root_dir: str | Path,
        state_root: str | Path,
        session_id: str = "",
        environment: VoiceEnvironment | None = None,
        worker_path: str | Path | None = None,
        runtime_python: str | Path | None = None,
        models_dir: str | Path | None = None,
        popen: Callable[..., Any] | None = None,
    ) -> None:
        self._emit = emit
        self.session_id = session_id
        self.root_dir = Path(root_dir)
        self.state_root = Path(state_root)
        self._fixed_environment = environment
        self._worker_path = worker_path
        self._runtime_python = runtime_python
        self._models_dir = models_dir
        self._popen = popen or subprocess.Popen
        self.prefs_path = prefs_path(self.root_dir)
        self._prefs = load_prefs(self.prefs_path)
        self._lock = threading.RLock()
        self._generation = 0
        self._activity = ""
        self._error_message = ""
        self._last_text = ""
        self._proc: Any = None
        self._stdin: Any = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._closing = False

    # ------------------------------------------------------------- ambiente --
    def environment(self) -> VoiceEnvironment:
        if self._fixed_environment is not None:
            return self._fixed_environment
        return resolve_environment(
            self.state_root,
            worker_path=self._worker_path,
            runtime_python=self._runtime_python,
            models_dir=self._models_dir,
        )

    def _state_locked(self, env: VoiceEnvironment) -> tuple[str, str]:
        if not self._prefs.enabled:
            return "disabled", ""
        if not env.available:
            return "error", MESSAGE_UNAVAILABLE
        if self._error_message:
            return "error", self._error_message
        if self._activity:
            return self._activity, ""
        return "ready", ""

    def _snapshot_locked(self, env: VoiceEnvironment) -> dict[str, Any]:
        state, message = self._state_locked(env)
        return {
            "enabled": bool(self._prefs.enabled),
            "auto_speak": bool(self._prefs.auto_speak),
            "available": env.available,
            "state": state,
            "generation": int(self._generation),
            "can_speak_last": bool(self._last_text),
            "capture_supported": False,
            "message": message,
        }

    @property
    def worker_pid(self) -> int | None:
        """PID vivo do worker (diagnóstico/teste); `None` quando não há processo."""
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return None
            return int(proc.pid)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot_locked(self.environment()))

    def _ack_locked(self, accepted: bool, reason: str = "") -> dict[str, Any]:
        env = self.environment()
        state, _ = self._state_locked(env)
        return {
            "accepted": bool(accepted),
            "generation": int(self._generation),
            "state": state,
            "reason": reason,
        }

    def _publish(self) -> None:
        with self._lock:
            env = self.environment()
            payload = self._snapshot_locked(env)
            payload["session_id"] = self.session_id
        try:
            self._emit(payload)
        except Exception:
            # Um cliente que já fechou stdout não pode derrubar a voz nem a tarefa.
            pass

    # ----------------------------------------------------------- preferências --
    def set(self, *, enabled: bool | None = None, auto_speak: bool | None = None) -> dict[str, Any]:
        with self._lock:
            next_prefs = VoicePrefs(
                enabled=self._prefs.enabled if enabled is None else bool(enabled),
                auto_speak=self._prefs.auto_speak if auto_speak is None else bool(auto_speak),
            )
        if not save_prefs(self.prefs_path, next_prefs):
            raise VoicePrefsError("Não foi possível gravar as preferências de voz.")
        with self._lock:
            was_enabled = self._prefs.enabled
            self._prefs = next_prefs
            if was_enabled and not next_prefs.enabled:
                self._generation += 1
                self._last_text = ""
                self._activity = ""
                self._error_message = ""
                self._kill_worker_locked()
            result = self._snapshot_locked(self.environment())
        self._publish()
        return result

    # ------------------------------------------------------------------- fala --
    def observe_reply(self, text: str) -> None:
        """Aplica a decisão de elegibilidade da resposta final; `""` limpa o último texto."""
        value = text if isinstance(text, str) else ""
        with self._lock:
            changed_last = bool(value) != bool(self._last_text)
            self._last_text = value
            auto = bool(value) and self._prefs.enabled and self._prefs.auto_speak
        if changed_last:
            self._publish()
        if auto:
            self.request_speak(value)

    def test(self) -> dict[str, Any]:
        return self.request_speak(TEST_PHRASE)

    def speak_last(self) -> dict[str, Any]:
        with self._lock:
            enabled = self._prefs.enabled
            text = self._last_text
        if not enabled:
            # Voz desligada explica-se pela preferência, mesmo sem texto guardado.
            return self._ack("disabled")
        if not text:
            return self._ack("no_last_response")
        return self.request_speak(text)

    def _ack(self, reason: str = "") -> dict[str, Any]:
        with self._lock:
            return self._ack_locked(False, reason)

    def request_speak(self, text: str) -> dict[str, Any]:
        bounded = " ".join((text or "").split())
        if not bounded:
            return self._ack("no_last_response")
        with self._lock:
            env = self.environment()
            if not self._prefs.enabled:
                return self._ack_locked(False, "disabled")
            if not env.available:
                return self._ack_locked(False, "unavailable")
            self._generation += 1
            generation = self._generation
            self._error_message = ""
            self._activity = "synthesizing"
            if not self._ensure_worker_locked(env):
                self._activity = ""
                self._error_message = MESSAGE_WORKER
                return self._ack_locked(False, "unavailable")
            # Uma fala nova substitui a anterior: o worker só reproduz a geração atual.
            if not (self._send_locked({"type": "stop"})
                    and self._send_locked({"type": "speak", "generation": generation,
                                           "text": bounded})):
                self._activity = ""
                self._error_message = MESSAGE_WORKER
                self._kill_worker_locked()
                return self._ack_locked(False, "unavailable")
            result = self._ack_locked(True)
        self._publish()
        return result

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._generation += 1
            self._activity = ""
            self._error_message = ""
            if self._proc is not None:
                self._send_locked({"type": "stop"})
            result = self._ack_locked(True)
        self._publish()
        return result

    # --------------------------------------------------------------- worker --
    def _worker_environment(self) -> dict[str, str]:
        env = {key: os.environ[key] for key in _WORKER_ENV_ALLOWLIST if os.environ.get(key)}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def _worker_argv(self, env: VoiceEnvironment) -> list[str]:
        return [str(env.python), str(env.worker), "--model-dir", str(env.models)]

    def _ensure_worker_locked(self, env: VoiceEnvironment) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        self._kill_worker_locked()
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
        self._reader = threading.Thread(target=self._read_loop, args=(proc,),
                                        name="voice-worker-reader", daemon=True)
        self._stderr_reader = threading.Thread(target=self._drain_stderr, args=(proc,),
                                               name="voice-worker-stderr", daemon=True)
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
        """Drena stderr sem o interpretar: nada do worker vaza para eventos, logs ou prefs."""
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
        try:
            if stream is not None:
                while True:
                    raw = stream.readline(MAX_WORKER_LINE_BYTES + 1)
                    if not raw:
                        break
                    if len(raw) > MAX_WORKER_LINE_BYTES and not raw.endswith(b"\n"):
                        break
                    line = raw.strip()
                    if line:
                        self._handle_line(proc, line)
        except (OSError, ValueError):
            pass
        finally:
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
        kill_worker = False
        with self._lock:
            if self._closing or self._proc is not proc:
                return
            if type(generation) is int and generation != self._generation:
                # Resultado de uma geração já substituída: descartado, nunca toca.
                return
            changed = False
            if kind == "state":
                activity = data.get("state")
                if activity in {"loading", "synthesizing", "playing"} and self._activity != activity:
                    self._activity = activity
                    changed = True
            elif kind in {"ready", "done"}:
                if self._activity:
                    self._activity = ""
                    changed = True
            elif kind == "error":
                code = data.get("code")
                message = _WORKER_ERROR_MESSAGES.get(str(code), MESSAGE_WORKER)
                if self._activity or self._error_message != message:
                    changed = True
                self._activity = ""
                self._error_message = message
                kill_worker = code == "model_unavailable"
        if changed:
            self._publish()
        if kill_worker:
            # Fora do lock: matar a árvore pode demorar e `voice.stop` não pode esperar por isso.
            with self._lock:
                self._kill_worker_locked()

    def _on_worker_exit(self, proc: Any) -> None:
        with self._lock:
            if self._proc is not proc:
                return
            self._proc = None
            self._stdin = None
            if self._closing:
                return
            self._generation += 1
            self._activity = ""
            self._error_message = MESSAGE_WORKER_LOST
        self._publish()

    def _kill_worker_locked(self) -> None:
        proc, self._proc = self._proc, None
        self._stdin = None
        if proc is None:
            return
        stream = getattr(proc, "stdin", None)
        if stream is not None:
            try:
                stream.write(b'{"type":"shutdown"}\n')
                stream.flush()
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            proc.wait(timeout=0.5)
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

    # ------------------------------------------------------------ encerrar --
    def close(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
            self._generation += 1
            self._activity = ""
            self._error_message = ""
            self._kill_worker_locked()

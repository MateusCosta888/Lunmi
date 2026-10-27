"""Readiness de voz da introdução — TTS e STT preparados em paralelo, sem áudio nem microfone.

O coordinator não abre dispositivos, não fala e não altera `enabled`/`auto_speak`. Só chama os
métodos já supervisionados de cada manager e publica um DTO sanitizado (`voice.readiness`).
Falhas e timeouts são terminais e honestos: a aplicação pode abrir em shell degradado em vez de
ficar bloqueada à espera de um runtime ausente.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

VOICE_WARMUP_TIMEOUT_MS = 30_000
# Nome curto mantido para compatibilidade interna/testes existentes.
WARMUP_TIMEOUT_MS = VOICE_WARMUP_TIMEOUT_MS
WARMUP_TIMEOUT_SECONDS = VOICE_WARMUP_TIMEOUT_MS / 1000.0
MESSAGE_WARMUP_FAILED = "A preparação da voz falhou."
MESSAGE_WARMUP_TIMEOUT = "A preparação da voz demorou demasiado."

_TERMINAL = frozenset({"ready", "unavailable", "error"})


class VoiceWarmup:
    def __init__(
        self,
        *,
        tts: Any,
        stt: Any,
        emit: Callable[[dict[str, Any]], None] | None = None,
        timeout_s: float = WARMUP_TIMEOUT_SECONDS,
    ) -> None:
        self._tts = tts
        self._stt = stt
        self._emit_external = emit
        self.timeout_s = float(timeout_s)
        self._lock = threading.Lock()
        self._tts_state = "idle"
        self._stt_state = "idle"
        self._tts_message = ""
        self._stt_message = ""
        self._started = False
        self._closing = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ API --
    def start(self) -> dict[str, Any]:
        """Inicia uma única preparação; chamadas seguintes devolvem o estado corrente."""
        should_start = False
        with self._lock:
            if self._closing:
                return self._snapshot_locked()
            if not self._started:
                self._started = True
                self._tts_state = "preparing"
                self._stt_state = "preparing"
                self._tts_message = ""
                self._stt_message = ""
                self._thread = threading.Thread(target=self._run, name="voice-warmup",
                                                daemon=True)
                should_start = True
            snapshot = self._snapshot_locked()
            thread = self._thread
        # Publicar o início antes de arrancar as pernas garante que nenhum `ready` terminal
        # aparece antes do primeiro `preparing` na ordem observada pelo cliente.
        self._publish(snapshot)
        if should_start and thread is not None:
            thread.start()
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def close(self) -> None:
        with self._lock:
            self._closing = True

    # -------------------------------------------------------------- interno --
    def _snapshot_locked(self) -> dict[str, Any]:
        tts = self._tts_state
        stt = self._stt_state
        if tts in _TERMINAL and stt in _TERMINAL:
            state = "ready"
            ready = True
        elif tts == "idle" and stt == "idle":
            state = "idle"
            ready = False
        else:
            state = "preparing"
            ready = False
        messages: list[str] = []
        for message in (self._tts_message, self._stt_message):
            if message and message not in messages:
                messages.append(message)
        return {
            "state": state,
            "ready": ready,
            "tts": tts,
            "stt": stt,
            "message": " ".join(messages),
        }

    def _run(self) -> None:
        legs = [
            ("tts", self._tts),
            ("stt", self._stt),
        ]
        threads: list[threading.Thread] = []
        for name, manager in legs:
            method = getattr(manager, "warm_up" if name == "tts" else "prepare", None)
            if method is None:
                self._set_leg(name, "error", MESSAGE_WARMUP_FAILED)
                continue
            thread = threading.Thread(target=self._run_leg, args=(name, method),
                                      name=f"voice-warmup-{name}", daemon=True)
            threads.append(thread)
            thread.start()
        deadline = time.monotonic() + self.timeout_s
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        for thread in threads:
            if thread.is_alive():
                # O manager já tem timeout próprio; isto cobre um manager injetado que bloqueie.
                self._set_leg("tts" if thread.name.endswith("tts") else "stt",
                              "error", MESSAGE_WARMUP_TIMEOUT)

    def _run_leg(self, name: str, method: Callable[..., Any]) -> None:
        try:
            result = method(self.timeout_s)
        except Exception:
            self._set_leg(name, "error", MESSAGE_WARMUP_FAILED)
            return
        state = ""
        message = ""
        if isinstance(result, tuple) and len(result) >= 2:
            state = str(result[0])
            message = str(result[1])
        elif isinstance(result, str):
            state = result
        if state not in _TERMINAL:
            state = "error"
            message = message or MESSAGE_WARMUP_FAILED
        self._set_leg(name, state, message if state != "ready" else "")

    def _set_leg(self, name: str, state: str, message: str) -> None:
        with self._lock:
            if self._closing:
                return
            current = self._tts_state if name == "tts" else self._stt_state
            if current in _TERMINAL:
                # Timeout/erro/disponibilidade são terminais: uma resposta tardia da mesma
                # perna não pode voltar a anunciar `ready` nem regredir o aggregate.
                return
            if name == "tts":
                self._tts_state = state
                self._tts_message = message
            else:
                self._stt_state = state
                self._stt_message = message
            snapshot = self._snapshot_locked()
        self._publish(snapshot)

    def _publish(self, payload: dict[str, Any]) -> None:
        if self._emit_external is None:
            return
        try:
            self._emit_external(dict(payload))
        except Exception:
            # Um cliente que já fechou stdout não pode bloquear a preparação.
            pass

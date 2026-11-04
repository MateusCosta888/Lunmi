"""Voice handlers for BridgeService, sharing its session and lifecycle state.

Moved without changing the IPC contract, following core.spotify_flow.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from app.bridge import protocol
from app.core.result import TaskResult
from app.settings import Settings
from app.voice.eligibility import speakable_reply
from app.voice.input_manager import VoiceInputManager
from app.voice.manager import VoiceError, VoiceManager
from app.voice.models import GROUPS as VOICE_MODEL_GROUPS
from app.voice.models import ModelsError, VoiceModels, manifest_for, models_root, older_voice_components
from app.voice.runtime import voice_home
from app.voice.warmup import VoiceWarmup, low_memory_pc


class VoiceBridge:
    # Session and settings are owned by BridgeService; these are type declarations only.
    session_id: str
    root_dir: Path
    settings: Settings
    _closed: bool
    _emit: Callable[[str, dict[str, Any]], None]

    def _init_voice(self, voice: VoiceManager | None, voice_input: VoiceInputManager | None) -> None:
        # VOICE V1: gestor leve — não importa NumPy/ORT/sounddevice na bridge e não arranca
        # nenhum processo antes do primeiro pedido de fala. Sandbox herdado de `root_dir`.
        self.voice = voice if voice is not None else VoiceManager(
            emit=self._emit_voice,
            root_dir=self.root_dir,
            state_root=self.settings.state_root,
            session_id=self.session_id,
        )

        # VOICE INPUT V1: entrada ortogonal e separada do TTS. O manager só abre processo no
        # primeiro `start`; o `get` é barato e honesto quando o runtime opcional está ausente.
        self.voice_input = voice_input if voice_input is not None else VoiceInputManager(
            emit=self._emit_voice_input,
            state_root=self.settings.state_root,
            session_id=self.session_id,
            on_speech_interrupt=self.voice.stop,
            on_active_changed=self.voice.set_input_active,
            emit_level=self._emit_voice_input_level,
        )
        # Warmup de introdução: só prepara recursos; não fala nem abre microfone. O renderer
        # pede explicitamente `voice.warmup` no loading e espera `voice.readiness`/timeout.
        self.voice_warmup = VoiceWarmup(
            tts=self.voice,
            stt=self.voice_input,
            emit=self._emit_voice_warmup,
            skip=self._skip_voice_warmup,
        )
        # Voice models are downloaded after install, on the user's request (01/11).
        self.voice_models = VoiceModels(
            models_root(self.settings.state_root),
            manifest_for(voice_home(self.settings.state_root)),
            emit=self._emit_voice_models,
            legacy_dirs=older_voice_components,
        )
        self._voice_models_usable: frozenset[str] = frozenset()
        if older_voice_components():
            # An update from Alpha 4-9: verify and keep the models that install already had,
            # without downloading or asking. Nothing happens when there is nothing to adopt.
            for group in VOICE_MODEL_GROUPS:
                self.voice_models.download(group, network=False)

    def _emit_voice(self, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        self._emit("voice.state", dict(payload))

    def _emit_voice_input(self, payload: dict[str, Any]) -> None:
        # Sem texto/áudio: o manager já fecha o payload em DTO+session_id.
        if self._closed:
            return
        self._emit("voice.input.state", dict(payload))

    def _emit_voice_input_level(self, payload: dict[str, Any]) -> None:
        # Só {session_id, generation, level}: o medidor do microfone, nunca áudio.
        if self._closed:
            return
        self._emit("voice.input.level", dict(payload))

    def _emit_voice_models(self, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        self._emit("voice.models", {**payload, "session_id": self.session_id})
        raw_groups = payload.get("groups")
        groups: dict[str, Any] = raw_groups if isinstance(raw_groups, dict) else {}
        usable = frozenset(name for name, item in groups.items()
                           if isinstance(item, dict) and (item.get("state") == "installed" or item.get("previous")))
        if usable != getattr(self, "_voice_models_usable", frozenset()):
            # Review 21a75b0 P1-4: voice and dictation availability changed under the UI; publish
            # their canonical state (same DTOs, session and generation guards) so it unlocks
            # without a reload.
            self._voice_models_usable = usable
            for manager in (self.voice, self.voice_input):
                try:
                    manager.refresh()
                except Exception:  # noqa: BLE001 - a stale badge is better than a broken download
                    pass

    @staticmethod
    def _older_voice_components() -> list[Path]:
        return older_voice_components()

    def _handle_voice_models(self, cmd: str, payload: dict[str, Any]) -> dict[str, Any]:
        if cmd == "voice.models.get":
            if payload:
                raise protocol.ProtocolError("invalid_payload", "voice.models.get não aceita campos.")
            return self.voice_models.snapshot()
        group = payload.get("group")
        allowed = {"group", "repair"} if cmd == "voice.models.download" else {"group"}
        if set(payload) - allowed or group not in VOICE_MODEL_GROUPS or not isinstance(payload.get("repair", False), bool):
            raise protocol.ProtocolError("invalid_payload", "Indica group: speech ou dictation.")
        try:
            if cmd == "voice.models.download":
                return self.voice_models.download(str(group), repair=bool(payload.get("repair", False)))
            return self.voice_models.cancel(str(group))
        except ModelsError as exc:
            raise protocol.ProtocolError("unavailable", str(exc)) from None

    def _emit_voice_warmup(self, payload: dict[str, Any]) -> None:
        if self._closed:
            return
        self._emit("voice.readiness", {**payload, "session_id": self.session_id})

    @staticmethod
    def _voice_empty_payload(payload: dict[str, Any]) -> None:
        if payload:
            raise protocol.ProtocolError(
                "invalid_payload", "Este comando de voz não aceita campos."
            )

    def _handle_voice_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._voice_empty_payload(payload)
        return self.voice.snapshot()

    def _handle_voice_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        unknown = set(payload) - {"enabled", "auto_speak"}
        if unknown or not payload:
            raise protocol.ProtocolError(
                "invalid_payload", "Use apenas 'enabled' e/ou 'auto_speak' em voice.set."
            )
        for key in ("enabled", "auto_speak"):
            if key in payload and type(payload[key]) is not bool:
                raise protocol.ProtocolError(
                    "invalid_payload", f"Campo '{key}' deve ser booleano."
                )
        try:
            return self.voice.set(enabled=payload.get("enabled"),
                                  auto_speak=payload.get("auto_speak"))
        except VoiceError as exc:
            # Preferência que não grava não muda o estado: erro tipado e público.
            raise protocol.ProtocolError("io_error", str(exc)) from None

    def _handle_voice_test(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._voice_empty_payload(payload)
        return self.voice.test()

    def _handle_voice_speak_last(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._voice_empty_payload(payload)
        return self.voice.speak_last()

    def _handle_voice_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._voice_empty_payload(payload)
        return self.voice.stop()

    def _skip_voice_warmup(self, leg: str) -> bool:
        """A fala desligada não carrega nada; num PC fraco, o ditado espera pelo primeiro uso."""
        return not self.voice.snapshot()['enabled'] if leg == 'tts' else low_memory_pc()

    def _handle_voice_warmup(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._voice_empty_payload(payload)
        return self.voice_warmup.start()

    def _handle_voice_warmup_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._voice_empty_payload(payload)
        return self.voice_warmup.snapshot()

    @staticmethod
    def _voice_input_empty_payload(payload: dict[str, Any]) -> None:
        if payload:
            raise protocol.ProtocolError(
                "invalid_payload", "Este comando de ditado não aceita campos."
            )

    def _handle_voice_input_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._voice_input_empty_payload(payload)
        return self.voice_input.snapshot()

    def _handle_voice_input_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        mode = "manual"
        if payload:
            if set(payload) != {"mode"} or payload.get("mode") not in {"manual", "hold"}:
                raise protocol.ProtocolError(
                    "invalid_payload", "Use {} ou {'mode':'hold'} em voice.input.start."
                )
            mode = payload["mode"]
        return self.voice_input.start() if mode == "manual" else self.voice_input.start(mode="hold")

    @staticmethod
    def _voice_input_generation(payload: dict[str, Any], command: str) -> int:
        if set(payload) != {"generation"}:
            raise protocol.ProtocolError(
                "invalid_payload", f"Use exatamente 'generation' em {command}."
            )
        generation = payload.get("generation")
        if type(generation) is not int or generation < 0 or generation > 2_147_483_647:
            raise protocol.ProtocolError(
                "invalid_payload", "Campo 'generation' deve ser um inteiro válido."
            )
        return generation

    def _handle_voice_input_finish(self, payload: dict[str, Any]) -> dict[str, Any]:
        generation = self._voice_input_generation(payload, "voice.input.finish")
        return self.voice_input.finish(generation)

    def _handle_voice_input_cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._voice_input_empty_payload(payload)
        return self.voice_input.cancel()

    def _handle_voice_input_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        generation = self._voice_input_generation(payload, "voice.input.result")
        return self.voice_input.result(generation)

    def _observe_voice(self, result: TaskResult, *, attachments: bool,
                       verified_prefix: str, speech: Any = None) -> None:
        """Entrega a decisão de fala ao gestor; voz nunca altera o resultado da tarefa."""
        try:
            text = speakable_reply(
                message=result.message,
                status=result.status,
                scope=result.scope,
                verified_prefix=verified_prefix,
                attachments=attachments,
                evidence_count=len(result.evidence or []),
                operations=result.operation_states or (),
                web_outcome=result.web_outcome,
                web_sources=len(result.web_sources or []),
            )
            # Passed in: the response reference is cleared before this runs (measured 01/11:
            # the streamed answer was spoken twice, the second restart costing ~1.5 s).
            if speech is not None and speech.finish(text):
                return  # already spoken as it was written; `text` is what speak_last repeats
            self.voice.observe_reply(text)
        except Exception:
            return

    def _close_voice(self) -> None:
        try:
            self.voice_warmup.close()
        except Exception:
            pass
        try:
            # Ordem: microfone primeiro; depois o worker de fala. Fechar a entrada nunca pode
            # deixar um processo de captura a escrever depois do terminal da bridge.
            self.voice_input.close()
        except Exception:
            pass
        try:
            self.voice.close()
        except Exception:
            pass

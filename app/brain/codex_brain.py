"""Codex CLI as an agent runtime behind the Brain contract.

Used in *inference* mode only: the CLI answers text and never controls the
desktop. The UFO HostAgent/AppAgent remains the single agent that acts.
"""

from __future__ import annotations

from typing import Any
from collections.abc import AsyncIterator
from contextlib import aclosing

from app.brain.contracts import (
    BrainError,
    BrainEvent,
    BrainHealth,
    Capabilities,
    Capability,
    ErrorKind,
    EventKind,
    HealthStatus,
    ModelInfo,
    events_for,
)
from app.brain.cli_web import web_runtime
from app.brain.codex_options import load_codex_options
from app.codex_app_server import CodexAppServer, CodexError, codex_executable
from app.llm import Completion
from app.settings import Settings


def translate(exc: BaseException) -> BrainError:
    """Map a Codex failure onto the application's own error vocabulary."""
    if isinstance(exc, BrainError):
        return exc
    if isinstance(exc, TimeoutError):
        return BrainError(ErrorKind.TIMEOUT, "O Codex não respondeu dentro do tempo limite.", detail=str(exc))
    if isinstance(exc, CodexError):
        code = getattr(exc, "code", None)
        detail = getattr(exc, "detail", "") or str(exc)
        if code == "configuration":
            return BrainError(ErrorKind.CONFIGURATION, str(exc))
        if code == "usageLimitExceeded":
            return BrainError(ErrorKind.QUOTA_EXCEEDED,
                              "Limite de uso da conta Codex atingido. Tente novamente quando a cota liberar.",
                              detail=detail)
        if code in {"unauthorized", "notLoggedIn"}:
            return BrainError(ErrorKind.AUTH_REQUIRED,
                              "Entre com a conta ChatGPT no login oficial do Codex.", detail=detail)
        message = str(exc)
        if "não encontrado" in message:
            return BrainError(ErrorKind.EXECUTABLE_MISSING,
                              "Codex CLI não encontrado. Instale o Codex ou inclua codex.exe no PATH.",
                              detail=message)
        if code == "transport":
            # The app-server's own channel failed: nothing was executed, and the cause is the
            # transport, not the user's configuration (see CODEX_LINE_LIMIT_BYTES).
            return BrainError(ErrorKind.TRANSPORT, message, detail=detail)
        if "encerrou a conexão" in message or "indisponível" in message:
            return BrainError(ErrorKind.PROVIDER_UNAVAILABLE, message, detail=message)
        if "Login não concluído" in message or "login oficial" in message:
            return BrainError(ErrorKind.AUTH_REQUIRED, message, detail=message)
        return BrainError(ErrorKind.UNKNOWN, message, detail=detail)
    return BrainError(ErrorKind.UNKNOWN, str(exc), detail=type(exc).__name__)


class CodexBrain:
    """Inference bridge to the official Codex app-server (JSON-RPC)."""

    provider_id = "codex"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = settings.model
        self._client = CodexAppServer()
        self.call_metrics = self._client.call_metrics
        executable = codex_executable()
        self._web = web_runtime('codex', settings, str(executable)) if executable else None
        # No SHELL/TOOL_CALLS/WORKING_DIRECTORY: in this mode the CLI must not act.
        self.capabilities = Capabilities.of(
            Capability.SYSTEM_PROMPTS,
            Capability.STRUCTURED_OUTPUT,
            Capability.USAGE,
            Capability.MODEL_DISCOVERY,
            Capability.INTERACTIVE_LOGIN,
            Capability.SESSIONS,
            Capability.CANCELLATION,
            Capability.STREAMING,
        )

    def __getattr__(self, name: str) -> Any:
        # A superfície nativa só existe com o executável: `can_complete_web` vê a ausência.
        if name in {'native_web_capability', 'complete_web', 'cancel_web'} and self._web is not None:
            return {'native_web_capability': self._web.capability, 'complete_web': self._web.complete,
                    'cancel_web': self._web.cancel_web}[name]
        raise AttributeError(name)

    def _turn(self) -> dict[str, Any]:
        options = load_codex_options(self.settings.state_root)
        supported = self._client._model_efforts.get(self.model)
        # Trocar de modelo pode deixar um nível que ele não tem: fica o padrão, sem erro.
        chosen = options.effort if supported is None or options.effort in supported else ""
        return {"effort": chosen or self.settings.effort, **({"fast": True} if options.fast else {})}

    async def complete(self, messages: list[dict[str, Any]]) -> Completion:
        try:
            turn = self._turn()
            if turn["effort"] or "fast" in turn:
                completion = None
                async for value in self._client.stream(self.model, messages, **turn):
                    if isinstance(value, Completion):
                        completion = value
                if completion is None:
                    raise BrainError(ErrorKind.MALFORMED_RESPONSE, "O Codex não concluiu a resposta.")
                return completion
            return await self._client.complete(self.model, messages)
        except BaseException as exc:  # noqa: BLE001 - normalized below, then re-raised as BrainError
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, Exception):
                raise translate(exc) from exc
            raise

    def events(self, completion: Completion) -> list[BrainEvent]:
        return events_for(self.provider_id, completion)

    async def stream(self, messages: list[dict[str, Any]]) -> AsyncIterator[BrainEvent]:
        yield BrainEvent(EventKind.STARTED, {"provider": self.provider_id, "streaming": True})
        try:
            completion = None
            async with aclosing(self._client.stream(self.model, messages, **self._turn())) as events:
                async for value in events:
                    if isinstance(value, str):
                        yield BrainEvent(EventKind.TEXT_DELTA, {"text": value})
                    else:
                        completion = value
            if completion is None:
                raise BrainError(ErrorKind.MALFORMED_RESPONSE, "O Codex não concluiu a resposta.")
            for event in self.events(completion)[1:]:
                yield event
        except Exception as exc:
            raise translate(exc) from exc

    async def login(self) -> list[ModelInfo]:
        """Open the vendor's official login when the account is not connected yet."""
        try:
            connection = await self._client.connect()
        except Exception as exc:
            raise translate(exc) from exc
        return [ModelInfo(identifier, label, default) for identifier, label, default in connection.models]

    async def models(self) -> list[ModelInfo]:
        try:
            entries = await self._client.list_models()
        except Exception as exc:
            raise translate(exc) from exc
        return [ModelInfo(identifier, label, default, tuple(self._client._model_efforts.get(identifier, ())),
                          identifier in self._client._fast_models)
                for identifier, label, default in entries]

    async def health(self) -> BrainHealth:
        if codex_executable() is None:
            return BrainHealth(HealthStatus.NOT_INSTALLED,
                               "Codex CLI não encontrado. Instale o Codex ou inclua codex.exe no PATH.")
        try:
            account = await self._client.account()
        except Exception as exc:
            translated = translate(exc)
            if translated.kind is ErrorKind.EXECUTABLE_MISSING:
                return BrainHealth(HealthStatus.NOT_INSTALLED, translated.message)
            return BrainHealth(HealthStatus.UNAVAILABLE, translated.message)
        if account.get("type") != "chatgpt":
            return BrainHealth(HealthStatus.LOGIN_REQUIRED,
                               "Conecte a conta ChatGPT no login oficial do Codex.")
        return BrainHealth(HealthStatus.READY, f"Conta {account.get('planType') or 'ChatGPT'} pronta.")

    async def close(self) -> None:
        if self._web is not None:
            await self._web.close()
        await self._client.close()

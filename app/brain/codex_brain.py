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

    async def complete(self, messages: list[dict[str, Any]]) -> Completion:
        try:
            if self.settings.effort:
                completion = None
                async for value in self._client.stream(self.model, messages, effort=self.settings.effort):
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
            async with aclosing(self._client.stream(self.model, messages, effort=self.settings.effort)) as events:
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
        return [ModelInfo(identifier, label, default) for identifier, label, default in entries]

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
        await self._client.close()

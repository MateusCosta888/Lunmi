"""Provider-neutral contracts for the AI Brain layer.

The rest of the application talks to a ``Brain`` and never to a concrete
provider, transport or vendor. Provider-specific knowledge stays inside the
adapters that implement this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from app.llm import Completion


class ErrorKind(str, Enum):
    """Normalized failure categories, independent of the underlying provider."""

    CONFIGURATION = "configuration"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    EXECUTABLE_MISSING = "executable_missing"
    AUTH_REQUIRED = "auth_required"
    INVALID_CREDENTIALS = "invalid_credentials"
    MODEL_UNAVAILABLE = "model_unavailable"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    TIMEOUT = "timeout"
    PROCESS_CRASHED = "process_crashed"
    MALFORMED_RESPONSE = "malformed_response"
    # The request itself is too large for the transport (an argv-only CLI on a
    # Windows command line, for example) — not a missing file or a bad model.
    REQUEST_TOO_LARGE = "request_too_large"
    # The reader could not frame a message the CLI sent (a line past the explicit
    # transport bound, a stream that ended mid-message, a pipe reset). It is neither a
    # UIA limitation nor a bad model: measured 2026-10-09, asyncio's default 64 KiB
    # stream limit raised "Separator is found, but chunk is longer than limit" and that
    # raw sentence was reported to the user as the task's result.
    TRANSPORT = "transport"
    UNSUPPORTED_FEATURE = "unsupported_feature"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class BrainError(RuntimeError):
    """A provider failure described in the application's own vocabulary.

    ``message`` is what a user may read. ``detail`` keeps the raw provider text
    for logs only and must never be shown verbatim or written to telemetry.
    """

    def __init__(self, kind: ErrorKind, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail


class Capability(str, Enum):
    """What a provider can actually do. Never assume; ask."""

    STREAMING = "streaming"
    IMAGES = "images"
    FILES = "files"
    TOOL_CALLS = "tool_calls"
    REASONING = "reasoning"
    SESSIONS = "sessions"
    RESUME = "resume"
    CANCELLATION = "cancellation"
    MODEL_DISCOVERY = "model_discovery"
    INTERACTIVE_LOGIN = "interactive_login"
    USAGE = "usage"
    COST = "cost"
    STRUCTURED_OUTPUT = "structured_output"
    SYSTEM_PROMPTS = "system_prompts"
    WORKING_DIRECTORY = "working_directory"
    SHELL = "shell"


@dataclass(frozen=True)
class Capabilities:
    values: frozenset[Capability] = field(default_factory=frozenset)
    # Advisory text budget; the adapter also checks its actual encoded transport.
    max_safe_prompt_chars: int | None = None

    @classmethod
    def of(cls, *items: Capability, max_safe_prompt_chars: int | None = None) -> Capabilities:
        return cls(frozenset(items), max_safe_prompt_chars=max_safe_prompt_chars)

    def supports(self, capability: Capability) -> bool:
        return capability in self.values

    def __contains__(self, capability: object) -> bool:
        return capability in self.values


class EventKind(str, Enum):
    """A normalized stream event. Providers fill in what they actually expose."""

    STARTED = "started"
    TEXT_DELTA = "text_delta"
    TEXT_COMPLETED = "text_completed"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    FILE_OPERATION = "file_operation"
    COMMAND = "command"
    PROGRESS = "progress"
    USAGE = "usage"
    COST = "cost"
    MODEL = "model"
    SESSION = "session"
    WARNING = "warning"
    ERROR = "error"
    AUTH_ERROR = "auth_error"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


@dataclass(frozen=True)
class BrainEvent:
    kind: EventKind
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Usage:
    """Token accounting. Unavailable fields stay ``None`` instead of being invented."""

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    @classmethod
    def of_completion(cls, completion: Completion) -> Usage:
        return cls(input_tokens=completion.input_tokens, output_tokens=completion.output_tokens)


@dataclass(frozen=True)
class SessionRef:
    """A conversation handle. Cloud APIs are tracked here; CLIs may own their own."""

    provider: str
    runtime: str
    model: str = ""
    session_id: str | None = None
    working_directory: str | None = None
    status: str = "active"


class HealthStatus(str, Enum):
    READY = "ready"
    LOGIN_REQUIRED = "login_required"
    NOT_INSTALLED = "not_installed"
    MISCONFIGURED = "misconfigured"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class BrainHealth:
    status: HealthStatus
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is HealthStatus.READY


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str = ""
    default: bool = False


class Brain(Protocol):
    """The single abstraction the application depends on."""

    provider_id: str
    capabilities: Capabilities

    async def complete(self, messages: list[dict[str, Any]]) -> Completion:
        """Answer with text. The caller owns planning, tools and desktop control."""

    def events(self, completion: Completion) -> list[BrainEvent]:
        """Normalize a finished response into stream events (text + usage)."""

    async def health(self) -> BrainHealth:
        """Report readiness without starting an interactive login."""

    async def models(self) -> list[ModelInfo]:
        """Enumerate models when the provider supports it; otherwise return ``[]``."""

    async def close(self) -> None:
        """Release processes, sockets and sessions owned by this provider."""


def events_for(provider_id: str, completion: Completion) -> list[BrainEvent]:
    """Shared normalization: a non-streaming reply becomes start/text/usage/complete."""
    events = [BrainEvent(EventKind.STARTED, {"provider": provider_id})]
    if completion.text:
        events.append(BrainEvent(EventKind.TEXT_COMPLETED, {"text": completion.text}))
    usage = Usage.of_completion(completion)
    if usage.total_tokens is not None:
        events.append(BrainEvent(EventKind.USAGE, {
            "input_tokens": usage.input_tokens,
            "cached_input_tokens": usage.cached_input_tokens,
            "output_tokens": usage.output_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "total_tokens": usage.total_tokens,
            "cost_usd": usage.cost_usd,
        }))
    events.append(BrainEvent(EventKind.COMPLETED, {}))
    return events

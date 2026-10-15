"""Declarative description of a local CLI agent, so adding one is data, not code."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.brain.contracts import BrainEvent, Capabilities


@dataclass(frozen=True)
class CliSpec:
    """Everything the generic adapter needs to drive one CLI.

    A new CLI normally means one more entry in ``catalog.py`` — no changes to
    the adapter, the registry or the application.
    """

    id: str
    label: str
    executable: str
    # Where to look when the CLI is not on PATH (vendor install dirs).
    search_paths: tuple[str, ...] = ()
    version_args: tuple[str, ...] = ("--version",)
    # Base arguments for one non-interactive inference turn.
    inference_args: tuple[str, ...] = ()
    # Prefix used to pass a model, e.g. ("--model",). Empty means "CLI decides".
    model_args: tuple[str, ...] = ()
    # FASE 10: prefix of the flag that lowers or raises the *reasoning effort* of one
    # turn, e.g. ("--effort",) for AGY and ("--variant",) for Kilo. Empty means this
    # CLI has no such flag, and Settings.effort is then ignored rather than guessed at.
    # Effort is a latency lever measured at ~30x between the extremes (AGY's first
    # listed model ran 2.4-21.9k output tokens and finished 0/10 tasks; the low-effort
    # one ran 0.2-0.8k and finished 7/10), which is why it is configuration and not a
    # decision taken inside the adapter.
    effort_args: tuple[str, ...] = ()
    # Accepted values, when the CLI documents a closed set. Empty means "any value".
    effort_values: tuple[str, ...] = ()
    # Curated default model, when the CLI's own first listing is not a good brain.
    # Recommendation only: the user can still choose any discovered model.
    preferred_model: str = ""
    # Flags that must sit immediately before the prompt, e.g. ("-p",) for CLIs
    # whose print flag consumes the following argument.
    prompt_args: tuple[str, ...] = ()
    # Cheap command that reveals the authentication state without inference.
    auth_probe_args: tuple[str, ...] = ()
    auth_probe_timeout: float = 25.0
    # When this environment variable is set, add --bare: the CLI then uses the
    # user's own key instead of a keychain it cannot reach headlessly.
    bare_with_env: str | None = None
    # Arguments that enumerate models for `list_models`.
    models_args: tuple[str, ...] = ()
    # How model ids are read out of the models command.
    models_parser: str = "lines-first-column"
    # How one JSON reply is turned into text + usage.
    reply_parser: str = "text-field"
    # Send the prompt on stdin instead of argv (keeps large prompts off the cmdline).
    prompt_on_stdin: bool = True
    # FASE 4: arguments that start a *persistent session* — one process answering several
    # prompts during one task, instead of one process per complete(). Empty (the default,
    # and every catalogued CLI today) keeps the one-shot behaviour. A spec opts in only
    # once a real CLI is verified to accept more than one prompt on the same stdin, so the
    # mechanism never invents an interactive protocol for a CLI that does not have one.
    session_args: tuple[str, ...] = ()
    # The marker that ends one turn in the session stream: reading stops on the first line
    # containing it. Only meaningful together with ``session_args``.
    session_end: str = ""
    # Some CLIs stream one JSON event per line instead of printing a single reply
    # object. The named parser then receives ``{"events": [...]}``, because the
    # answer and the token usage arrive in different events.
    json_reply_stream: bool = False
    # Extra time allowed for a cold start (Node CLIs are slower than native ones).
    startup_timeout: float = 60.0
    capabilities: Capabilities = field(default_factory=Capabilities)
    notes: str = ""


ReplyParser = Callable[[dict[str, Any]], tuple[str, tuple[int | None, int | None]]]
ModelsParser = Callable[[str], list[tuple[str, str]]]

# Parsers are looked up by name so a spec stays pure data.
REPLY_PARSERS: dict[str, ReplyParser] = {}
MODEL_PARSERS: dict[str, ModelsParser] = {}


def reply_parser(name: str) -> Callable[[ReplyParser], ReplyParser]:
    """Register a reply parser under the name a ``CliSpec`` refers to."""
    def decorator(function: ReplyParser) -> ReplyParser:
        REPLY_PARSERS[name] = function
        return function
    return decorator


def models_parser(name: str) -> Callable[[ModelsParser], ModelsParser]:
    """Register a model-list parser under the name a ``CliSpec`` refers to."""
    def decorator(function: ModelsParser) -> ModelsParser:
        MODEL_PARSERS[name] = function
        return function
    return decorator


def text_from(events: list[BrainEvent]) -> str:
    """Join the textual part of a normalized event stream."""
    return "".join(event.data.get("text", "") for event in events
                   if event.kind.value in {"text_delta", "text_completed"})

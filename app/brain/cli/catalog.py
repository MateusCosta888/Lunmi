"""Declarative catalog of the local CLI agents this application can drive.

Everything here is data plus tolerant parsers: adding another CLI is one more
``CliSpec`` entry, with no change to the adapter, the registry or the app.

Every parser assumes as little as possible about the CLI's output shape, because
these tools change between versions (spec §32: malformed, truncated or
decorated output must never crash the application).
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.brain.cli.spec import CliSpec, models_parser, reply_parser
from app.brain.contracts import Capabilities, Capability

# Text may live under any of these keys, depending on the CLI and its version.
_TEXT_KEYS = ("result", "text", "content", "message", "response", "output")
# Token usage travels inside one of these containers when it exists at all.
_USAGE_CONTAINERS = ("usage", "usageMetadata", "usage_metadata", "token_usage", "tokenUsage")
_INPUT_KEYS = ("input_tokens", "prompt_tokens", "inputTokens", "promptTokens", "promptTokenCount")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "outputTokens", "completionTokens",
                "candidatesTokenCount")
# Decorative or heading lines that are never a model id.
_NOISE_PREFIXES = ("fetching", "using", "usage", "available", "models", "model",
                   "listing", "default", "id", "name")
_SEPARATORS = set("-=_*·•─ \t")


def _as_int(value: Any) -> int | None:
    """Accept an int or a numeric string; never guess or invent a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _first_number(source: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key in source:
            number = _as_int(source[key])
            if number is not None:
                return number
    return None


def _text_of(value: Any) -> str:
    """Pull human-readable text out of any of the shapes these CLIs emit."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text_of(item) for item in value)
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            if key in value:
                text = _text_of(value[key])
                if text:
                    return text
    return ""


def _usage_numbers(source: dict) -> tuple[int | None, int | None]:
    return _first_number(source, _INPUT_KEYS), _first_number(source, _OUTPUT_KEYS)


def _usage_of(value: Any, depth: int = 0) -> tuple[int | None, int | None]:
    """Find (input_tokens, output_tokens) in a nested reply, or (None, None)."""
    if depth > 2 or not isinstance(value, dict):
        return None, None
    for container in _USAGE_CONTAINERS:
        nested = value.get(container)
        if isinstance(nested, dict):
            numbers = _usage_numbers(nested)
            if numbers != (None, None):
                return numbers
    numbers = _usage_numbers(value)
    if numbers != (None, None):
        return numbers
    for key in ("result", "message", "data", "response"):
        child = value.get(key)
        if isinstance(child, dict):
            numbers = _usage_of(child, depth + 1)
            if numbers != (None, None):
                return numbers
    return None, None


def _reply(payload: dict) -> tuple[str, tuple[int | None, int | None]]:
    return _text_of(payload), _usage_of(payload)


@reply_parser("claude-json")
def parse_claude_reply(payload: dict) -> tuple[str, tuple[int | None, int | None]]:
    """Claude Code ``--print --output-format json``.

    Shape today: ``{"type": "result", "result": "...", "usage": {...}}``.
    Older/newer versions may rename the keys, so extraction stays tolerant.
    """
    return _reply(payload)


@reply_parser("agy-json")
def parse_agy_reply(payload: dict) -> tuple[str, tuple[int | None, int | None]]:
    """AGY ``--print --output-format json``; same family of output as Claude Code."""
    return _reply(payload)


@reply_parser("qoder-json")
def parse_qoder_reply(payload: dict) -> tuple[str, tuple[int | None, int | None]]:
    """Qoder ``--print -o json``; same family of output as Claude Code."""
    return _reply(payload)


@reply_parser("kilo-json")
def parse_kilo_reply(payload: dict) -> tuple[str, tuple[int | None, int | None]]:
    """Kilo ``run --format json``: an event stream, not a single reply object.

    Shape today (7.8.8): the answer is one ``{"type": "text", "part": {"text": ...}}``
    event and the usage is a later ``{"type": "step_finish", "part": {"tokens": ...}}``
    one, which is why the adapter hands every event over together. An ``error``
    event with no text at all is a failure, never an empty answer: an empty reply
    that looks like success is exactly what the failure vocabulary exists to stop.
    """
    events = payload.get("events")
    if not isinstance(events, list):
        # Tolerant: a version that prints one object still parses.
        return _reply(payload)
    texts: list[str] = []
    input_tokens: int | None = None
    output_tokens: int | None = None
    errors: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        part = event.get("part")
        if isinstance(part, dict):
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                value = _as_int(tokens.get("input"))
                if value is not None:
                    input_tokens = value
                value = _as_int(tokens.get("output"))
                if value is not None:
                    output_tokens = value
        error = event.get("error")
        if isinstance(error, dict):
            data = error.get("data")
            message = data.get("message") if isinstance(data, dict) else None
            errors.append(str(message or error.get("name") or error))
    text = "".join(texts)
    if not text and errors:
        # The vocabulary lives in the adapter; imported here so the parser stays
        # one entry of data-free code and cannot be misclassified as success.
        from app.brain.cli.headless import classify_failure
        raise classify_failure(" ".join(errors))
    return text, (input_tokens, output_tokens)


@models_parser("kiro-models")
def parse_kiro_models(stdout: str) -> list[tuple[str, str]]:
    """Kiro ``chat --list-models --format json``: one JSON document, not rows.

    Shape today (2.28.0): ``{"models": [{"model_id": ..., "model_name": ...,
    "description": ..., "rate_multiplier": ...}], "default_model": "auto"}``.
    A version that changes to columns still parses through the tolerant fallback.
    """
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return _catalog_models(stdout)
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return _catalog_models(stdout)
    models: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("model_id") or entry.get("model_name")
        if not isinstance(identifier, str) or not identifier.strip():
            continue
        identifier = identifier.strip()
        if identifier in seen:
            continue
        seen.add(identifier)
        label = entry.get("model_name")
        label = label.strip() if isinstance(label, str) and label.strip() else identifier
        models.append((identifier, label))
    return models


def _model_from_json_line(line: str) -> tuple[str, str] | None:
    try:
        parsed = json.loads(line)
    except (ValueError, TypeError):
        return None
    candidates = parsed if isinstance(parsed, list) else [parsed]
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        identifier = next((entry[key] for key in ("id", "model", "name", "slug")
                           if isinstance(entry.get(key), str) and entry[key].strip()), None)
        if identifier is None:
            continue
        label = next((entry[key] for key in ("label", "displayName", "display_name", "title")
                      if isinstance(entry.get(key), str) and entry[key].strip()), identifier)
        return identifier.strip(), str(label).strip()
    return None


def _looks_like_identifier(token: str) -> bool:
    if len(token) < 2 or token.casefold() in _NOISE_PREFIXES:
        return False
    # `~` is part of a model id, not decoration: Kilo lists its "latest" aliases as
    # `kilo/~anthropic/claude-fable-latest` and that exact string is what `-m`
    # accepts, so filtering the tilde out silently hid 17 usable models.
    return all(character.isalnum() or character in "-._/:@~" for character in token)


def _model_from_text_line(line: str) -> tuple[str, str] | None:
    if set(line) <= _SEPARATORS or line.casefold().startswith(_NOISE_PREFIXES):
        return None
    parts = [part.strip() for part in re.split(r"\t+|\s{2,}", line) if part.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        parts = line.split(None, 1)
        if len(parts) == 1:
            parts.append("")
    identifier = parts[0].strip()
    if not _looks_like_identifier(identifier):
        return None
    label = " ".join(part for part in parts[1:] if part).strip() or identifier
    return identifier, label


def _catalog_models(stdout: str) -> list[tuple[str, str]]:
    """Read model rows from tab/space columns or JSON lines, skipping noise."""
    models: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in stdout.splitlines():
        if not raw.strip() or raw[:1].isspace():
            # Indented lines are detail rows, not model entries.
            continue
        line = raw.strip()
        entry = _model_from_json_line(line) or _model_from_text_line(line)
        if entry is None:
            continue
        identifier, label = entry
        if identifier not in seen:
            seen.add(identifier)
            models.append((identifier, label))
    return models


@models_parser("agy-models")
def parse_agy_models(stdout: str) -> list[tuple[str, str]]:
    """AGY ``models`` prints ``id<TAB>Label`` rows under a fetching header."""
    return _catalog_models(stdout)


@models_parser("qoder-models")
def parse_qoder_models(stdout: str) -> list[tuple[str, str]]:
    """Qoder ``--list-models``; column or JSON style, whichever the version uses."""
    return _catalog_models(stdout)


@reply_parser("kiro-json")
def parse_kiro_reply(payload: dict) -> tuple[str, tuple[int | None, int | None]]:
    """Kiro ``chat --output-format stream-json``: ACP events, one JSON per line.

    Shape today (2.28.0): the answer arrives as ``agent_message_chunk`` updates and is
    restated whole in the closing ``{"type": "runFinished", "data": {"status":
    "success", "finalText": ...}}``, which is also the only place that says whether
    the turn succeeded. A run that ends in any other status is a failure, never an
    empty answer. Token usage is metered in credits, not tokens, so it stays ``None``
    rather than being invented from a percentage.
    """
    events = payload.get("events")
    if not isinstance(events, list):
        # A version that prints one object still parses.
        return _reply(payload)
    chunks: list[str] = []
    final_text = ""
    status = ""
    errors: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        if kind == "runFinished":
            status = str(data.get("status") or "")
            text = data.get("finalText")
            if isinstance(text, str):
                final_text = text
            for key in ("error", "failure"):
                problem = data.get(key)
                if problem:
                    errors.append(str(problem) if isinstance(problem, str) else _text_of(problem))
        elif kind == "sessionUpdate":
            update = data.get("update")
            if not isinstance(update, dict) or update.get("sessionUpdate") != "agent_message_chunk":
                continue
            content = update.get("content")
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                chunks.append(content["text"])
        elif kind == "error":
            errors.append(_text_of(data) or str(data))
    text = final_text or "".join(chunks)
    if status and status.casefold() not in {"success", "ok", "end_turn"}:
        from app.brain.cli.headless import classify_failure
        raise classify_failure(" ".join(errors) or status)
    if not text and errors:
        from app.brain.cli.headless import classify_failure
        raise classify_failure(" ".join(errors))
    return text, (None, None)


@models_parser("kilo-models")
def parse_kilo_models(stdout: str) -> list[tuple[str, str]]:
    """Kilo ``models`` prints one ``provider/model`` id per line, with no labels."""
    return _catalog_models(stdout)


@models_parser("lines-first-column")
def parse_lines_first_column(stdout: str) -> list[tuple[str, str]]:
    """Default parser named by ``CliSpec.models_parser`` for future CLIs."""
    return _catalog_models(stdout)


# Inference-only capabilities: a CLI used as a brain must never be advertised as
# able to run shell commands or call tools (spec §27). The UFO agent acts; the
# CLI only answers.
_INFERENCE_CAPABILITIES = (
    Capability.SYSTEM_PROMPTS,
    Capability.STRUCTURED_OUTPUT,
    Capability.USAGE,
    Capability.SESSIONS,
    Capability.CANCELLATION,
)


def _capabilities(*extra: Capability, max_safe_prompt_chars: int | None = None) -> Capabilities:
    return Capabilities.of(*_INFERENCE_CAPABILITIES, *extra,
                           max_safe_prompt_chars=max_safe_prompt_chars)


CLI_SPECS: dict[str, CliSpec] = {
    "claude": CliSpec(
        id="claude",
        label="Claude Code",
        executable="claude",
        search_paths=("~/.local/bin",),
        # --bare is required: without it the CLI blocks on a keychain login it
        # cannot reach headlessly. With it, a missing key fails fast instead.
        inference_args=("--bare", "-p", "--output-format", "json", "--allowedTools", "",
                        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'),
        model_args=("--model",),
        models_args=(),
        # Without --bare the CLI blocks waiting for a keychain login it cannot
        # reach headlessly; with --bare it fails fast and tells us why.
        auth_probe_args=("--bare", "-p", "ping"),
        reply_parser="claude-json",
        capabilities=_capabilities(),
        notes="Sem enumeração de modelos: use --model ou deixe o CLI escolher.",
    ),
    "agy": CliSpec(
        id="agy",
        label="AGY",
        executable="agy",
        search_paths=("%LOCALAPPDATA%/agy/bin",),
        # AGY's --print consumes the NEXT argument as the prompt, so every other
        # flag must come first and the prompt must be last (verified on 1.3.0).
        inference_args=("--output-format", "json", "--sandbox"),
        prompt_args=("-p",),
        prompt_on_stdin=False,
        model_args=("--model",),
        # Measured on 1.3.0: `--effort low|medium|high|xhigh|max`. The curated default
        # model already carries the low end of this scale; the flag makes it explicit and
        # is what BRAIN_EFFORT=low uses.
        effort_args=("--effort",),
        effort_values=("low", "medium", "high", "xhigh", "max"),
        models_args=("models",),
        models_parser="agy-models",
        reply_parser="agy-json",
        # `agy models` prints the highest-effort model first, which is not a
        # recommendation: driving the UFO host with it produced 0 actions and
        # NOT_VERIFIED on every task, while the low-effort model completed real
        # tasks. Keep that as the offered default; the user may pick any model.
        preferred_model="gemini-3.8-flash-low",
        capabilities=_capabilities(Capability.MODEL_DISCOVERY, max_safe_prompt_chars=28_000),
    ),
    "qoder": CliSpec(
        id="qoder",
        label="Qoder CLI",
        executable="qodercli",
        search_paths=("~/.qoder/bin/qodercli", "~/.qoder/entry"),
        inference_args=("-p", "-o", "json", "--tools", ""),
        model_args=("-m",),
        models_args=("--list-models",),
        models_parser="qoder-models",
        reply_parser="qoder-json",
        # Same trap as AGY: the first listed model (Qwen3.8-Max) answers with
        # "credit usage limit" while Qwen3.8-Flash completes real work, as
        # verified against the real account. Curated default, not a restriction.
        preferred_model="Qwen3.8-Flash",
        capabilities=_capabilities(Capability.MODEL_DISCOVERY),
    ),
    "kilo": CliSpec(
        id="kilo",
        label="Kilo CLI",
        executable="kilo",
        # npm's global bin, where this CLI installs itself on Windows.
        search_paths=("~/AppData/Roaming/npm",),
        # `run` is the headless entry point; `--agent plan` is what keeps a coding
        # agent from touching the machine: it is the only agent whose `edit`
        # permission is `deny` (verified against 7.8.8 with `kilo agent list`),
        # the same role qoder's `--tools ""` plays. Never add `--auto` here: that
        # flag is the CLI's auto-approval switch, not ours to give.
        inference_args=("run", "--format", "json", "--agent", "plan"),
        model_args=("-m",),
        # Measured on 7.8.8: `--variant` is "model variant (provider-specific reasoning
        # effort, e.g., high, max, minimal)", so the set of values is the provider's, not
        # this catalog's — hence no effort_values here.
        effort_args=("--variant",),
        models_args=("models",),
        models_parser="kilo-models",
        reply_parser="kilo-json",
        json_reply_stream=True,
        # The free auto-router, not the first listed model: it answered at zero
        # cost while the metered models are what a listing puts first, the same
        # trap as AGY and Qoder. The `-m` value is the full listing id
        # ("provider/model"), not the bare suffix. Routed calls were served by
        # nvidia/nemotron-3-ultra-550b-a55b:free.
        preferred_model="kilo/kilo-auto/free",
        capabilities=_capabilities(Capability.MODEL_DISCOVERY),
        notes="O agente `plan` é somente leitura. O CLI acrescenta um prompt próprio "
              "de ~22 mil tokens por chamada, contabilizados no custo, não no pedido.",
    ),
    "kiro": CliSpec(
        id="kiro",
        label="Kiro CLI",
        executable="kiro-cli",
        # Where the Windows installer puts it when the directory is not on PATH.
        search_paths=("%LOCALAPPDATA%/Kiro-Cli",),
        # `chat --output-format stream-json` implies --no-interactive and is the only
        # mode that states whether the turn succeeded. `--trust-tools=` (empty) trusts
        # no tool at all: verified against 2.28.0 `chat --help`, which documents
        # "trust no tools" for exactly that value. `--trust-all-tools` is never used.
        inference_args=("chat", "--no-interactive", "--output-format", "stream-json",
                        "--trust-tools="),
        model_args=("--model",),
        effort_args=("--effort",),
        effort_values=("low", "medium", "high", "xhigh", "max"),
        models_args=("chat", "--list-models", "--format", "json"),
        models_parser="kiro-models",
        reply_parser="kiro-json",
        json_reply_stream=True,
        auth_probe_args=("whoami",),
        # The CLI's own default_model is `auto`; keep it as the offered default and
        # never as a restriction — every discovered model stays selectable.
        preferred_model="auto",
        # A single 430 MB native binary: measure the cold start generously, but the
        # per-turn wait is still governed by Settings.timeout_seconds.
        startup_timeout=90.0,
        capabilities=_capabilities(Capability.MODEL_DISCOVERY),
        notes="Ferramentas próprias desabilitadas (--trust-tools=). O prompt vai por stdin; "
              "o uso é medido em créditos, então nenhum token é inventado.",
    ),
}


def specs() -> list[CliSpec]:
    """All known CLI runtimes, in catalog order."""
    return list(CLI_SPECS.values())


def find_spec(cli_id: str) -> CliSpec | None:
    """Return the spec for one CLI id, or ``None`` when it is not catalogued."""
    return CLI_SPECS.get(cli_id)

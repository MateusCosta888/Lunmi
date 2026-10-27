"""Native web runtime for Kiro CLI 2.34.0 (V3 engine).

The adapter is deliberately narrow: it owns two private agents under the application's
state root, uses the V3 engine, and treats the PreToolUse hook receipt as the canonical
proof of which web tool actually ran. V2 hook objects are not evidence; V3 ``title`` and
``toolCallId`` are presentation, not identity. A turn without a receipt for a web tool is
not allowed to contribute a source.

No global Kiro configuration is read or written. No credential, page text or query is
logged. The only network calls are the ones Kiro itself makes after the hook allows them.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.brain.cli.process import CliProcess, spawn
from app.brain.cli.spec import CliSpec
from app.brain.contracts import BrainError, ErrorKind
from app.brain.kiro_web_hook import url_syntax_problem
from app.brain.web import (
    NativeWebCapability,
    NativeWebOutcome,
    NativeWebResult,
    NativeWebSource,
    NativeWebState,
)
from app.settings import Settings

VERIFIED_VERSION = "2.34.0"
OFF_AGENT = "lunmi-off-v3"
WEB_AGENT = "lunmi-web-v3"
WEB_TOOLS = ("web_search", "web_fetch")
MAX_WEB_STDOUT_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
MAX_SOURCES = 5
# Finite bound for one owned turn's teardown. `complete_web` returns only after this,
# and `cancel_web` uses the same bound: a fallback may never start while the child lives.
MAX_STOP_SECONDS = 10.0
PROFILE_PROMPT = "Follow the current user request. Never use tools that are not available."
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_VERSION_PATTERN = re.compile(r"\d+\.\d+(?:\.\d+)?")


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _has_observed_body(content: Any) -> bool:
    """True only when a fetched payload carries observed text blocks."""
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        inner = block.get("content")
        text = inner.get("text") if isinstance(inner, dict) else block.get("text")
        if isinstance(text, str) and text.strip():
            return True
    return False


# The V3 fetch message is the exact envelope observed in the live probe:
#   Fetched content from: <url>\nSize: <n> bytes\n\nContent:\n---\n<body>
# Selective reads add the observed Mode header; URL and body remain mandatory.
# A message without that envelope (for example "Fetch failed") is not body.
_FETCH_MESSAGE = re.compile(
    r"Fetched content from: (?P<url>[^\r\n]+)\r?\n"
    r"Size: [0-9]+ bytes\r?\n(?:Mode: Selective \([0-9]{1,3} matches found\)\r?\n)?"
    r"\r?\nContent:\r?\n---\r?\n(?P<body>.+)",
    re.DOTALL,
)


def _message_has_observed_body(message: Any, url: str) -> bool:
    if not isinstance(message, str):
        return False
    match = _FETCH_MESSAGE.fullmatch(message.strip())
    if match is None:
        return False
    return (match.group("url").strip() == url.strip()
            and bool(match.group("body").strip()))


def _published_date(value: Any) -> str | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        moment = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if not 2000 <= moment.year <= 2100:
        return None
    return moment.strftime("%Y-%m-%d")


def _hook_command() -> str:
    script = Path(__file__).resolve().with_name("kiro_web_hook.py")
    args = [sys.executable, "-I", str(script)]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


class KiroNativeWebRuntime:
    """One Kiro installation's OFF and web turns, scoped to the app state root."""

    provider_id = "kiro"

    def __init__(self, settings: Settings, spec: CliSpec, executable: str, *,
                 cwd: str | Path,
                 version: str | None = None,
                 engine_v3: bool | None = None) -> None:
        self.settings = settings
        self.spec = spec
        self.executable = executable
        self.cwd = Path(cwd)
        self._injected_version = version
        self._injected_engine = engine_v3
        self._verified: tuple[str | None, bool] | None = None
        self._closed = False
        self._process: CliProcess | None = None
        self._hook_path = Path(__file__).resolve().with_name("kiro_web_hook.py")

    # -- profiles -------------------------------------------------------------
    @property
    def _agents_dir(self) -> Path:
        return self.cwd / ".kiro" / "agents"

    def _profile_path(self, name: str) -> Path:
        return self._agents_dir / f"{name}.json"

    def _off_document(self) -> dict[str, Any]:
        return {
            "name": OFF_AGENT,
            "description": "Lunmi OFF profile: no tools, no web.",
            "prompt": PROFILE_PROMPT,
            "mcpServers": {},
            "resources": [],
            "includeMcpJson": False,
            "includePowers": False,
            "tools": [],
            "excludedTools": ["web"],
            "permissions": {"rules": [
                {"capability": "web_search", "effect": "deny"},
                {"capability": "web_fetch", "effect": "deny"},
            ]},
        }

    def _web_document(self) -> dict[str, Any]:
        return {
            "name": WEB_AGENT,
            "description": "Lunmi web profile: only the guarded native web tools.",
            "prompt": PROFILE_PROMPT,
            "mcpServers": {},
            "resources": [],
            "includeMcpJson": False,
            "includePowers": False,
            "tools": ["web"],
            "permissions": {"rules": [
                {"capability": "web_search", "effect": "allow"},
                {"capability": "web_fetch", "effect": "allow"},
            ]},
            "hooks": [{
                "name": "lunmi-web-guard",
                "trigger": "PreToolUse",
                "matcher": "*",
                "action": {"type": "command", "command": _hook_command()},
                "timeout": 2,
            }],
        }

    def _write_profile(self, name: str, document: dict[str, Any]) -> Path:
        path = self._profile_path(name)
        data = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        try:
            self._agents_dir.mkdir(parents=True, exist_ok=True)
            if path.is_file() and path.read_text(encoding="utf-8") == data:
                return path
            descriptor, temporary = tempfile.mkstemp(prefix=f".{name}-", suffix=".tmp",
                                                     dir=self._agents_dir)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(data)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except OSError as exc:
            raise BrainError(ErrorKind.CONFIGURATION,
                             "Não foi possível preparar o perfil privado do Kiro.",
                             detail=str(exc)) from exc
        return path

    def ensure_off(self) -> Path:
        return self._write_profile(OFF_AGENT, self._off_document())

    def ensure_web(self) -> Path:
        if not self._hook_path.is_file():
            raise BrainError(ErrorKind.CONFIGURATION,
                             "O guard do Kiro não está disponível; a pesquisa nativa fica desligada.")
        return self._write_profile(WEB_AGENT, self._web_document())

    # -- capability -----------------------------------------------------------
    def _verify(self) -> tuple[str | None, bool]:
        if self._injected_version is not None and self._injected_engine is not None:
            return self._injected_version or None, bool(self._injected_engine)
        if self._verified is not None:
            return self._verified
        version: str | None = None
        engine = False
        try:
            result = subprocess.run(
                [self.executable, "--version"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=5, creationflags=_CREATE_NO_WINDOW, check=False,
            )
            version_text = f"{result.stdout or ''}\n{result.stderr or ''}"
            match = _VERSION_PATTERN.search(version_text)
            if match:
                version = match.group(0)
            help_result = subprocess.run(
                [self.executable, "chat", "--help"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=5, creationflags=_CREATE_NO_WINDOW, check=False,
            )
            help_text = f"{help_result.stdout or ''}\n{help_result.stderr or ''}"
            engine = "--agent-engine" in help_text and "v3" in help_text
        except (OSError, subprocess.SubprocessError):
            version, engine = None, False
        self._verified = (version, engine)
        return self._verified

    def _can_disable(self) -> bool:
        try:
            self.ensure_off()
        except BrainError:
            return False
        return True

    def capability(self) -> NativeWebCapability:
        if self._closed:
            return NativeWebCapability(state=NativeWebState.UNAVAILABLE)
        if self.settings.model.strip() != "auto":
            return NativeWebCapability(supported=True, available=False,
                                       can_disable=self._can_disable(),
                                       state=NativeWebState.UNKNOWN)
        version, engine = self._verify()
        if version is None:
            return NativeWebCapability(supported=True, available=False,
                                       can_disable=self._can_disable(),
                                       state=NativeWebState.UNKNOWN)
        if version != VERIFIED_VERSION or not engine:
            return NativeWebCapability(supported=True, available=False,
                                       can_disable=self._can_disable(),
                                       state=NativeWebState.UNAVAILABLE)
        try:
            self.ensure_off()
            self.ensure_web()
        except BrainError:
            return NativeWebCapability(supported=True, available=False,
                                       can_disable=self._can_disable(),
                                       state=NativeWebState.UNAVAILABLE)
        return NativeWebCapability.ready()

    # -- command lines --------------------------------------------------------
    def _effort(self) -> list[str]:
        effort = self.settings.effort.strip()
        if not effort or not self.spec.effort_args:
            return []
        if self.spec.effort_values and effort not in self.spec.effort_values:
            raise BrainError(ErrorKind.CONFIGURATION,
                             f"Esforço de raciocínio '{effort}' não é válido para "
                             f"{self.spec.label}. Use um de: "
                             + ", ".join(self.spec.effort_values) + ".")
        return [*self.spec.effort_args, effort]

    def _argv(self, agent: str, trust_tools: str) -> list[str]:
        argv = [self.executable, "chat", "--agent", agent, "--agent-engine", "v3"]
        if self.settings.model.strip():
            argv.extend(["--model", self.settings.model])
        argv.extend(self._effort())
        argv.extend(["--no-interactive", "--output-format", "stream-json",
                     f"--trust-tools={trust_tools}"])
        return argv

    def off_argv(self) -> list[str]:
        """Pure command line (no profile write); callers use ``off_turn`` to run it."""
        return self._argv(OFF_AGENT, "")

    def web_argv(self) -> list[str]:
        return self._argv(WEB_AGENT, ",".join(WEB_TOOLS))

    def off_turn(self) -> tuple[list[str], Path]:
        self.ensure_off()
        return self.off_argv(), self.cwd

    def web_turn(self) -> tuple[list[str], Path]:
        self.ensure_web()
        return self.web_argv(), self.cwd

    # -- receipts -------------------------------------------------------------
    def _read_receipts(self, path: Path) -> tuple[tuple[str, bool], ...] | None:
        """Read sanitized receipts; ``None`` means malformed/untrusted (fail closed)."""
        try:
            if not path.exists():
                return ()
            data = path.read_bytes()
        except OSError:
            return None
        if len(data) > MAX_RECEIPT_BYTES:
            return None
        records: list[tuple[str, bool]] = []
        for line in data.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None
            if not isinstance(record, dict) or set(record) != {"tool_name", "denied"}:
                return None
            name = record.get("tool_name")
            denied = record.get("denied")
            if not isinstance(name, str) or not isinstance(denied, bool):
                return None
            if name not in WEB_TOOLS:
                return None
            records.append((name, denied))
        return tuple(records)

    # -- execution ------------------------------------------------------------
    def _new_receipt(self) -> Path:
        try:
            descriptor, name = tempfile.mkstemp(prefix="lunmi-kiro-receipt-",
                                                suffix=".jsonl", dir=self.cwd)
            os.close(descriptor)
        except OSError as exc:
            raise BrainError(ErrorKind.CONFIGURATION,
                             "Não foi possível preparar o recibo do guard do Kiro.",
                             detail=str(exc)) from exc
        return Path(name).resolve()

    async def _exchange(self, handle: CliProcess, prompt: str) -> str:
        if self.spec.prompt_on_stdin:
            await handle.write(prompt)
            await handle.close_input()
        deadline = time.monotonic() + max(self.spec.startup_timeout,
                                          self.settings.timeout_seconds)
        chunks: list[str] = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # No stdout/stderr tail here: a web failure must not carry a URL or page
                # text into a BrainError detail/log.
                raise BrainError(ErrorKind.TIMEOUT,
                                 "A pesquisa nativa não terminou dentro do tempo limite.")
            line = await handle.readline(remaining)
            if not line:
                break
            total += len(line.encode("utf-8"))
            if total > MAX_WEB_STDOUT_BYTES:
                raise BrainError(ErrorKind.TRANSPORT,
                                 "A pesquisa nativa excedeu o limite de saída do adaptador.")
            chunks.append(line)
        return "".join(chunks)

    async def complete(self, messages: list[dict[str, Any]]) -> NativeWebResult:
        if self._closed:
            raise BrainError(ErrorKind.PROVIDER_UNAVAILABLE, "Este provider já foi encerrado.")
        if not self.capability().selectable:
            raise BrainError(ErrorKind.UNSUPPORTED_FEATURE,
                             "Pesquisa nativa indisponível neste Kiro/modelo.")
        # Lazy imports keep this module out of the headless adapter's import cycle.
        from app.brain.cli.headless import extract_json_events, render_prompt

        prompt = render_prompt(messages)
        if not prompt.strip():
            raise BrainError(ErrorKind.CONFIGURATION, "Nada a enviar ao agente local.")
        argv, cwd = self.web_turn()
        receipt = self._new_receipt()
        handle: CliProcess | None = None
        try:
            handle = await spawn(argv, cwd=cwd,
                                 extra_env={"LUNMI_KIRO_RECEIPT": str(receipt)})
            self._process = handle
            try:
                stdout = await self._exchange(handle, prompt)
            except BrainError as exc:
                if self._closed:
                    raise BrainError(ErrorKind.CANCELLED, "Operação cancelada.") from exc
                if exc.kind is ErrorKind.TIMEOUT:
                    return self._failure(NativeWebOutcome.TIMEOUT, "timeout",
                                         failure="timeout")
                if exc.kind is ErrorKind.CANCELLED:
                    raise
                # Never forward a process detail: it can contain a URL/page tail.
                return self._failure(NativeWebOutcome.FAILED, "tool_error",
                                     failure="failed")
            if self._closed:
                raise BrainError(ErrorKind.CANCELLED, "Operação cancelada.")
            try:
                exit_code = await handle.wait(30)
            except BrainError as exc:
                if self._closed:
                    raise BrainError(ErrorKind.CANCELLED, "Operação cancelada.") from exc
                if exc.kind is ErrorKind.TIMEOUT:
                    return self._failure(NativeWebOutcome.TIMEOUT, "timeout",
                                         failure="timeout")
                if exc.kind is ErrorKind.CANCELLED:
                    raise
                return self._failure(NativeWebOutcome.FAILED, "tool_error",
                                     failure="failed")
            if exit_code != 0:
                # A successful event stream does not make a crashed CLI a success.
                return self._failure(NativeWebOutcome.FAILED, "tool_error",
                                     failure="failed")
            # No answer may be published while the owned child is still alive. The
            # teardown proof is required even for a successful turn; `finally` repeats
            # it for every early return and for cancellation.
            if not await self._stop_handle(handle):
                return self._failure(NativeWebOutcome.FAILED, "teardown_unconfirmed",
                                     failure="unknown")
            try:
                events = extract_json_events(stdout)
            except BrainError:
                return self._failure(NativeWebOutcome.FAILED, "malformed",
                                     failure="malformed")
            receipts = self._read_receipts(receipt)
            return self._interpret(events, receipts)
        finally:
            # Every return and every propagation passes through a bounded teardown.
            # A fallback may only start after `cancel_web` reports the child ended.
            if handle is not None:
                await self._stop_handle(handle)
            try:
                receipt.unlink()
            except OSError:
                pass

    async def _stop_handle(self, handle: CliProcess) -> bool:
        """Stop one owned turn and prove the child ended, within a finite bound."""
        try:
            await asyncio.wait_for(handle.stop(), timeout=MAX_STOP_SECONDS)
        except (asyncio.TimeoutError, OSError, RuntimeError):
            return False
        ended = handle.process.returncode is not None
        if ended and self._process is handle:
            self._process = None
        return ended

    async def cancel_web(self) -> bool:
        """C1's cleanup gate: True only when this adapter owns no live child.

        Idempotent: after `complete` returned, there is nothing left to stop. If the
        teardown cannot be confirmed within the bound, the runtime keeps the handle and
        returns False, so the controller does not start a recovery on top of live work.
        """
        handle = self._process
        if handle is None:
            return True
        return await self._stop_handle(handle)

    async def close(self) -> None:
        self._closed = True
        handle = self._process
        if handle is not None:
            await self._stop_handle(handle)

    # -- result interpretation ------------------------------------------------
    def _failure(self, outcome: NativeWebOutcome, reason: str, *,
                 failure: str = "", text: str = "",
                 tools: tuple[str, ...] = (),
                 sources: tuple[NativeWebSource, ...] = ()) -> NativeWebResult:
        """Build a typed result; `failure` is the closed class consumed by C1.

        `failure_reason` keeps the old, more specific diagnostic string. Only causes
        actually observed in this turn are reported; `permission` is not inferred from
        a hook denial or from a missing receipt.
        """
        return NativeWebResult(text=text, provider_id=self.provider_id,
                               tool_names=tools, sources=sources,
                               outcome=outcome, failure=failure,
                               failure_reason=reason)

    def _interpret(self, events: list[dict[str, Any]],
                   receipts: tuple[tuple[str, bool], ...] | None) -> NativeWebResult:
        if receipts is None:
            return self._failure(NativeWebOutcome.FAILED, "malformed",
                                 failure="malformed")
        tool_names: list[str] = []
        denied = False
        for name, was_denied in receipts:
            if name not in tool_names:
                tool_names.append(name)
            denied = denied or was_denied
        tools = tuple(tool_names)

        final_text = ""
        chunks: list[str] = []
        status: str | None = None
        stop_reason = ""
        truncated = False
        tool_error = False
        security_url = False
        identity_mismatch = False
        any_tool_event = False
        search_completed = False
        search_empty = False
        sources: list[NativeWebSource] = []
        completed_ids: set[str] = set()
        completed_shapes: dict[str, str] = {}

        for event in events:
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            data = event.get("data")
            if kind == "runFinished" and isinstance(data, dict):
                raw_status = data.get("status")
                status = raw_status if isinstance(raw_status, str) else None
                raw_text = data.get("finalText")
                final_text = raw_text if isinstance(raw_text, str) else ""
                truncated = data.get("finalTextTruncated") is True
                raw_stop = data.get("stopReason")
                stop_reason = raw_stop if isinstance(raw_stop, str) else ""
                continue
            if kind != "sessionUpdate" or not isinstance(data, dict):
                continue
            update = data.get("update")
            if not isinstance(update, dict):
                continue
            update_kind = update.get("sessionUpdate")
            if update_kind == "agent_message_chunk":
                content = update.get("content")
                if isinstance(content, dict):
                    text = content.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
                continue
            if update_kind not in {"tool_call", "tool_call_update"}:
                continue
            any_tool_event = True
            if update_kind == "tool_call":
                continue
            status_update = update.get("status")
            call_id = update.get("toolCallId")
            if not isinstance(call_id, str):
                continue
            if status_update in {"failed", "cancelled"}:
                tool_error = True
                continue
            if status_update != "completed":
                continue
            raw_input = update.get("rawInput")
            raw_output = update.get("rawOutput")
            shape = ("search" if isinstance(raw_output, dict)
                     and isinstance(raw_output.get("results"), list)
                     else "fetch" if isinstance(raw_output, dict)
                     and (isinstance(raw_output.get("message"), str)
                          or isinstance(raw_output.get("content"), list))
                     else "unknown")
            if call_id in completed_ids:
                if completed_shapes.get(call_id) != shape:
                    # The same ID must not change meaning between updates.
                    identity_mismatch = True
                continue
            completed_ids.add(call_id)
            completed_shapes[call_id] = shape
            if not isinstance(raw_output, dict):
                tool_error = True
                continue
            results = raw_output.get("results")
            if isinstance(results, list):
                # Search updates may omit rawInput; the result list is the payload.
                if "web_search" not in tools:
                    identity_mismatch = True
                    continue
                search_completed = True
                if not results:
                    search_empty = True
                sources.extend(self._search_sources(results))
                continue
            message = raw_output.get("message")
            if isinstance(message, str) or isinstance(raw_output.get("content"), list):
                if not isinstance(raw_input, dict) or not isinstance(raw_input.get("url"), str):
                    tool_error = True
                    continue
                if "web_fetch" not in tools:
                    identity_mismatch = True
                    continue
                if url_syntax_problem(raw_input["url"]):
                    # The hook should have refused this before execution. If it reached
                    # the adapter anyway, it is a security observation, not permission.
                    security_url = True
                    continue
                source = self._fetch_source(raw_input["url"], raw_input, raw_output)
                if source is None:
                    tool_error = True
                else:
                    sources.append(source)
                continue
            tool_error = True

        text = final_text or "".join(chunks)
        if denied:
            # The only writer of `denied=true` is our PreToolUse hook: URL/DNS,
            # sensitive query, malformed input or a failed receipt. That is security,
            # never the recoverable `permission` class (C0 §3).
            return self._failure(NativeWebOutcome.BLOCKED, "hook_denied",
                                 failure="security", text=text, tools=tools)
        if status is None:
            return self._failure(NativeWebOutcome.FAILED, "malformed",
                                 failure="malformed", text=text, tools=tools)
        if status != "success":
            return self._failure(NativeWebOutcome.FAILED, "tool_error",
                                 failure="failed", text=text, tools=tools)
        if identity_mismatch:
            return self._failure(NativeWebOutcome.FAILED, "malformed",
                                 failure="malformed", text=text, tools=tools)
        if security_url:
            return self._failure(NativeWebOutcome.BLOCKED, "private_url",
                                 failure="security", text=text, tools=tools)
        if tool_error:
            return self._failure(NativeWebOutcome.FAILED, "tool_error",
                                 failure="failed", text=text, tools=tools)
        if not receipts and any_tool_event:
            # Kiro emitted a tool event but the hook never proved its name. Absence of
            # proof is not permission and does not authorize recovery.
            return self._failure(NativeWebOutcome.FAILED, "malformed",
                                 failure="malformed", text=text, tools=tools)
        if receipts and not completed_ids:
            return self._failure(NativeWebOutcome.FAILED, "tool_error",
                                 failure="failed", text=text, tools=tools)
        merged = self._merge_sources(sources)
        if truncated or (stop_reason and stop_reason != "end_turn"):
            return self._failure(NativeWebOutcome.NOT_VERIFIED, "",
                                 text=text, tools=tools, sources=tuple(merged))
        if search_completed and search_empty and not merged:
            # Positive proof: the search tool completed under an allowed receipt and
            # returned an explicit empty list. This is the one closed reason that may
            # authorize the sequential Free Search recovery; absence of sources alone
            # (NOT_VERIFIED below) stays `failure=""`.
            return self._failure(NativeWebOutcome.NO_RESULTS, "search_empty",
                                 failure="exhausted", text=text, tools=tools)
        if not merged or not text.strip():
            # No sources or no text is absence of evidence, not a research failure.
            return self._failure(NativeWebOutcome.NOT_VERIFIED, "",
                                 text=text, tools=tools, sources=tuple(merged))
        # Citations in the answer are presentation. A missing `[n]` marker is not a
        # failed search; provenance comes only from receipts and tool payloads.
        return NativeWebResult(text=text, provider_id=self.provider_id,
                               tool_names=tools, sources=tuple(merged),
                               outcome=NativeWebOutcome.SUCCESS, failure="",
                               failure_reason="")

    def _search_sources(self, results: list[Any]) -> list[NativeWebSource]:
        found: list[NativeWebSource] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or url_syntax_problem(url):
                continue
            published = _published_date(item.get("publishedDate"))
            found.append(NativeWebSource(
                title=_clean(item.get("title"), 300),
                url=url.strip(),
                snippet=_clean(item.get("snippet"), 600),
                published_at=published,
                date_source="provider" if published else None,
                retrieved_at=_now(),
                read=False,
                truncated=False,
                cached=False,
                engines=("kiro",),
            ))
        return found

    def _fetch_source(self, url: str, raw_input: dict[str, Any],
                      raw_output: dict[str, Any]) -> NativeWebSource | None:
        if url_syntax_problem(url):
            return None
        mode = raw_input.get("mode")
        complete = isinstance(mode, str) and mode.strip().lower() in {"full", "complete"}
        # ``rawOutput.message`` can be an error/refusal with text. Accept content
        # blocks, or the exact observed V3 fetch envelope for this same URL; a
        # bare error message is not evidence that the page was read.
        observed = (_has_observed_body(raw_output.get("content"))
                    or _message_has_observed_body(raw_output.get("message"), url))
        if not observed:
            return None
        return NativeWebSource(
            title="",
            url=url.strip(),
            snippet="",
            published_at=None,
            date_source=None,
            retrieved_at=_now(),
            read=True,
            # Absent mode is not proof of a complete read.
            truncated=not complete,
            cached=False,
            engines=("kiro",),
        )

    @staticmethod
    def _merge_sources(sources: list[NativeWebSource]) -> list[NativeWebSource]:
        ordered: list[str] = []
        by_url: dict[str, NativeWebSource] = {}
        for source in sources:
            key = source.url.strip()
            existing = by_url.get(key)
            if existing is None:
                ordered.append(key)
                by_url[key] = source
                continue
            read = existing.read or source.read
            truncated = source.truncated if source.read else existing.truncated
            by_url[key] = NativeWebSource(
                title=existing.title or source.title,
                url=existing.url,
                snippet=existing.snippet or source.snippet,
                published_at=existing.published_at or source.published_at,
                date_source=existing.date_source or source.date_source,
                retrieved_at=existing.retrieved_at,
                read=read,
                truncated=truncated,
                cached=False,
                engines=("kiro",),
            )
        return [by_url[key] for key in ordered[:MAX_SOURCES]]

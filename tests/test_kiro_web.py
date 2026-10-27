from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from app.brain.cli.catalog import find_spec
from app.brain.cli.headless import HeadlessCliBrain
from app.brain.contracts import BrainError, ErrorKind
from app.brain.kiro_web import (
    KiroNativeWebRuntime,
    OFF_AGENT,
    WEB_AGENT,
    VERIFIED_VERSION,
)
from app.brain.kiro_web_hook import (
    canonical_tool_name,
    fetch_problem,
    url_syntax_problem,
    validate_tool,
    write_receipt,
)
from app.brain.web import (
    NativeWebOutcome,
    NativeWebState,
    WebOriginSnapshot,
    should_recover_web,
    stop_native_web,
)
from app.settings import Settings


def _resolver(*addresses: str):
    def resolve(host: str, port: int, **_: object) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
                for address in addresses]
    return resolve


def _runtime(tmp_path: Path, *, model: str = "auto",
             version: str | None = VERIFIED_VERSION,
             engine_v3: bool = True) -> KiroNativeWebRuntime:
    spec = find_spec("kiro")
    assert spec is not None
    settings = Settings(provider="kiro", model=model, state_root=tmp_path)
    return KiroNativeWebRuntime(settings, spec, "kiro-cli",
                                cwd=tmp_path / ".local" / "kiro-agents",
                                version=version, engine_v3=engine_v3)


def _event(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False) + "\n"


def _search_event(url: str = "https://example.test/a") -> list[str]:
    return [
        _event({"type": "sessionUpdate", "data": {"update": {
            "sessionUpdate": "tool_call", "toolCallId": "tooluse_search",
            "title": "web_search",
            "rawInput": {"query": "python downloads"},
        }}}),
        _event({"type": "sessionUpdate", "data": {"update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "tooluse_search",
            "status": "completed",
            "rawOutput": {"results": [{
                "title": "Python", "url": url, "snippet": "Downloads",
                "publishedDate": 1704067200000,
            }]},
        }}}),
    ]


def _fetch_event(url: str = "https://example.test/a",
                 status: str = "completed") -> list[str]:
    update: dict = {
        "sessionUpdate": "tool_call_update", "toolCallId": "tooluse_fetch",
        "status": status, "title": "Fetch URL",
        "rawInput": {"url": url, "mode": "truncated"},
        "_meta": {"kiro": {"toolOrigin": "default"}},
    }
    if status == "completed":
        update["rawOutput"] = {
            "message": "Fetched content: page body",
            "content": [{"type": "content", "content": {
                "type": "text", "text": "page body"}}],
        }
    return [_event({"type": "sessionUpdate", "data": {"update": update}})]


def _finish(text: str = "Resposta", *, status: str = "success",
            truncated: bool = False) -> list[str]:
    return [_event({"type": "runFinished", "data": {
        "status": status, "stopReason": "end_turn",
        "finalText": text, "finalTextTruncated": truncated,
    }})]


class _FakeProcess:
    def __init__(self, code: int | None) -> None:
        self.returncode = code


class _FakeHandle:
    def __init__(self, lines: list[str], *, code: int | None = 0,
                 wait_error: BrainError | None = None,
                 read_error: BaseException | None = None,
                 stop_code: int | None = 0) -> None:
        self.lines = list(lines)
        self.prompt = ""
        self.stopped = False
        self.process = _FakeProcess(code)
        self._wait_error = wait_error
        self._read_error = read_error
        self._stop_code = stop_code

    async def write(self, data: str) -> None:
        self.prompt += data

    async def close_input(self) -> None:
        pass

    async def readline(self, timeout: float) -> str:
        if self._read_error is not None:
            raise self._read_error
        return self.lines.pop(0) if self.lines else ""

    async def wait(self, timeout: float) -> int:
        if self._wait_error is not None:
            raise self._wait_error
        assert self.process.returncode is not None
        return self.process.returncode

    async def stop(self) -> None:
        self.stopped = True
        self.process.returncode = self._stop_code

    def diagnostics(self) -> str:
        return ""


def _install_fake_spawn(monkeypatch: pytest.MonkeyPatch,
                        lines: list[str], receipts: list[dict[str, object]] | None,
                        calls: list[dict[str, object]], *,
                        code: int | None = 0,
                        wait_error: BrainError | None = None,
                        read_error: BaseException | None = None,
                        stop_code: int | None = 0,
                        target: str = "app.brain.kiro_web.spawn") -> _FakeHandle:
    handle = _FakeHandle(lines, code=code, wait_error=wait_error,
                         read_error=read_error, stop_code=stop_code)

    async def fake_spawn(argv: list[str], *, cwd: object = None,
                         extra_env: dict[str, str] | None = None) -> _FakeHandle:
        calls.append({"argv": list(argv), "cwd": cwd, "env": dict(extra_env or {})})
        if receipts is not None:
            path = Path((extra_env or {})["LUNMI_KIRO_RECEIPT"])
            with path.open("a", encoding="utf-8") as stream:
                for record in receipts:
                    stream.write(json.dumps(record) + "\n")
        return handle

    monkeypatch.setattr(target, fake_spawn)
    return handle


# -- hook ---------------------------------------------------------------------
def test_hook_accepts_a_bounded_search_query_without_dns() -> None:
    assert validate_tool("web_search", {"query": "python downloads"}) == ""
    assert validate_tool("web_search", {"query": "x" * 601}) == "query"
    assert validate_tool("web_search", {"query": "a\x00b"}) == "query"
    assert validate_tool("web_search", {"query": ""}) == "query"
    assert validate_tool("shell", {"query": "x"}) == "tool"


def test_hook_keeps_the_shared_sensitive_query_boundary() -> None:
    for query in (
        "OPENAI_API_KEY=not-a-real-key",
        "Authorization: Bearer abc",
        "password: hunter2",
        "sk-1234567890abcdef",
        "-----BEGIN RSA PRIVATE KEY-----",
        r"C:\Users\alice\secret.txt",
    ):
        assert validate_tool("web_search", {"query": query}) == "query", query
    assert validate_tool("web_search", {"query": "Python official releases"}) == ""


def test_hook_normalizes_only_the_observed_remote_search_alias(tmp_path: Path) -> None:
    assert canonical_tool_name("remote_web_search") == "web_search"
    assert canonical_tool_name("web_search") == "web_search"
    assert canonical_tool_name("remote_fetch") is None
    assert validate_tool("remote_web_search", {"query": "python downloads"}) == ""
    assert validate_tool("remote_web_search", {"query": ""}) == "query"
    assert validate_tool("remote_fetch", {"url": "https://example.test/a"}) == "tool"

    receipt = tmp_path / "receipt.jsonl"
    assert write_receipt("remote_web_search", False, receipt) is True
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "tool_name": "web_search", "denied": False,
    }


def test_hook_rejects_private_urls_even_when_the_host_resolves() -> None:
    public = _resolver("93.184.216.34")
    private = _resolver("127.0.0.1")
    mixed = _resolver("93.184.216.34", "10.0.0.7")
    assert url_syntax_problem("https://example.test/a") == ""
    assert fetch_problem("https://example.test/a", resolver=public) == ""
    assert fetch_problem("https://example.test/a", resolver=private) == "address"
    assert fetch_problem("https://example.test/a", resolver=mixed) == "address"

    for url in ("http://127.0.0.1/a", "http://[::1]/a", "http://localhost/a",
                "https://printer.local/a", "https://user:pass@example.test/a",
                "https://example.test:8443/a", "ftp://example.test/a",
                "https://singlelabel/a"):
        assert url_syntax_problem(url), url


def test_hook_receipt_is_sanitized_and_required(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.jsonl"
    assert write_receipt("web_fetch", True, receipt) is True
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload == {"tool_name": "web_fetch", "denied": True}
    assert "example" not in receipt.read_text(encoding="utf-8")
    assert write_receipt("web_fetch", False, "") is False


def test_hook_process_blocks_private_fetch_before_the_request(tmp_path: Path) -> None:
    hook = Path(__file__).resolve().parents[1] / "app" / "brain" / "kiro_web_hook.py"
    receipt = tmp_path / "receipt.jsonl"
    environment = {**os.environ, "LUNMI_KIRO_RECEIPT": str(receipt)}
    result = subprocess.run(
        [sys.executable, "-I", str(hook)],
        input=json.dumps({"tool_name": "web_fetch",
                          "tool_input": {"url": "http://127.0.0.1/"}}),
        text=True, capture_output=True, env=environment, timeout=20, check=False,
    )
    assert result.returncode == 2
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "tool_name": "web_fetch", "denied": True,
    }
    assert "127.0.0.1" not in receipt.read_text(encoding="utf-8")


def test_hook_process_allows_search_and_writes_a_receipt(tmp_path: Path) -> None:
    hook = Path(__file__).resolve().parents[1] / "app" / "brain" / "kiro_web_hook.py"
    receipt = tmp_path / "receipt.jsonl"
    environment = {**os.environ, "LUNMI_KIRO_RECEIPT": str(receipt)}
    result = subprocess.run(
        [sys.executable, "-I", str(hook)],
        input=json.dumps({"tool_name": "web_search",
                          "tool_input": {"query": "python downloads"}}),
        text=True, capture_output=True, env=environment, timeout=20, check=False,
    )
    assert result.returncode == 0
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "tool_name": "web_search", "denied": False,
    }
    assert "python downloads" not in receipt.read_text(encoding="utf-8")


def test_hook_without_receipt_path_denies(tmp_path: Path) -> None:
    hook = Path(__file__).resolve().parents[1] / "app" / "brain" / "kiro_web_hook.py"
    environment = {key: value for key, value in os.environ.items()
                   if key != "LUNMI_KIRO_RECEIPT"}
    result = subprocess.run(
        [sys.executable, "-I", str(hook)],
        input=json.dumps({"tool_name": "web_search",
                          "tool_input": {"query": "python"}}),
        text=True, capture_output=True, env=environment, timeout=20, check=False,
    )
    assert result.returncode == 2


# -- profiles/capability -------------------------------------------------------
def test_runtime_writes_private_v3_profiles_with_an_array_hook(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    capability = runtime.capability()
    assert capability.state is NativeWebState.AVAILABLE
    assert capability.selectable is True

    agents = runtime.cwd / ".kiro" / "agents"
    off = json.loads((agents / f"{OFF_AGENT}.json").read_text(encoding="utf-8"))
    web = json.loads((agents / f"{WEB_AGENT}.json").read_text(encoding="utf-8"))
    assert off["tools"] == [] and off["excludedTools"] == ["web"]
    assert off["permissions"]["rules"] == [
        {"capability": "web_search", "effect": "deny"},
        {"capability": "web_fetch", "effect": "deny"},
    ]
    assert "hooks" not in off
    assert web["tools"] == ["web"]
    assert web["permissions"]["rules"] == [
        {"capability": "web_search", "effect": "allow"},
        {"capability": "web_fetch", "effect": "allow"},
    ]
    assert isinstance(web["hooks"], list)
    assert web["hooks"][0]["trigger"] == "PreToolUse"
    assert web["hooks"][0]["action"]["type"] == "command"
    assert "kiro_web_hook.py" in web["hooks"][0]["action"]["command"]
    assert "allowedTools" not in web

    assert runtime.off_argv() == [
        "kiro-cli", "chat", "--agent", OFF_AGENT, "--agent-engine", "v3",
        "--model", "auto", "--no-interactive", "--output-format", "stream-json",
        "--trust-tools=",
    ]
    assert runtime.web_argv()[-1] == "--trust-tools=web_search,web_fetch"
    assert "--trust-all-tools" not in runtime.web_argv()


def test_runtime_capability_is_hidden_for_non_auto_model(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, model="claude-sonnet-4")
    capability = runtime.capability()
    assert capability.supported is True
    assert capability.available is False
    assert capability.selectable is False
    assert capability.state is NativeWebState.UNKNOWN


def test_runtime_capability_requires_the_verified_engine(tmp_path: Path) -> None:
    old = _runtime(tmp_path / "old", version="2.28.0")
    assert old.capability().state is NativeWebState.UNAVAILABLE
    no_engine = _runtime(tmp_path / "engine", version=VERIFIED_VERSION,
                         engine_v3=False)
    assert no_engine.capability().state is NativeWebState.UNAVAILABLE


def test_runtime_refuses_web_when_the_hook_is_missing(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(runtime, "_hook_path", tmp_path / "missing-hook.py")
    capability = runtime.capability()
    assert capability.state is NativeWebState.UNAVAILABLE
    assert capability.selectable is False


# -- complete_web --------------------------------------------------------------
def test_complete_web_uses_receipts_and_returns_only_observed_sources(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    calls: list[dict[str, object]] = []
    lines = _search_event() + _fetch_event() + _finish("Resposta final")
    _install_fake_spawn(monkeypatch, lines, [
        {"tool_name": "web_search", "denied": False},
        {"tool_name": "web_fetch", "denied": False},
    ], calls)

    result = asyncio.run(runtime.complete([{"role": "user", "content": "pergunta"}]))

    assert result.outcome is NativeWebOutcome.SUCCESS
    assert result.failure == ""
    assert result.text == "Resposta final"
    assert result.tool_names == ("web_search", "web_fetch")
    assert result.source_count == 1
    source = result.sources[0]
    assert source.url == "https://example.test/a"
    assert source.title == "Python"
    assert source.snippet == "Downloads"
    assert source.published_at == "2024-01-01"
    assert source.date_source == "provider"
    assert source.read is True and source.truncated is True
    assert source.cached is False and source.engines == ("kiro",)
    assert calls[0]["argv"][:6] == ["kiro-cli", "chat", "--agent", WEB_AGENT,
                                    "--agent-engine", "v3"]
    assert calls[0]["argv"][-1] == "--trust-tools=web_search,web_fetch"
    assert Path(calls[0]["env"]["LUNMI_KIRO_RECEIPT"]).exists() is False
    assert calls[0]["cwd"] == runtime.cwd


def test_complete_web_does_not_require_structured_citations(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    calls: list[dict[str, object]] = []
    _install_fake_spawn(monkeypatch,
                        _search_event() + _finish("Resposta sem marcadores [n]"),
                        [{"tool_name": "web_search", "denied": False}], calls)

    result = asyncio.run(runtime.complete([{"role": "user", "content": "pergunta"}]))

    # A citation marker is presentation, not research provenance: its absence must
    # not turn an observed source + answer into a failed search.
    assert result.outcome is NativeWebOutcome.SUCCESS
    assert result.failure == ""
    assert result.source_count == 1


def test_complete_web_accepts_the_observed_fetch_message_as_body(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    calls: list[dict[str, object]] = []
    url = "https://example.test/a"
    update = {
        "sessionUpdate": "tool_call_update", "toolCallId": "tooluse_fetch",
        "status": "completed", "title": "Fetch URL",
        "rawInput": {"url": url, "mode": "truncated"},
        "rawOutput": {"message": (
            f"Fetched content from: {url}\nSize: 9 bytes\n\nContent:\n---\npage body"
        )},
    }
    lines = [_event({"type": "sessionUpdate", "data": {"update": update}})]
    _install_fake_spawn(monkeypatch, lines + _finish("Leitura concluída"),
                        [{"tool_name": "web_fetch", "denied": False}], calls)

    result = asyncio.run(runtime.complete([{"role": "user", "content": "pergunta"}]))

    assert result.outcome is NativeWebOutcome.SUCCESS
    assert result.sources[0].url == url
    assert result.sources[0].read is True
    assert result.sources[0].truncated is True


def test_complete_web_blocks_when_the_hook_denied_the_fetch(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    calls: list[dict[str, object]] = []
    lines = _fetch_event(status="failed") + _finish("Bloqueado pelo guard")
    _install_fake_spawn(monkeypatch, lines, [
        {"tool_name": "web_fetch", "denied": True},
    ], calls)

    result = asyncio.run(runtime.complete([{"role": "user", "content": "pergunta"}]))

    assert result.outcome is NativeWebOutcome.BLOCKED
    # A hook denial is security, never the recoverable `permission` class (C0 §3).
    assert result.failure == "security"
    assert result.failure != "permission"
    assert result.failure_reason == "hook_denied"
    assert result.sources == ()
    assert result.tool_names == ("web_fetch",)
    assert len(calls) == 1  # no retry, no paid fallback
    assert calls[0]["env"]["LUNMI_KIRO_RECEIPT"] is not None


def test_complete_web_fails_closed_without_a_hook_receipt(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    calls: list[dict[str, object]] = []
    _install_fake_spawn(monkeypatch, _search_event() + _finish("Resposta"), [],
                        calls)

    result = asyncio.run(runtime.complete([{"role": "user", "content": "pergunta"}]))

    assert result.outcome is NativeWebOutcome.FAILED
    assert result.failure == "malformed"
    assert result.failure != "permission"
    assert result.failure_reason == "malformed"
    assert result.sources == ()
    assert result.tool_names == ()


def test_complete_web_drops_private_search_urls(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    calls: list[dict[str, object]] = []
    _install_fake_spawn(monkeypatch, _search_event("http://127.0.0.1/a") + _finish(),
                        [{"tool_name": "web_search", "denied": False}], calls)

    result = asyncio.run(runtime.complete([{"role": "user", "content": "pergunta"}]))

    assert result.outcome is NativeWebOutcome.NOT_VERIFIED
    # Absence of structured/usable sources is not positive exhaustion: no failure
    # class is invented, so C1 does not recover from it.
    assert result.failure == ""
    assert result.failure_reason == ""
    assert result.sources == ()


def test_exhausted_is_the_only_empty_search_class_that_permits_recovery() -> None:
    snapshot = WebOriginSnapshot(
        preference="auto", origin="provider",
        attempts=("provider", "lunmi"), recovery=True,
    )
    assert should_recover_web(snapshot, outcome="NO_RESULTS",
                              failure="exhausted", remaining_s=10) is True
    assert should_recover_web(snapshot, outcome="NO_RESULTS",
                              failure="", remaining_s=10) is False
    assert should_recover_web(snapshot, outcome="NOT_VERIFIED",
                              failure="", remaining_s=10) is False
    assert should_recover_web(snapshot, outcome="SUCCESS",
                              failure="exhausted", remaining_s=10) is False


def test_complete_web_does_not_treat_an_error_message_as_a_read(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    calls: list[dict[str, object]] = []
    error = [
        _event({"type": "sessionUpdate", "data": {"update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "tooluse_fetch",
            "status": "completed", "title": "Fetch URL",
            "rawInput": {"url": "https://example.test/a", "mode": "truncated"},
            "rawOutput": {"message": "Fetch failed: 404 Not Found"},
        }}}),
    ]
    _install_fake_spawn(monkeypatch, error + _finish("Sem leitura"),
                        [{"tool_name": "web_fetch", "denied": False}], calls)

    result = asyncio.run(runtime.complete([{"role": "user", "content": "pergunta"}]))

    assert result.outcome is NativeWebOutcome.FAILED
    assert result.failure == "failed"
    assert result.failure_reason == "tool_error"
    assert result.sources == ()
    assert result.tool_names == ("web_fetch",)


def test_complete_web_marks_a_proven_empty_search_as_exhausted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    calls: list[dict[str, object]] = []
    empty = [
        _event({"type": "sessionUpdate", "data": {"update": {
            "sessionUpdate": "tool_call_update", "toolCallId": "tooluse_search",
            "status": "completed", "rawOutput": {"results": []},
        }}}),
    ]
    _install_fake_spawn(monkeypatch, empty + _finish("Sem resultados"),
                        [{"tool_name": "web_search", "denied": False}], calls)

    result = asyncio.run(runtime.complete([{"role": "user", "content": "pergunta"}]))

    assert result.outcome is NativeWebOutcome.NO_RESULTS
    # results=[] on a completed, allowed web_search is positive proof of exhaustion,
    # not a generic absence of evidence.
    assert result.failure == "exhausted"
    assert result.failure_reason == "search_empty"
    assert result.sources == ()


def test_complete_web_timeout_is_typed_and_not_retried(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    calls: list[dict[str, object]] = []
    handle = _install_fake_spawn(monkeypatch, _finish(),
                                 [{"tool_name": "web_search", "denied": False}], calls,
                                 wait_error=BrainError(ErrorKind.TIMEOUT, "timeout"))

    result = asyncio.run(runtime.complete([{"role": "user", "content": "pergunta"}]))

    assert result.outcome is NativeWebOutcome.TIMEOUT
    assert result.failure == "timeout"
    assert result.failure_reason == "timeout"
    assert len(calls) == 1
    assert handle.stopped is True
    assert runtime._process is None  # teardown finished before the result was returned
    # The C1 gate also confirms it, so no recovery can start over live work.
    assert asyncio.run(stop_native_web(runtime)) is True


def test_complete_web_cancellation_tears_down_before_propagating(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    calls: list[dict[str, object]] = []
    handle = _install_fake_spawn(monkeypatch, [], None, calls, code=None,
                                 stop_code=0, read_error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime.complete([{"role": "user", "content": "pergunta"}]))

    assert handle.stopped is True
    assert runtime._process is None
    assert asyncio.run(runtime.cancel_web()) is True


def test_complete_web_never_publishes_success_without_teardown(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    calls: list[dict[str, object]] = []
    _install_fake_spawn(monkeypatch,
                        _search_event() + _finish("Resposta"),
                        [{"tool_name": "web_search", "denied": False}], calls,
                        stop_code=None)

    result = asyncio.run(runtime.complete([{"role": "user", "content": "pergunta"}]))

    assert result.outcome is NativeWebOutcome.FAILED
    assert result.failure == "unknown"
    assert result.failure_reason == "teardown_unconfirmed"
    assert result.text == ""
    # The handle stays owned so C1's gate can retry and refuse recovery.
    assert runtime._process is not None
    assert asyncio.run(runtime.cancel_web()) is False


def test_cancel_web_confirms_the_owned_child_ended(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handle = _FakeHandle([], code=None, stop_code=0)
    runtime._process = handle

    assert asyncio.run(runtime.cancel_web()) is True
    assert handle.stopped is True
    assert runtime._process is None
    # Idempotent: after the child ended there is nothing left to stop.
    assert asyncio.run(runtime.cancel_web()) is True


def test_cancel_web_blocks_recovery_when_teardown_is_unconfirmed(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handle = _FakeHandle([], code=None, stop_code=None)
    runtime._process = handle

    assert asyncio.run(runtime.cancel_web()) is False
    assert handle.stopped is True
    assert runtime._process is handle


def test_complete_web_private_fetch_url_is_security_not_permission(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(tmp_path)
    calls: list[dict[str, object]] = []
    update = {
        "sessionUpdate": "tool_call_update", "toolCallId": "tooluse_fetch",
        "status": "completed", "title": "Fetch URL",
        "rawInput": {"url": "http://127.0.0.1/a", "mode": "truncated"},
        "rawOutput": {"content": [{"type": "content", "content": {
            "type": "text", "text": "page body"}}]},
    }
    _install_fake_spawn(monkeypatch,
                        [_event({"type": "sessionUpdate", "data": {"update": update}})]
                        + _finish("Sem leitura"),
                        [{"tool_name": "web_fetch", "denied": False}], calls)

    result = asyncio.run(runtime.complete([{"role": "user", "content": "pergunta"}]))

    assert result.outcome is NativeWebOutcome.BLOCKED
    assert result.failure == "security"
    assert result.failure != "permission"
    assert result.sources == ()


# -- OFF turn and Brain surface ------------------------------------------------
def test_off_turn_never_uses_the_default_agent(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    spec = find_spec("kiro")
    assert spec is not None
    settings = Settings(provider="kiro", model="auto", state_root=tmp_path)
    brain = HeadlessCliBrain(settings, spec, executable="kiro-cli")
    calls: list[dict[str, object]] = []
    _install_fake_spawn(monkeypatch, _finish("OK"),
                        None, calls, target="app.brain.cli.headless.spawn")

    completion = asyncio.run(brain.complete([{"role": "user", "content": "ola"}]))

    assert completion.text == "OK"
    assert calls[0]["argv"][:6] == ["kiro-cli", "chat", "--agent", OFF_AGENT,
                                    "--agent-engine", "v3"]
    assert calls[0]["argv"][-1] == "--trust-tools="
    assert calls[0]["cwd"] == brain._kiro_web.cwd
    assert "hooks" not in json.loads(
        (brain._kiro_web.cwd / ".kiro" / "agents" / f"{OFF_AGENT}.json")
        .read_text(encoding="utf-8"))


def test_brain_exposes_the_native_web_capability(tmp_path: Path) -> None:
    spec = find_spec("kiro")
    assert spec is not None
    settings = Settings(provider="kiro", model="auto", state_root=tmp_path)
    brain = HeadlessCliBrain(settings, spec, executable="kiro-cli")
    assert brain._kiro_web is not None
    brain._kiro_web._verify = lambda: (VERIFIED_VERSION, True)
    assert brain.native_web_capability().state is NativeWebState.AVAILABLE
    assert brain._kiro_web.capability().selectable is True
    assert callable(brain.complete_web)
    assert callable(brain.cancel_web)
    assert asyncio.run(stop_native_web(brain)) is True


def test_other_cli_brains_do_not_publish_native_web(tmp_path: Path) -> None:
    spec = find_spec("claude")
    assert spec is not None
    settings = Settings(provider="claude", model="sonnet", state_root=tmp_path)
    brain = HeadlessCliBrain(settings, spec, executable="claude")
    assert not hasattr(brain, "native_web_capability")
    assert not hasattr(brain, "complete_web")
    assert not hasattr(brain, "cancel_web")

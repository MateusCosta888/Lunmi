from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import pytest

from dataclasses import replace

from app.brain import detect as detect_module
from app.brain.cli.catalog import CLI_SPECS, find_spec, specs
from app.brain.cli.headless import (HeadlessCliBrain, classify_failure, extract_json_events,
                                    extract_json_reply, render_prompt)
from app.brain.cli.spec import MODEL_PARSERS, REPLY_PARSERS
from app.brain.contracts import BrainError, Capability, ErrorKind
from app.brain.registry import cloud_providers, local_agents, resolve
from app.settings import Settings


def test_render_prompt_puts_system_first_and_drops_images() -> None:
    prompt = render_prompt([
        {"role": "system", "content": "Voce e um agente."},
        {"role": "user", "content": [{"type": "text", "text": "Primeiro"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]},
        {"role": "user", "content": "Segundo"},
    ])
    assert prompt.startswith("Voce e um agente.")
    assert "Primeiro" in prompt and "Segundo" in prompt
    assert "data:image" not in prompt


def test_render_prompt_without_system() -> None:
    assert render_prompt([{"role": "user", "content": "So isso"}]) == "So isso"


def test_extract_json_reply_skips_log_lines_and_reads_last_object() -> None:
    raw = 'iniciando sessao\n{"type":"result","result":"antigo"}\n{"type":"result","result":"novo"}\n'
    assert extract_json_reply(raw)["result"] == "novo"


def test_extract_json_reply_accepts_multiline_document() -> None:
    raw = json.dumps({"result": "ok", "usage": {"input_tokens": 1}})
    assert extract_json_reply(raw)["result"] == "ok"


def test_extract_json_reply_is_a_typed_failure_on_garbage() -> None:
    with pytest.raises(BrainError) as failure:
        extract_json_reply("nao ha json aqui")
    assert failure.value.kind is ErrorKind.MALFORMED_RESPONSE


@pytest.mark.parametrize("text,kind", [
    ("You've hit your usage limit", ErrorKind.QUOTA_EXCEEDED),
    ("429 Too Many Requests", ErrorKind.QUOTA_EXCEEDED),
    ("Not logged in. Please run login.", ErrorKind.AUTH_REQUIRED),
    ("Unauthorized", ErrorKind.AUTH_REQUIRED),
    ("segmentation fault", ErrorKind.PROVIDER_UNAVAILABLE),
    # Kilo answers a bad id with `Model not found: <id>`, which is a configuration
    # mistake, not a broken provider.
    ("Model not found: kilo/nope", ErrorKind.MODEL_UNAVAILABLE),
])
def test_classify_failure_uses_the_application_vocabulary(text: str, kind: ErrorKind) -> None:
    assert classify_failure(text).kind is kind


def test_claude_reply_parser_reads_text_and_usage() -> None:
    payload = {"type": "result", "subtype": "success", "result": "Ola mundo",
               "usage": {"input_tokens": 12, "output_tokens": 4}}
    text, (input_tokens, output_tokens) = REPLY_PARSERS["claude-json"](payload)
    assert text == "Ola mundo"
    assert (input_tokens, output_tokens) == (12, 4)


def test_reply_parsers_tolerate_renamed_keys_and_never_invent_numbers() -> None:
    text, tokens = REPLY_PARSERS["agy-json"]({"content": [{"type": "text", "text": "resposta"}]})
    assert text == "resposta"
    assert tokens == (None, None)
    text, tokens = REPLY_PARSERS["qoder-json"]({"message": {"text": "x", "usage": {"prompt_tokens": "7",
                                                                                   "completion_tokens": "2"}}})
    assert text == "x"
    assert tokens == (7, 2)


def test_catalog_parsers_accept_column_and_json_rows() -> None:
    columns = "Fetching available models...\ngemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n\n"
    assert MODEL_PARSERS["agy-models"](columns) == [("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)")]
    rows = '{"id":"qwen3","displayName":"Qwen 3"}\n{"id":"glm","displayName":"GLM"}'
    assert MODEL_PARSERS["qoder-models"](rows) == [("qwen3", "Qwen 3"), ("glm", "GLM")]


def test_models_parser_ignores_noise_lines() -> None:
    assert MODEL_PARSERS["agy-models"]("=============\nUsing default model\n") == []


def test_catalog_covers_the_five_headless_clis() -> None:
    assert set(CLI_SPECS) == {"claude", "agy", "qoder", "kilo", "kiro"}
    assert len(specs()) == 5
    assert find_spec("nope") is None


def test_cli_specs_never_claim_shell_or_tools() -> None:
    for spec in specs():
        assert not spec.capabilities.supports(Capability.SHELL)
        assert not spec.capabilities.supports(Capability.TOOL_CALLS)


def test_inference_argv_disables_tools_and_appends_the_model() -> None:
    spec = find_spec("claude")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="claude", model="sonnet"), spec, executable="claude")
    argv = brain.inference_argv()
    # --bare is mandatory: without it the CLI blocks on an unreachable keychain.
    assert argv[:3] == ["claude", "--bare", "-p"]
    assert "--allowedTools" in argv and argv[argv.index("--allowedTools") + 1] == ""
    assert argv[-2:] == ["--model", "sonnet"]
    assert brain.spec.auth_probe_args == ("--bare", "-p", "ping")


def test_agy_prompt_flag_is_last_so_it_cannot_swallow_another_option() -> None:
    spec = find_spec("agy")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="agy", model="gemini-3.8-flash-low"), spec,
                             executable="agy")
    argv = brain.inference_argv()
    assert argv.index("--model") < argv.index("-p")
    assert argv[-1] == "-p"


def test_reasoning_effort_reaches_only_the_clis_that_declare_it() -> None:
    """FASE 10: effort is a measured latency lever, so it is explicit and refused loudly."""
    agy = find_spec("agy")
    kilo = find_spec("kilo")
    assert agy is not None and kilo is not None
    applied = HeadlessCliBrain(Settings(provider="agy", model="m", effort="low"), agy,
                               executable="agy").inference_argv()
    assert applied[applied.index("--effort") + 1] == "low"
    assert applied.index("--effort") < applied.index("-p"), "o prompt continua por último"
    variant = HeadlessCliBrain(Settings(provider="kilo", model="m", effort="minimal"), kilo,
                              executable="kilo").inference_argv()
    assert variant[variant.index("--variant") + 1] == "minimal"
    # A CLI whose spec declares no such flag ignores the setting instead of guessing a
    # flag for it: qoder's -p consumes the next argument.
    qoder = find_spec("qoder")
    assert qoder is not None
    untouched = HeadlessCliBrain(Settings(provider="qoder", model="m", effort="low"), qoder,
                                executable="qodercli").inference_argv()
    assert "low" not in untouched and "--effort" not in untouched
    with pytest.raises(BrainError) as failure:
        HeadlessCliBrain(Settings(provider="agy", model="m", effort="turbo"), agy,
                         executable="agy").inference_argv()
    assert failure.value.kind is ErrorKind.CONFIGURATION
    assert "turbo" in failure.value.message and "xhigh" in failure.value.message


def test_inference_argv_omits_the_model_when_the_cli_decides() -> None:
    spec = find_spec("qoder")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="qoder", model=""), spec, executable="qodercli")
    argv = brain.inference_argv()
    assert argv[:3] == ["qodercli", "-p", "-o"]
    assert "--tools" in argv
    assert "-m" not in argv
    assert brain.models_argv() == ["qodercli", "--list-models"]


def test_registry_partitions_local_agents_and_cloud_providers() -> None:
    local = {descriptor.id for descriptor in local_agents()}
    cloud = {descriptor.id for descriptor in cloud_providers()}
    assert {"codex", "claude", "agy", "qoder", "kilo", "kiro"} <= local
    assert {"openai", "gemini", "ollama"} <= cloud
    assert local & cloud == set()


def test_resolve_uses_the_executable_from_the_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.brain.registry.resolve_executable" if False else
                        "app.brain.cli.process.resolve_executable", lambda *args, **kwargs: "C:/fake/claude.exe")
    brain = resolve(Settings(provider="claude", model="sonnet"))
    assert brain.provider_id == "claude"
    assert brain.executable == "C:/fake/claude.exe"


def test_resolve_reports_a_missing_cli_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.brain.cli.process.resolve_executable", lambda *args, **kwargs: None)
    with pytest.raises(BrainError) as failure:
        resolve(Settings(provider="agy", model=""))
    assert failure.value.kind is ErrorKind.EXECUTABLE_MISSING


def test_uncatalogued_cli_is_visible_but_not_selectable() -> None:
    with pytest.raises(BrainError) as failure:
        resolve(Settings(provider="opencode", model="x"))
    assert failure.value.kind is ErrorKind.UNSUPPORTED_FEATURE


def test_detect_reports_missing_executable_without_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detect_module, "resolve_executable", lambda *args, **kwargs: None)

    async def scenario() -> None:
        result = await detect_module.detect(find_spec("claude"))
        assert result.installed is False
        assert result.status == "not_installed"

    asyncio.run(scenario())


def test_detect_all_covers_every_catalogued_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detect_module, "resolve_executable", lambda *args, **kwargs: None)

    async def scenario() -> None:
        detections = await detect_module.detect_all()
        assert {item.cli_id for item in detections} == set(CLI_SPECS)

    asyncio.run(scenario())


def test_oversized_prompt_fails_before_any_process_is_spawned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long AGY round used to surface as a bogus "executable not found"."""
    spec = find_spec("agy")
    assert spec is not None and spec.prompt_on_stdin is False
    brain = HeadlessCliBrain(Settings(provider="agy", model="gemini-3.8-flash-low"), spec, executable="agy")
    spawned: list[list[str]] = []

    async def fake_spawn(argv: list[str], **kwargs: object) -> object:
        spawned.append(list(argv))
        raise AssertionError("spawn must not be reached for an oversized request")

    monkeypatch.setattr("app.brain.cli.headless.spawn", fake_spawn)
    monkeypatch.setattr("app.brain.cli.headless.WINDOWS_COMMAND_LINE_LIMIT", 30_000)

    async def scenario() -> None:
        with pytest.raises(BrainError) as failure:
            await brain.complete([{"role": "user", "content": "x" * 40_000}])
        assert failure.value.kind is ErrorKind.REQUEST_TOO_LARGE
        assert "linha de comando" in failure.value.message

    asyncio.run(scenario())
    assert spawned == []


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line limit")
def test_windows_command_line_overflow_is_a_size_error_not_a_missing_binary() -> None:
    from app.brain.cli.process import spawn

    async def scenario() -> None:
        with pytest.raises(BrainError) as failure:
            await spawn([sys.executable, "x" * 40_000])
        assert failure.value.kind is ErrorKind.REQUEST_TOO_LARGE
        assert "não foi encontrado" not in failure.value.message

    asyncio.run(scenario())


@pytest.mark.parametrize("argv, expected", [
    (["ab", "cd"], 6),  # 'ab cd' plus NUL
    (["tool", "a b"], 11),  # 'tool "a b"' plus NUL
    (["tool", 'a"b'], 10),  # embedded quote must be escaped
    (["tool", "😀"], 8),  # surrogate pair, not one Windows character
    (["tool", "a b\\"], 13),  # trailing backslash doubled before closing quote
    ([], 0),
])
def test_command_line_length_counts_windows_quoting_and_utf16(argv: list[str], expected: int) -> None:
    from app.brain.cli.process import command_line_length

    assert command_line_length(argv) == expected


def test_agy_declares_a_safe_prompt_budget() -> None:
    spec = find_spec("agy")
    assert spec is not None
    assert spec.capabilities.max_safe_prompt_chars == 28_000
    assert find_spec("qoder").capabilities.max_safe_prompt_chars is None


def test_prompt_budget_refuses_without_truncation_or_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = find_spec("agy")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="agy", model="gemini-3.8-flash-low"), spec, executable="agy")
    monkeypatch.setattr("app.brain.cli.headless.spawn", lambda *args, **kwargs: pytest.fail("must not spawn"))
    prompt = "x" * 28_001
    messages = [{"role": "user", "content": prompt}]
    with pytest.raises(BrainError) as failure:
        asyncio.run(brain.complete(messages))
    assert failure.value.kind is ErrorKind.REQUEST_TOO_LARGE
    assert messages[0]["content"] == prompt
    assert brain.cli_calls[-1]["prompt_chars"] == 28_001
    assert brain.cli_calls[-1]["ok"] is False


def test_quoted_prompt_is_checked_even_below_character_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = find_spec("agy")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="agy", model="gemini-3.8-flash-low"), spec, executable="agy")
    monkeypatch.setattr("app.brain.cli.headless.spawn", lambda *args, **kwargs: pytest.fail("must not spawn"))
    monkeypatch.setattr("app.brain.cli.headless.WINDOWS_COMMAND_LINE_LIMIT", 30_000)
    with pytest.raises(BrainError) as failure:
        asyncio.run(brain.complete([{"role": "user", "content": '"' * 16_000}]))
    assert failure.value.kind is ErrorKind.REQUEST_TOO_LARGE


def test_stdin_prompt_is_not_bound_by_the_argv_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = find_spec("qoder")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="qoder", model="Qwen3.8-Flash"), spec, executable="qodercli")
    handles = [_FakeHandle(['{"result":"ok"}\n'])]
    monkeypatch.setattr("app.brain.cli.headless.spawn", _spawn_from(handles))
    prompt = "x" * 40_000
    assert asyncio.run(brain.complete([{"role": "user", "content": prompt}])).text == "ok"
    assert handles[0].written == prompt
    assert handles[0].input_closed and handles[0].stopped
    assert brain.cli_calls[-1]["argv_utf16_units"] < 100
    assert brain.cli_calls[-1]["prompt_budget_chars"] is None


def test_argv_prompt_is_delivered_whole_and_budget_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = find_spec("agy")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="agy", model="gemini-3.8-flash-low"), spec, executable="agy")
    handle = _FakeHandle(['{"result":"ok"}\n'])
    received: list[list[str]] = []
    async def fake_spawn(argv: list[str], **kwargs: object) -> Any:
        received.append(argv)
        return handle
    monkeypatch.setattr("app.brain.cli.headless.spawn", fake_spawn)
    prompt = 'Objetivo: ação 😀 "literal" C:/Área de Trabalho'
    assert asyncio.run(brain.complete([{"role": "user", "content": prompt}])).text == "ok"
    assert received[0][-2:] == ["-p", prompt]
    assert handle.written == ""
    assert brain.cli_calls[-1]["prompt_budget_chars"] == 28_000
    assert brain.cli_calls[-1]["argv_utf16_units"] > len(prompt)


def test_cli_curated_default_model_wins_over_the_listing_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """`agy models` prints the highest-effort model first; that is not a default."""
    async def fake_run_once(argv: list[str], *, timeout: float) -> tuple[int, str, str]:
        return 0, ("Fetching available models...\n"
                   "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
                   "gemini-3.8-flash-low\tGemini 3.8 Flash (Low)\n"), ""

    monkeypatch.setattr("app.brain.cli.headless.run_once", fake_run_once)
    spec = find_spec("agy")
    assert spec is not None and spec.preferred_model == "gemini-3.8-flash-low"
    brain = HeadlessCliBrain(Settings(provider="agy", model="gemini-3.8-flash-low"), spec, executable="agy")
    assert brain.recommended_model == "gemini-3.8-flash-low"

    async def scenario() -> None:
        models = await brain.models()
        # Exactly one default, and it is the curated one, not the first row.
        assert [item.default for item in models] == [False, True]

    asyncio.run(scenario())


def test_qoder_curated_default_is_the_model_that_has_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    """`qodercli --list-models` prints the credit-limited model first."""
    async def fake_run_once(argv: list[str], *, timeout: float) -> tuple[int, str, str]:
        return 0, "MODEL\nQwen3.8-Max\nQwen3.8-Flash\n", ""

    monkeypatch.setattr("app.brain.cli.headless.run_once", fake_run_once)
    spec = find_spec("qoder")
    assert spec is not None and spec.preferred_model == "Qwen3.8-Flash"
    brain = HeadlessCliBrain(Settings(provider="qoder", model="Qwen3.8-Flash"), spec, executable="qodercli")
    assert brain.recommended_model == "Qwen3.8-Flash"

    async def scenario() -> None:
        models = await brain.models()
        assert [item.id for item in models] == ["Qwen3.8-Max", "Qwen3.8-Flash"]
        assert [item.default for item in models] == [False, True]

    asyncio.run(scenario())


def test_clis_without_a_curated_default_keep_the_listing_order() -> None:
    # Claude Code exposes no model listing, so there is nothing to curate.
    spec = find_spec("claude")
    assert spec is not None and spec.preferred_model == "" and spec.models_args == ()


def test_detection_cache_reuses_results(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def fake_detect_all(**kwargs: object) -> list[detect_module.CliDetection]:
        calls.append(1)
        return [detect_module.CliDetection("claude", "Claude Code", True)]

    monkeypatch.setattr(detect_module, "detect_all", fake_detect_all)

    async def scenario() -> None:
        cache = detect_module.DetectionCache(ttl=300)
        await cache.get()
        await cache.get()
        assert len(calls) == 1
        await cache.get(force=True)
        assert len(calls) == 2

    asyncio.run(scenario())


class _FakeProcess:
    """The part of asyncio.subprocess.Process the adapter reads back."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


class _FakeHandle:
    """Stands in for CliProcess: canned lines out, written input recorded."""

    def __init__(self, lines: list[str], *, code: int = 0, error: BrainError | None = None) -> None:
        self._lines = list(lines)
        self._code = code
        self._error = error
        self.written = ""
        self.input_closed = False
        self.stopped = False
        self.process = _FakeProcess(code)

    async def write(self, data: str) -> None:
        self.written += data

    async def close_input(self) -> None:
        self.input_closed = True

    async def readline(self, timeout: float) -> str:
        if self._error is not None:
            raise self._error
        return self._lines.pop(0) if self._lines else ""

    async def wait(self, timeout: float) -> int:
        return self._code

    async def stop(self, *, grace: float = 3.0) -> None:
        self.stopped = True

    def diagnostics(self) -> str:
        return "fake"


def _spawn_from(handles: list[_FakeHandle]) -> Any:
    """A `spawn` replacement handing out one prepared handle per call."""
    pending = list(handles)

    async def spawn(argv: list[str], **kwargs: object) -> Any:
        return pending.pop(0)

    return spawn


def test_each_cli_call_records_spawn_wait_and_total_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow round must be attributable to CLI startup, the model wait, or neither."""
    spec = find_spec("claude")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="claude", model="sonnet"), spec, executable="claude")
    reply = '{"type": "result", "subtype": "success", "result": "ok"}\n'
    handles = [_FakeHandle([reply])]
    monkeypatch.setattr("app.brain.cli.headless.spawn", _spawn_from(handles))
    messages = [{"role": "user", "content": "oi"}]

    async def scenario() -> None:
        completion = await brain.complete(messages)
        assert completion.text == "ok"

    asyncio.run(scenario())

    assert len(brain.cli_calls) == 1
    record = brain.cli_calls[0]
    assert record["ok"] is True
    assert record["prompt_chars"] == len(render_prompt(messages))
    assert record["exit_code"] == 0
    assert record["spawn_ms"] >= 0 and record["total_ms"] >= 0
    first_byte = record["first_byte_ms"]
    assert first_byte is not None and first_byte >= 0
    # A stdin-based spec receives the whole rendered prompt, not a fragment.
    assert handles[0].written == render_prompt(messages)
    assert handles[0].stopped is True
    # The history survives close(): a finished task stays explainable.
    asyncio.run(brain.close())
    assert len(brain.cli_calls) == 1


def test_spawn_timing_measures_the_spawn_and_not_the_whole_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """`spawn_ms` measures only the spawn, separated from the model wait.

    A controlled `perf_counter` makes the two phases exact instead of relying on
    `asyncio.sleep` wall delays, whose Windows timer granularity can undershoot
    the fake delay and make the assertion flaky without any product regression.
    """
    import time as real_time
    from types import SimpleNamespace

    import app.brain.cli.headless as headless

    spec = find_spec("claude")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="claude", model="sonnet"), spec, executable="claude")
    reply = '{"type": "result", "subtype": "success", "result": "ok"}\n'
    now = [1000.0]

    def perf_counter() -> float:
        return now[0]

    def advance(ms: float) -> None:
        now[0] += ms / 1000

    class SlowHandle(_FakeHandle):
        async def readline(self, timeout: float) -> str:
            advance(250)  # the model is slow, the spawn is not
            return await super().readline(timeout)

    handle = SlowHandle([reply])

    async def spawn(argv: list[str], **kwargs: object) -> Any:
        advance(20)
        return handle

    monkeypatch.setattr(headless, "time", SimpleNamespace(perf_counter=perf_counter,
                                                         monotonic=real_time.monotonic))
    monkeypatch.setattr(headless, "spawn", spawn)

    asyncio.run(brain.complete([{"role": "user", "content": "oi"}]))

    record = brain.cli_calls[0]
    assert record["total_ms"] >= 250
    assert record["spawn_ms"] == pytest.approx(20.0), "spawn_ms must be the 20 ms spawn, measured at the spawn"
    assert record["spawn_ms"] < 150


def test_a_failed_or_timed_out_call_still_records_its_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider failure must not erase the timing of the round that failed."""
    spec = find_spec("qoder")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="qoder", model="Qwen3.8-Flash"), spec,
                             executable="qodercli")
    handles = [_FakeHandle(["budget pool exhausted\n"], code=1),
               _FakeHandle([], error=BrainError(ErrorKind.TIMEOUT, "O agente local não respondeu a tempo."))]
    monkeypatch.setattr("app.brain.cli.headless.spawn", _spawn_from(handles))

    async def scenario() -> None:
        for _ in range(2):
            with pytest.raises(BrainError):
                await brain.complete([{"role": "user", "content": "oi"}])

    asyncio.run(scenario())

    assert [record["ok"] for record in brain.cli_calls] == [False, False]
    assert brain.cli_calls[0]["exit_code"] == 1
    assert brain.cli_calls[0]["total_ms"] >= 0
    # Nothing was ever printed by the CLI that timed out.
    assert brain.cli_calls[1]["first_byte_ms"] is None


def test_cli_call_history_is_capped_at_the_newest_32(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long session must not grow an unbounded timing list."""
    spec = find_spec("claude")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="claude", model="sonnet"), spec, executable="claude")
    reply = '{"type": "result", "subtype": "success", "result": "ok"}\n'
    handles = [_FakeHandle([reply]) for _ in range(35)]
    monkeypatch.setattr("app.brain.cli.headless.spawn", _spawn_from(handles))

    async def scenario() -> None:
        for _ in range(35):
            await brain.complete([{"role": "user", "content": "oi"}])

    asyncio.run(scenario())

    assert len(brain.cli_calls) == 32
    assert all(record["ok"] is True for record in brain.cli_calls)


# --- Kilo (7.8.8) -------------------------------------------------------------------
# Kilo differs from the other three CLIs in two ways that each broke a naive
# integration: it streams one JSON event per line instead of one reply object, and
# it is a coding agent whose default agent may edit the machine.


def test_extract_json_events_keeps_every_object_and_refuses_decoration_only() -> None:
    raw = 'log line\n{"type":"a"}\n{"type":"b"}\n'
    assert extract_json_events(raw) == [{"type": "a"}, {"type": "b"}]
    with pytest.raises(BrainError) as failure:
        extract_json_events("nenhum evento aqui\n")
    assert failure.value.kind is ErrorKind.MALFORMED_RESPONSE


def test_kilo_reply_parser_reads_an_event_stream_not_just_the_last_object() -> None:
    """The answer and the usage arrive in different events."""
    events = [
        {"type": "step_start", "part": {"type": "step-start"}},
        {"type": "text", "part": {"type": "text", "text": "ok"}},
        {"type": "step_finish", "part": {"type": "step-finish", "reason": "stop",
                                           "tokens": {"total": 21794, "input": 21771,
                                                      "output": 2}}},
    ]
    text, (input_tokens, output_tokens) = REPLY_PARSERS["kilo-json"]({"events": events})
    assert text == "ok"
    assert (input_tokens, output_tokens) == (21771, 2)
    # The last object alone carries no text: that is the bug this parser prevents.
    assert "text" not in json.dumps(events[-1])
    # Several text events concatenate in order.
    split = {"events": [{"type": "text", "part": {"type": "text", "text": "a"}},
                         {"type": "text", "part": {"type": "text", "text": "b"}}]}
    assert REPLY_PARSERS["kilo-json"](split)[0] == "ab"
    # A version that prints a single object still parses through the same entry.
    single = REPLY_PARSERS["kilo-json"]({"result": "ok", "usage": {"input_tokens": 5}})
    assert single == ("ok", (5, None))


@pytest.mark.parametrize("message,kind", [
    ("Model not found: kilo/does/not-exist. Did you mean: dots-studio/dots-3-note-preview:free?",
     ErrorKind.MODEL_UNAVAILABLE),
    ("You've reached your daily usage limit for Chat.", ErrorKind.QUOTA_EXCEEDED),
])
def test_kilo_error_event_is_a_typed_failure_not_an_empty_answer(message: str, kind: ErrorKind) -> None:
    payload = {"events": [{"type": "error", "error": {"name": "UnknownError",
                                                      "data": {"message": message}}}]}
    with pytest.raises(BrainError) as failure:
        REPLY_PARSERS["kilo-json"](payload)
    assert failure.value.kind is kind


def test_kilo_models_parser_reads_one_id_per_line_and_skips_decoration() -> None:
    stdout = ("kilo/~anthropic/claude-fable-latest\n"
              "kilo/kilo-auto/free\n"
              "kilo/nvidia/nemotron-3-ultra-550b-a55b:free\n"
              "\n"
              "██ ██ ██🬺🬏 ██ ██\n")
    assert MODEL_PARSERS["kilo-models"](stdout) == [
        # The `~` alias survives: it is a real id, and filtering it out hid 17 models.
        ("kilo/~anthropic/claude-fable-latest", "kilo/~anthropic/claude-fable-latest"),
        ("kilo/kilo-auto/free", "kilo/kilo-auto/free"),
        ("kilo/nvidia/nemotron-3-ultra-550b-a55b:free", "kilo/nvidia/nemotron-3-ultra-550b-a55b:free"),
    ]


def test_kilo_is_driven_headless_with_a_read_only_agent_and_stdin_for_the_prompt() -> None:
    spec = find_spec("kilo")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="kilo", model="kilo/kilo-auto/free"), spec,
                             executable="kilo")
    # The `-m` value is the full listing id: Kilo parses it as provider/model.
    assert brain.inference_argv() == ["kilo", "run", "--format", "json", "--agent", "plan",
                                      "-m", "kilo/kilo-auto/free"]
    # The auto-approval switch is the CLI's, never ours to give: `plan` is the agent
    # whose edit permission is denied, which is what keeps a coding agent read-only.
    assert "--auto" not in brain.inference_argv()
    # stdin keeps a long agent round off the 32.767-character command line.
    assert spec.prompt_on_stdin is True
    assert spec.capabilities.max_safe_prompt_chars is None
    assert brain.models_argv() == ["kilo", "models"]
    assert brain.recommended_model == "kilo/kilo-auto/free"


def test_kilo_round_trip_reads_text_and_tokens_from_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = find_spec("kilo")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="kilo", model="kilo/kilo-auto/free"), spec,
                             executable="kilo")
    stream = ('{"type":"step_start","part":{"type":"step-start"}}\n'
              '{"type":"text","part":{"type":"text","text":"ok"}}\n'
              '{"type":"step_finish","part":{"tokens":{"input":21771,"output":2}}}\n')
    handles = [_FakeHandle([stream])]
    monkeypatch.setattr("app.brain.cli.headless.spawn", _spawn_from(handles))
    prompt = "x" * 70_000

    async def scenario() -> None:
        completion = await brain.complete([{"role": "user", "content": prompt}])
        assert completion.text == "ok"
        assert (completion.input_tokens, completion.output_tokens) == (21771, 2)

    asyncio.run(scenario())

    assert handles[0].written == prompt
    assert brain.cli_calls[-1]["ok"] is True
    assert brain.cli_calls[-1]["prompt_budget_chars"] is None
    assert brain.cli_calls[-1]["argv_utf16_units"] < 100


# --- Kiro CLI (2.28.0) ---------------------------------------------------------------
# Kiro is not Kilo: a different vendor, a different CLI, a different protocol. It
# streams ACP events and the only place that says whether a turn succeeded is the
# closing runFinished envelope.


def test_kiro_models_parser_reads_the_single_json_document() -> None:
    stdout = json.dumps({
        "models": [
            {"model_name": "auto", "model_id": "auto", "description": "chosen by task",
             "rate_multiplier": 1.0},
            {"model_name": "qwen3-coder-next", "model_id": "qwen3-coder-next",
             "description": "preview", "rate_multiplier": 0.05},
        ],
        "default_model": "auto",
    })
    assert MODEL_PARSERS["kiro-models"](stdout) == [("auto", "auto"),
                                                    ("qwen3-coder-next", "qwen3-coder-next")]
    # A version that prints columns instead must still parse through the fallback.
    assert MODEL_PARSERS["kiro-models"]("auto\tAuto\n") == [("auto", "Auto")]


def test_kiro_reply_parser_reads_the_final_text_from_the_event_stream() -> None:
    events = [
        {"type": "runStarted", "data": {"engine": "v2"}},
        {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "agent_message_chunk",
                                                       "content": {"type": "text", "text": ""}}}},
        {"type": "sessionUpdate", "data": {"update": {"sessionUpdate": "agent_message_chunk",
                                                       "content": {"type": "text", "text": "PONG"}}}},
        {"type": "metadata", "data": {"meteringUsage": [{"value": 0.02, "unit": "credit"}]}},
        {"type": "runFinished", "data": {"status": "success", "finalText": "PONG"}},
    ]
    text, tokens = REPLY_PARSERS["kiro-json"]({"events": events})
    assert text == "PONG"
    # Credits are not tokens: nothing is invented from a percentage or a multiplier.
    assert tokens == (None, None)
    # The chunks alone are enough when a version omits finalText.
    assert REPLY_PARSERS["kiro-json"]({"events": events[:3]})[0] == "PONG"


def test_kiro_failed_run_is_a_typed_failure_not_an_empty_answer() -> None:
    payload = {"events": [{"type": "runFinished", "data": {"status": "error",
                                                       "finalText": "",
                                                       "error": "usage limit reached"}}]}
    with pytest.raises(BrainError) as failure:
        REPLY_PARSERS["kiro-json"](payload)
    assert failure.value.kind is ErrorKind.QUOTA_EXCEEDED


def test_kiro_is_driven_headless_with_every_tool_untrusted() -> None:
    spec = find_spec("kiro")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="kiro", model="auto"), spec, executable="kiro-cli")
    argv = brain.inference_argv()
    assert argv == ["kiro-cli", "chat", "--no-interactive", "--output-format", "stream-json",
                    "--trust-tools=", "--model", "auto"]
    # The CLI's own auto-approval switch is never ours to give.
    assert "--trust-all-tools" not in argv
    assert spec.prompt_on_stdin is True
    assert brain.models_argv() == ["kiro-cli", "chat", "--list-models", "--format", "json"]
    assert spec.auth_probe_args == ("whoami",)
    assert brain.recommended_model == "auto"


def test_kiro_marks_the_clis_own_default_model_as_the_default(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Kiro already publishes `default_model`; the spec keeps that as `auto`."""
    document = ('{"models":[{"model_name":"glm-5","model_id":"glm-5"},'
                '{"model_name":"auto","model_id":"auto"}],"default_model":"auto"}')

    async def fake_run_once(argv: list[str], *, timeout: float) -> tuple[int, str, str]:
        return 0, document, ""

    monkeypatch.setattr("app.brain.cli.headless.run_once", fake_run_once)
    spec = find_spec("kiro")
    assert spec is not None and spec.preferred_model == "auto"
    brain = HeadlessCliBrain(Settings(provider="kiro", model="auto"), spec, executable="kiro-cli")

    async def scenario() -> list[tuple[str, bool]]:
        return [(item.id, item.default) for item in await brain.models()]

    # Exactly one default, and it is the curated one rather than the first row.
    assert asyncio.run(scenario()) == [("glm-5", False), ("auto", True)]


def test_kilo_default_model_is_the_free_router_not_the_first_listed_model(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The first listed model is a metered one; the curated default is the free router."""
    spec = find_spec("kilo")
    assert spec is not None and spec.preferred_model == "kilo/kilo-auto/free"

    async def fake_run_once(argv: list[str], *, timeout: float) -> tuple[int, str, str]:
        return 0, "kilo/~openai/gpt-sol-latest\nkilo/kilo-auto/balanced\nkilo/kilo-auto/free\n", ""

    monkeypatch.setattr("app.brain.cli.headless.run_once", fake_run_once)
    brain = HeadlessCliBrain(Settings(provider="kilo", model="kilo/kilo-auto/free"), spec,
                             executable="kilo")

    async def scenario() -> None:
        models = await brain.models()
        assert [item.id for item in models] == ["kilo/~openai/gpt-sol-latest", "kilo/kilo-auto/balanced",
                                                "kilo/kilo-auto/free"]
        assert [item.default for item in models] == [False, False, True]

    asyncio.run(scenario())


# --- FASE 4: sessão por tarefa -------------------------------------------------------
# No installed CLI declares a verified multi-turn protocol yet (every catalogued spec keeps
# ``session_args`` empty and stays one-shot), so the session transport is exercised with a
# spec that declares one and a fake process that streams one turn at a time. The mechanism
# is what the tests pin: one spawn per task, ``session_reused`` on the following calls,
# clean-respawn retry after a broken turn (once), and close() killing the tree.


def _session_spec() -> Any:
    """A catalogued CLI spec plus a declared, test-verified session protocol."""
    spec = find_spec("kilo")
    assert spec is not None
    return replace(spec, session_args=("session",), session_end="__END__")


def _session_turn(text: str) -> list[str]:
    return [f'{{"type":"text","part":{{"type":"text","text":"{text}"}}}}\n', "__END__\n"]


def _session_brain(spec: Any) -> HeadlessCliBrain:
    return HeadlessCliBrain(Settings(provider="kilo", model="kilo/kilo-auto/free"), spec,
                            executable="kilo")


def test_session_reuses_one_process_across_the_whole_task(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _session_spec()
    brain = _session_brain(spec)
    handle = _FakeHandle(_session_turn("um") + _session_turn("dois") + _session_turn("tres"))
    spawns: list[list[str]] = []

    async def spawn(argv: list[str], **kwargs: object) -> Any:
        spawns.append(argv)
        return handle

    monkeypatch.setattr("app.brain.cli.headless.spawn", spawn)

    async def scenario() -> None:
        for message in ("um", "dois", "tres"):
            completion = await brain.complete([{"role": "user", "content": message}])
            assert completion.text == message
        await brain.close()

    asyncio.run(scenario())

    assert len(spawns) == 1, "a sessão deveria nascer uma vez por tarefa"
    assert spawns[0] == ["kilo", "session", "-m", "kilo/kilo-auto/free"]
    assert handle.input_closed is False, "o stdin fica aberto entre as rodadas"
    assert [record["session_reused"] for record in brain.cli_calls] == [False, True, True]
    assert brain.cli_calls[0]["spawn_ms"] >= 0
    assert brain.cli_calls[1]["spawn_ms"] == 0.0 and brain.cli_calls[2]["spawn_ms"] == 0.0
    assert handle.stopped is True, "close() encerra a árvore da sessão"


def test_session_retries_once_on_a_clean_spawn_after_a_broken_turn(
        monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _session_spec()
    brain = _session_brain(spec)
    broken = _FakeHandle(["nao e json\n", "__END__\n"])
    fresh = _FakeHandle(_session_turn("ok"))
    monkeypatch.setattr("app.brain.cli.headless.spawn", _spawn_from([broken, fresh]))

    async def scenario() -> None:
        completion = await brain.complete([{"role": "user", "content": "oi"}])
        assert completion.text == "ok"

    asyncio.run(scenario())

    assert broken.stopped is True
    assert [record["ok"] for record in brain.cli_calls] == [False, True]
    # O respawn limpo não é reuso: a sessão quebrada foi morta antes.
    assert [record["session_reused"] for record in brain.cli_calls] == [False, False]
    assert brain.cli_calls[1]["spawn_ms"] >= 0


def test_session_a_second_failure_is_reported_and_not_retried_again(
        monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _session_spec()
    brain = _session_brain(spec)
    first = _FakeHandle(["nao e json\n", "__END__\n"])
    second = _FakeHandle(["tambem nao\n", "__END__\n"])
    monkeypatch.setattr("app.brain.cli.headless.spawn", _spawn_from([first, second]))

    with pytest.raises(BrainError) as failure:
        asyncio.run(brain.complete([{"role": "user", "content": "oi"}]))

    assert failure.value.kind is ErrorKind.MALFORMED_RESPONSE
    assert [record["ok"] for record in brain.cli_calls] == [False, False]  # no máximo 2 tentativas
    assert first.stopped is True and second.stopped is True

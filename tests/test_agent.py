from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.computer.ufo_engine import verify_open
from app.brain.contracts import Capabilities
from app.codex_app_server import CodexAppServer, CodexError, IncompleteTurn, login_url_allowed
from app.core.controller import TaskController
from app.llm import AnthropicProvider, OpenAICompatibleProvider, Plan, ProviderError, UnsupportedTask, parse_plan
from app.settings import ConfigurationError, Settings, _protect
from benchmark_report import summarize


def agent_settings(**overrides: object) -> Settings:
    """Settings for a fixture that asserts what the *agent* does.

    FASE 8 resolves a provable command like "Abra a Calculadora." without inference, so
    a fixture about HostAgent prompts, provider timing or cold starts has to opt out of
    it; the deterministic path has its own tests.
    """
    overrides.setdefault("fast_path", False)
    return Settings(**overrides)  # type: ignore[arg-type]

@pytest.mark.parametrize("value", [
    '{"supported":true,"application":"notepad","operation":"open","reason":""}',
    '```json\n{"supported":true,"application":"notepad","operation":"open","reason":""}\n```',
])
def test_plan_authorizes_only_notepad(value: str) -> None:
    parse_plan(value).authorize()


@pytest.mark.parametrize("value", ["Done", "{}", "[]", '{"supported":"true"}',
    '{"supported":true,"application":"notepad","operation":"open","reason":"","command":"cmd"}',
    "a" * 20_000,
])
def test_invalid_output_never_authorized(value: str) -> None:
    with pytest.raises(ProviderError):
        parse_plan(value)


@pytest.mark.parametrize("application,operation", [("notepad", "close"), ("cmd", "open"),
    ("notepad.exe & powershell", "open"), ("notepad", "delete"), ("explorer", "open")])
def test_destructive_and_unimplemented_plans_blocked(application: str, operation: str) -> None:
    with pytest.raises(UnsupportedTask):
        Plan(supported=True, application=application, operation=operation, reason="Blocked").authorize()


def test_llm_done_is_not_evidence() -> None:
    assert verify_open([], [{"message": "Done"}]) == (False, [])


@pytest.mark.parametrize("windows", [[], [{"handle": 10, "pid": 1, "process": "cmd.exe", "visible": True}],
    [{"handle": 10, "pid": 1, "process": "notepad.exe", "visible": False}],
    [{"handle": 0, "pid": 1, "process": "notepad.exe", "visible": True}]])
def test_missing_or_wrong_window_not_success(windows: list[dict]) -> None:
    assert not verify_open([], windows)[0]


def test_preexisting_notepad_does_not_prove_launch() -> None:
    window = {"handle": 10, "pid": 1, "process": "Notepad.exe", "visible": True}
    assert not verify_open([window], [window])[0]
    assert verify_open([], [window]) == (True, [window])


@pytest.mark.parametrize("url", ["http://example.com/v1", "https://user:secret@example.com/v1",
    "https://example.com/v1?key=secret", "file:///tmp", "https://example.com/#token"])
def test_insecure_endpoint_rejected(url: str) -> None:
    with pytest.raises(ConfigurationError):
        Settings(provider="custom", model="model", base_url=url).validate()


@pytest.mark.parametrize("url", ["http://127.0.0.1:1234/v1", "http://localhost:8000/v1", "https://example.com/v1"])
def test_compatible_endpoint(url: str) -> None:
    Settings(provider="custom", model="model", base_url=url).validate()


def test_dpapi_roundtrip() -> None:
    plaintext = b"unit-test-credential-not-a-real-api-key"
    protected = _protect(plaintext)
    assert plaintext not in protected
    assert _protect(protected, decrypt=True) == plaintext


def test_settings_repr_never_contains_key() -> None:
    assert "SECRET" not in repr(Settings(api_key="SECRET"))


def test_codex_settings_need_a_model_but_no_api_key() -> None:
    Settings(provider="codex", model="account-model").validate()
    with pytest.raises(ConfigurationError, match="modelo"):
        Settings(provider="codex").validate()
    with pytest.raises(ConfigurationError, match="login oficial"):
        Settings(provider="codex", model="model", api_key="secret").validate()
    with pytest.raises(ConfigurationError, match="URL base"):
        Settings(provider="codex", model="model", base_url="https://example.test").validate()


@pytest.mark.parametrize("url", ["https://auth.openai.com/login", "https://chatgpt.com/auth"])
def test_codex_login_accepts_official_https_hosts(url: str) -> None:
    assert login_url_allowed(url)


@pytest.mark.parametrize("url", ["http://auth.openai.com", "https://openai.com.attacker.test/",
    "https://user@openai.com", "https://example.test/auth", "javascript:alert(1)"])
def test_codex_login_rejects_untrusted_urls(url: str) -> None:
    assert not login_url_allowed(url)


def test_codex_tool_calls_fail_closed() -> None:
    class Input:
        def __init__(self) -> None:
            self.data = b""
        def write(self, data: bytes) -> None:
            self.data += data

    class Process:
        stdin = Input()

    client = CodexAppServer()
    client.process = Process()  # type: ignore[assignment]
    client._handle_message({"id": 4, "method": "item/commandExecution/requestApproval", "params": {}})
    assert json.loads(client.process.stdin.data) == {"id": 4, "result": {"decision": "decline"}}
    client.process.stdin.data = b""
    client._handle_message({"id": 5, "method": "item/tool/call", "params": {}})
    assert json.loads(client.process.stdin.data)["result"]["success"] is False
    client.process.stdin.data = b""
    client._handle_message({"id": 6, "method": "item/permissions/requestApproval", "params": {}})
    response = json.loads(client.process.stdin.data)
    assert response["result"]["permissions"]["network"]["enabled"] is False
    assert response["result"]["permissions"]["fileSystem"] == {"read": [], "write": []}


def test_codex_incomplete_turn_retries_once_and_quota_limit_fails_fast() -> None:
    from app.llm import Completion

    async def scenario() -> None:
        client = CodexAppServer()
        failed: list[int] = []

        async def always_incomplete(model: str, system: str, inputs: list[dict]) -> Completion:
            failed.append(1)
            raise IncompleteTurn("The stream disconnected.", retryable=True)

        client._infer_once = always_incomplete  # type: ignore[method-assign]
        with pytest.raises(CodexError, match="UFO: The stream disconnected"):
            await client.complete("account-model", [{"role": "user", "content": "passo"}])
        assert len(failed) == 2

        recovered: list[int] = []

        async def flaky(model: str, system: str, inputs: list[dict]) -> Completion:
            recovered.append(1)
            if len(recovered) == 1:
                raise IncompleteTurn("temporary failure", retryable=True)
            return Completion('{"status":"FINISH"}')

        client._infer_once = flaky  # type: ignore[method-assign]
        action = await client.complete("account-model", [{"role": "user", "content": "passo"}])
        assert action.text == '{"status":"FINISH"}'
        assert len(recovered) == 2

        limited: list[int] = []

        async def quota_exceeded(model: str, system: str, inputs: list[dict]) -> Completion:
            limited.append(1)
            raise IncompleteTurn("You have hit your usage limit.", retryable=False)

        client._infer_once = quota_exceeded  # type: ignore[method-assign]
        with pytest.raises(CodexError, match="usage limit"):
            await client.complete("account-model", [{"role": "user", "content": "passo"}])
        assert len(limited) == 1

    asyncio.run(scenario())


def test_codex_complete_runs_one_inference_and_releases_the_thread() -> None:
    from app.llm import Completion

    class Input:
        def __init__(self) -> None:
            self.data = b""
        def write(self, data: bytes) -> None:
            self.data += data
        async def drain(self) -> None:
            pass

    class Output:
        # The transport frames one message per line with ``readuntil`` and an explicit
        # limit, so an oversized message fails by name instead of as a bare reader error.
        def __init__(self, lines: list[dict]) -> None:
            self.lines = list(lines)
            self.buffer = b""
        async def readuntil(self, separator: bytes) -> bytes:
            while separator not in self.buffer:
                if not self.lines:
                    raise asyncio.IncompleteReadError(self.buffer, None)
                self.buffer += (json.dumps(self.lines.pop(0)) + "\n").encode()
            line, self.buffer = self.buffer.split(separator, 1)
            return line + separator

    class Process:
        returncode = None
        def __init__(self, lines: list[dict]) -> None:
            self.stdin = Input()
            self.stdout = Output(lines)

    answer = '{"observation":"window","thought":"open","status":"FINISH"}'
    lines = [
        {"id": 1, "result": {"thread": {"id": "thread-1"}}},
        {"id": 2, "result": {}},
        {"method": "thread/tokenUsage/updated", "params": {"threadId": "thread-1",
            "tokenUsage": {"last": {"inputTokens": 120, "outputTokens": 7}}}},
        {"method": "item/completed", "params": {"threadId": "thread-1",
            "item": {"type": "agentMessage", "text": answer}}},
        {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"status": "completed",
            "items": [{"type": "agentMessage", "text": answer}]}}},
        {"id": 3, "result": {}},
    ]

    async def scenario() -> None:
        client = CodexAppServer()
        process = Process(lines)
        client.process = process  # type: ignore[assignment]
        action = await client.complete("account-model", [
            {"role": "system", "content": "Official UFO HostAgent prompt"},
            {"role": "user", "content": "Abra a Calculadora."},
        ])
        assert action == Completion(answer, 120, 7)
        writes = [json.loads(line) for line in process.stdin.data.decode().splitlines()]
        assert [w.get("method") for w in writes] == ["thread/start", "turn/start", "thread/unsubscribe"]
        assert writes[0]["params"]["developerInstructions"].startswith("Official UFO HostAgent prompt")
        assert writes[1]["params"]["input"] == [{"type": "text", "text": "Abra a Calculadora."}]
    asyncio.run(scenario())


def test_an_oversized_app_server_message_is_a_named_transport_failure() -> None:
    """Measured 2026-10-09: the reader's own error surfaced as the task's result.

    Two chat runs of "pesquisa por michel jackson no spotify" died on the fifth inference
    with "Separator is found, but chunk is longer than limit" as the *task's* message, at
    a point where the AppAgent had already typed the query into Spotify. That sentence is
    what ``readline`` raises over its limit, so it is named here as a transport failure and
    classified as a provider failure, never as the user's configuration.
    """
    import app.codex_app_server as module

    class Output:
        async def readuntil(self, separator: bytes) -> bytes:
            raise asyncio.LimitOverrunError("Separator is found, but chunk is longer than limit",
                                            1_048_576)

    class Process:
        returncode = None
        stdin = None
        stdout = Output()

    async def scenario() -> None:
        client = module.CodexAppServer()
        client.process = Process()  # type: ignore[assignment]
        with pytest.raises(module.CodexError) as failure:
            await client._read(1)
        assert failure.value.code == "transport"
        assert "bytes por linha deste transporte" in str(failure.value)
    asyncio.run(scenario())


def test_a_large_app_server_message_is_delivered_whole_and_named(caplog: pytest.LogCaptureFixture) -> None:
    """The ceiling is a boundary, not a suspicion: 300 000 bytes arrive intact, and loudly."""
    import logging

    import app.codex_app_server as module

    payload = {"method": "item/completed", "params": {"text": "x" * 300_000}}

    class Output:
        def __init__(self) -> None:
            self.buffer = (json.dumps(payload) + "\n").encode()

        async def readuntil(self, separator: bytes) -> bytes:
            line, self.buffer = self.buffer.split(separator, 1)
            return line + separator

    class Process:
        returncode = None
        stdin = None
        stdout = Output()

    async def scenario() -> dict:
        client = module.CodexAppServer()
        client.process = Process()  # type: ignore[assignment]
        return await client._read(1)
    with caplog.at_level(logging.WARNING, logger="app.codex_app_server"):
        assert asyncio.run(scenario()) == payload
    assert any("bytes do app-server do Codex" in record.getMessage() for record in caplog.records)


def test_a_codex_transport_failure_is_not_a_configuration_error() -> None:
    from app.brain.codex_brain import translate
    from app.brain.contracts import ErrorKind

    error = translate(CodexError("mensagem grande demais", code="transport", detail="LimitOverrunError"))
    assert error.kind == ErrorKind.TRANSPORT
    assert "configuration" not in {error.kind.value}


def test_openai_and_custom_payloads() -> None:
    async def scenario(provider_name: str) -> None:
        provider = OpenAICompatibleProvider(Settings(provider=provider_name, model="test-model", api_key="test-key",
                                                     base_url="http://127.0.0.1:9876/v1"))
        calls = []
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            body = json.loads(request.content)
            assert body["model"] == "test-model"
            assert body["messages"][1]["content"] == "Abra o Bloco de Notas."
            assert request.headers["Authorization"] == "Bearer test-key"
            if provider_name == "openai":
                assert body["max_completion_tokens"] == 2048
                assert body["store"] is False
            else:
                assert body["max_tokens"] == 2048
                assert "response_format" not in body
            return httpx.Response(200, json={"choices": [{"message": {"content":
                '{"supported":true,"application":"notepad","operation":"open","reason":""}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20}})
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await provider.generate("Abra o Bloco de Notas.")
            result.plan.authorize()
            assert (result.input_tokens, result.output_tokens) == (10, 20)
            assert len(calls) == 1
        finally:
            await provider.close()
    asyncio.run(scenario("openai"))
    asyncio.run(scenario("custom"))


def test_anthropic_payload() -> None:
    async def scenario() -> None:
        provider = AnthropicProvider(Settings(provider="anthropic", model="test-model", api_key="test-key"))
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert request.headers["Authorization"] == "Bearer test-key"
            assert "system" in json.loads(request.content)
            return httpx.Response(200, json={"content": [{"type": "text", "text":
                '{"supported":true,"application":"notepad","operation":"open","reason":""}'}],
                "usage": {"input_tokens": 15, "output_tokens": 25}})
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            result = await provider.generate("Open Notepad")
            assert result.input_tokens == 15
        finally:
            await provider.close()
    asyncio.run(scenario())


def test_api_error_does_not_echo_secret_or_body() -> None:
    async def scenario() -> None:
        provider = OpenAICompatibleProvider(Settings(model="model", api_key="secret-key"))
        await provider.client.aclose()
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(401, text="secret-key echoed by API")))
        try:
            with pytest.raises(ProviderError) as error:
                await provider.generate("Open Notepad")
            assert "secret-key" not in str(error.value)
        finally:
            await provider.close()
    asyncio.run(scenario())


class WaitingEngine:
    """Unit-test fixture ONLY. Not used by desktop, CLI or live benchmarks."""
    process = None
    worker_ready = False  # every fixture task here is a COLD start
    def __init__(self) -> None:
        self.stopped = False
        self.calls = 0
    async def start(self) -> None:
        pass
    async def execute(self) -> dict:
        self.calls += 1
        await asyncio.sleep(10)
        raise AssertionError("Cancellation should interrupt this")
    async def stop(self) -> None:
        self.stopped = True


def test_codex_provider_supplies_official_ufo_inference_without_preplanning(tmp_path: Path) -> None:
    from app.llm import Completion

    class CodexFixture:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.model = ""
            self.capabilities = Capabilities.of()
        async def complete(self, messages: list[dict]) -> Completion:
            self.calls.append((self.model, messages[1]["content"]))
            return Completion('{"observation":"window","thought":"open","status":"FINISH"}')
        async def close(self) -> None:
            pass

    class EngineFixture:
        process = None
        worker_ready = False
        async def start(self) -> None:
            pass
        async def run_task(self, instruction, completion, confirm, *,
                     approval_mode: str = "manual") -> dict:
            # The engine hands the agent name along with the official messages.
            response = await completion([{"role": "system", "content": "Official UFO HostAgent prompt"},
                                         {"role": "user", "content": instruction}], "HostAgent")
            assert json.loads(response)["status"] == "FINISH"
            return {"status": "SUCCESS", "message": "window verified", "actions": 1, "steps": 1,
                    "evidence": [{"process": "CalculatorApp.exe", "verified": True}]}
        async def stop(self) -> None:
            pass

    async def scenario() -> None:
        controller = TaskController(agent_settings(provider="codex", model="account-model",
                                             logs_path=tmp_path, collect_metrics=False))
        codex = CodexFixture()
        codex.model = controller.settings.model
        controller.brain = codex  # type: ignore[assignment]
        controller.engine = EngineFixture()  # type: ignore[assignment]
        result = await controller.run("Abra a Calculadora.")
        assert result.status == "SUCCESS"
        assert result.provider == "codex"
        assert result.llm_calls == 1
        assert codex.calls == [("account-model", "Abra a Calculadora.")]
        assert result.evidence[0]["process"] == "CalculatorApp.exe"
        await controller.close()
    asyncio.run(scenario())


def test_write_plan_prepares_and_binds_a_new_notepad_document(tmp_path: Path) -> None:
    class PlannedEngine:
        process = None
        worker_ready = False
        def __init__(self) -> None:
            self.prepare_document = False
            self.written = ""
        async def start(self) -> None:
            pass
        async def execute(self, *, prepare_document: bool = False) -> dict:
            self.prepare_document = prepare_document
            return {"before": [], "document_bound": True, "observation_time_ms": 1,
                    "execution_time_ms": 2, "actions": 1, "steps": 1}
        async def verify(self) -> dict:
            return {"windows": [{"handle": 11, "pid": 22, "process": "Notepad.exe", "visible": True}],
                    "text_verified": True}
        async def write(self, text: str) -> dict:
            self.written = text
            return {"execution_time_ms": 3, "actions": 1, "steps": 1}
        async def stop(self) -> None:
            pass

    async def scenario() -> None:
        controller = TaskController(agent_settings(logs_path=tmp_path, collect_metrics=False))
        fixture = PlannedEngine()
        controller.engine = fixture
        plan = Plan(supported=True, application="notepad", operation="write", text="Hello World", reason="")
        result = await controller.run("write Hello World", engine_only=True, engine_plan=plan)
        assert result.status == "SUCCESS"
        assert fixture.prepare_document is True
        assert fixture.written == "Hello World"
        await controller.close()
    asyncio.run(scenario())


def test_cancel_stops_engine_and_returns_cancelled(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = TaskController(agent_settings(logs_path=tmp_path, collect_metrics=False))
        fixture = WaitingEngine()
        controller.engine = fixture
        task = asyncio.create_task(controller.run("open_notepad", engine_only=True))
        await asyncio.sleep(0.03)
        controller.cancel()
        result = await task
        assert result.status == "CANCELLED"
        assert fixture.stopped
        assert not controller.active
        assert fixture.calls == 1
        row = json.loads((tmp_path / "tasks.jsonl").read_text())
        assert row["status"] == "CANCELLED"
        assert row["instruction"] == "[not stored]"
    asyncio.run(scenario())


def test_timeout_is_failure_not_success(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = TaskController(agent_settings(logs_path=tmp_path, timeout_seconds=0.02, collect_metrics=False))
        fixture = WaitingEngine()
        controller.engine = fixture
        result = await controller.run("open_notepad", engine_only=True)
        assert result.status == "FAILED"
        assert fixture.stopped
    asyncio.run(scenario())


def test_missing_credentials_fail_before_computer_action(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = TaskController(agent_settings(logs_path=tmp_path, collect_metrics=False))
        fixture = WaitingEngine()
        controller.engine = fixture
        result = await controller.run("Abra o Bloco de Notas")
        assert result.status == "FAILED"
        assert fixture.calls == 0
        assert result.llm_calls == 0
    asyncio.run(scenario())


def test_worker_readiness_timeout_is_a_clear_computer_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow cold start must not surface as an unexplained task timeout.

    The worker needs several seconds to import UFO²; the readiness wait therefore
    has its own budget and its own message, distinct from the task budget.
    """
    from app.computer import ufo_engine

    class FakeProcess:
        returncode = None
        # A pid that cannot exist: stop() must never signal a real process here.
        pid = 999_999_999

        def kill(self) -> None:
            pass

        async def wait(self) -> int:
            return 0

    async def fake_spawn(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess()

    async def never_ready(self: object) -> dict[str, object]:
        await asyncio.sleep(30)
        return {}

    monkeypatch.setattr(ufo_engine.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(ufo_engine.UFOEngine, "_read", never_ready)
    monkeypatch.setattr(ufo_engine, "WORKER_STARTUP_SECONDS", 0.05)

    async def scenario() -> None:
        engine = ufo_engine.UFOEngine(Settings(logs_path=tmp_path, timeout_seconds=1))
        with pytest.raises(ufo_engine.ComputerError) as failure:
            await engine.start()
        assert "não ficou pronto" in str(failure.value)
        assert "worker.log" in str(failure.value)
        assert engine.process is None

    asyncio.run(scenario())



def test_worker_receives_controller_identity_without_provider_credentials(tmp_path, monkeypatch):
    from app.computer import ufo_engine

    captured = {}
    class FakeProcess:
        returncode = 0

    async def spawn(*args, **kwargs):
        captured.update(kwargs["env"])
        return FakeProcess()

    async def ready(self):
        return {"ready": True}

    monkeypatch.setenv("UFO_CONTROLLER_PID", "123")
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY"):
        monkeypatch.setenv(key, "test-only-secret")
    monkeypatch.setattr(ufo_engine.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(ufo_engine.UFOEngine, "_read", ready)

    async def scenario():
        engine = ufo_engine.UFOEngine(Settings(logs_path=tmp_path))
        try:
            await engine.start()
            assert captured["UFO_CONTROLLER_PID"] == str(ufo_engine.os.getpid())
            assert not {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY"} & captured.keys()
        finally:
            await engine.stop()
    asyncio.run(scenario())


def test_report_does_not_mix_engine_and_api_results() -> None:
    assert "Tasks: 0" in summarize([{"scope": "engine_only", "status": "SUCCESS"}])
    row = {"scope": "end_to_end", "status": "SUCCESS", "duration_ms": 5000, "steps": 1,
           "llm_calls": 1, "llm_time_ms": 100, "observation_time_ms": 50,
           "execution_time_ms": 4800, "verification_time_ms": 50, "startup_time_ms": 0}
    text = summarize([row])
    assert "Tasks: 1" in text
    assert "Success rate: 100.0%" in text
    assert "P95 latency (nearest rank): 5.000s" in text


def test_every_error_kind_has_a_deliberate_failure_class() -> None:
    """A new provider error must be classified on purpose, not fall into a catch-all."""
    from app.brain.contracts import ErrorKind
    from app.core.controller import FAILURE_KINDS, _KIND_BY_ERROR_KIND

    assert set(ErrorKind) <= set(_KIND_BY_ERROR_KIND)
    assert set(_KIND_BY_ERROR_KIND.values()) <= set(FAILURE_KINDS)


def test_provider_timing_survives_a_failing_task(tmp_path: Path) -> None:
    """The slow runs are the failing ones (§59): their CLI timing must not be dropped."""
    from app.computer.ufo_engine import ComputerError
    from app.llm import Completion

    class CliBrain:
        def __init__(self) -> None:
            self.model = "fixture-model"
            self.capabilities = Capabilities.of()
            self.cli_calls: list[dict] = []
        async def complete(self, messages: list[dict]) -> Completion:
            self.cli_calls.append({"ok": True, "spawn_ms": 300.0, "first_byte_ms": 1200.0,
                                   "total_ms": 2500.0, "prompt_chars": 10, "exit_code": 0,
                                   "session_reused": True})
            return Completion('{"status":"FINISH"}', 5, 1)
        async def close(self) -> None:
            pass

    class FailingEngine:
        process = None
        worker_ready = False
        async def start(self) -> None:
            pass
        async def run_task(self, instruction, completion, confirm, *,
                     approval_mode: str = "manual") -> dict:
            await completion([{"role": "user", "content": "x"}], "HostAgent")
            raise ComputerError("executor UFO falhou a meio")
        async def stop(self) -> None:
            pass

    async def scenario() -> None:
        controller = TaskController(agent_settings(provider="codex", model="fixture-model",
                                             logs_path=tmp_path, collect_metrics=False))
        brain = CliBrain()
        controller.brain = brain  # type: ignore[assignment]
        controller.engine = FailingEngine()  # type: ignore[assignment]
        result = await controller.run("Abra a Calculadora.")
        assert result.status == "FAILED"
        assert result.failure_kind == "ACTION_FAILURE"
        assert result.provider_startup_ms == 300.0
        assert result.provider_wait_ms == 2200.0
        assert len(result.provider_calls) == 1
        assert result.session_reused == 1   # FASE 4: a rodada reusou a sessão da tarefa
        assert result.prompt_calls[0]["ok"] is True
        await controller.close()
    asyncio.run(scenario())


def test_a_task_that_never_reached_inference_inherits_no_provider_timing(tmp_path: Path) -> None:
    """A Brain outlives a task; its history must not leak into one that never called it."""
    class StaleBrain:
        def __init__(self) -> None:
            self.model = "fixture-model"
            self.capabilities = Capabilities.of()
            self.cli_calls: list[dict] = [{"ok": True, "spawn_ms": 999.0, "first_byte_ms": 999.0,
                                           "total_ms": 999.0, "prompt_chars": 1, "exit_code": 0}]
        async def complete(self, messages: list[dict]) -> object:
            raise AssertionError("Nenhuma inferência deveria acontecer aqui")
        async def close(self) -> None:
            pass

    async def scenario() -> None:
        controller = TaskController(agent_settings(provider="codex", model="fixture-model",
                                             logs_path=tmp_path, collect_metrics=False))
        controller.brain = StaleBrain()  # type: ignore[assignment]
        result = await controller.run("   ")
        assert result.status == "FAILED"
        assert result.failure_kind == "CONFIGURATION"
        assert result.llm_calls == 0
        assert result.provider_calls == []
        assert (result.provider_startup_ms, result.provider_wait_ms) == (0, 0)
        assert result.session_reused == 0
        await controller.close()
    asyncio.run(scenario())


def test_a_readiness_wait_that_timed_out_is_still_counted() -> None:
    """An aborted wait must not look free: it is the wait worth counting most."""
    import time as clock
    from app.computer.windows_tools import WindowsTools

    async def never(_: str) -> bool:
        return False

    tools = WindowsTools(state=object(), confirm=never)
    tools.record_wait(clock.perf_counter() - 0.5, 9)
    assert tools.waits_ms >= 500.0
    assert tools.wait_polls == 9


def test_settings_pages_resolve_names_instead_of_navigating_the_ui() -> None:
    """§4 (FASE 4): the page names are a table, not something to be discovered by clicking."""
    from app.computer.settings_pages import SettingsPage, SettingsPageError, resolve_page

    assert resolve_page("Sistema > Som").uri == "ms-settings:sound"
    assert resolve_page("som").uri == "ms-settings:sound"
    assert resolve_page("Configurações > Sistema > Som").uri == "ms-settings:sound"
    assert resolve_page("ÁUDIO").uri == "ms-settings:sound"
    assert resolve_page("  Aplicativos  ").uri == "ms-settings:appsfeatures"
    assert resolve_page("Windows Update").uri == "ms-settings:windowsupdate"
    assert resolve_page("sistema > tela").uri == "ms-settings:display"
    # A catalogued URI resolves to the same page, with the same proof of arrival.
    known = resolve_page("ms-settings:display")
    assert known.name == "Tela" and known.verify == ("Tela", "Display")
    # An uncatalogued URI is still accepted, but only its window can be verified, and
    # that has to be visible in the evidence instead of looking like a page check.
    carried = resolve_page("ms-settings:easeofaccess-cursor")
    assert (carried.uri, carried.verify) == ("ms-settings:easeofaccess-cursor", ())
    with pytest.raises(SettingsPageError) as unknown:
        resolve_page("teletransporte")
    assert "Disponíveis" in str(unknown.value)
    with pytest.raises(SettingsPageError):
        resolve_page("   ")
    # Two pages carrying the same alias is refused loudly: a data table can regress,
    # and opening the wrong page silently is worse than stopping.
    import app.computer.settings_pages as settings_pages
    doubled = settings_pages.PAGES + (SettingsPage("Som (cópia)", "ms-settings:sound-copy",
                                                   ("som",), ("Som",)),)
    original, settings_pages.PAGES = settings_pages.PAGES, doubled
    try:
        with pytest.raises(SettingsPageError) as ambiguous:
            resolve_page("som")
    finally:
        settings_pages.PAGES = original
    assert "ambígua" in str(ambiguous.value)
    assert resolve_page("som").name == "Som"


def test_the_launch_budget_is_adaptive_but_never_wider_than_the_ceiling() -> None:
    """§4 (FASE 4): one constant for every launch is what the measurements disproved."""
    from app.computer.windows_tools import LAUNCH_BUDGET_CEILING, LAUNCH_BUDGET_DEFAULT, launch_budget

    assert launch_budget("ms-settings") == LAUNCH_BUDGET_DEFAULT["ms-settings"]
    assert launch_budget("folder") == LAUNCH_BUDGET_DEFAULT["folder"]
    # Ready in under a second: the default allowance is the floor, never a target.
    assert launch_budget("ms-settings", [423.0, 480.0, 290.0]) == LAUNCH_BUDGET_DEFAULT["ms-settings"]
    # A target measured slower than the default floor gets its own allowance, derived from
    # its own samples: 4 x 2 500 ms, still under the ceiling.
    assert launch_budget("ms-settings", [2500.0] * 5) == 10.0
    assert launch_budget("ms-settings", [2500.0] * 5) > LAUNCH_BUDGET_DEFAULT["ms-settings"]
    # Never wider than the ceiling that used to be the answer for all of them.
    assert launch_budget("exe", [9000.0] * 5) == LAUNCH_BUDGET_CEILING
    # `exe` is the kind whose own slowest observation reaches the ceiling (a cold Steam,
    # measured 2026-10-09 at 8 891-8 951 ms idle and past 10 s with four applications
    # opening), so its floor is the ceiling: a first launch has no history to extend it.
    assert LAUNCH_BUDGET_DEFAULT["exe"] == LAUNCH_BUDGET_CEILING
    # Only the most recent samples count, so one slow launch does not widen it forever.
    assert launch_budget("exe", [9000.0] + [300.0] * 5) == LAUNCH_BUDGET_DEFAULT["exe"]


def test_the_verification_budget_is_adaptive_but_never_wider_than_the_ceiling() -> None:
    """§6 (FASE 6): the fixed 3 s deadline becomes a budget sized by this kind's history."""
    from app.computer.windows_tools import (VERIFY_BUDGET_CEILING, VERIFY_BUDGET_DEFAULT,
                                            verification_budget)

    assert verification_budget("window") == VERIFY_BUDGET_DEFAULT["window"]
    assert verification_budget("settings") == VERIFY_BUDGET_DEFAULT["settings"]
    # A transition measured in tens of milliseconds keeps the floor, never a target.
    assert verification_budget("window", [51.0, 128.0]) == VERIFY_BUDGET_DEFAULT["window"]
    # A kind measured slow gets its own allowance, derived from its own samples.
    assert verification_budget("window", [700.0] * 5) == 2.8
    # Never wider than the constant that used to be the answer for all of them.
    assert verification_budget("window", [9000.0] * 5) == VERIFY_BUDGET_CEILING
    # Only the most recent samples count, so one stuck wait does not widen it forever.
    assert verification_budget("window", [9000.0] + [60.0] * 5) == VERIFY_BUDGET_DEFAULT["window"]


def test_a_settled_state_is_decided_instead_of_burning_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """§6 (FASE 6): measured 3 089 ms polling a state that had already stopped changing."""
    import time as clock
    import app.computer.windows_tools as windows_tools
    from app.computer.windows_tools import WindowsTools

    async def never(_: str) -> bool:
        return False

    monkeypatch.setattr(windows_tools, "VERIFY_SETTLE_FLOOR_MS", 0.1)
    tools = WindowsTools(state=object(), confirm=never)

    async def scenario() -> tuple[bool, int, float, float]:
        return await tools._await_condition("window", lambda: (False, ("alive", False)))

    started = clock.perf_counter()
    satisfied, polls, elapsed_ms, budget_ms = asyncio.run(scenario())
    wall_ms = (clock.perf_counter() - started) * 1000
    assert satisfied is False
    assert wall_ms < 1000.0          # the 3 s deadline was not paid for a decided state
    assert elapsed_ms < 1000.0
    assert polls >= 3
    assert budget_ms == windows_tools.VERIFY_BUDGET_DEFAULT["window"] * 1000
    assert tools.waits_ms > 0 and tools.wait_polls == polls
    assert tools.verify_failfast_saved_ms > 2000.0   # what the old constant always paid
    assert tools._verify_history == {}               # a stuck wait never widens the budget


def test_a_transition_that_settles_late_is_still_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """The floor protects a real transition: a late state is confirmed, not cut short."""
    import app.computer.windows_tools as windows_tools
    from app.computer.windows_tools import WindowsTools

    async def never(_: str) -> bool:
        return False

    monkeypatch.setattr(windows_tools, "VERIFY_SETTLE_FLOOR_MS", 0.05)
    tools = WindowsTools(state=object(), confirm=never)
    reads = {"n": 0}

    def step() -> tuple[bool, object]:
        # The snapshot keeps changing while the transition is in flight, so the wait cannot
        # mistake a moving state for a settled one and cut a real transition short.
        reads["n"] += 1
        return reads["n"] >= 4, ("alive", reads["n"])

    async def scenario() -> tuple[bool, int, float, float]:
        return await tools._await_condition("window", step)

    satisfied, polls, _, _ = asyncio.run(scenario())
    assert satisfied is True and polls == 4
    assert tools.verify_failfast_saved_ms == 0.0    # nothing was skipped
    assert tools._verify_history.get("window")      # a real transition feeds the budget


def test_a_verify_window_read_of_a_loading_tree_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """§6 (FASE 6): one read produced the FASE 5 false negative; a changing tree is re-read."""
    import app.computer.windows_tools as windows_tools
    from app.computer.windows_tools import WindowsTools

    class Control:
        def __init__(self, text: str) -> None:
            self.text = text

        def get_value(self) -> str:
            return self.text

    class Window:
        handle = 3

        def __init__(self) -> None:
            self.reads = 0

        def window_text(self) -> str:
            return "UFO-Test-2"

        def descendants(self, control_type: str = "") -> list[Control]:
            self.reads += 1
            items = [Control("UFO-Test-2")]
            # The item only appears on the second read: the tree was still loading.
            return items + [Control("marcador-fase6.txt")] if self.reads > 1 else items

    async def never(_: str) -> bool:
        return False

    window = Window()
    monkeypatch.setattr(windows_tools.user32, "IsWindow", lambda handle: 1)
    tools = WindowsTools(state=type("State", (), {"selected_app_window": window})(), confirm=never)
    server = tools.server()

    async def scenario() -> dict:
        result = await server.call_tool("verify_window", {"expected_text": "marcador-fase6"})
        return getattr(result, "structured_content", None) or getattr(result, "data", None) or {}

    data = asyncio.run(scenario())
    assert data["verified"] is True
    assert data["text_matches"] == ["marcador-fase6.txt"]
    assert data["polls"] == 2 and window.reads == 2
    assert 0 < data["verification_ms"] < 1000.0


def test_a_verify_window_miss_is_decided_once_the_tree_stops_changing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A miss the state repeats is final: it is not polled for the rest of a budget."""
    import app.computer.windows_tools as windows_tools
    from app.computer.windows_tools import WindowsTools

    class Control:
        def get_value(self) -> str:
            return "UFO-Test-2"

    class Window:
        handle = 5

        def __init__(self) -> None:
            self.reads = 0

        def window_text(self) -> str:
            return "UFO-Test-2"

        def descendants(self, control_type: str = "") -> list[Control]:
            self.reads += 1
            return [Control()]

    async def never(_: str) -> bool:
        return False

    window = Window()
    monkeypatch.setattr(windows_tools.user32, "IsWindow", lambda handle: 1)
    tools = WindowsTools(state=type("State", (), {"selected_app_window": window})(), confirm=never)
    server = tools.server()

    async def scenario() -> dict:
        result = await server.call_tool("verify_window", {"expected_text": "nao-existe"})
        return getattr(result, "structured_content", None) or getattr(result, "data", None) or {}

    data = asyncio.run(scenario())
    assert data["verified"] is False and data["text_matches"] == []
    assert window.reads == 2                     # two agreeing reads, then decided
    assert data["verification_ms"] < 1000.0
    assert tools.verify_failfast_saved_ms == 0.0  # a re-read has no deadline to have saved


def test_a_window_that_reports_no_controls_is_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """§6 (FASE 6): the FASE 5 miss was a tree that reported nothing — it is read again."""
    import app.computer.windows_tools as windows_tools
    from app.computer.windows_tools import WindowsTools

    class Control:
        def __init__(self, text: str) -> None:
            self.text = text

        def get_value(self) -> str:
            return self.text

    class Window:
        handle = 7

        def __init__(self) -> None:
            self.reads = 0

        def window_text(self) -> str:
            return "UFO-Test-2"

        def descendants(self, control_type: str = "") -> list[Control]:
            self.reads += 1
            # An empty read is not an observation: the tree has not been built yet.
            if self.reads > 2:
                return [Control("Carregando"), Control("UFO-Test-2")]
            return []

    async def never(_: str) -> bool:
        return False

    window = Window()
    monkeypatch.setattr(windows_tools.user32, "IsWindow", lambda handle: 1)
    tools = WindowsTools(state=type("State", (), {"selected_app_window": window})(), confirm=never)
    server = tools.server()

    async def scenario() -> dict:
        result = await server.call_tool("verify_window", {"expected_text": "UFO-Test-2"})
        return getattr(result, "structured_content", None) or getattr(result, "data", None) or {}

    data = asyncio.run(scenario())
    assert data["verified"] is True
    assert data["text_matches"] == ["UFO-Test-2"]
    assert data["polls"] == 3 and window.reads == 3   # two empty reads, then the item


def test_a_close_the_ui_reports_as_failed_does_not_wait_for_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """§6 (FASE 6): a close the UI refuses is reported at once, not after the wait."""
    import sys
    import time as clock
    import types
    import app.computer.windows_tools as windows_tools
    from app.computer.windows_tools import WindowsTools

    async def approve(_: str) -> bool:
        return True

    class Window:
        handle = 11

        def process_id(self) -> int:
            return 1

        def window_text(self) -> str:
            return "Sem título - Bloco de notas"

    class Receiver:
        def __init__(self, *args: object) -> None:
            pass

    class Refusing:
        def __init__(self, *args: object) -> None:
            pass

        def execute(self) -> str:
            return "An error occurred while closing the window"

    # The vendor package is not on the test path; the mutation engine `close` uses is
    # stubbed at the seam it is imported from, which is what the tool actually calls.
    stub = types.ModuleType("ufo.automator.ui_control.controller")
    stub.ControlReceiver = Receiver  # type: ignore[attr-defined]
    stub.AtomicCommand = Refusing  # type: ignore[attr-defined]
    for name in ("ufo", "ufo.automator", "ufo.automator.ui_control",
                 "ufo.automator.ui_control.controller"):
        monkeypatch.setitem(sys.modules, name, stub if name.endswith(".controller")
                            else types.ModuleType(name))
    monkeypatch.setattr(windows_tools.psutil, "Process",
                        lambda pid: type("P", (), {"name": lambda self: "Notepad.exe"})())
    tools = WindowsTools(state=type("State", (), {"selected_app_window": Window()})(), confirm=approve)
    server = tools.server()

    async def scenario() -> None:
        await server.call_tool("window_action", {"operation": "close"})

    started = clock.perf_counter()
    with pytest.raises(Exception):
        asyncio.run(scenario())
    assert (clock.perf_counter() - started) * 1000 < 500.0
    assert tools.waits_ms == 0.0                 # nothing was waited for
    assert tools.verify_failfast_saved_ms == 0.0
    assert tools.evidence == []


def test_the_desktop_reset_kills_only_the_apps_the_battery_itself_started(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """§7 (FASE 7) and R6: each run starts with nothing the battery provoked still running,
    and nothing the user had open is ever closed."""
    import psutil
    import pywinauto
    import desktop_fixture

    class Process:
        def __init__(self, pid: int, name: str) -> None:
            self.info = {"pid": pid, "name": name}
            self.killed = False

        def kill(self) -> None:
            self.killed = True

    processes = [Process(1, "Notepad.exe"), Process(2, "explorer.exe"),
                 Process(3, "CalculatorApp.exe"), Process(4, "SystemSettings.exe")]
    closed: list[int] = []

    class Window:
        def __init__(self, handle: int, klass: str, name: str) -> None:
            self.handle = handle
            self._class = klass
            self.element_info = type("Info", (), {"name": name})()

        def class_name(self) -> str:
            return self._class

        def close(self) -> None:
            closed.append(self.handle)

    class FakeDesktop:
        def windows(self) -> list[Window]:
            return [Window(10, "CabinetWClass", "Downloads"), Window(11, "Notepad", "Bloco")]

    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter(processes))
    monkeypatch.setattr(pywinauto, "Desktop", lambda **kwargs: FakeDesktop())
    # R6: ownership is recorded, never read from the image name. `explorer.exe` (pid 2) is
    # not owned, so it is not a candidate even though it is running.
    monkeypatch.setattr(desktop_fixture, "owned_pids", lambda: {1, 3, 4})
    monkeypatch.setattr(desktop_fixture, "_OWNED_WINDOWS", {10})
    monkeypatch.setattr(desktop_fixture, "load_owned", lambda: None)
    monkeypatch.setattr(desktop_fixture, "save_owned", lambda: None)

    first = desktop_fixture.reset_desktop()
    # The battery's own processes die; anything else on the desktop is left alone.
    assert [process.killed for process in processes] == [True, False, True, True]
    assert closed == [10]                       # only the owned Explorer window is closed
    assert first["killed"] == ["Notepad.exe (1)", "CalculatorApp.exe (3)", "SystemSettings.exe (4)"]
    assert first["explorer_windows"] == ["Downloads (10)"]
    # Idempotent: a second reset on a clean desktop reports the same thing.
    assert desktop_fixture.reset_desktop() == first
    # Never killed to end a run: the shell, and anything the battery did not start.
    assert "explorer.exe" not in first["killed"]


def test_ownership_is_a_pid_and_a_creation_time_not_a_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """R6: a process with the right name but no record of ours is never closed."""
    import psutil
    import desktop_fixture

    class Process:
        def __init__(self, pid: int, name: str, created: float) -> None:
            self.info = {"pid": pid, "name": name}
            self._created = created
            self.killed = False

        def create_time(self) -> float:
            return self._created

        def kill(self) -> None:
            self.killed = True

    survivor = Process(7, "cmd.exe", 100.0)
    ours = Process(8, "cmd.exe", 500.0)
    monkeypatch.setattr(desktop_fixture, "owned_pids", lambda: {8})
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter([survivor, ours]))
    assert desktop_fixture.kill_apps(("cmd.exe",)) == ["cmd.exe (8)"]
    assert ours.killed and not survivor.killed


def test_each_task_starts_from_its_own_described_state(monkeypatch: pytest.MonkeyPatch,
                                                       tmp_path: Path) -> None:
    """§7 (FASE 7): T05/T06/T07 stop depending on each other having run."""
    import desktop_fixture

    monkeypatch.setattr(desktop_fixture, "desktop_folder", lambda: tmp_path)
    monkeypatch.setattr(desktop_fixture, "reset_desktop",
                        lambda: {"killed": [], "explorer_windows": []})
    monkeypatch.setattr(desktop_fixture, "_start_calculator", lambda: True)
    created, renamed = tmp_path / "UFO-Test", tmp_path / "UFO-Test-2"

    # Start from the contaminated desktop the baseline actually had: both folders, one
    # holding leftovers of an earlier run.
    (renamed / "resto").mkdir(parents=True)
    created.mkdir()
    (created / "resto.txt").write_text("x", encoding="utf-8")

    # T05 creates the folder, so neither may survive.
    report = desktop_fixture.prepare_for(5)
    assert not created.exists() and not renamed.exists()
    assert sorted(report["removed"]) == ["UFO-Test", "UFO-Test-2"]

    # T06 renames into UFO-Test-2: the source must exist empty, the destination must not.
    report = desktop_fixture.prepare_for(6)
    assert created.is_dir() and not any(created.iterdir())
    assert not renamed.exists()
    assert report["created"] == ["UFO-Test"]

    # T07 only opens the folder: it must exist whether or not T05/T06 ran, and no leftover
    # source of a hypothetical T06 may be sitting next to it.
    desktop_fixture.prepare_for(7)
    assert renamed.is_dir() and not created.exists()

    # T10 closes the Calculator, so the fixture has to have opened it.
    assert desktop_fixture.prepare_for(10)["launched"] == ["CalculatorApp.exe"]

    # A task with no fixture of its own leaves the desktop exactly as it found it.
    desktop_fixture.prepare_for(7)
    before = sorted(path.name for path in tmp_path.iterdir())
    report = desktop_fixture.prepare_for(1)
    assert sorted(path.name for path in tmp_path.iterdir()) == before
    assert report["removed"] == [] and report["created"] == [] and report["launched"] == []
    assert report["precondition"] == desktop_fixture.precondition(1)
    # Every task in the battery has a described starting state.
    assert all(desktop_fixture.precondition(task) for task in range(1, 11))
    assert len({desktop_fixture.precondition(task) for task in range(1, 11)}) == 10


def test_a_launch_that_exits_in_error_does_not_wait_for_a_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """§4 (FASE 4): measured 12 027 ms spent waiting for a window that could not come."""
    import time as clock
    import app.computer.windows_tools as windows_tools
    from app.computer.windows_tools import WindowsTools

    class Dead:
        # A real Popen always carries the pid of the process it started; the launcher reads
        # it, because the window that proves an application opened can be raised by an
        # instance that was already running rather than by this process.
        pid = 4242

        def __init__(self, code: int) -> None:
            self.code = code

        def poll(self) -> int:
            return self.code

    async def never(_: str) -> bool:
        return False

    monkeypatch.setattr(windows_tools.shutil, "which", lambda name: "C:/Windows/System32/where.exe")
    monkeypatch.setattr(windows_tools.subprocess, "Popen", lambda command, **kwargs: Dead(2))
    monkeypatch.setattr(windows_tools, "windows", lambda: [])
    monkeypatch.setattr(windows_tools, "launch_budget", lambda kind, observed=(): 5.0)
    tools = WindowsTools(state=object(), confirm=never)

    async def scenario() -> float:
        started = clock.perf_counter()
        with pytest.raises(RuntimeError, match="código 2"):
            await tools.launch("where.exe")
        return clock.perf_counter() - started

    elapsed = asyncio.run(scenario())
    assert elapsed < 1.0, elapsed
    assert tools.launch_failfast_saved_ms > 4000.0
    assert 0 < tools.waits_ms < 1000.0  # the aborted wait is still counted, briefly
    assert tools.evidence == []


def test_a_zero_exit_is_a_handoff_to_another_process_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """calc.exe exits 0 while CalculatorApp.exe opens behind it: that wait must continue."""
    import app.computer.windows_tools as windows_tools
    from app.computer.windows_tools import WindowsTools

    class Handoff:
        pid = 4243

        def poll(self) -> int:
            return 0

    async def never(_: str) -> bool:
        return False

    monkeypatch.setattr(windows_tools.shutil, "which", lambda name: "C:/Windows/System32/calc.exe")
    monkeypatch.setattr(windows_tools.subprocess, "Popen", lambda command, **kwargs: Handoff())
    monkeypatch.setattr(windows_tools, "windows", lambda: [])
    monkeypatch.setattr(windows_tools, "launch_budget", lambda kind, observed=(): 0.2)
    tools = WindowsTools(state=object(), confirm=never)

    async def scenario() -> None:
        with pytest.raises(TimeoutError):
            await tools.launch("calc.exe")

    asyncio.run(scenario())
    assert tools.launch_failfast_saved_ms == 0.0


def test_a_settings_page_that_never_shows_its_text_is_not_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """FASE 4: the fast path verifies the page text, so it cannot fake a successful arrival."""
    import pywinauto
    import app.computer.windows_tools as windows_tools
    from app.computer.windows_tools import WindowsTools

    class Control:
        def __init__(self, text: str) -> None:
            self.text = text

        def get_value(self) -> str:
            return self.text

    class Window:
        def __init__(self, texts: list[str]) -> None:
            self.texts = texts

        def descendants(self) -> list[Control]:
            return [Control(text) for text in self.texts]

    class FakeDesktop:
        def __init__(self, texts: list[str]) -> None:
            self.texts = texts

        def window(self, handle: int) -> object:
            texts = self.texts
            return type("Wrapper", (), {"wrapper_object": lambda self: Window(texts)})()

    async def never(_: str) -> bool:
        return False

    monkeypatch.setattr(pywinauto, "Desktop", lambda **kwargs: FakeDesktop(["Rede e Internet", "Wi-Fi"]))
    # FASE 6: the page text never arrives, so the wait is decided by the settled state and
    # not by a clock. The floor is shortened only to keep this test fast.
    monkeypatch.setattr(windows_tools, "VERIFY_SETTLE_FLOOR_MS", 0.05)
    tools = WindowsTools(state=object(), confirm=never)

    async def fake_launch(target: str) -> dict[str, object]:
        return {"operation": "launch", "target": target, "verified": True, "handle": 11, "readiness_ms": 240.0}

    tools.launch = fake_launch  # type: ignore[method-assign]

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="não confirmada"):
            await tools.open_settings_page("Som")
        wrong_page = tools.evidence[-1]
        assert wrong_page["verified"] is False and wrong_page["text_matches"] == []
        # A page that never shows its text is decided early, and the deadline the old
        # constant would have paid in full is reported as saved rather than spent.
        assert wrong_page["verification_ms"] < 1000.0
        assert tools.verify_failfast_saved_ms > 2000.0
        arrived = await tools.open_settings_page("Rede e Internet")
        assert arrived["verified"] is True and arrived["verified_by"] == "page_text"
        assert arrived["text_matches"] == ["Rede e Internet"]
        assert arrived["uri"] == "ms-settings:network"

    asyncio.run(scenario())


def test_control_ranking_is_deterministic_and_keeps_what_matters() -> None:
    """§5 (FASE 5): the control list is a context budget, ranked — not the window's size."""
    from app.computer.controls import CONTROL_LIMIT_DEFAULT, rank_controls

    window = {"id": "1", "name": "Downloads - Explorador de Arquivos", "type": "Window"}
    controls = [window] + [{"id": str(n), "name": f"Item {n}", "type": "Text"} for n in range(2, 136)]
    controls.insert(77, {"id": "downloads-item", "name": "Downloads", "type": "ListItem"})
    assert len(controls) == 136

    narrowed = rank_controls(controls, keywords=("Abra o Explorer e vá para Downloads.",),
                             limit=CONTROL_LIMIT_DEFAULT)
    assert len(narrowed) == CONTROL_LIMIT_DEFAULT
    assert narrowed[0] is window  # the anchor the agent refers to is never dropped
    ids = [control["id"] for control in narrowed]
    assert "downloads-item" in ids  # a control the request names survives any cut
    assert ids == [control["id"] for control in controls if control["id"] in set(ids)]  # order kept
    assert all(control in controls for control in narrowed)
    # Same window, same request, same list: same prompt. No sampling, no clock.
    assert rank_controls(controls, keywords=("Downloads",), limit=CONTROL_LIMIT_DEFAULT) == \
        rank_controls(controls, keywords=("Downloads",), limit=CONTROL_LIMIT_DEFAULT)


def test_control_ranking_prefers_names_then_interactivity() -> None:
    """A named control outranks a merely clickable one; among unnamed ones, interactivity wins."""
    from app.computer.controls import rank_controls

    controls = [{"id": "1", "name": "Janela", "type": "Window"},
                {"id": "2", "name": "Rótulo", "type": "Text"},
                {"id": "3", "name": "Botão qualquer", "type": "Button"},
                {"id": "4", "name": "Hello World", "type": "Text"},
                {"id": "5", "name": "Editor", "type": "Edit"}]
    kept = {control["id"] for control in rank_controls(controls, keywords=("escreva Hello World",),
                                                        limit=3)}
    assert kept == {"1", "4", "5"}


def test_the_control_limit_never_cuts_a_list_that_already_fits() -> None:
    """The smallest window that completed a measured task had 30 controls; 30 is the floor."""
    from app.computer.controls import CONTROL_LIMIT_DEFAULT, rank_controls

    controls = [{"id": str(n), "name": f"Item {n}", "type": "ListItem"} for n in range(30)]
    assert CONTROL_LIMIT_DEFAULT == 30
    assert rank_controls(controls) == controls
    assert rank_controls(controls, limit=0) == controls  # zero means "send everything"
    assert rank_controls(controls + [{"id": "30", "name": "Item 30", "type": "ListItem"}], limit=5) != controls
    assert len(rank_controls(controls * 5, limit=5)) == 5


def test_a_failed_round_escalates_to_the_full_control_list_exactly_once() -> None:
    """§5 (FASE 5): narrowing must cost at most one round, never the task."""
    from app.computer.controls import apply_escalation

    class Prompter:
        full_controls = False

    class Runtime:
        def __init__(self) -> None:
            self.escalate_controls = True
            self.controls_escalated = 0

    prompter, runtime = Prompter(), Runtime()
    assert apply_escalation(prompter, runtime) is True
    assert prompter.full_controls is True
    assert runtime.controls_escalated == 1
    assert runtime.escalate_controls is False  # consumed by this round
    # The next round narrows again unless it also fails.
    assert apply_escalation(prompter, runtime) is False
    assert prompter.full_controls is False
    assert runtime.controls_escalated == 1
    assert apply_escalation(None, runtime) is False  # a prompter we cannot address


def test_failure_kind_speaks_one_vocabulary_for_every_cause() -> None:
    """Optimising the wrong component starts with blaming the wrong one (§32)."""
    from app.brain.contracts import BrainError, ErrorKind
    from app.computer.ufo_engine import ComputerError
    from app.core.controller import classify_failure_kind

    assert classify_failure_kind(BrainError(ErrorKind.QUOTA_EXCEEDED, "sem cota")) == "PROVIDER_FAILURE"
    assert classify_failure_kind(BrainError(ErrorKind.REQUEST_TOO_LARGE, "grande")) == "PROVIDER_FAILURE"
    assert classify_failure_kind(BrainError(ErrorKind.MALFORMED_RESPONSE, "lixo")) == "PROVIDER_FAILURE"
    assert classify_failure_kind(BrainError(ErrorKind.AUTH_REQUIRED, "login")) == "CONFIGURATION"
    assert classify_failure_kind(BrainError(ErrorKind.CANCELLED, "parado")) == "CANCELLED"
    assert classify_failure_kind(BrainError(ErrorKind.TIMEOUT, "lento")) == "TIMEOUT"
    assert classify_failure_kind(ComputerError("UFO falhou")) == "ACTION_FAILURE"
    assert classify_failure_kind(asyncio.CancelledError()) == "CANCELLED"
    assert classify_failure_kind(TimeoutError()) == "TIMEOUT"
    assert classify_failure_kind(ValueError("instrução inválida")) == "CONFIGURATION"
    assert classify_failure_kind(RuntimeError("inesperado")) == "INTERNAL_ERROR"


def test_prompt_accounting_counts_text_blocks_not_python_repr(tmp_path: Path) -> None:
    from app.llm import Completion

    class Engine:
        process = None
        worker_ready = False
        async def start(self) -> None:
            pass
        async def stop(self) -> None:
            pass
        async def run_task(self, instruction, completion, confirm, *,
                     approval_mode: str = "manual") -> dict:
            await completion([{"role": "system", "content": "abc"},
                              {"role": "user", "content": [{"type": "text", "text": "ação 😀"},
                               {"type": "image_url", "image_url": {"url": "data:ignored"}}]}], "AppAgent")
            return {"status": "NOT_VERIFIED", "evidence": []}

    class BrainFixture(SilentBrain):
        async def complete(self, messages: list[dict]) -> Completion:
            return Completion("{}")

    async def scenario() -> None:
        controller = TaskController(agent_settings(provider="codex", model="fixture-model",
                                             logs_path=tmp_path, collect_metrics=False))
        controller.brain = BrainFixture()  # type: ignore[assignment]
        controller.engine = Engine()  # type: ignore[assignment]
        result = await controller.run("teste")
        assert result.prompt_calls[0]["chars"] == 9
        assert result.prompt_calls[0]["char_basis"] == "text_content_v2"
        await controller.close()
    asyncio.run(scenario())


def test_prompt_accounting_attributes_every_call_to_its_agent(tmp_path: Path) -> None:
    """A task's prompt cost must be attributable to HostAgent or AppAgent, not lumped."""
    from app.llm import Completion

    class AccountingEngine:
        process = None
        worker_ready = False
        async def start(self) -> None:
            pass
        async def run_task(self, instruction, completion, confirm, *,
                     approval_mode: str = "manual") -> dict:
            messages = [{"role": "system", "content": "s" * 100}, {"role": "user", "content": "u" * 50}]
            await completion(messages, "HostAgent")
            await completion(messages, "AppAgent")
            return {"status": "SUCCESS", "message": "ok", "actions": 2, "steps": 2,
                    "evidence": [{"operation": "launch", "verified": True}],
                    "uia_controls_collected": 41, "uia_controls_sent": 41, "waits_ms": 120.0}
        async def stop(self) -> None:
            pass

    class BrainFixture:
        def __init__(self) -> None:
            self.model = "fixture-model"
            self.capabilities = Capabilities.of()
        async def complete(self, messages: list[dict]) -> Completion:
            return Completion('{"status":"FINISH"}', 10, 2)
        async def close(self) -> None:
            pass

    async def scenario() -> None:
        controller = TaskController(agent_settings(provider="codex", model="fixture-model",
                                             logs_path=tmp_path, collect_metrics=False))
        controller.brain = BrainFixture()  # type: ignore[assignment]
        controller.engine = AccountingEngine()  # type: ignore[assignment]
        result = await controller.run("Abra a Calculadora.")
        assert result.status == "SUCCESS"
        assert [call["agent"] for call in result.prompt_calls] == ["HostAgent", "AppAgent"]
        assert [call["base_chars"] for call in result.prompt_calls] == [150, 150]
        assert [call["chars"] for call in result.prompt_calls] == [150 + result.memory_context_chars, 150]
        assert result.prompt_chars == 300 + result.memory_context_chars
        assert result.prompt_calls[1]['memory_context_chars'] == 0
        assert result.prompt_calls[0]["ok"] is True
        assert (result.input_tokens, result.output_tokens) == (20, 4)
        assert (result.uia_controls_collected, result.uia_controls_sent) == (41, 41)
        assert result.waits_ms == 120.0
        assert result.cold_start is True
        assert result.false_success is False
        assert result.failure_kind == ""
        await controller.close()
    asyncio.run(scenario())


class SilentBrain:
    """Minimal Brain double: the controller reads model/capabilities and closes it.

    ``complete`` refuses, because a task driven by these fixtures must never reach
    inference — the engine double answers instead.
    """
    def __init__(self, model: str = "fixture-model") -> None:
        self.model = model
        self.capabilities = Capabilities.of()

    async def complete(self, messages: list[dict]) -> object:
        raise AssertionError("Esta tarefa não deve chamar o Brain")

    async def close(self) -> None:
        pass


def test_an_unverified_attempt_that_was_repeated_and_verified_is_not_a_false_success() -> None:
    """Measured 2026-10-09: the Spotify search was confirmed and still called unconfirmed.

    The failed first ``uia_verify`` of the results page sat in the evidence next to the
    second, verified one, so the run was downgraded to "sucesso não confirmado" although
    the search had run and its result had been read back independently.
    """
    from app.core.controller import unresolved_evidence

    retried = [
        {"operation": "select_application_window", "verified": True, "handle": 1901472},
        {"operation": "uia_verify", "verified": False, "handle": 1901472,
         "error": "expected_text não encontrado no Document"},
        {"operation": "uia_verify", "verified": True, "handle": 1901472},
    ]
    assert unresolved_evidence(retried) == []

    # Nothing was retried, so the claim has no evidence to stand on.
    unproven = [
        {"operation": "select_application_window", "verified": True, "handle": 1901472},
        {"operation": "uia_verify", "verified": False, "handle": 1901472},
    ]
    assert len(unresolved_evidence(unproven)) == 1

    # A verified read of a *different* window does not resolve this window's attempt.
    other_window = [
        {"operation": "uia_verify", "verified": False, "handle": 1901472},
        {"operation": "uia_verify", "verified": True, "handle": 4242},
    ]
    assert len(unresolved_evidence(other_window)) == 1

    # The same chat, after the collection fix: a *dispatched* action is not a claim. Enter
    # only dispatched the query, and the verified read of the result row is its
    # postcondition being checked.
    dispatched = [
        {"operation": "set_edit_text", "verified": True, "handle": 1901472},
        {"operation": "keyboard_input", "verified": False, "handle": 1901472,
         "needs_postcondition": True},
        {"operation": "uia_verify", "verified": True, "handle": 1901472},
    ]
    assert unresolved_evidence(dispatched) == []

    # A dispatch nobody ever read back stays unresolved, in this window or another one.
    never_read = [
        {"operation": "keyboard_input", "verified": False, "handle": 1901472,
         "needs_postcondition": True},
    ]
    assert len(unresolved_evidence(never_read)) == 1
    read_elsewhere = [
        {"operation": "keyboard_input", "verified": False, "handle": 1901472,
         "needs_postcondition": True},
        {"operation": "uia_verify", "verified": True, "handle": 4242},
    ]
    assert len(unresolved_evidence(read_elsewhere)) == 1


def test_success_without_evidence_is_counted_as_a_false_success(tmp_path: Path) -> None:
    """The one outcome the verification design exists to prevent must be countable."""
    class EvidenceFreeEngine:
        process = None
        worker_ready = False
        async def start(self) -> None:
            pass
        async def run_task(self, instruction, completion, confirm, *,
                     approval_mode: str = "manual") -> dict:
            return {"status": "SUCCESS", "message": "o modelo disse que terminou", "actions": 1,
                    "steps": 1, "evidence": []}
        async def stop(self) -> None:
            pass

    async def scenario() -> None:
        controller = TaskController(agent_settings(provider="codex", model="fixture-model",
                                             logs_path=tmp_path, collect_metrics=False))
        controller.brain = SilentBrain()  # type: ignore[assignment]
        controller.engine = EvidenceFreeEngine()  # type: ignore[assignment]
        result = await controller.run("Feche a Calculadora.")
        assert result.status == "NOT_VERIFIED"
        assert result.failure_kind == "VERIFICATION_FAILURE"
        assert result.evidence == []
        assert result.false_success is True
        await controller.close()
    asyncio.run(scenario())


def test_the_guard_only_counts_identical_failures_without_new_evidence() -> None:
    """§3: repeating a *successful* action can be legitimate; repeating a failure cannot."""
    from app.computer.batches import NoProgressGuard, action_signature

    class Command:
        def __init__(self, tool_name: str, parameters: dict | None = None) -> None:
            self.tool_type, self.tool_name = "action", tool_name
            self.parameters = parameters or {}

    click = [Command("click_input", {"id": "3"})]
    other = [Command("click_input", {"id": "4"})]
    assert action_signature(click) == action_signature([Command("click_input", {"id": "3"})])
    assert action_signature(click) != action_signature(other)
    # Only single-action batches are comparable.
    assert action_signature(click + other) is None
    assert action_signature([]) is None

    guard = NoProgressGuard(limit=3)
    signature = action_signature(click)
    for streak in range(1, 4):
        assert guard.before_batch(signature) is False
        guard.after_batch(signature, failed=True, evidence_count=0)
        assert guard._streak == streak
    assert guard.before_batch(signature) is True, "a terceira falha idêntica deve abortar"

    # Another failure record is not progress and cannot clear the streak.
    guard.after_batch(signature, failed=True, evidence_count=5)
    assert guard.before_batch(signature) is True
    # Success never triggers the guard, however often it repeats.
    for _ in range(10):
        guard.after_batch(signature, failed=False, evidence_count=5)
    assert guard.before_batch(signature) is False
    # A different action starts its own streak instead of inheriting one.
    guard.after_batch(signature, failed=True, evidence_count=5)
    guard.after_batch(action_signature(other), failed=True, evidence_count=5)
    assert guard._streak == 1


def test_the_guard_stops_rounds_that_carry_no_action() -> None:
    """Measured on T08: 21 rounds, no action, 118 s. Planning without acting is waste."""
    from app.computer.batches import NoProgressGuard

    guard = NoProgressGuard(idle_limit=5)
    for round_number in range(1, 6):
        assert guard.before_batch(None, idle=True) is False
        guard.after_batch(None, failed=False, evidence_count=0, idle=True)
        assert guard._idle == round_number
    assert guard.before_batch(None, idle=True) is True
    # New evidence means the round accomplished something, even with no action.
    guard.after_batch(None, failed=False, evidence_count=3, idle=True)
    assert guard.before_batch(None, idle=True) is False
    # A round with an action clears the idle streak.
    guard.after_batch(None, failed=False, evidence_count=3, idle=True)
    guard.after_batch(("click_input", "{}"), failed=False, evidence_count=3)
    assert guard._idle == 0


def test_a_cold_start_does_not_consume_the_task_budget(tmp_path: Path) -> None:
    """§16: the boot is its own phase; it must not eat the agent's time."""
    class SlowBootEngine:
        process = None
        worker_ready = False  # COLD: this task pays the worker's boot
        # The boot outlasts the task budget: without a per-phase allowance the task
        # would time out while the worker was still starting.
        async def start(self) -> None:
            await asyncio.sleep(1.2)
        async def stop(self) -> None:
            pass
        async def run_task(self, instruction, completion, confirm, *,
                     approval_mode: str = "manual") -> dict:
            return {"status": "SUCCESS", "message": "ok", "actions": 1, "steps": 1,
                    "evidence": [{"operation": "launch", "verified": True}]}

    async def scenario() -> None:
        controller = TaskController(agent_settings(provider="codex", model="fixture-model",
                                             timeout_seconds=1, logs_path=tmp_path,
                                             collect_metrics=False))
        controller.brain = SilentBrain()  # type: ignore[assignment]
        controller.engine = SlowBootEngine()  # type: ignore[assignment]
        result = await controller.run("Abra o Bloco de Notas.")
        assert result.cold_start is True
        assert result.status == "SUCCESS", result.message
        await controller.close()
    asyncio.run(scenario())


def test_batch_counting_ignores_observations_and_meta_calls() -> None:
    """§3: multi-action is only confirmed if what counts as an action is defined once."""
    from app.computer.batches import count_actions, is_action

    class Command:
        def __init__(self, tool_type: str, tool_name: str | None) -> None:
            self.tool_type = tool_type
            self.tool_name = tool_name

    batch = [Command("action", "click_input"), Command("action", "set_edit_text"),
             Command("action", "list_tools"), Command("data_collection", "get_app_window_info"),
             Command("action", ""), Command("action", None)]
    assert count_actions(batch) == 2
    assert count_actions([]) == 0
    assert is_action(Command("action", "click_input"))
    assert not is_action(Command("action", "list_tools"))
    assert not is_action(Command("data_collection", "get_app_window_info"))


def test_fase_3_measurements_reach_the_task_record(tmp_path: Path) -> None:
    """Batch shape, context discovery and AppAgent creation must be attributable (§3)."""
    class Engine:
        process = None
        worker_ready = False
        async def start(self) -> None:
            pass
        async def stop(self) -> None:
            pass
        async def run_task(self, instruction, completion, confirm, *,
                     approval_mode: str = "manual") -> dict:
            return {"status": "NOT_VERIFIED", "evidence": [], "batches": 4,
                    "multi_action_batches": 2, "max_batch_actions": 3,
                    "context_provision_ms": 812.5, "agent_create_ms": 240.0, "app_agents": 2}

    async def scenario() -> None:
        controller = TaskController(agent_settings(provider="codex", model="fixture-model",
                                             logs_path=tmp_path, collect_metrics=False))
        controller.brain = SilentBrain()  # type: ignore[assignment]
        controller.engine = Engine()  # type: ignore[assignment]
        result = await controller.run("Abra o Bloco de Notas.")
        assert (result.batches, result.multi_action_batches, result.max_batch_actions) == (4, 2, 3)
        assert (result.context_provision_ms, result.agent_create_ms, result.app_agents) == (812.5, 240.0, 2)
        assert result.failure_kind == "VERIFICATION_FAILURE"
        await controller.close()
    asyncio.run(scenario())


def test_a_provider_failure_does_not_tear_down_the_worker(tmp_path: Path) -> None:
    """§59: killing a healthy executor on a quota error only pays a cold start later."""
    from app.brain.contracts import BrainError, ErrorKind

    class QuotaBrain:
        model = "fixture-model"
        capabilities = Capabilities.of()

        async def complete(self, messages: list[dict]) -> object:
            raise BrainError(ErrorKind.QUOTA_EXCEEDED, "sem cota no provedor")

        async def close(self) -> None:
            pass

    class Engine:
        process = None
        worker_ready = True

        def __init__(self) -> None:
            self.stops = 0

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            self.stops += 1

        async def run_task(self, instruction, completion, confirm, *,
                     approval_mode: str = "manual") -> dict:
            await completion([{"role": "user", "content": "x"}], "HostAgent")
            return {}

    async def scenario() -> None:
        controller = TaskController(agent_settings(provider="codex", model="fixture-model",
                                             logs_path=tmp_path, collect_metrics=False))
        engine = Engine()
        controller.brain = QuotaBrain()  # type: ignore[assignment]
        controller.engine = engine  # type: ignore[assignment]
        result = await controller.run("Abra a Calculadora.")
        assert result.status == "FAILED"
        assert result.failure_kind == "PROVIDER_FAILURE"
        assert engine.stops == 0, "um worker saudável não deve ser derrubado por falha de provider"
        await controller.close()
    asyncio.run(scenario())


def test_an_executor_failure_still_stops_the_worker(tmp_path: Path) -> None:
    """The opposite case must keep its old behaviour: a broken executor is stopped."""
    from app.computer.ufo_engine import ComputerError

    class Engine:
        process = None
        worker_ready = True

        def __init__(self) -> None:
            self.stops = 0

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            self.stops += 1

        async def run_task(self, instruction, completion, confirm, *,
                     approval_mode: str = "manual") -> dict:
            raise ComputerError("Worker UFO encerrou; consulte logs/worker.log.")

    async def scenario() -> None:
        controller = TaskController(agent_settings(provider="codex", model="fixture-model",
                                             logs_path=tmp_path, collect_metrics=False))
        engine = Engine()
        controller.brain = SilentBrain()  # type: ignore[assignment]
        controller.engine = engine  # type: ignore[assignment]
        result = await controller.run("Abra a Calculadora.")
        assert result.failure_kind == "ACTION_FAILURE"
        assert engine.stops == 1
        await controller.close()
    asyncio.run(scenario())


def test_an_oversized_worker_message_is_a_transport_failure_not_a_configuration_one() -> None:
    """Measured 2026-10-09: a prompt past the reader's bound was blamed on the user.

    The chat run of "pesquisa por michel jackson no spotify" ended at the fifth inference
    with "Separator is found, but chunk is longer than limit" as the task's own result, zero
    actions, classified CONFIGURATION — the reader's exception, not a model failure.
    """
    from app.computer import ufo_engine
    from app.core.controller import classify_failure_kind

    class Output:
        async def readuntil(self, separator: bytes) -> bytes:
            raise asyncio.LimitOverrunError("Separator is found, but chunk is longer than limit",
                                            65_536)

    class Process:
        returncode = None
        stdin = None
        stdout = Output()

    async def scenario() -> None:
        engine = ufo_engine.UFOEngine(Settings(provider="codex", model="fixture-model"))
        engine.process = Process()  # type: ignore[assignment]
        with pytest.raises(ufo_engine.WorkerTransportError) as failure:
            await engine._read()
        assert "bytes por linha deste canal" in str(failure.value)
    asyncio.run(scenario())
    assert classify_failure_kind(ufo_engine.WorkerTransportError("x")) == "PROVIDER_FAILURE"


def test_a_large_worker_message_is_delivered_whole() -> None:
    """The ceiling is a boundary, not a suspicion: 300 000 bytes arrive intact."""
    from app.computer import ufo_engine

    payload = {"event": "llm", "messages": [{"role": "user", "content": "á" * 150_000}]}
    line = (json.dumps(payload) + "\n").encode()

    class Output:
        def __init__(self) -> None:
            self.buffer = line

        async def readuntil(self, separator: bytes) -> bytes:
            chunk, self.buffer = self.buffer.split(separator, 1)
            return chunk + separator

    class Process:
        returncode = None
        stdin = None
        stdout = Output()

    async def scenario() -> dict:
        engine = ufo_engine.UFOEngine(Settings(provider="codex", model="fixture-model"))
        engine.process = Process()  # type: ignore[assignment]
        return await engine._read()
    assert len(line) > 300_000
    assert asyncio.run(scenario()) == payload


def test_run_task_consumes_the_worker_reply_when_the_provider_fails() -> None:
    """A kept-alive worker must be left idle: a stale reply would poison the next task."""
    from app.brain.contracts import BrainError, ErrorKind
    from app.computer import ufo_engine

    class Input:
        def __init__(self) -> None:
            self.data = b""

        def write(self, data: bytes) -> None:
            self.data += data

        async def drain(self) -> None:
            pass

    class Output:
        # One message per line, framed the way the engine frames it (``readuntil`` with an
        # explicit limit), so a large message is delivered whole and a larger one is named.
        def __init__(self, lines: list[dict]) -> None:
            self.lines = list(lines)
            self.buffer = b""

        async def readuntil(self, separator: bytes) -> bytes:
            while separator not in self.buffer:
                if not self.lines:
                    raise asyncio.IncompleteReadError(self.buffer, None)
                self.buffer += (json.dumps(self.lines.pop(0)) + "\n").encode()
            line, self.buffer = self.buffer.split(separator, 1)
            return line + separator

    class Process:
        returncode = None

        def __init__(self, lines: list[dict]) -> None:
            self.stdin = Input()
            self.stdout = Output(lines)

        async def wait(self) -> int:
            return 0

        def kill(self) -> None:
            pass

    async def scenario() -> None:
        engine = ufo_engine.UFOEngine(Settings(provider="codex", model="fixture-model"))
        lines = [{"event": "llm", "messages": [{"role": "user", "content": "x"}],
                  "agent": "HostAgent"},
                 {"ok": False, "error": "UFO falhou; nenhuma próxima ação será iniciada."}]
        process = Process(lines)
        engine.process = process  # type: ignore[assignment]
        # This test exercises the protocol, not the boot: it declares the worker ready
        # the same way a completed start() would (a spawn in flight is not ready).
        engine._ready = True

        async def completion(messages: list[dict], agent: str) -> str:
            raise BrainError(ErrorKind.QUOTA_EXCEEDED, "sem cota no provedor")

        async def confirm(message: str) -> bool:
            return False

        with pytest.raises(BrainError):
            await engine.run_task("tarefa", completion, confirm)
        assert engine.process is not None, "o worker saudável não deve ser encerrado"
        assert process.stdout.lines == [], "a resposta de falha do worker ficou pendente no protocolo"
    asyncio.run(scenario())


def test_unverified_work_is_a_verification_failure_not_a_provider_one(tmp_path: Path) -> None:
    class UnverifiedEngine:
        process = None
        worker_ready = False
        async def start(self) -> None:
            pass
        async def run_task(self, instruction, completion, confirm, *,
                     approval_mode: str = "manual") -> dict:
            return {"status": "NOT_VERIFIED", "message": "evidência insuficiente", "actions": 2,
                    "steps": 2, "evidence": [{"operation": "verify_window", "verified": False}]}
        async def stop(self) -> None:
            pass

    async def scenario() -> None:
        controller = TaskController(agent_settings(provider="codex", model="fixture-model",
                                             logs_path=tmp_path, collect_metrics=False))
        controller.brain = SilentBrain()  # type: ignore[assignment]
        controller.engine = UnverifiedEngine()  # type: ignore[assignment]
        result = await controller.run("Abra as Configurações e entre em Sistema > Som.")
        assert result.status == "NOT_VERIFIED"
        assert result.failure_kind == "VERIFICATION_FAILURE"
        assert result.false_success is False
        await controller.close()
    asyncio.run(scenario())


def test_tasks_reuse_one_brain_and_reconfigure_closes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm import Completion
    from app.core import controller as controller_module

    class BrainFixture(SilentBrain):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.closed = 0
        async def complete(self, messages: list[dict]) -> Completion:
            self.calls += 1
            return Completion("{}")
        async def close(self) -> None:
            self.closed += 1

    class Engine:
        process = None
        worker_ready = True
        async def start(self) -> None:
            pass
        async def stop(self) -> None:
            pass
        async def run_task(self, instruction, completion, confirm, *,
                     approval_mode: str = "manual") -> dict:
            await completion([{"role": "user", "content": instruction}], "HostAgent")
            return {"status": "NOT_VERIFIED", "evidence": []}

    brain = BrainFixture()
    resolutions = []
    def resolve(settings: Settings) -> BrainFixture:
        resolutions.append(settings)
        return brain
    monkeypatch.setattr(controller_module, "resolve_brain", resolve)
    async def scenario() -> None:
        settings = Settings(provider="codex", model="fixture-model", logs_path=tmp_path, collect_metrics=False)
        controller = TaskController(settings)
        controller.engine = Engine()  # type: ignore[assignment]
        for instruction in ("primeira", "segunda"):
            assert (await controller.run(instruction)).status == "NOT_VERIFIED"
        assert len(resolutions) == 1
        assert brain.calls == 2 and brain.closed == 0
        await controller.reconfigure(settings)
        assert brain.closed == 1 and controller.brain is None
        await controller.close()
    asyncio.run(scenario())


def test_cold_and_warm_starts_are_never_mixed(tmp_path: Path) -> None:
    """§6: a task that boots the worker and one that finds it running are different runs."""
    class WarmEngine:
        process = None
        worker_ready = True
        async def start(self) -> None:
            pass
        async def run_task(self, instruction, completion, confirm, *,
                     approval_mode: str = "manual") -> dict:
            return {"status": "SUCCESS", "message": "ok", "actions": 1, "steps": 1,
                    "evidence": [{"operation": "launch", "verified": True}]}
        async def stop(self) -> None:
            pass

    async def scenario() -> None:
        controller = TaskController(agent_settings(provider="codex", model="fixture-model",
                                             logs_path=tmp_path, collect_metrics=False))
        controller.brain = SilentBrain()  # type: ignore[assignment]
        engine = WarmEngine()
        controller.engine = engine  # type: ignore[assignment]
        warm = await controller.run("Abra a Calculadora.")
        engine.worker_ready = False
        cold = await controller.run("Abra a Calculadora.")
        assert warm.cold_start is False
        assert cold.cold_start is True
        await controller.close()
    asyncio.run(scenario())

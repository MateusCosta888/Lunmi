from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.brain.api_brain import ApiBrain
from app.brain.contracts import BrainError, Capability, EventKind
from app.codex_app_server import CodexAppServer, CodexError
from app.llm import Completion
from app.settings import Settings


class Bytes(httpx.AsyncByteStream):
    def __init__(self, data: bytes, *, split: int = 1) -> None:
        self.data, self.split, self.closed = data, split, False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self.data), self.split):
            yield self.data[start:start + self.split]

    async def aclose(self) -> None:
        self.closed = True


def sse(*values: Any) -> bytes:
    return "".join("data: " + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
                   + "\r\n\r\n" for value in values).encode("utf-8")


def openai_frames(*, finish: str = "stop") -> bytes:
    return sse(
        {"choices": [{"index": 0, "delta": {"reasoning_content": "PRIVATE_REASONING"}}]},
        {"choices": [{"index": 0, "delta": {"content": "Olá "}}]},
        {"choices": [{"index": 0, "delta": {"content": "🌙"}, "finish_reason": finish}]},
        {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}, "[DONE]")


async def api_fixture(provider: str, data: bytes) -> tuple[ApiBrain, Bytes, list[dict[str, Any]]]:
    brain = ApiBrain(Settings(provider=provider, model="fixture", api_key="PRIVATE_KEY"))
    await brain._provider.client.aclose()
    body = Bytes(data)
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    brain._provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return brain, body, requests


def test_openai_stream_is_incremental_unicode_bounded_and_not_reasoning() -> None:
    async def scenario() -> None:
        brain, body, requests = await api_fixture("openai", openai_frames())
        stream = brain.stream([{"role": "user", "content": "PRIVATE_PROMPT"}])
        assert (await anext(stream)).data["streaming"] is True
        assert (await anext(stream)).data == {"text": "Olá "}
        assert body.closed is False  # Text arrives before terminal response/connection close.
        rest = [event async for event in stream]
        assert [event.data["text"] for event in rest if event.kind == EventKind.TEXT_DELTA] == ["🌙"]
        assert rest[-1].kind == EventKind.COMPLETED
        assert rest[-1].data["completion"] == Completion("Olá 🌙", 4, 2)
        assert body.closed
        assert requests[0]["stream"] is True
        assert brain.call_metrics[-1]["first_text_ms"] is not None
        assert "PRIVATE" not in json.dumps(brain.call_metrics)
        await brain.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("data", [
    openai_frames().replace(b"data: [DONE]\r\n\r\n", b""),
    openai_frames(finish="length"),
    sse({"choices": [{"delta": {"tool_calls": [{"function": {"arguments": "{"}}]}}]}),
    b"data: {\xff}\n\n",
    b"data: {}",
    sse({"error": {"message": "PRIVATE_PROVIDER_ERROR"}}),
])
def test_stream_failures_never_complete_or_retry(data: bytes) -> None:
    async def scenario() -> None:
        brain, body, requests = await api_fixture("openai", data)
        events = []
        with pytest.raises(BrainError) as failure:
            async for event in brain.stream([{"role": "user", "content": "fixture"}]):
                events.append(event)
        assert not any(event.kind == EventKind.COMPLETED for event in events)
        assert "PRIVATE" not in str(failure.value)
        assert len(requests) == 1 and body.closed
        assert brain.call_metrics[-1]["ok"] is False
        await brain.close()

    asyncio.run(scenario())


def test_anthropic_stream_filters_thinking_and_accumulates_usage() -> None:
    data = sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 7}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "thinking_delta", "thinking": "PRIVATE_REASONING"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Olá 🌙"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 2}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"})

    async def scenario() -> None:
        brain, body, requests = await api_fixture("anthropic", data)
        events = [event async for event in brain.stream([{"role": "user", "content": "fixture"}])]
        assert events[-1].data["completion"] == Completion("Olá 🌙", 7, 3)
        assert [event.data["text"] for event in events if event.kind == EventKind.TEXT_DELTA] == ["Olá 🌙"]
        assert body.closed and len(requests) == 1
        await brain.close()

    asyncio.run(scenario())


def test_anthropic_rejects_new_text_block_after_finish_before_publishing_it() -> None:
    data = sse(
        {"type": "message_start", "message": {"usage": {}}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": "LATE_AFTER_FINISH"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_stop"})

    async def scenario() -> None:
        brain, body, _ = await api_fixture("anthropic", data)
        events = []
        with pytest.raises(BrainError):
            async for event in brain.stream([]):
                events.append(event)
        assert not any(event.kind in {EventKind.TEXT_DELTA, EventKind.COMPLETED} for event in events)
        assert body.closed
        await brain.close()

    asyncio.run(scenario())


def test_sse_many_small_frames_do_not_consume_the_generated_text_limit() -> None:
    frames = [{"choices": [{"index": 0, "delta": {"content": "x"}}],
               "id": "chatcmpl-fixture-000000000000000000000000", "object": "chat.completion.chunk", "created": 1,
               "model": "fixture", "system_fingerprint": "fp_test"} for _ in range(1300)]
    data = sse(*frames, {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}, "[DONE]")
    assert len(data) > 262_144

    async def scenario() -> None:
        brain, body, _ = await api_fixture("openai", data)
        body.split = 4096
        events = [event async for event in brain.stream([])]
        assert events[-1].data["completion"] == Completion("x" * 1300)
        assert len([event for event in events if event.kind == EventKind.TEXT_DELTA]) == 1300
        assert body.closed
        await brain.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("values", [
    [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "BAD"}}],
    [{"type": "message_start", "message": {}}, {"type": "message_start", "message": {}}],
    [{"type": "message_start", "message": {}},
     {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
     {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}],
    [{"type": "message_start", "message": {}},
     {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
     {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}],
    [{"type": "message_start", "message": {}},
     {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "BAD"}}],
    [{"type": "message_start", "message": {}}, {"type": "content_block_stop", "index": 0}],
    [{"type": "message_start", "message": {}},
     {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
     {"type": "content_block_stop", "index": 0},
     {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "BAD"}}],
])
def test_anthropic_protocol_order_is_fail_closed(values: list[dict[str, Any]]) -> None:
    async def scenario() -> None:
        brain, body, _ = await api_fixture("anthropic", sse(*values, {"type": "message_stop"}))
        events = []
        with pytest.raises(BrainError):
            async for event in brain.stream([]):
                events.append(event)
        assert not any(event.kind in {EventKind.TEXT_DELTA, EventKind.COMPLETED} for event in events)
        assert body.closed
        await brain.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("bound", ["frame", "wire", "generated_text"])
def test_sse_wire_frame_and_generated_text_have_independent_bounds(
        monkeypatch: pytest.MonkeyPatch, bound: str) -> None:
    import app.llm as llm

    assert (llm.MAX_COMPLETION_BYTES, llm.MAX_SSE_FRAME_BYTES, llm.MAX_SSE_BYTES) == (262_144, 262_144, 4 * 1024 * 1024)
    if bound == "frame":
        monkeypatch.setattr(llm, "MAX_SSE_FRAME_BYTES", 150)
        data = sse({"choices": [], "padding": "x" * 150})
    elif bound == "wire":
        monkeypatch.setattr(llm, "MAX_SSE_BYTES", 150)
        data = sse(*[{"choices": [], "padding": "x" * 25} for _ in range(4)])
    else:
        monkeypatch.setattr(llm, "MAX_COMPLETION_BYTES", 3)
        data = sse({"choices": [{"delta": {"content": "ok"}}]},
                   {"choices": [{"delta": {"content": "BAD"}}]})

    async def scenario() -> None:
        brain, body, _ = await api_fixture("openai", data)
        events = []
        with pytest.raises(BrainError):
            async for event in brain.stream([]):
                events.append(event)
        assert not any(event.kind == EventKind.COMPLETED for event in events)
        if bound == "generated_text":
            assert [event.data["text"] for event in events if event.kind == EventKind.TEXT_DELTA] == ["ok"]
        assert body.closed
        await brain.close()

    asyncio.run(scenario())


def test_anthropic_api_key_auth_uses_the_official_header() -> None:
    async def scenario() -> None:
        brain = ApiBrain(Settings(provider="anthropic", model="fixture", api_key="PRIVATE_API_KEY"))
        _, headers, _ = brain._provider._request([])  # type: ignore[attr-defined]
        assert headers["x-api-key"] == "PRIVATE_API_KEY"
        assert headers["anthropic-version"] == "2023-06-01"
        assert "Authorization" not in headers
        await brain._provider.client.aclose()
        requests = []

        def catalogue(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.method == "GET" and request.url.path == "/v1/models"
            assert request.headers["x-api-key"] == "PRIVATE_API_KEY"
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"data": [{"id": "fixture"}]})

        brain._provider.client = httpx.AsyncClient(transport=httpx.MockTransport(catalogue))
        assert [model.id for model in await brain.models()] == ["fixture"]
        assert len(requests) == 1
        await brain.close()

    asyncio.run(scenario())


def test_unknown_endpoint_is_final_only_and_does_not_probe_or_add_parameters() -> None:
    async def scenario() -> None:
        brain = ApiBrain(Settings(provider="custom", model="fixture", api_key="key",
                                  base_url="http://127.0.0.1:8000/v1", effort="unknown"))
        await brain._provider.client.aclose()
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "final"}}]})

        brain._provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        events = [event async for event in brain.stream([{"role": "user", "content": "fixture"}])]
        assert not brain.capabilities.supports(Capability.STREAMING)
        assert events[0].data["streaming"] is False
        assert events[-1].data["completion"] == Completion("final")
        assert not any(event.kind == EventKind.TEXT_DELTA for event in events)
        assert len(requests) == 1
        assert "stream" not in requests[0] and "reasoning_effort" not in requests[0]
        await brain.close()

    asyncio.run(scenario())


def test_cancel_after_delta_closes_http_stream_and_never_completes() -> None:
    class Waiting(Bytes):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield sse({"choices": [{"delta": {"content": "partial"}}]})
            await asyncio.Event().wait()

    async def scenario() -> None:
        brain, _, requests = await api_fixture("openai", b"")
        body = Waiting(b"")
        await brain._provider.client.aclose()
        brain._provider.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)))
        events = []
        delta = asyncio.Event()

        async def consume() -> None:
            async for event in brain.stream([{"role": "user", "content": "fixture"}]):
                events.append(event)
                if event.kind == EventKind.TEXT_DELTA:
                    delta.set()

        task = asyncio.create_task(consume())
        await asyncio.wait_for(delta.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert body.closed
        assert not any(event.kind == EventKind.COMPLETED for event in events)
        assert brain.call_metrics[-1]["ok"] is False
        assert not requests
        await brain.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('effort', ['', 'medium'])
def test_codex_stream_preserves_pre_ack_delta_and_filters_other_turns(effort: str) -> None:
    class Input:
        data = b""

        def write(self, data: bytes) -> None:
            self.data += data

        async def drain(self) -> None:
            pass

    class Process:
        returncode = None
        stdin = Input()

    async def scenario() -> None:
        server = CodexAppServer()
        server.process = Process()  # type: ignore[assignment]
        server._model_efforts["fixture"] = {"medium"}
        messages = iter([
            {"id": 1, "result": {"thread": {"id": "thread"}}},
            {"method": "item/agentMessage/delta", "params": {
                "threadId": "thread", "turnId": "turn", "itemId": "answer", "delta": "Olá "}},
            {"id": 2, "result": {"turn": {"id": "turn"}}},
            {"method": "item/agentMessage/delta", "params": {
                "threadId": "thread", "turnId": "OLD", "itemId": "old", "delta": "PRIVATE"}},
            {"method": "item/reasoning/textDelta", "params": {
                "threadId": "thread", "turnId": "turn", "delta": "PRIVATE"}},
            {"method": "item/agentMessage/delta", "params": {
                "threadId": "thread", "turnId": "turn", "itemId": "answer", "delta": "🌙"}},
            {"method": "turn/completed", "params": {"threadId": "thread", "turn": {
                "id": "turn", "status": "completed", "items": [{"type": "agentMessage", "text": "Olá 🌙"}]}}},
            {"id": 3, "result": {}},
        ])

        async def read(timeout: float) -> dict[str, Any]:
            return next(messages)

        server._read = read  # type: ignore[method-assign]
        values = [value async for value in server.stream("fixture", [{"role": "user", "content": "fixture"}],
                                                        effort=effort)]
        assert values == ["Olá ", "🌙", Completion("Olá 🌙")]
        requests = [json.loads(line) for line in server.process.stdin.data.splitlines()]  # type: ignore[union-attr]
        if effort:
            assert requests[1]["params"]["effort"] == effort
        else:
            assert "effort" not in requests[1]["params"]  # No forced low/default guessing.
        assert [request["method"] for request in requests] == ["thread/start", "turn/start", "thread/unsubscribe"]

    asyncio.run(scenario())


def test_codex_stream_failed_after_output_is_not_retried() -> None:
    async def scenario() -> None:
        server = CodexAppServer()
        calls = []

        async def infer(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            calls.append(True)
            yield "partial"
            raise CodexError("failed")

        server._infer_stream = infer  # type: ignore[method-assign]
        seen = []
        with pytest.raises(CodexError):
            async for value in server.stream("fixture", [{"role": "user", "content": "fixture"}]):
                seen.append(value)
        assert seen == ["partial"] and calls == [True]

    asyncio.run(scenario())


def agy_events(*, status: str = "SUCCESS", response: str = "Olá 🌙") -> list[dict[str, Any]]:
    return [
        {"event": "init", "conversation_id": "conversation", "init": {}},
        {"event": "step_update", "step_update": {"conversation_id": "conversation", "step_index": 1,
            "state": "ACTIVE", "step_type": "agent_response", "text_delta": "Olá "}},
        {"event": "step_update", "step_update": {"conversation_id": "conversation", "step_index": 1,
            "state": "DONE", "step_type": "agent_response", "text_delta": "🌙"}},
        {"event": "result", "result": {"conversation_id": "conversation", "status": status,
            "response": response, "usage": {"input_tokens": 5, "output_tokens": 2}}},
    ]


@pytest.mark.parametrize("failure", ["", "missing_result", "mismatch", "failed", "other_conversation"])
def test_agy_official_active_done_deltas_and_terminal_are_validated(
        monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    from app.brain.cli.catalog import find_spec
    from app.brain.cli.headless import HeadlessCliBrain

    values = agy_events()
    if failure == "missing_result":
        values.pop()
    elif failure == "mismatch":
        values[-1]["result"]["response"] = "different"
    elif failure == "failed":
        values[-1]["result"]["status"] = "ERROR"
    elif failure == "other_conversation":
        values[1]["step_update"]["conversation_id"] = "wrong"

    class Process:
        returncode = 0

    class Handle:
        process = Process()
        stopped = False

        def __init__(self) -> None:
            self.lines = [json.dumps(value, ensure_ascii=False) + "\n" for value in values]

        async def readline(self, timeout: float) -> str:
            return self.lines.pop(0) if self.lines else ""

        async def stop(self) -> None:
            self.stopped = True

        async def wait(self, timeout: float) -> int:
            return 0

        def diagnostics(self) -> str:
            return ""

    handle = Handle()
    argvs = []

    async def spawn(argv: list[str], **kwargs: Any) -> Any:
        argvs.append(argv)
        return handle

    monkeypatch.setattr("app.brain.cli.headless.spawn", spawn)
    spec = find_spec("agy")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="agy", model="selected-model", effort="low"), spec,
                             executable="agy")

    async def scenario() -> None:
        events = []
        try:
            async for event in brain.stream([{"role": "user", "content": "synthetic"}]):
                events.append(event)
        except BrainError:
            assert failure
        else:
            assert not failure
            assert [event.data["text"] for event in events if event.kind == EventKind.TEXT_DELTA] == ["Olá ", "🌙"]
            assert events[-1].data["completion"] == Completion("Olá 🌙", 5, 2)
            assert events[0].data["streaming"] is True
        assert any(event.kind == EventKind.COMPLETED for event in events) is (not failure)
        assert len(argvs) == 1 and handle.stopped
        assert "--sandbox" in argvs[0]
        assert argvs[0][argvs[0].index("--output-format") + 1] == "stream-json"
        assert argvs[0][argvs[0].index("--model") + 1] == "selected-model"
        assert brain.inference_argv()[brain.inference_argv().index("--output-format") + 1] == "json"

    asyncio.run(scenario())


def test_agy_cancel_after_output_stops_process_and_clears_pending_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.brain.cli.catalog import find_spec
    from app.brain.cli.headless import HeadlessCliBrain

    class Process:
        returncode = None

    class Handle:
        process = Process()
        stopped = False

        def __init__(self) -> None:
            self.lines = [json.dumps(value) + "\n" for value in agy_events()[:2]]

        async def readline(self, timeout: float) -> str:
            if self.lines:
                return self.lines.pop(0)
            await asyncio.Event().wait()
            return ""

        async def stop(self) -> None:
            self.stopped = True

    handle = Handle()

    async def spawn(*args: Any, **kwargs: Any) -> Any:
        return handle

    monkeypatch.setattr("app.brain.cli.headless.spawn", spawn)
    spec = find_spec("agy")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="agy", model="fixture"), spec, executable="agy")

    async def scenario() -> None:
        stream = brain.stream([{"role": "user", "content": "fixture"}])
        assert (await anext(stream)).kind == EventKind.STARTED
        assert (await asyncio.wait_for(anext(stream), 1)).kind == EventKind.TEXT_DELTA
        await stream.aclose()
        assert handle.stopped and brain._process is None
        assert brain.cli_calls[-1]["ok"] is False
        assert brain.cli_calls[-1]["first_text_ms"] is not None

    asyncio.run(scenario())


def test_codex_explicit_effort_uses_confirmed_model_capabilities_not_a_global_scale() -> None:
    async def scenario() -> None:
        server = CodexAppServer()
        discoveries = []

        async def models() -> list[tuple[str, str, bool]]:
            discoveries.append(True)
            server._model_efforts["selected"] = {"medium", "high"}
            return [("selected", "selected", True)]

        async def infer(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            assert args[0] == "selected" and kwargs["effort"] == "medium"
            yield Completion("answer")

        server.list_models = models  # type: ignore[method-assign]
        server._infer_stream = infer  # type: ignore[method-assign]
        for _ in range(2):
            assert [value async for value in server.stream("selected", [], effort="medium")] == [Completion("answer")]
        assert discoveries == [True]
        with pytest.raises(CodexError) as failure:
            async for _ in server.stream("selected", [], effort="low"):
                pytest.fail("Unsupported effort reached inference")
        assert failure.value.code == "configuration"

    asyncio.run(scenario())


def test_stream_waits_once_for_a_short_rate_limit_before_any_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """O 429 chega antes de qualquer evento: esperar e repetir não duplica texto já mostrado."""
    import app.llm as llm
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(llm.asyncio, "sleep", fake_sleep)

    async def scenario() -> None:
        brain = ApiBrain(Settings(provider="openai", model="fixture", api_key="PRIVATE_KEY"))
        await brain._provider.client.aclose()
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "1"})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Bytes(openai_frames()))

        brain._provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        events = [event async for event in brain.stream([{"role": "user", "content": "oi"}])]
        texts = [event.data["text"] for event in events if event.kind == EventKind.TEXT_DELTA]
        assert texts == ["Olá ", "🌙"] and calls["n"] == 2 and waits == [1.5]
        assert events[-1].kind == EventKind.COMPLETED
        await brain.close()

    asyncio.run(scenario())

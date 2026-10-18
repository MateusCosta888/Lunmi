"""Automatic chat uses the existing dispatcher, never a second action executor."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def module(monkeypatch: Any) -> Any:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(root / "vendor" / "UFO")
    monkeypatch.syspath_prepend(str(root / "vendor" / "UFO"))
    from app.computer import ufo_runtime
    return ufo_runtime


def runtime_for_dispatch(module: Any, handler: Any, *, allow_actions: bool = True) -> tuple[Any, list[Any], list[Any]]:
    calls: list[Any] = []
    events: list[Any] = []
    uia = SimpleNamespace(references={}, invalidate_tree=lambda: None)
    tools = SimpleNamespace(cancelled=False, pending_ui=False, evidence=[], uia=uia)
    runtime = SimpleNamespace(tools=tools, round=SimpleNamespace(agent=object()),
        task_id="task", worker_instance="worker", allow_actions=allow_actions, operation_events=True,
        batches=0, max_batch_actions=0, multi_action_batches=0, actions=0, actions_executed=0,
        verification_ms=0.0, execution_ms=0.0, observation_ms=0.0,
        state=SimpleNamespace(selected_app_window=None))

    async def exchange(event: dict[str, Any]) -> dict[str, Any]:
        events.append(event)
        return {}

    async def check_risk(command: Any) -> None:
        events.append({"risk": command.tool_name})

    class Computer:
        def command2tool(self, command: Any) -> Any:
            return SimpleNamespace(namespace="fixture", command=command)

        async def run_actions(self, commands: list[Any]) -> list[Any]:
            calls.extend(commands)
            return [handler(commands[0].command, tools)]

    async def computer(kind: str) -> Any:
        return Computer()

    runtime.exchange, runtime.check_risk, runtime.computer = exchange, check_risk, computer
    return runtime, calls, events


def command(module: Any, *, name: str = "filesystem", call_id: str = "one", **args: Any) -> Any:
    return module.Command(tool_name=name, tool_type="action", call_id=call_id, parameters=args)


def test_verified_command_replay_uses_result_and_rejects_identity_collision(module: Any) -> None:
    runtime, calls, events = runtime_for_dispatch(module, lambda c, t: SimpleNamespace(is_error=False, data={"verified": True}))
    dispatcher = module.RuntimeDispatcher(runtime)
    first = command(module, operation="create_file", path="fixture.txt", text="PRIVATE")
    async def run() -> None:
        assert (await dispatcher.execute_commands([first]))[0].status == module.ResultStatus.SUCCESS
        assert (await dispatcher.execute_commands([first]))[0].status == module.ResultStatus.SUCCESS
        collision = command(module, operation="delete", path="other.txt")
        assert (await dispatcher.execute_commands([collision]))[0].status == module.ResultStatus.FAILURE
    asyncio.run(run())
    assert len(calls) == 1
    assert events[0] == {"risk": "filesystem"}
    public = [event for event in events if "event" in event]
    assert [event["event"] for event in public] == ["operation.started", "operation.finished"]
    assert public[-1]["operation_id"].startswith("op-")
    assert len(public[-1]["operation_id"]) == 35
    assert "PRIVATE" not in json.dumps(public + dispatcher.operation_states())


def test_read_only_exception_is_failed_no_effect_and_does_not_block_mutation(module: Any) -> None:
    def handler(c: Any, tools: Any) -> Any:
        if c.tool_name == "uia_verify":
            raise RuntimeError("synthetic read failure")
        return SimpleNamespace(is_error=False, data={"verified": True})

    runtime, calls, _ = runtime_for_dispatch(module, handler)
    dispatcher = module.RuntimeDispatcher(runtime)

    async def run() -> None:
        read = command(module, name="uia_verify", call_id="read", id="synthetic")
        assert (await dispatcher.execute_commands([read]))[0].status == module.ResultStatus.FAILURE
        state = dispatcher.operation_states()[0]
        assert state["state"] == "failed_no_effect"
        assert state["failure_reason"] == "read_only_exception"
        mutation = command(module, name="keyboard_input", call_id="mutation", keys="{ENTER}", id="synthetic")
        assert (await dispatcher.execute_commands([mutation]))[0].status == module.ResultStatus.SUCCESS

    asyncio.run(run())
    assert len(calls) == 2


def test_operation_diagnostics_are_closed_and_never_carry_arguments(module: Any) -> None:
    def handler(c: Any, tools: Any) -> Any:
        raise RuntimeError("PRIVATE_SENTINEL_EXCEPTION")

    runtime, _, events = runtime_for_dispatch(module, handler)
    dispatcher = module.RuntimeDispatcher(runtime)
    request = command(module, operation="read", path="PRIVATE_SENTINEL_PATH",
                      call_id="PRIVATE_SENTINEL_CALL")
    asyncio.run(dispatcher.execute_commands([request]))
    public = [event for event in events if "event" in event]
    finished = next(event for event in public if event["event"] == "operation.finished")
    assert finished["tool"] == "filesystem"
    assert finished["category"] == "read_only"
    assert finished["failure_reason"] == "read_only_exception"
    assert finished["state"] == "failed_no_effect"
    encoded = json.dumps(public + dispatcher.operation_states())
    assert "PRIVATE_SENTINEL" not in encoded


def test_model_call_id_is_opaque_in_public_operation_metadata(module: Any) -> None:
    runtime, _, events = runtime_for_dispatch(module, lambda c, t: SimpleNamespace(is_error=False, data={"verified": True}))
    dispatcher = module.RuntimeDispatcher(runtime)
    request = command(module, call_id="PRIVATE_SENTINEL/path", operation="metadata", path="fixture.txt")
    async def run() -> None:
        first = (await dispatcher.execute_commands([request]))[0]
        assert first.call_id == "PRIVATE_SENTINEL/path"
        original = dispatcher.operation_states()[0]["operation_id"]
        await dispatcher.execute_commands([request])
        assert dispatcher.operation_states()[0]["operation_id"] == original
    asyncio.run(run())
    public = [event for event in events if "event" in event]
    encoded = json.dumps(public + dispatcher.operation_states())
    assert "PRIVATE_SENTINEL" not in encoded
    assert "/path" not in encoded
    assert "task:worker" not in encoded


def test_failure_after_dispatch_blocks_new_mutations_but_allows_consultation(module: Any) -> None:
    def handler(c: Any, tools: Any) -> Any:
        if c.parameters["operation"] == "create_file":
            raise TimeoutError("effect unknown")
        return SimpleNamespace(is_error=False, data={"verified": True})
    runtime, calls, _ = runtime_for_dispatch(module, handler)
    dispatcher = module.RuntimeDispatcher(runtime)
    async def run() -> None:
        first = command(module, operation="create_file", path="fixture.txt", text="x")
        assert (await dispatcher.execute_commands([first]))[0].status == module.ResultStatus.FAILURE
        assert dispatcher.operation_states()[0]["state"] == "uncertain"
        again = command(module, call_id="two", operation="create_file", path="fixture.txt", text="x")
        assert (await dispatcher.execute_commands([again]))[0].status == module.ResultStatus.FAILURE
        read = command(module, call_id="three", operation="metadata", path="fixture.txt")
        assert (await dispatcher.execute_commands([read]))[0].status == module.ResultStatus.SUCCESS
        assert dispatcher.operation_states()[0]["state"] == "uncertain"
    asyncio.run(run())
    assert len(calls) == 2


def test_read_only_authority_applies_to_every_round(module: Any) -> None:
    runtime, calls, _ = runtime_for_dispatch(module, lambda c, t: SimpleNamespace(is_error=False, data={"verified": True}), allow_actions=False)
    dispatcher = module.RuntimeDispatcher(runtime)
    async def run() -> None:
        read = command(module, operation="read", path="fixture.txt")
        assert (await dispatcher.execute_commands([read]))[0].status == module.ResultStatus.SUCCESS
        mutation = command(module, call_id="two", operation="create_file", path="fixture.txt", text="x")
        assert (await dispatcher.execute_commands([mutation]))[0].status == module.ResultStatus.FAILURE
    asyncio.run(run())
    assert len(calls) == 1
    assert dispatcher.operation_states()[-1]["state"] == "failed_no_effect"


def test_confirmation_denial_is_not_an_uncertain_dispatch(module: Any) -> None:
    runtime, calls, _ = runtime_for_dispatch(module, lambda c, t: None)
    async def deny(command: Any) -> None:
        raise PermissionError("denied")
    runtime.check_risk = deny
    dispatcher = module.RuntimeDispatcher(runtime)
    asyncio.run(dispatcher.execute_commands([command(module, operation="delete", path="fixture.txt")]))
    assert not calls
    assert dispatcher.operation_states()[0]["state"] == "failed_no_effect"


@pytest.mark.parametrize("handle,property_name,resolved", [(42, "expanded", True), (99, "expanded", False), (42, "focused", False)])
def test_verified_postcondition_resolves_only_its_normal_pending_dispatch(module: Any, handle: int, property_name: str, resolved: bool) -> None:
    def handler(c: Any, tools: Any) -> Any:
        if c.tool_name == "uia_verify":
            data = {"operation": "uia_verify", "verified": True, "handle": handle,
                    "property": property_name, "observed": 1, "runtime_id": [1]}
        else:
            data = {"operation": "invoke", "verified": False, "handle": 42,
                    "needs_postcondition": True, "runtime_id": [1], "before": {"expanded": 0}}
        tools.evidence.append(data)
        return SimpleNamespace(is_error=False, data=data)
    runtime, calls, _ = runtime_for_dispatch(module, handler)
    dispatcher = module.RuntimeDispatcher(runtime)
    async def run() -> None:
        await dispatcher.execute_commands([command(module, name="uia_action", operation="invoke")])
        assert dispatcher.operation_states()[0]["state"] == "uncertain"
        await dispatcher.execute_commands([command(module, name="uia_verify", call_id="proof", id="result", name_="label")])
        assert dispatcher.operation_states()[0]["state"] == ("completed_verified" if resolved else "uncertain")
    asyncio.run(run())
    assert len(calls) == 2


@pytest.mark.parametrize("operation", [
    {"name": "keyboard_input", "args": {}},
    {"name": "launch_application", "args": {"target": "x", "keys": "^r"}},
    {"name": "find_application", "args": {"query": "x", "limit": True}},
    {"name": "filesystem", "args": {"operation": "create_file", "path": "x", "attachment_ref": "unknown"}},
    {"name": "filesystem", "args": {"operation": "delete", "path": "x", "attachment_ref": "att-0"}},
    {"name": "filesystem", "args": {"operation": "create_file", "path": "x", "attachment_ref": "att-0", "text": "override"}},
])
def test_bootstrap_refuses_unknown_tools_arguments_and_attachment_authority(module: Any, operation: Any) -> None:
    with pytest.raises(ValueError):
        module.UFORuntime.bootstrap_command(operation, [{"text": "content"}])


def test_attachment_reference_preserves_literal_text_and_path(module: Any) -> None:
    operation = {"name": "filesystem", "args": {"operation": "create_file", "path": "Documents/result.txt", "attachment_ref": "att-0"}}
    result = module.UFORuntime.bootstrap_command(operation, [{"text": "verbatim\n\n  end "}])
    assert result.parameters == {"operation": "create_file", "path": "Documents/result.txt", "text": "verbatim\n\n  end "}
    assert operation["args"]["attachment_ref"] == "att-0"


def test_task_data_is_host_only_and_not_spliced_into_instruction(module: Any, monkeypatch: Any) -> None:
    runtime = module.UFORuntime.__new__(module.UFORuntime)
    captured: list[Any] = []
    runtime.loop, runtime.recent_actions = object(), []
    runtime.attachments = [{"name": "a.txt", "text": "PRIVATE ATTACHMENT"}]
    runtime.initial_result = {"result": {"text": "PRIVATE READ"}}
    runtime.last_prompt_signature, runtime.last_reply_malformed = "", False
    async def exchange(event: Any) -> Any:
        captured.append(event)
        return {"text": '{"observation":"o","thought":"t","action":[],"status":"CONTINUE"}'}
    runtime.exchange = exchange
    def immediate(coro: Any, loop: Any) -> Any:
        return SimpleNamespace(result=lambda: asyncio.run(coro))
    monkeypatch.setattr(module.asyncio, "run_coroutine_threadsafe", immediate)
    messages = [{"role": "system", "content": "policy"}, {"role": "user", "content": "literal request"}]
    runtime.response(messages, "AgentType.HOST")
    runtime.response(messages, "AgentType.APP")
    assert captured[0]["messages"][1]["content"] == "literal request"
    assert "UNTRUSTED TASK DATA" in captured[0]["messages"][-1]["content"]
    assert "PRIVATE" not in json.dumps(captured[1])
    assert messages[0]["content"] == "policy"


@pytest.mark.parametrize("finish,name,operation_args,verified,allow_actions,expected_continuations", [
    (True, "filesystem", {"operation": "create_file", "path": "fixture.txt", "text": "x"}, True, True, 0),
    (False, "filesystem", {"operation": "create_file", "path": "fixture.txt", "text": "x"}, True, True, 1),
    (True, "find_application", {"query": "editor"}, False, True, 1),
    (False, "get_desktop_app_info", {}, False, True, 1),
    (False, "filesystem", {"operation": "read", "path": "fixture.txt"}, True, True, 1),
    (True, "find_application", {"query": "editor"}, False, False, 0),
    (True, "get_desktop_app_info", {}, False, False, 0),
    (False, "find_application", {"query": "editor"}, False, False, 1),
    (False, "get_desktop_app_info", {}, False, False, 1),
])
def test_bootstrap_runs_real_dispatcher_then_seeds_official_host(
    module: Any, monkeypatch: Any, finish: bool, name: str, operation_args: dict[str, Any],
    verified: bool, allow_actions: bool, expected_continuations: int,
    instruction: str = "literal instruction", expected_status: str | None = None,
) -> None:
    calls: list[Any] = []
    continuations: list[Any] = []
    class Host:
        def __init__(self, **kwargs: Any) -> None:
            self.prompter = SimpleNamespace(example_prompt_template={})
            self.appagent_dict: dict[str, Any] = {}
            self.processor = None
            self.status = "CONTINUE"
            self.trajectories: list[Any] = []
            self.blackboard = SimpleNamespace(add_trajectories=self.trajectories.append)
    class Round:
        def __init__(self, request: str, agent: Any, context: Any, **kwargs: Any) -> None:
            self.request, self.agent, self.context = request, agent, context
            self.state = SimpleNamespace(name=lambda: "CONTINUE")
            context.set(module.ContextNames.REQUEST, request)
        async def run(self) -> None:
            continuations.append((self.request, self.agent.trajectories,
                                  self.context.command_dispatcher.runtime.initial_result))
            self.state = SimpleNamespace(name=lambda: "FINISH")
            self.context.set(module.ContextNames.ROUND_RESULT, "continued")
    monkeypatch.setattr(module, "ProviderHostAgent", Host)
    monkeypatch.setattr(module, "UIARound", Round)
    monkeypatch.setattr(module, "windows", lambda: [])
    async def run() -> Any:
        async def exchange(event: Any) -> Any:
            return {}
        runtime = module.UFORuntime(exchange)
        class Computer:
            def command2tool(self, command: Any) -> Any:
                return SimpleNamespace(namespace="fixture", command=command)
            async def run_actions(self, commands: Any) -> Any:
                calls.append(commands[0].command)
                data: Any = {"count": 1, "results": [{"name": "Example"}]}
                if name == "get_desktop_app_info":
                    data = [{"id": "1", "name": "PRIVATE WINDOW", "kind": "window"}]
                if verified:
                    data = {"operation": operation_args["operation"], "verified": True, "path": "fixture.txt"}
                    if operation_args["operation"] == "read":
                        data["text"] = "PRIVATE FILE CONTENT"
                    runtime.tools.evidence.append({key: value for key, value in data.items() if key != "text"})
                return [SimpleNamespace(is_error=False, data=data)]
        async def computer(kind: str) -> Any:
            return Computer()
        async def check_risk(command: Any) -> None:
            pass
        monkeypatch.setattr(runtime, "computer", computer)
        monkeypatch.setattr(runtime, "check_risk", check_risk)
        operation = {"name": name, "args": operation_args, "finish": finish}
        result = await runtime.run(instruction, 4, task_id="task", initial_operation=operation,
                                   allow_actions=allow_actions)
        assert runtime.round.context.get(module.ContextNames.REQUEST) == instruction
        return result
    result = asyncio.run(run())
    assert len(calls) == 1
    assert len(continuations) == expected_continuations
    if continuations:
        assert continuations[0][0] == instruction
        assert continuations[0][1][0]["already_executed"] is True
        assert continuations[0][1][0]["action_representation"] == name
        assert "PRIVATE" not in json.dumps(continuations[0][1])
        if name == "get_desktop_app_info":
            assert continuations[0][2]["result"] == [{"id": "1", "name": "PRIVATE WINDOW", "kind": "window"}]
        if operation_args.get("operation") == "read":
            assert continuations[0][2]["result"]["text"] == "PRIVATE FILE CONTENT"
    consultation = not allow_actions and name in {"find_application", "get_desktop_app_info"}
    assert result["status"] == (expected_status or ("SUCCESS" if verified or consultation else "NOT_VERIFIED"))
    assert len(result["operation_states"]) == 1
    if consultation:
        assert result["evidence"] == [{"operation": name, "read_only": True, "verified": True}]
        if finish:
            assert "aberta" not in result["message"].lower()
            assert ("PRIVATE WINDOW" if name == "get_desktop_app_info" else "Example") in result["message"]


@pytest.mark.parametrize("name,instruction", [
    ("find_application", "Procura as aplicações instaladas chamadas editor"),
    ("get_desktop_app_info", "Lista as janelas abertas no computador"),
])
def test_query_permission_without_query_intent_remains_fail_closed(module: Any, monkeypatch: Any, name: str, instruction: str) -> None:
    # The current worker contract carries permission, not a trusted query-only objective.
    test_bootstrap_runs_real_dispatcher_then_seeds_official_host(
        module, monkeypatch, True, name, {"query": "editor"} if name == "find_application" else {},
        False, True, 1, instruction, "NOT_VERIFIED")


@pytest.mark.parametrize("instruction", [
    "Abre o editor de texto instalado", "Procura as aplicações instaladas chamadas editor e abre uma",
])
def test_finding_does_not_complete_a_request_to_open(module: Any, monkeypatch: Any, instruction: str) -> None:
    test_bootstrap_runs_real_dispatcher_then_seeds_official_host(
        module, monkeypatch, True, "find_application", {"query": "editor"}, False, True, 1,
        instruction, "NOT_VERIFIED")


def test_cancellation_after_dispatch_retains_uncertain_identity(module: Any) -> None:
    def cancel(command: Any, tools: Any) -> Any:
        raise asyncio.CancelledError()
    runtime, calls, _ = runtime_for_dispatch(module, cancel)
    dispatcher = module.RuntimeDispatcher(runtime)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dispatcher.execute_commands([command(module, operation="create_file", path="fixture.txt")]))
    assert dispatcher.operation_states()[0]["state"] == "uncertain"
    assert len(calls) == 1


def test_engine_transports_operations_without_arguments_or_text() -> None:
    from app.computer.ufo_engine import UFOEngine
    sent: list[Any] = []
    captured: list[Any] = []
    class Writer:
        def write(self, value: bytes) -> None:
            sent.append(json.loads(value))
        async def drain(self) -> None:
            pass
    async def run() -> Any:
        engine = UFOEngine(SimpleNamespace(max_steps=3))
        engine.process = SimpleNamespace(stdin=Writer())
        async def start() -> None:
            pass
        replies = iter([
            {"event": "operation.started", "operation_id": "task:worker:one", "state": "started", "args": "PRIVATE"},
            {"event": "operation.finished", "operation_id": "task:worker:one", "state": "completed_verified", "verified": True, "text": "PRIVATE"},
            {"ok": True, "data": {"status": "SUCCESS"}},
        ])
        async def read() -> Any:
            return next(replies)
        async def complete(messages: Any, name: str) -> str:
            raise AssertionError("No inference required")
        async def confirm(text: str) -> bool:
            raise AssertionError("No confirmation expected")
        engine.start, engine._read = start, read
        result = await engine.run_task("literal", complete, confirm, task_id="task", allow_actions=False,
            operation_callback=lambda kind, data: captured.append((kind, data)))
        assert engine.operation_states == [{"operation_id": "task:worker:one", "state": "completed_verified", "verified": True}]
        return result
    assert asyncio.run(run())["status"] == "SUCCESS"
    assert sent[0]["allow_actions"] is False
    assert sent[1:] == [{}, {}]
    assert "PRIVATE" not in json.dumps(captured)

"""Standalone contract check; isolated vendor imports, no desktop mutations."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.computer.worker import check_source
from app.brain.cli.catalog import find_spec
from app.brain.cli.headless import HeadlessCliBrain, render_prompt
from app.settings import Settings


def main() -> None:
    vendor = ROOT / "vendor" / "UFO"
    check_source(vendor)
    sys.path.insert(0, str(vendor))
    os.chdir(vendor)
    from aip.messages import MCPToolInfo
    from app.computer.prompts import CompactAppPrompter, CompactHostPrompter
    from config.config_loader import get_ufo_config
    from ufo.prompter.agent_prompter import AppAgentPrompter, HostAgentPrompter
    from ufo.prompter.prompt_sanitizer import sanitize_user_input

    cfg = get_ufo_config()
    cfg.system.action_sequence = True  # mirrors app/computer/worker.py
    cfg.system.enabled_third_party_agents = []
    controls = [{"id": str(i), "name": f'Configuração {i}: ação 😀 "literal"', "type": "Button"}
                for i in range(100)]
    fields: dict[str, Any] = {
        "control_item": controls, "prev_subtask": [{"result": "C:/Área de Trabalho"}],
        "prev_plan": ["Preservar documento\n  espaço e {chaves}"],
        "user_request": 'Escreva exatamente: {"ação": "😀", "espaços": "  "}',
        "subtask": "Não fechar o documento do usuário", "current_application": "Notepad.exe",
        "host_message": ["Preservar consentimento"], "last_success_actions": [{"text": "á  b\n c"}],
        "retrieved_docs": "Texto de referência  sem alterações",
    }
    compact = CompactAppPrompter(False, cfg.system.appagent_prompt, "")
    legacy = AppAgentPrompter(False, cfg.system.appagent_prompt, "")
    tools = [MCPToolInfo(tool_key="WindowsTools::verify_window", tool_name="verify_window", tool_type="action", namespace="WindowsTools",
                         description="Verificar texto literal sem falsos sucessos.",
                         input_schema={"properties": {"expected_text": {"type": "string", "default": "á  b"},
                                                      "active": {"type": "boolean", "description": "Ativo?"}}},
                         output_schema={"description": "Evidência real"})]
    for prompter in (compact, legacy):
        prompter.example_prompt_template = {}
        prompter.prompt_template["system_as_nonvisual"] = prompter.prompt_template["system_as"]
        prompter.create_api_prompt_template(tools)
    # FASE 5 narrows *what is sent*; the representation must stay reversible for whatever
    # is sent, so the lossless assertions run with the complete list.
    compact.full_controls = True
    legacy_user = legacy.user_prompt_construction(**fields)
    compact_user = compact.user_prompt_construction(**fields)
    # The unstructured text and vendor sanitizer must remain byte-for-byte equal.
    for name in ("user_request", "subtask", "retrieved_docs"):
        assert sanitize_user_input(fields[name], name) in compact_user
    assert fields["current_application"] in compact_user
    labels = {
        "control_item": "<Available Control Item:>", "prev_subtask": "<Previous Sub-tasks Results:>",
        "prev_plan": "<Your Next Plan:>", "host_message": "<Message and Tips from the HostAgent:>",
        "last_success_actions": "<The actions you took at the last step and their results:>",
    }
    for name, label in labels.items():
        value = next(line.split(label + " ", 1)[1] for line in compact_user.splitlines() if line.startswith(label))
        decoded = json.loads(value)
        if name == "control_item" and isinstance(decoded, dict):
            decoded = [dict(zip(decoded["columns"], row, strict=True)) for row in decoded["rows"]]
        assert decoded == fields[name], name
    # Exactly the semantic values in the official tool formatter, including examples.
    rows = json.loads(compact.api_prompt_template.split("\n", 1)[1])
    assert rows == [["verify_window", "Verificar texto literal sem falsos sucessos.",
                     [["expected_text", "string", "optional", "", "á  b"],
                      ["active", "boolean", "required", "Ativo?", "N/A"]],
                     "Evidência real", "verify_window(expected_text='á  b', active='')"]]
    concise = compact.system_prompt_construction()
    formal = legacy.system_prompt_construction()
    assert len(concise) < len(formal)
    assert "{apis}" not in concise and "{examples}" not in concise
    assert "verify_window" in concise  # the official tool list is still substituted
    assert "## On screenshots" not in concise and "Screenshot for the last step" not in concise
    for marker in ("## Control item", "## Actions", "## Status of the task", "## Other Guidelines",
                   "\"CONTINUE\"", "\"FINISH\"", "\"FAIL\"", "\"CONFIRM\"", "UserConfirm", "json.loads()",
                   "observation", "thought", "action", "plan", "comment", "save_screenshot", "result",
                   "webcam", "Installing or uninstalling", "browser history", "RepeatTimes", "hotkeys"):
        assert marker in concise, marker
    control_line = next(line for line in compact_user.splitlines() if line.startswith(labels["control_item"]))
    assert json.loads(control_line.split(":> ", 1)[1])["columns"] == ["id", "name", "type"]

    # FASE 5: the narrowed prompt carries a deterministic subset — the window anchor and
    # the controls the sub-task names survive the cut — and both counts are reported, so
    # "menos observações" is a measured number instead of a claim.
    compact.full_controls = False
    compact.control_limit = 30
    narrowed = json.loads(next(line for line in compact.user_prompt_construction(**fields).splitlines()
                               if line.startswith(labels["control_item"])).split(":> ", 1)[1])
    assert narrowed["columns"] == ["id", "name", "type"]
    assert len(narrowed["rows"]) == 30, len(narrowed["rows"])
    assert narrowed["rows"][0][0] == fields["control_item"][0]["id"]
    assert all(row in [[item["id"], item["name"], item["type"]] for item in fields["control_item"]]
               for row in narrowed["rows"])
    assert (compact.controls_collected, compact.controls_sent) == (100, 30)
    compact.full_controls = True
    widened = json.loads(next(line for line in compact.user_prompt_construction(**fields).splitlines()
                              if line.startswith(labels["control_item"])).split(":> ", 1)[1])
    assert widened["rows"] == [[item["id"], item["name"], item["type"]]
                               for item in fields["control_item"]]
    assert (compact.controls_collected, compact.controls_sent) == (100, 100)
    compact.full_controls = False
    # The real adapter supplies metadata without changing official TargetRegistry IDs.
    compact.uia_metadata = {item["id"]: {**item, "parent": "0", "automation_id": "fixture",
        "patterns": ["Invoke"], "enabled": True, "offscreen": False, "focusable": True}
        for item in controls}
    compact.uia_observation = {"truncated": True, "complete": False}
    enriched = json.loads(next(line for line in compact.user_prompt_construction(**fields).splitlines()
        if line.startswith(labels["control_item"])).split(":> ", 1)[1])
    assert enriched["observation"]["truncated"] and not enriched["observation"]["complete"]
    assert len(enriched["elements"]) == 30
    assert enriched["elements"][1]["context"]["id"] == "0"
    assert enriched["elements"][1]["patterns"] == ["Invoke"]
    compact.uia_metadata = {}
    compact.uia_observation = {}
    for cls in (CompactHostPrompter, HostAgentPrompter):
        host = cls(False, cfg.system.hostagent_prompt, "", cfg.system.api_prompt)
        host.create_api_prompt_template(tools)
        text = host.user_prompt_construction(**{k: fields[k] for k in (
            "control_item", "prev_subtask", "prev_plan", "user_request", "retrieved_docs")})
        assert sanitize_user_input(fields["user_request"], "user_request") in text

    # The runtime must actually wire our prompters, not just define them.
    # Also exercised here: the dispatcher's action detection (now shared with the
    # FASE 3 batch counter) and the counters themselves, which pytest cannot import.
    from app.computer.ufo_runtime import ProviderAppAgent, ProviderHostAgent
    wired_app = ProviderAppAgent.get_prompter(None, False, cfg.system.appagent_prompt, "")
    wired_host = ProviderHostAgent.get_prompter(None, False, cfg.system.hostagent_prompt, "",
                                                cfg.system.api_prompt)
    assert isinstance(wired_app, CompactAppPrompter) and isinstance(wired_host, CompactHostPrompter)
    wired_app.example_prompt_template = {}
    wired_app.prompt_template["system_as_nonvisual"] = wired_app.prompt_template["system_as"]
    wired_app.create_api_prompt_template(tools)
    assert wired_app.system_prompt_construction() == concise

    from app.computer.ufo_runtime import RuntimeDispatcher

    class Command:
        def __init__(self, tool_type: str, tool_name: str) -> None:
            self.tool_type, self.tool_name = tool_type, tool_name
            self.parameters: dict[str, Any] = {}
            self.call_id = "call"
            self.namespace = "WindowsTools"

    class Reply:
        is_error = False
        data: dict[str, Any] = {}
        content = None

    class Computer:
        def __init__(self, fail: bool = False) -> None:
            self.fail = fail

        def command2tool(self, command: Any) -> Any:
            return command

        async def run_actions(self, tools: list[Any]) -> list[Any]:
            reply = Reply()
            reply.is_error = self.fail
            return [reply]

    class BatchRuntime:
        def __init__(self, fail: bool = False) -> None:
            self.fail = fail
            self.round = type("Round", (), {"agent": None})()
            self.batches = 0
            self.multi_action_batches = 0
            self.max_batch_actions = 0
            self.actions = 0
            self.controls_sent = 0
            self.controls_escalated = 0
            self.escalate_controls = False
            self.execution_ms = self.observation_ms = self.verification_ms = 0.0
            self.invalidations: list[int] = []
            uia = type("UIA", (), {
                "references": {},
                "invalidate_tree": lambda self: self.owner.invalidations.append(1)})()
            uia.owner = self
            self.tools = type("Tools", (), {
                "pending_ui": False, "evidence": [], "cancelled": False,
                # FASE 2.2: the dispatcher invalidates the snapshot after a failed batch; the
                # real WindowsTools always carries the engine, so the fixture mirrors it.
                "uia": uia})()
            self.state = type("State", (), {"selected_app_window": None})()

        async def computer(self, kind: str) -> Computer:
            return Computer(self.fail)

        async def check_risk(self, command: Any) -> None:
            pass

    batch_runtime = BatchRuntime()
    dispatcher = RuntimeDispatcher(batch_runtime)
    asyncio.run(dispatcher.execute_commands([Command("action", "click_input"),
                                             Command("action", "set_edit_text")]))
    asyncio.run(dispatcher.execute_commands([Command("action", "list_tools"),
                                             Command("action", "")]))
    # Two real actions counted, the meta call and the empty name not.
    assert batch_runtime.actions == 2, batch_runtime.actions
    assert (batch_runtime.batches, batch_runtime.multi_action_batches,
            batch_runtime.max_batch_actions) == (2, 1, 2)

    # FASE 5: a failed batch asks the next round for the complete control list.
    failed_runtime = BatchRuntime(fail=True)
    asyncio.run(RuntimeDispatcher(failed_runtime).execute_commands([Command("action", "click_input")]))
    assert failed_runtime.escalate_controls is True
    assert batch_runtime.escalate_controls is False

    # FASE 2.2/2.3: a control that vanished mid-batch stops the batch instead of letting
    # the remaining actions run against a dead reference, and the snapshot is invalidated
    # so the next round re-observes the window.
    vanished = BatchRuntime(fail=True)
    asyncio.run(RuntimeDispatcher(vanished).execute_commands(
        [Command("action", "click_input"), Command("action", "set_edit_text")]))
    assert vanished.actions == 1, vanished.actions
    assert vanished.invalidations == [1], vanished.invalidations
    assert batch_runtime.invalidations == [], batch_runtime.invalidations

    # A round that repeats the same failing action must stop itself (FASE 3), and the
    # `wait` shadow must only return early when the window is actually observable.
    from app.computer.ufo_runtime import NoProgressAbort, PersistentComputer

    # FASE 4 fast path: the Settings page tool must exist on the real server, and the
    # dispatcher must not ask for an AppAgent after it — it verifies its own page text.
    from app.computer.settings_pages import resolve_page
    from app.computer.windows_tools import WindowsTools

    async def never_confirm(_: str) -> bool:
        return False

    assert resolve_page("Sistema > Som").uri == "ms-settings:sound"
    server = WindowsTools(state=object(), confirm=never_confirm).server()
    registered = sorted(tool.name for tool in asyncio.run(server.list_tools()))
    assert "open_settings_page" in registered, registered

    # FASE 6: the fixed 3 s deadline is a state-directed budget now, and the tools that
    # apply it must still exist on the real server.
    from app.computer.windows_tools import (VERIFY_BUDGET_CEILING, VERIFY_BUDGET_DEFAULT,
                                            verification_budget)
    assert {"verify_window", "window_action"} <= set(registered), registered
    assert verification_budget("window") == VERIFY_BUDGET_DEFAULT["window"]
    assert verification_budget("window", [90.0, 120.0]) == VERIFY_BUDGET_DEFAULT["window"]
    assert verification_budget("window", [9000.0] * 5) == VERIFY_BUDGET_CEILING
    assert verification_budget("settings", [4000.0] * 5) == VERIFY_BUDGET_CEILING

    class LoadingControl:
        def __init__(self, text: str) -> None:
            self.text = text

        def get_value(self) -> str:
            return self.text

    class LoadingWindow:
        handle = 9
        reads = 0

        def window_text(self) -> str:
            return "UFO-Test-2"

        def descendants(self, control_type: str = "") -> list[LoadingControl]:
            type(self).reads += 1
            # The item the caller asks for only appears on the second read.
            if self.reads > 1:
                return [LoadingControl("Carregando"), LoadingControl("UFO-Test-2")]
            return [LoadingControl("Carregando")]

    loading = LoadingWindow()
    loading_server = WindowsTools(state=type("State", (), {"selected_app_window": loading})(),
                                  confirm=never_confirm).server()
    import app.computer.windows_tools as windows_tools_module
    real_is_window = windows_tools_module.user32.IsWindow
    windows_tools_module.user32.IsWindow = lambda handle: 1
    try:
        result = asyncio.run(loading_server.call_tool("verify_window",
                                                      {"expected_text": "UFO-Test-2"}))
    finally:
        windows_tools_module.user32.IsWindow = real_is_window
    page = getattr(result, "structured_content", None) or getattr(result, "data", None) or {}
    # One read saw a tree that was still loading; the re-read saw the text it carries.
    assert page["verified"] is True and page["polls"] == 2 and loading.reads == 2, page

    # A selection is not evidence of a completed writing subtask. A bare AppAgent
    # FINISH must stay inside the official loop while its UI postcondition is pending.
    selection_runtime = BatchRuntime()
    selection_runtime.state.selected_app_window = object()
    asyncio.run(RuntimeDispatcher(selection_runtime).execute_commands([
        Command("action", "select_application_window")]))
    assert selection_runtime.tools.pending_ui is True
    assert selection_runtime.tools.evidence[0]["verified"] is True

    from app.computer.ufo_runtime import NoScreenshotsActions
    from ufo.agents.processors.strategies.app_agent_processing_strategy import AppActionExecutionStrategy
    from ufo.agents.processors.context.processing_context import ProcessingPhase, ProcessingResult
    original_execute = AppActionExecutionStrategy.execute
    action = type("Action", (), {"function": "", "arguments": {}, "status": "FINISH"})()
    action_info = type("Actions", (), {"actions": [action]})()
    async def unverified_finish(self: Any, agent: Any, context: Any) -> Any:
        return ProcessingResult(success=True, phase=ProcessingPhase.ACTION_EXECUTION,
                                data={"status": "FINISH", "action_info": action_info})
    context = type("Context", (), {
        "get_local": lambda self, name: None,
        "global_context": type("Global", (), {
            "command_dispatcher": type("Dispatcher", (), {"runtime": selection_runtime})()})()})()
    selection_runtime.tools.cancelled = False
    from app.computer.ufo_runtime import UIAHostProcessor
    from ufo.agents.processors.host_agent_processor import HostAgentProcessor
    original_finalize = HostAgentProcessor._finalize_processing_context
    class HostContext:
        def __init__(self) -> None:
            self.values = {"function_name": "select_application_window", "selected_application_root": "Notepad.exe",
                           "status": "CONTINUE"}
            self.phase_results = {ProcessingPhase.ACTION_EXECUTION: ProcessingResult(
                success=True, data={"execution_result": [type("Reply", (), {"status": ResultStatus.SUCCESS})()]})}
        def get_local(self, key: str) -> Any:
            return self.values.get(key)
        def set_local(self, key: str, value: Any) -> None:
            self.values[key] = value
    from aip.messages import ResultStatus
    host_context = HostContext()
    try:
        HostAgentProcessor._finalize_processing_context = lambda self, context: None
        host_context.global_context = context.global_context
        UIAHostProcessor.__new__(UIAHostProcessor)._finalize_processing_context(host_context)
    finally:
        HostAgentProcessor._finalize_processing_context = original_finalize
    assert host_context.values["status"] == "ASSIGN"
    from app.computer.ufo_runtime import UFORuntime
    class TextControl:
        element_info = type("Info", (), {"control_type": "Document"})()
        def get_value(self) -> str:
            return "agent-owned text"
    risk_runtime = UFORuntime.__new__(UFORuntime)
    risk_runtime.state = type("State", (), {
        "control_dict": {"1": TextControl()}, "selected_app_window": type("Window", (), {
            "handle": 29, "process_id": lambda self: os.getpid()})()})()
    risk_runtime.tools = WindowsTools(risk_runtime.state, never_confirm)
    risk_runtime.tools.owned_documents[29] = "agent-owned text"
    overwrite = Command("action", "set_edit_text")
    overwrite.parameters = {"id": "1", "clear_current_text": True}
    asyncio.run(risk_runtime.check_risk(overwrite))
    risk_runtime.tools.owned_documents.clear()
    try:
        asyncio.run(risk_runtime.check_risk(overwrite))
    except PermissionError:
        assert risk_runtime.tools.cancelled
    else:
        raise AssertionError("User-owned contents must still require overwrite consent")
    # Empty-action sentinels are official no-ops, not sensitive unknown tools.
    noop_runtime = BatchRuntime()
    asyncio.run(RuntimeDispatcher(noop_runtime).execute_commands([Command("action", "N/A")]))
    assert noop_runtime.actions == 0 and not noop_runtime.tools.pending_ui
    cancelled_runtime = BatchRuntime()
    cancelled_runtime.tools.cancelled = True
    refused = asyncio.run(RuntimeDispatcher(cancelled_runtime).execute_commands([
        Command("action", "click_input")]))
    assert cancelled_runtime.actions == 0 and refused[0].status == ResultStatus.FAILURE
    # A failed verification invalidates older launch evidence; it must never inherit
    # the previous task's selected window or silently leave pending_ui false.
    verification_runtime = BatchRuntime(fail=True)
    asyncio.run(RuntimeDispatcher(verification_runtime).execute_commands([
        Command("action", "verify_window")]))
    assert verification_runtime.tools.pending_ui is True
    try:
        AppActionExecutionStrategy.execute = unverified_finish
        guarded_finish = asyncio.run(NoScreenshotsActions().execute(None, context))
        assert guarded_finish.data["status"] == "CONTINUE"
        assert action.status == "CONTINUE"
        selection_runtime.tools.pending_ui = False
        guarded_finish = asyncio.run(NoScreenshotsActions().execute(None, context))
        assert guarded_finish.data["status"] == "FINISH"
    finally:
        AppActionExecutionStrategy.execute = original_execute

    settings_runtime = BatchRuntime()
    settings_dispatcher = RuntimeDispatcher(settings_runtime)
    asyncio.run(settings_dispatcher.execute_commands([Command("action", "open_settings_page")]))
    assert settings_runtime.tools.pending_ui is False
    launch = Command("action", "launch_application")
    launch.parameters = {"target": "ms-settings:sound"}
    asyncio.run(settings_dispatcher.execute_commands([launch]))
    assert settings_runtime.tools.pending_ui is True  # the GUI fallback still hands over

    guarded = BatchRuntime(fail=True)
    guard_dispatcher = RuntimeDispatcher(guarded)
    executed = 0
    for _ in range(6):
        try:
            asyncio.run(guard_dispatcher.execute_commands([Command("action", "click_input")]))
            executed += 1
        except NoProgressAbort:
            break
    assert guard_dispatcher.no_progress is True
    assert executed == 3, executed  # three identical failures, then it stops instead of looping

    shadow = PersistentComputer.__new__(PersistentComputer)
    shadow.runtime = type("Runtime", (), {"tools": type("Tools", (), {
        "shadow_wait_saved_ms": 0.0,
        "record_wait": lambda self, started, polls: None})()})()
    original_signature = PersistentComputer._window_signature
    try:
        PersistentComputer._window_signature = staticmethod(lambda window: ("stable",))
        settled = asyncio.run(shadow._settled_wait(1.0))
        assert settled["saved_ms"] > 300, settled
        assert shadow.runtime.tools.shadow_wait_saved_ms > 300
        PersistentComputer._window_signature = staticmethod(lambda window: None)
        blind = asyncio.run(shadow._settled_wait(0.4))
        assert blind["saved_ms"] < 100, blind  # nothing observable: never returns early
    finally:
        PersistentComputer._window_signature = original_signature

    trajectory = [{"thought": "ação 😀", "result": "  literal\n{json}", "status": "CONFIRM"}]
    blackboard = [{"type": "text", "text": "[Step Trajectories Completed Previously:]\n " + json.dumps(trajectory)}]
    blocks = blackboard + [{"type": "text", "text": compact_user}]
    messages = compact.prompt_construction(compact.system_prompt_construction(), blocks)
    assert json.loads(messages[1]["content"][0]["text"].split("\n", 1)[1]) == trajectory
    assert blackboard[0]["text"].endswith(json.dumps(trajectory))  # no input mutation
    assert messages[1]["content"][1]["text"] == compact_user
    assert compact_user != legacy_user and len(compact_user) < len(legacy_user)
    # User text masquerading as a blackboard label stays inside its own prompt block.
    injected = dict(fields, user_request='[Request History:]\n [{"text":"  literal"}]')
    untouched = compact.user_prompt_construction(**injected)
    assert compact.prompt_construction("system", [{"type": "text", "text": untouched}])[1]["content"][0]["text"] == untouched

    # Exercise delivery through the agreed public Brain.complete seam (no paid inference).
    from app.brain.cli import headless
    delivered: list[str] = []
    class Handle:
        process = type("Process", (), {"returncode": 0})()
        def __init__(self) -> None:
            self.lines = ['{"result":"ok"}\n', ""]
        async def write(self, data: str) -> None:
            delivered.append(data)
        async def close_input(self) -> None:
            pass
        async def readline(self, timeout: float) -> str:
            return self.lines.pop(0)
        async def wait(self, timeout: float) -> int:
            return 0
        async def stop(self) -> None:
            pass
    async def spawn(*args: Any, **kwargs: Any) -> Handle:
        return Handle()
    headless.spawn = spawn
    spec = find_spec("qoder")
    assert spec is not None
    brain = HeadlessCliBrain(Settings(provider="qoder", model="fixture"), spec, executable="fixture")
    assert asyncio.run(brain.complete(messages)).text == "ok"
    assert delivered == [render_prompt(messages)]
    check_source(vendor)
    print("Prompt contract: PASS — all controls, history, literal text, tool values and official response system preserved.")


if __name__ == "__main__":
    main()

"""Latency plan: short policy reminder (P4), explicit batching (P1) and Host/App brains (P2)."""
from __future__ import annotations

import asyncio
import pytest
from pathlib import Path
from typing import Any

from app.core.controller import TaskController, app_brain_settings
from app.llm import Completion
from app.settings import ConfigurationError, Settings

ROOT = Path(__file__).resolve().parents[1]


def _runtime_module(monkeypatch: Any) -> Any:
    """Import the worker-side runtime the way the worker itself sees it.

    The pinned vendor config loads from relative paths, so the import (like the
    worker boot) runs with ``vendor/UFO`` as the working directory. Scoped to the
    test: the directory is restored afterwards.
    """
    monkeypatch.chdir(ROOT / "vendor" / "UFO")
    monkeypatch.syspath_prepend(str(ROOT / "vendor" / "UFO"))
    import app.computer.ufo_runtime as runtime_module
    return runtime_module


def test_fast_path_failure_retains_executed_action_metrics(monkeypatch: Any) -> None:
    from types import SimpleNamespace
    module = _runtime_module(monkeypatch)
    monkeypatch.setattr(module, "windows", lambda: [])
    runtime = module.UFORuntime.__new__(module.UFORuntime)
    runtime.tools = SimpleNamespace(uia=SimpleNamespace(controls_inspected=2), waits_ms=0)
    evidence = [{"operation": "set_edit_text", "executed": True, "verified": True},
                {"operation": "keyboard_input", "executed": True, "verified": False},
                {"operation": "uia_verify", "verified": False}]
    result = runtime._fast_result("unverified", "missing results", "search", "fixture", evidence, 3, 20)
    assert result["actions_executed"] == 2
    result = runtime._fast_result("success", "already done", "search", "fixture",
                                  [{"operation": "set_edit_text", "executed": False, "verified": True}], 1, 20)
    assert result["actions_executed"] == 0
    result = runtime._fast_result("success", "page verified", "settings", "fixture",
                                  [{"operation": "open_settings_page", "verified": True}], 1, 20)
    assert result["actions_executed"] == 1


def _runtime(captured: list[dict[str, Any]], runtime_module: Any) -> Any:
    runtime = runtime_module.UFORuntime.__new__(runtime_module.UFORuntime)
    runtime.loop = object()
    runtime.recent_actions = []
    runtime.last_prompt_signature = ""
    runtime.last_reply_malformed = False

    async def exchange(event: dict[str, Any]) -> dict[str, Any]:
        captured.append(event)
        return {"text": '{"observation": "o", "thought": "t", "action": [], "status": "CONTINUE"}'}

    runtime.exchange = exchange  # type: ignore[method-assign]
    return runtime


def _answer(runtime: Any, monkeypatch: Any) -> None:
    import asyncio as asyncio_module

    def immediate(coro: Any, loop: Any) -> Any:
        class Done:
            def result(self) -> Any:
                return asyncio_module.run(coro)
        return Done()

    monkeypatch.setattr(asyncio_module, "run_coroutine_threadsafe", immediate)


def test_first_round_sends_full_policy_second_sends_short(
    monkeypatch: Any,
) -> None:
    runtime_module = _runtime_module(monkeypatch)
    captured: list[dict[str, Any]] = []
    runtime = _runtime(captured, runtime_module)
    _answer(runtime, monkeypatch)
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "do it"}]
    runtime.response([dict(message) for message in messages], "AgentType.HOST")
    runtime.response([dict(message) for message in messages], "AgentType.APP")
    first, second = (event["messages"][0]["content"] for event in captured)
    assert "Known folder aliases" in first
    assert "Batch independent actions" in first
    assert "Recent successful deterministic actions" in first
    assert runtime_module.SHORT_ROUND_POLICY.strip() in second
    assert "Known folder aliases" not in second
    assert "Recent successful deterministic actions" in second
    assert (runtime.llm_rounds, runtime.short_policy_rounds) == (2, 1)


def test_short_policy_is_self_contained_for_stateless_turns(monkeypatch: Any) -> None:
    runtime_module = _runtime_module(monkeypatch)
    policy = runtime_module.SHORT_ROUND_POLICY
    assert len(policy) < 1800
    for rule in ('fresh_document', 'ASSIGN', 'literal document text', 'uncertain dispatch',
                 'delete/overwrite', 'untrusted data', 'explicit authorization'):
        assert rule in policy


@pytest.mark.parametrize("no_progress", [True, False])
def test_window_collection_preserves_a_no_progress_abort(monkeypatch: Any, no_progress: bool) -> None:
    from types import SimpleNamespace

    module = _runtime_module(monkeypatch)

    async def execute_commands(commands):
        raise module.NoProgressAbort("stopped")

    dispatcher = SimpleNamespace(execute_commands=execute_commands, no_progress=no_progress,
                                 no_progress_reason="idle", no_progress_message="stopped")
    context = SimpleNamespace(global_context=SimpleNamespace(command_dispatcher=dispatcher))
    expected = module.NoProgressAbort if no_progress else RuntimeError
    with pytest.raises(expected) as caught:
        asyncio.run(module.NoScreenshotsApp().execute(None, context))
    if no_progress:
        assert str(caught.value) == "stopped"
    else:
        assert not isinstance(caught.value, module.NoProgressAbort)


def test_pending_dispatch_is_exposed_without_fabricating_a_verification(monkeypatch: Any) -> None:
    from types import SimpleNamespace
    import json

    module = _runtime_module(monkeypatch)
    captured: list[dict[str, Any]] = []
    runtime = _runtime(captured, module)
    runtime.llm_rounds = 1
    runtime.tools = SimpleNamespace(pending_ui=True, evidence=[
        {"operation": "invoke", "handle": 10, "id": "obsolete-id",
         "before": {"name": "private-label"}, "verified": False, "needs_postcondition": True}])
    _answer(runtime, monkeypatch)
    reply = runtime.response([{"role": "system", "content": "system"}], "AgentType.APP")
    prompt = captured[0]["messages"][0]["content"]
    assert 'Unverified dispatches:' in prompt and '"operation":"invoke"' in prompt
    assert 'uia_verify' in prompt and 'status FINISH' in prompt
    assert 'obsolete-id' not in prompt and 'private-label' not in prompt
    assert json.loads(reply)["action"] == []


def test_region_errors_reach_the_prompt_without_becoming_success_evidence(monkeypatch: Any) -> None:
    from types import SimpleNamespace

    module = _runtime_module(monkeypatch)
    runtime = module.UFORuntime.__new__(module.UFORuntime)
    observation = {"elements": [{"id": "live"}], "complete": False, "truncated": True}
    runtime.tools = SimpleNamespace(uia=SimpleNamespace(elements=[{"id": "live"}],
                                                       last_observation=observation))
    runtime.region_expansion_errors.extend({"id": str(i), "code": "provider_error"} for i in range(8))
    runtime.regions_read[(1,)] = 0
    agent = SimpleNamespace(prompter=SimpleNamespace())
    context = SimpleNamespace(get_local=lambda key: None,
                              global_context=SimpleNamespace(command_dispatcher=SimpleNamespace(runtime=runtime)))

    async def send_prompt(self, agent, context):
        delivered = agent.prompter.uia_observation
        assert len(delivered["uia_region_errors"]) == 4
        assert delivered["uia_region_reads"] == 1
        assert not delivered["complete"] and delivered["truncated"]
        assert "elements" not in delivered and "verified" not in delivered
        return "sent"

    monkeypatch.setattr(module.AppLLMInteractionStrategy, "execute", send_prompt)
    assert asyncio.run(module.NoScreenshotsLLM().execute(agent, context)) == "sent"
    assert "uia_region_errors" not in observation


def test_app_brain_defaults_to_reusing_the_main_brain() -> None:
    assert app_brain_settings(Settings(provider="codex", model="m")) is None


def test_app_brain_inherits_model_only_with_the_same_provider() -> None:
    settings = app_brain_settings(Settings(provider='openai', model='gpt-x', app_provider='openai'))
    assert settings is not None
    assert (settings.provider, settings.model) == ('openai', 'gpt-x')
    with pytest.raises(ConfigurationError):
        app_brain_settings(Settings(provider='openai', model='gpt-x', app_provider='nvidia'))


def test_app_brain_drops_a_key_a_cli_runtime_must_not_carry() -> None:
    settings = app_brain_settings(Settings(provider="openai", model="gpt-x", api_key="k",
                                          app_provider="qoder", app_model="fast"))
    assert settings is not None
    assert (settings.api_key, settings.base_url) == ("", "")


def test_app_brain_rejects_an_unknown_provider() -> None:
    try:
        app_brain_settings(Settings(provider="openai", model="m",
                                   app_provider="nope", app_model="x"))
    except ConfigurationError:
        return
    raise AssertionError("unknown app provider was accepted")


def test_malformed_retry_keeps_nudge_when_policy_changes(monkeypatch):
    module = _runtime_module(monkeypatch)
    captured = []
    rt = _runtime(captured, module)
    async def exchange(event):
        captured.append(event)
        return {'text': 'not json' if len(captured) == 1 else '{}'}
    rt.exchange = exchange
    _answer(rt, monkeypatch)
    messages = [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'task'}]
    with pytest.raises(module.MalformedReply):
        rt.response(messages, 'AgentType.APP')
    rt.response(messages, 'AgentType.APP')
    assert captured[-1]['messages'][-1]['content'] == module.REPLY_PROTOCOL_NUDGE
    assert messages[0]['content'] == 'system'


def test_model_only_override_uses_same_provider():
    settings = app_brain_settings(Settings(provider='kiro', model='auto', app_model='fast'))
    assert (settings.provider, settings.model) == ('kiro', 'fast')


def test_different_provider_never_inherits_key_or_endpoint(monkeypatch):
    monkeypatch.setenv('NVIDIA_API_KEY', 'own-nvidia-key')
    monkeypatch.delenv('LLM_APP_BASE_URL', raising=False)
    settings = app_brain_settings(Settings(provider='openai', model='m', api_key='openai-secret',
        base_url='https://openai.example/v1', app_provider='nvidia', app_model='fast'))
    assert settings.api_key == 'own-nvidia-key' and settings.base_url == ''


class _HostBrain:
    async def complete(self, messages: list[dict]) -> Completion:
        return Completion('{"status": "FINISH", "comment": "host"}', 10, 2)

    async def close(self) -> None:
        pass


class _AppBrain:
    async def complete(self, messages: list[dict]) -> Completion:
        return Completion('{"status": "FINISH", "comment": "app"}', 5, 1)

    async def close(self) -> None:
        pass


class _RoutingEngine:
    process = None
    worker_ready = True

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def run_task(self, instruction: str, completion: Any, confirm: Any, *,
                       approval_mode: str = "manual") -> dict[str, Any]:
        host = await completion([{"role": "user", "content": instruction}], "HostAgent")
        app = await completion([{"role": "user", "content": instruction}], "AgentType.APP")
        assert "host" in host and "app" in app
        return {"status": "SUCCESS", "message": "ok", "actions": 2, "steps": 2,
                "evidence": [{"operation": "launch", "verified": True}]}


def test_completion_routes_app_agent_to_the_fast_brain(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    from app.core import controller as controller_module

    def resolve(settings: Settings) -> Any:
        return _AppBrain() if settings.provider == "qoder" else _HostBrain()

    monkeypatch.setattr(controller_module, "resolve_brain", resolve)

    async def scenario() -> Any:
        settings = Settings(provider="codex", model="fixture-model", app_provider="qoder",
                            app_model="fast", fast_path=False, logs_path=tmp_path,
                            collect_metrics=False)
        controller = TaskController(settings)
        controller.engine = _RoutingEngine()  # type: ignore[assignment]
        result = await controller.run("Abra a Calculadora.")
        await controller.close()
        return result

    result = asyncio.run(scenario())
    assert result.status == "SUCCESS"
    assert [call["brain"] for call in result.prompt_calls] == ["host", "app"]
    assert (result.app_provider, result.app_model) == ("qoder", "fast")


def test_completion_without_app_pair_keeps_one_brain(tmp_path: Path, monkeypatch: Any) -> None:
    from app.core import controller as controller_module

    brains: list[str] = []

    def resolve(settings: Settings) -> Any:
        brains.append(settings.provider)
        return _HostBrain()

    monkeypatch.setattr(controller_module, "resolve_brain", resolve)

    async def scenario() -> Any:
        settings = Settings(provider="codex", model="fixture-model", fast_path=False,
                            logs_path=tmp_path, collect_metrics=False)
        controller = TaskController(settings)
        controller.engine = _RoutingEngine()  # type: ignore[assignment]
        result = await controller.run("Abra a Calculadora.")
        await controller.close()
        return result

    result = asyncio.run(scenario())
    assert brains == ["codex"]
    assert [call["brain"] for call in result.prompt_calls] == ["host", "host"]
    assert (result.app_provider, result.app_model) == ("", "")

"""B2: offline destination payloads, task-local provenance and applied inheritance."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app import settings as settings_module
from app.brain.api_brain import ApiBrain
from app.brain.effort import reasoning_parameters
from app.brain.registry import descriptors
from app.bridge.service import BridgeService, public_task_result_dto
from app.core.controller import TaskController, TaskResult, app_brain_settings
from app.brain.contracts import Capabilities
from app.llm import Completion
from app.memory import MemoryManager
from app.settings import ConfigurationError, Settings


@pytest.mark.parametrize('provider,model,effort,expected', [
    ('gemini', 'gemini-2.5-flash', 'none', {'reasoning_effort': 'none'}),
    ('gemini', 'models/gemini-2.5-pro', 'low', {'reasoning_effort': 'low'}),
    ('gemini', 'gemini-3-flash-preview', 'minimal', {'reasoning_effort': 'minimal'}),
    ('gemini', 'gemini-3.8-flash', 'low', {'reasoning_effort': 'low'}),
    ('gemini', 'gemini-3-pro-preview', 'high', {'reasoning_effort': 'high'}),
    ('gemini', 'gemini-future', 'low', {}),
    ('gemini', 'gemini-2.5-flash', '', {}),
    ('openai', 'o3', 'low', {'reasoning_effort': 'low'}),
    ('openai', 'gpt-5-mini', 'minimal', {'reasoning_effort': 'minimal'}),
    ('openai', 'gpt-4o', 'low', {}),
    ('openrouter', 'google/gemini-3-flash-preview', 'low',
     {'reasoning': {'effort': 'low', 'exclude': True}}),
    ('openrouter', 'openrouter/auto', 'low', {}),
    ('anthropic', 'claude-sonnet-4-6', 'medium', {'output_config': {'effort': 'medium'}}),
    ('anthropic', 'claude-old', 'low', {}),
    ('nvidia', 'nvidia/nemotron-3-super-120b-a12b', 'low', {}),
    ('custom', 'gemini-2.5-flash', 'low', {}),
    ('ollama', 'o3', 'low', {}),
])
def test_effort_only_for_documented_destination(provider, model, effort, expected) -> None:
    assert reasoning_parameters(Settings(provider=provider, model=model, effort=effort)) == expected


@pytest.mark.parametrize('model,effort', [('gemini-2.5-pro', 'none'), ('gemini-3-pro-preview', 'none'),
                                       ('gemini-3-pro-preview', 'medium'), ('gemini-2.5-flash', 'turbo')])
def test_invalid_documented_effort_is_not_silently_replaced(model, effort) -> None:
    with pytest.raises(ConfigurationError):
        reasoning_parameters(Settings(provider='gemini', model=model, effort=effort))


@pytest.mark.parametrize('provider,url,model', [
    ('gemini', 'https://example.test/v1beta/openai', 'gemini-2.5-flash'),
    ('gemini', 'https://generativelanguage.googleapis.com/custom', 'gemini-2.5-flash'),
    ('openrouter', 'https://example.test/api/v1', 'openai/o3'),
])
def test_borrowed_provider_name_does_not_inject_effort(provider, url, model) -> None:
    assert reasoning_parameters(Settings(provider=provider, model=model, base_url=url, effort='low')) == {}


def test_effort_reaches_one_final_only_request_and_usage_zero_survives() -> None:
    async def scenario() -> None:
        brain = ApiBrain(Settings(provider='openrouter', model='openai/o3', api_key='SYNTHETIC', effort='low'))
        await brain._provider.client.aclose()
        bodies = []
        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'message': {'content': 'answer'}}],
                                           'usage': {'prompt_tokens': 0, 'completion_tokens': 0}})
        brain._provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        events = [event async for event in brain.stream([{'role': 'user', 'content': 'fixture'}])]
        assert len(bodies) == len(brain.call_metrics) == 1
        assert bodies[0]['reasoning'] == {'effort': 'low', 'exclude': True}
        assert events[0].data['streaming'] is False
        assert events[-1].data['completion'].input_tokens == 0
        assert brain.settings.model == 'openai/o3'
        await brain.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('provider,model,inheritance', [('', '', 'full'), ('', 'app-fixed', 'provider'),
                                                      ('qoder', '', 'model'), ('qoder', 'app-fixed', 'none')])
def test_applied_effective_selection_and_partial_persistence(tmp_path, monkeypatch, provider, model, inheritance) -> None:
    monkeypatch.setattr(settings_module, 'ROOT', tmp_path)
    for name in ('LLM_PROVIDER', 'LLM_MODEL', 'LLM_APP_PROVIDER', 'LLM_APP_MODEL'):
        monkeypatch.delenv(name, raising=False)
    base = Settings(provider='qoder', model='host-fixed', app_provider=provider, app_model=model)
    svc = BridgeService.__new__(BridgeService)
    svc.settings, svc.sandbox_dir = base, tmp_path
    candidate = svc._candidate_settings_from_payload({'model': 'host-new'})
    assert (candidate.app_provider, candidate.app_model) == (provider, model)
    effective = svc._public_brain_state()['effective']
    assert effective['app_inheritance'] == inheritance
    assert effective['app']['model'] == (model or 'host-fixed')
    app = app_brain_settings(base)
    assert (app is None) is (inheritance == 'full')
    svc._persist_brain_selection(candidate, remember=False)
    path = tmp_path / '.local/app-brain.json'
    assert path.exists() is (inheritance != 'full')
    settings_module.save_selection('qoder', 'host-new')
    settings_module.save_app_selection(provider, model)
    saved = Settings.load()
    assert (saved.app_provider, saved.app_model) == (provider, model)


def test_distinct_app_provider_never_inherits_host_key(monkeypatch) -> None:
    monkeypatch.setenv('GEMINI_API_KEY', 'SYNTHETIC_APP')
    app = app_brain_settings(Settings(provider='openrouter', model='host', api_key='SYNTHETIC_HOST',
                                     app_provider='gemini', app_model='gemini-2.5-flash', effort='low'))
    assert app is not None and app.api_key == 'SYNTHETIC_APP' and app.effort == ''


def test_metrics_ring_buffer_roles_failure_and_cancellation_do_not_reuse_stale() -> None:
    controller = TaskController.__new__(TaskController)
    controller.settings = Settings(provider='gemini', model='fixture')
    old = [{'ok': True, 'total_ms': 999} for _ in range(32)]
    brain = SimpleNamespace(provider_id='gemini', call_metrics=old)
    result = TaskResult()
    with controller._provider_metrics(result, brain, 'app'):
        brain.call_metrics.append({'ok': True, 'total_ms': 2, 'first_text_ms': 0, 'api_key': 'SENTINEL'})
        del brain.call_metrics[:-32]
    assert len(result.provider_calls) == 1
    call = result.provider_calls[0]
    assert (call['brain'], call['requests'], call['retries'], call['first_text_ms']) == ('app', 1, 0, 0)
    assert 'SENTINEL' not in json.dumps(result.provider_calls)
    for exc in (RuntimeError(), asyncio.CancelledError()):
        failed = TaskResult()
        with pytest.raises(type(exc)), controller._provider_metrics(failed, brain, 'host'):
            raise exc
        controller._record_provider_timing(failed)
        assert failed.provider_calls == [] and failed.provider_wait_ms is None
    failed = TaskResult()
    with pytest.raises(RuntimeError), controller._provider_metrics(failed, brain, 'host'):
        brain.call_metrics.append({'ok': False, 'total_ms': 0, 'private': 'SENTINEL'})
        del brain.call_metrics[:-32]
        raise RuntimeError()
    assert len(failed.provider_calls) == 1 and failed.provider_calls[0]['ok'] is False
    cli = SimpleNamespace(provider_id='qoder', cli_calls=[])
    with controller._provider_metrics(result, cli, 'host'):
        cli.cli_calls.append({'ok': False, 'spawn_ms': 0, 'total_ms': 3})
    assert result.provider_calls[-1]['requests'] is result.provider_calls[-1]['retries'] is None
    controller._record_provider_timing(result)
    assert result.provider_wait_ms == 5 and result.provider_startup_ms == 0


@pytest.mark.parametrize('attempts,status,total,reported', [
    ([{'input_tokens': 0, 'output_tokens': 0}], 'complete', 0, 0),
    ([{'input_tokens': 12, 'output_tokens': 0}, {}], 'partial', None, 12),
    ([{}], 'unavailable', None, None),
])
def test_usage_is_attempt_based_partial_and_null_not_zero(attempts, status, total, reported) -> None:
    controller = TaskController.__new__(TaskController)
    result = TaskResult(llm_calls=len(attempts), prompt_calls=[{'usage_attempts': attempts}])
    controller._record_provider_timing(result)
    dto = public_task_result_dto(result)
    assert dto['token_usage']['status'] == status
    assert dto['total_tokens'] == total
    assert dto['token_usage']['input_reported'] == reported
    assert dto['token_usage']['calls_total'] == len(attempts)


def test_operation_counts_use_one_coherent_set() -> None:
    result = TaskResult(operation_states=[
        {'category': 'read_only', 'state': 'completed_verified', 'verified': True},
        {'category': 'mutation', 'state': 'completed_verified', 'verified': True},
        {'category': 'mutation', 'state': 'uncertain', 'verified': False},
    ])
    assert public_task_result_dto(result)['operation_counts'] == {
        'total': 3, 'read_only': 1, 'mutation': 2, 'verified_mutations': 1, 'uncertain': 1}
    result.operation_states.append({'state': 'unknown'})
    assert 'operation_counts' not in public_task_result_dto(result)


def test_matrix_inventory_is_finite_and_not_a_promise_of_runtime_versions() -> None:
    assert len(descriptors()) == 17
    assert next(desc for desc in descriptors() if desc.id == 'opencode').implemented is False
    # free-pi-cli is a known runtime, not an adapter: visible, disabled, with a reason.
    freepi = next(desc for desc in descriptors() if desc.id == 'freepi')
    assert freepi.implemented is False and freepi.unavailable_reason


def test_real_controller_attributes_inherited_app_role_and_empty_retry_usage(tmp_path) -> None:
    class Brain:
        provider_id = 'custom'
        capabilities = Capabilities.of()
        def __init__(self):
            self.call_metrics = []
            self.count = 0
        async def complete(self, messages):
            self.count += 1
            self.call_metrics.append({'ok': True, 'total_ms': self.count})
            if self.count == 1:
                return Completion('', 0, 0)
            return (Completion('{"status":"FINISH"}', 10, 3) if self.count == 2
                    else Completion('{"status":"FINISH"}'))
        async def close(self):
            pass
    class Engine:
        process = None
        worker_ready = False
        async def start(self):
            pass
        async def run_task(self, instruction, completion, confirm, **kwargs):
            await completion([{'role': 'user', 'content': 'fixture'}], 'HostAgent')
            await completion([{'role': 'user', 'content': 'fixture'}], 'AppAgent')
            return {'status': 'SUCCESS', 'message': 'fixture',
                    'evidence': [{'operation': 'launch', 'verified': True}]}
        async def stop(self):
            pass
    async def scenario():
        settings = Settings(provider='custom', model='fixture', api_key='synthetic',
                            base_url='http://127.0.0.1:1/v1', fast_path=False, prewarm=False,
                            app_index=False, app_search=False, collect_metrics=False,
                            state_root=tmp_path, logs_path=tmp_path/'logs', output_path=tmp_path/'output')
        memory = MemoryManager(tmp_path/'memory.sqlite3')
        memory.set_options(enabled=False, episodes=False, history=False)
        controller = TaskController(settings, memory=memory)
        brain = Brain()
        controller.brain = brain
        controller.engine = Engine()
        try:
            result = await controller.run('Synthetic automation.')
            assert result.status == 'SUCCESS'
            assert brain.count == result.llm_calls == 3
            assert [call['brain'] for call in result.provider_calls] == ['host', 'host', 'app']
            assert [call['brain'] for call in result.prompt_calls] == ['host', 'app']
            assert result.prompt_calls[0]['attempts'] == 2
            assert result.token_usage == {'status': 'partial', 'calls_total': 3,
                'calls_with_input': 2, 'calls_with_output': 2, 'input_reported': 10, 'output_reported': 3}
            assert result.input_tokens is result.output_tokens is result.total_tokens is None
        finally:
            await controller.close()
    asyncio.run(scenario())

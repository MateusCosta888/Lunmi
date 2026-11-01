"""Opt-in execution regressions, without a desktop or provider."""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from app.core.automatic import parse_decision, validate_write_document
from app.settings import Settings


@contextlib.contextmanager
def runtime_module() -> Iterator[Any]:
    import sys
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'vendor' / 'UFO'))
    previous = os.getcwd()
    os.chdir(root / 'vendor' / 'UFO')
    try:
        import app.computer.ufo_runtime as module
        yield module
    finally:
        os.chdir(previous)


def test_execution_options_default_on_after_the_real_matrix() -> None:
    assert Settings().computer_write_macro is True
    assert Settings().computer_direct_app is True


@pytest.mark.parametrize('args', [
    {'app': 'cmd', 'text': 'x', 'mode': 'new'},
    {'app': 'notepad', 'text': 'x', 'mode': 'replace'},
    {'app': 'notepad', 'text': '', 'mode': 'new'},
    {'app': 'notepad', 'text': 'x' * 4001, 'mode': 'new'},
    {'app': 'notepad', 'text': '\0', 'mode': 'new'},
    {'app': 'notepad', 'text': 'x', 'mode': 'new', 'save': True},
])
def test_write_validation_rejects_unsupported_requests(args: dict) -> None:
    with pytest.raises(ValueError):
        validate_write_document(args)


def test_decision_preserves_literal_unicode_content() -> None:
    import json
    args = {'app': 'notepad', 'text': 'pão\nfeijão\tmar', 'mode': 'append'}
    _, _, operation = parse_decision(json.dumps({'kind': 'tool', 'operation': {
        'name': 'write_document', 'args': args}, 'finish': True}))
    assert operation is not None and operation['args'] == args


def test_direct_app_entry_follows_any_verified_launch_but_not_settings() -> None:
    """01/11: the literal-only scope never fired in the matrix; the HostAgent cost ~8 s and
    ~28k chars per task only to select the window the launch had already verified."""
    with runtime_module() as module:
        eligible = module.UFORuntime.direct_app_eligible
        for target in ('notepad.exe', 'calc.exe', 'msedge.exe'):
            assert eligible('abre e faz algo', {'name': 'launch_application', 'args': {'target': target}})
        assert not eligible('abre o som', {'name': 'launch_application', 'args': {'target': 'ms-settings:sound'}})
        assert not eligible('guarda', {'name': 'filesystem', 'args': {'operation': 'read'}})
        assert not eligible('guarda', None)


@pytest.mark.parametrize('mode,failure', [('new', ''), ('append', ''), ('new', 'launch'),
    ('append', 'ambiguous'), ('new', 'write'), ('append', 'write'), ('new', 'not_blank')])
def test_macro_runs_through_the_real_dispatcher_and_never_replays_partial_effects(
        monkeypatch, mode: str, failure: str) -> None:
    """01/11: the old version replaced the dispatcher with a fake, so it missed that every
    real macro failed on `round.agent` (there is no UFO round). Only the desktop is fake now."""
    with runtime_module() as module:
        runtime = module.UFORuntime.__new__(module.UFORuntime)
        calls: list[tuple[str, str, dict]] = []
        window = SimpleNamespace(handle=10, is_visible=lambda: True, process_id=lambda: 100,
                                 element_info=SimpleNamespace(name='Notepad'))
        runtime.state = SimpleNamespace(last_app_windows={'1': window}, selected_app_window=None)
        if failure == 'ambiguous':
            runtime.state.last_app_windows['2'] = window
        runtime.config = SimpleNamespace(system=SimpleNamespace(max_step=20))
        runtime.tools = SimpleNamespace(cancelled=False, evidence=[], pending_ui=False,
            uia=SimpleNamespace(references={}, invalidate_tree=lambda: None,
                resolve=lambda **kw: {'status': 'unique', 'candidates': [{'id': 'editor', 'name': 'Text editor'}]},
                text=lambda *a: {'readable': True, 'truncated': False,
                                  'text': 'existing' if failure == 'not_blank' else ''}))
        runtime.round, runtime.macro_computer = None, ''
        runtime.task_id, runtime.worker_instance, runtime.allow_actions = 'task', 'worker', True
        runtime.operation_events = False
        runtime.batches = runtime.max_batch_actions = runtime.multi_action_batches = 0
        runtime.actions = runtime.actions_executed = 0
        runtime.verification_ms = runtime.execution_ms = runtime.observation_ms = 0.0
        runtime.approval_mode, runtime.approvals = 'manual', []
        runtime._fast_result = lambda *args: {'evidence': args[4], 'message': args[1]}

        async def exchange(event):
            return {}

        async def check_risk(command):
            return None

        class Desktop:
            def __init__(self, kind):
                self.kind = kind

            def command2tool(self, command):
                return SimpleNamespace(namespace='fixture', command=command)

            async def run_actions(self, tools):
                command = tools[0].command
                name = command.tool_name
                calls.append((self.kind, name, command.parameters))
                if name == 'launch_application':
                    data = {'verified': failure != 'launch', 'blank_editor': True, 'handle': 10}
                elif name == 'set_edit_text':
                    data = ({'verified': False, 'executed': True} if failure == 'write'
                            else {'verified': True, 'comparison': 'exact_content'})
                elif name == 'select_application_window':
                    runtime.state.selected_app_window = window
                    data = {'id': '1'}
                else:
                    data = {'windows': 1}
                if name == 'set_edit_text':
                    runtime.tools.evidence.append({'operation': name, **data})
                return [SimpleNamespace(is_error=False, data=data)]

        async def computer(kind):
            return Desktop(kind)

        runtime.exchange, runtime.check_risk, runtime.computer = exchange, check_risk, computer
        monkeypatch.setattr(module, 'psutil_name', lambda w: 'notepad.exe')
        monkeypatch.setattr(module, 'assistant_process_ids', lambda: set())
        result = asyncio.run(runtime._write_document({'app': 'notepad', 'text': 'pão\n', 'mode': mode}))
        assert result['status'] == ('SUCCESS' if not failure else 'NOT_VERIFIED'), result['message']
        names = [name for _, name, _ in calls]
        assert names.count('launch_application') == (1 if mode == 'new' else 0)
        assert names.count('set_edit_text') <= 1
        assert not any(name in {'keyboard_input', 'filesystem'} for name in names)
        # Writing runs on the AppAgent executor, opening and selecting on the Host's.
        assert all(kind == ('AppAgent' if name == 'set_edit_text' else 'HostAgent') for kind, name, _ in calls)
        if not failure:
            assert calls[-1][1:] == ('set_edit_text', {'id': 'editor', 'name': 'Text editor',
                                                       'text': 'pão\n', 'clear_current_text': mode == 'new'})


def test_disabled_macro_rejected_before_reset_or_dispatch(monkeypatch) -> None:
    monkeypatch.setenv('COMPUTER_WRITE_MACRO', '0')
    with runtime_module() as module:
        runtime = module.UFORuntime.__new__(module.UFORuntime)
        with pytest.raises(ValueError, match='ativada'):
            asyncio.run(runtime.run('write', 20, initial_operation={'name': 'write_document',
                'args': {'app': 'notepad', 'text': 'x', 'mode': 'new'}}))


@pytest.mark.parametrize('fail', [False, True])
def test_direct_entry_preserves_host_fallback_after_construction_failure(monkeypatch, fail: bool) -> None:
    with runtime_module() as module:
        from ufo.module.context import Context
        runtime = module.UFORuntime.__new__(module.UFORuntime)
        host = SimpleNamespace(processor='original-processor', appagent_dict={}, _active_appagent=None)
        runtime.round = SimpleNamespace(agent=host, state='original-state')
        runtime.tools = SimpleNamespace(pending_ui=False)
        app = SimpleNamespace(set_state=lambda s: None)

        async def provision(**kw):
            return None

        async def bind(**kw):
            return {'root_name': 'notepad', 'window_info': {'handle': 10, 'process_id': 100,
                                                         'process_name': 'notepad.exe'}}

        def create(context):
            host.appagent_dict['new'] = app
            host._active_appagent = app
            if fail:
                raise RuntimeError('construction failed')
            return app

        host.create_subagent = create
        host.context_provision = provision
        runtime.bind_selected_window = bind
        context = Context()
        context.set(module.ContextNames.SUBTASK, 'original task')
        monkeypatch.setattr(module, 'window_record', lambda h: {'process': 'notepad.exe', 'pid': 100})
        monkeypatch.setattr(module, 'UIAHostProcessor', lambda **kw: 'new-processor')
        assert asyncio.run(runtime._enter_app_directly(host, context, {'verified': True,
            'blank_editor': True, 'handle': 10, 'pid': 100}, 'literal task')) is not fail
        if fail:
            assert context.get(module.ContextNames.SUBTASK) == 'original task'
            assert runtime.round.agent is host and runtime.round.state == 'original-state'
            assert host.processor == 'original-processor' and host.appagent_dict == {}
            assert host._active_appagent is None
            assert host._context_provision_executed is False
        else:
            assert runtime.round.agent is app and runtime.tools.pending_ui


def test_direct_entry_refuses_changed_window_before_binding(monkeypatch) -> None:
    with runtime_module() as module:
        from ufo.module.context import Context
        runtime = module.UFORuntime.__new__(module.UFORuntime)
        host = SimpleNamespace(processor=None, appagent_dict={})
        runtime.round = SimpleNamespace(agent=host, state='host')
        monkeypatch.setattr(module, 'window_record', lambda h: {'process': 'notepad.exe', 'pid': 101})
        assert asyncio.run(runtime._enter_app_directly(host, Context(), {'verified': True,
            'blank_editor': True, 'handle': 10, 'pid': 100}, 'task')) is False

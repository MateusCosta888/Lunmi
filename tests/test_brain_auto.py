from __future__ import annotations

import asyncio
import json
import os

import pytest

from app.brain.contracts import BrainError, Capabilities
from app.core.automatic import allows_actions, parse_decision
from app.core.controller import TaskController
from app.llm import Completion
from app.memory import MemoryManager
from app.settings import Settings


@pytest.mark.parametrize('instruction,expected', [
    ('Como posso apagar uma pasta?', False), ('Explica como abrir o Spotify.', False),
    ('Qual é o conteúdo deste ficheiro?', False), ('Abre aquele programa.', True),
    ('Abre o Spotify.', True), ('Apaga a pasta Testes.', True),
    ('Podes explicar como apagar uma pasta?', False), ('Podes criar um ficheiro?', True),
    ('Por favor, cria um ficheiro.', True), ('Não execute nada; apaga a pasta.', False),
    ('"Apaga a pasta" é um exemplo.', False), ('Analisa o anexo que diz apaga.', False),
    ('Write an explanation of how to delete a folder.', False),
    ('Faz uma explicação sobre como apagar uma pasta.', False),
    ('Escreve um exemplo de código Python.', False),
    ('Escreve este texto no Bloco de Notas.', True),
    ('Write an explanation of how to delete a file.', False),
    ('Faz uma explicação sobre como apagar um arquivo.', False),
    ('Cria um ficheiro com uma explicação sobre pastas.', True),
    ('Create an explanation of how to delete a folder.', False),
    ('Create an example of deleting a folder.', False),
    ('Create an example of how to delete a file.', False),
    ('Create a file.', True),
    ('Create a file with an explanation about folders.', True),
    ('Cria uma pasta.', True),
    ('Create a folder named Testes.', True),
    ('Create a directory named Testes.', True),
])
def test_action_authorization_comes_only_from_direct_user_request(instruction, expected):
    assert allows_actions(instruction) is expected


@pytest.mark.parametrize('raw', [
    '{"kind":"tool","operation":{"name":"shell","args":{"command":"x"}}}',
    '{"kind":"tool","operation":{"name":"filesystem","args":{"operation":"delete","path":"x","unknown":1}}}',
    '{"kind":"answer","message":"hello","operation":{}}',
    '{"kind":"answer","message":"hello","kind":"tool"}',
    '{"kind":"tool","operation":{"name":"filesystem","args":{"operation":"read","path":"x"}},"finish":"yes"}',
    '{"kind":"tool","operation":',
])
def test_auto_protocol_is_fail_closed(raw):
    with pytest.raises(BrainError):
        parse_decision(raw)


class FakeBrain:
    capabilities = Capabilities.of()

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        return Completion(json.dumps(self.answer), 10, 3)

    async def close(self):
        pass


class FakeEngine:
    process = None
    worker_ready = False
    starts = 0
    host_pids = set()

    def __init__(self, fail=False):
        self.fail = fail
        self.requests = []

    async def start(self):
        self.starts += 1
        self.worker_ready = True

    async def stop(self):
        self.worker_ready = False

    async def run_task(self, instruction, completion, confirm, **kwargs):
        self.requests.append((instruction, kwargs))
        callback = kwargs['operation_callback']
        callback('operation.started', {'operation_id': 'task.worker.1', 'state': 'started'})
        if self.fail:
            raise TimeoutError()
        callback('operation.finished', {'operation_id': 'task.worker.1', 'state': 'completed_verified', 'verified': True})
        return {'status': 'SUCCESS', 'message': 'Ficheiro criado e verificado.', 'actions': 1,
                'actions_executed': 1, 'evidence': [{'operation': 'create_file', 'verified': True}], 'steps': 1}


def controller(tmp_path, answer, *, fail=False, real_engine=False):
    s = Settings(provider='custom', model='fixture', api_key='synthetic',
                 base_url='http://127.0.0.1:1/v1', prewarm=False, app_index=False,
                 app_search=False, collect_metrics=False, logs_path=tmp_path/'logs',
                 output_path=tmp_path/'output', state_root=tmp_path/'state', log_instruction=True)
    c = TaskController(s, memory=MemoryManager(tmp_path/'memory.sqlite3'))
    c.brain = FakeBrain(answer)
    if not real_engine:
        c.engine = FakeEngine(fail)
    return c


def test_auto_answer_one_call_no_worker_and_ambiguous_request_clarifies(tmp_path):
    async def scenario():
        c = controller(tmp_path, {'kind': 'answer', 'action': None, 'status': 'FINISH',
                                  'message': ['Qual é o programa que queres abrir?']})
        try:
            r = await c.run('Abre aquele programa.', automatic=True)
            assert r.status == 'SUCCESS' and r.scope == 'conversation'
            assert r.llm_calls == 1 and len(c.brain.calls) == 1
            assert c.engine.starts == 0
        finally:
            await c.close()
    asyncio.run(scenario())


def test_question_cannot_become_mutation_even_when_model_asks(tmp_path):
    async def scenario():
        c = controller(tmp_path, {'kind': 'tool', 'operation': {'name': 'filesystem',
            'args': {'operation': 'delete', 'path': 'Documents/Testes'}}, 'finish': True})
        try:
            r = await c.run('Como posso apagar uma pasta?', automatic=True)
            assert r.status == 'FAILED' and c.engine.starts == 0
            assert c.engine.requests == [] and r.llm_calls == 1
        finally:
            await c.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fail', [False, True])
def test_initial_operation_is_dispatched_once_and_timeout_stays_uncertain(tmp_path, fail):
    async def scenario():
        c = controller(tmp_path, {'kind': 'tool', 'operation': {'name': 'filesystem',
            'args': {'operation': 'create_file', 'path': str(tmp_path/'result.txt'), 'text': 'test'}},
            'finish': True}, fail=fail)
        try:
            r = await c.run('Cria um ficheiro de ensaio.', automatic=True, task_id='tsk-fixture')
            assert len(c.engine.requests) == 1 and len(c.brain.calls) == 1
            kwargs = c.engine.requests[0][1]
            assert kwargs['initial_operation']['name'] == 'filesystem'
            assert kwargs['task_id'] == 'tsk-fixture' and kwargs['allow_actions']
            assert r.operation_states[-1]['state'] == ('uncertain' if fail else 'completed_verified')
            assert r.status == ('FAILED' if fail else 'SUCCESS')
        finally:
            await c.close()
    asyncio.run(scenario())


def test_attachment_turn_never_becomes_ram_history_episode_or_optin_log(tmp_path):
    async def scenario():
        secret = 'PRIVATE_ATTACHMENT_SENTINEL'
        c = controller(tmp_path, {'kind': 'answer', 'action': None, 'status': 'FINISH', 'message': [secret]})
        c.memory.set_options(episodes=True, history=True)
        try:
            r = await c.run('Resume o anexo.', automatic=True,
                           attachments=[{'name': 'test.txt', 'text': secret}])
            assert r.status == 'SUCCESS' and secret in r.message
            assert c.turns.pairs(c.session_id) == []
            assert c.memory.profile_snapshot().text == ''
            assert c.memory.list() == []
            assert secret not in (tmp_path/'logs/tasks.jsonl').read_text(encoding='utf-8')
        finally:
            await c.close()
    asyncio.run(scenario())


@pytest.mark.skipif(os.name != 'nt', reason='Real Windows worker')
def test_real_worker_bootstrap_writes_attachment_exactly_without_second_inference(tmp_path):
    async def scenario():
        target = tmp_path/'attached-proof.txt'
        text = 'Dados de ensaio\nacentos e Lua 🌙\n'
        c = controller(tmp_path, {'kind': 'tool', 'operation': {'name': 'filesystem',
            'args': {'operation': 'create_file', 'path': str(target), 'attachment_ref': 'att-0'}},
            'finish': True}, real_engine=True)
        try:
            r = await c.run(f'Cria o ficheiro {target} com o conteúdo exato do anexo.', automatic=True,
                           task_id='tsk-real-worker', attachments=[{'name': 'source.txt', 'text': text}])
            assert r.status == 'SUCCESS', (r.message, r.failure_kind)
            assert target.read_bytes() == text.encode('utf-8')
            assert r.llm_calls == 1 and len(c.brain.calls) == 1
            assert r.operation_states[-1]['state'] == 'completed_verified'
            assert c.turns.pairs(c.session_id) == []
        finally:
            await c.close()
        assert c.engine.process is None
    asyncio.run(scenario())

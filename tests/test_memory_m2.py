from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core.controller import TaskController, TaskResult
from app.memory import MemoryItem, MemoryManager, MemoryPolicyError
from app.memory.commands import command
from app.memory.manager import CONTEXT_MAX_CHARS
from test_memory_controller import FixtureBrain, settings


def result(instruction: str = 'Projeto Python fixture', **changes) -> TaskResult:
    return TaskResult(instruction=instruction, status='SUCCESS', evidence=[{'verified': True}],
                      plan_source='llm', llm_calls=1, **changes)


def test_episode_structured_searchable_dedup_and_failure(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / 'db')
    task = result()
    first_task = task
    task.message = 'PRIVATE raw result deliberately excluded'
    task.evidence = [{'verified': True, 'control_text': 'PRIVATE tool body'}]
    memory.commit_episode(task)
    memory.commit_episode(task)
    assert len(memory.list()) == 1
    item = memory.recall('Python', kinds=['episode'])[0]
    assert item.metadata['outcome'] == 'SUCCESS' and item.metadata['paths_are_hypotheses'] is True
    assert 'PRIVATE' not in json.dumps(memory.export())
    task = result('Projeto Python incompleto')
    task.status = 'NOT_VERIFIED'
    memory.commit_episode(task)
    assert memory.recall('incompleto')[0].metadata['outcome'] == 'NOT_VERIFIED'
    task = result('Sucesso sem evidência')
    task.evidence = []
    memory.commit_episode(task)
    assert memory.recall('evidencia')[0].metadata['outcome'] == 'NOT_VERIFIED'
    for scope in ['conversation', 'memory_command']:
        memory.commit_episode(TaskResult(scope=scope, instruction='Lembra que prefiro curto'))
    assert len(memory.list()) == 3
    assert memory._store.db.execute('SELECT COUNT(*) FROM episodes_fts WHERE episodes_fts MATCH ?', ('"Python"',)).fetchone()[0] == 2
    memory.delete(item.id)
    memory.commit_episode(first_task)
    assert not any(i.id == item.id for i in memory.list())


@pytest.mark.parametrize('instruction', ['senha: fixture', 'Mensagem recebida no Discord: fixture',
                                       'tool_output: fixture', 'x' * 401])
def test_episode_policy_never_truncates_away_secrets(tmp_path: Path, instruction: str) -> None:
    memory = MemoryManager(tmp_path / 'db')
    with pytest.raises(MemoryPolicyError):
        memory.commit_episode(result(instruction))
    assert memory.list() == []


def test_proposals_consent_reject_dedup_and_invalid_accept(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / 'db')
    proposal = MemoryItem(text='Prefiro respostas detalhadas', kind='preference', source='system',
                          metadata={'conflict_key': 'response_length'})
    proposal_id = memory.propose(proposal)
    assert memory.propose(proposal) == proposal_id
    assert memory.recall('detalhadas') == []
    assert memory.proposals()[0].id == proposal_id
    memory_id = memory.accept_proposal(proposal_id)
    assert memory.recall('detalhadas')[0].source == 'user_confirmed'
    assert memory.recall('detalhadas')[0].id == memory_id and memory.proposals() == []
    with pytest.raises(KeyError):
        memory.accept_proposal(proposal_id)
    rejected = memory.propose(MemoryItem(text='Uso projeto Alpha', source='system'))
    memory.reject_proposal(rejected)
    assert memory.proposals() == [] and memory.recall('Alpha') == []
    with memory._store.transaction() as db:
        db.execute("INSERT INTO proposals VALUES('bad',?,'pending',NULL)",
                   (json.dumps({'text': 'senha: fixture'}),))
    with pytest.raises(MemoryPolicyError):
        memory.accept_proposal('bad')
    assert len(memory.list()) == 1


def test_explicit_conflict_supersede_and_invalidation(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / 'db')
    command('Lembra que prefiro respostas curtas.', memory)
    old = memory.list()[0]
    command('Lembra que prefiro respostas longas.', memory)
    assert len(memory.list()) == 1 and 'longas' in memory.list()[0].text
    assert memory.list()[0].supersedes_id == old.id
    assert memory.list(status='superseded')[0].id == old.id
    assert memory.recall('curtas') == [] and 'curtas' not in memory.context('explicar', 'fixture')[0]
    command('Lembra que prefiro usar editor Alpha.', memory)
    assert command('Não quero mais usar editor Alpha.', memory) == 'Preferência invalidada.'
    assert memory.recall('Alpha') == []
    assert command('Não quero mais usar algo ausente.', memory).startswith('Seleção')


def test_history_opt_in_safe_summaries_no_profile_resurrection(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / 'db')
    memory.record_conversation('session', 'Explique assunto fixture', 'Resposta fixture')
    assert memory._store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0
    memory.set_options(history=True)
    for n in range(6):
        memory.record_conversation('session', f'Explique tópico Python exemplo {n}', 'Resposta didática')
    assert memory._store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 12
    assert memory.recall('Python', kinds=['conversation'])
    context, _ = memory.context('Continua Python de ontem', 'different-session')
    assert 'Conversa' in context and len(context) <= CONTEXT_MAX_CHARS
    for text in ['lembra que prefiro respostas curtas', 'esquece item', 'prefiro azul']:
        memory.record_conversation('session', text, 'Resposta fixture')
    assert memory._store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 12
    for text in ['senha: fixture', 'Mensagem recebida no Discord: fixture', 'Transcrição: fixture']:
        with pytest.raises(MemoryPolicyError):
            memory.record_conversation('session', text, 'Resposta fixture')
    profile = memory.remember(MemoryItem(text='Prefiro respostas curtas'))
    memory.delete(profile)
    assert memory._store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 12
    assert 'curtas' not in memory.context('Continua de ontem', 'session')[0]
    summary_id = memory.recall('Python', kinds=['conversation'])[0].id
    memory.delete(summary_id)
    memory.record_conversation('session', 'Explique tópico Python novo', 'Resposta didática')
    assert memory.recall('Python', kinds=['conversation']) == []
    memory.wipe()
    for table in ['messages', 'conversations', 'conversation_summaries', 'memories', 'proposals', 'episodes']:
        assert memory._store.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0


def test_export_import_all_layers_and_atomic_failure(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / 'one')
    memory.remember(MemoryItem(text='Profile seguro'))
    memory.commit_episode(result())
    memory.propose(MemoryItem(text='Proposta segura', source='system'))
    memory.set_options(history=True)
    for n in range(6):
        memory.record_conversation('fixture', f'Python tópico {n}', 'Resposta segura')
    payload = memory.export()
    other = MemoryManager(tmp_path / 'two')
    assert other.import_data(payload) == 3
    assert len(other.proposals()) == 1
    assert other._store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 12
    assert other.recall('Python', kinds=['episode'])
    assert other.recall('Python', kinds=['conversation'])
    before = other.export()
    payload['messages'][0]['content'] = 'password: fixture'
    with pytest.raises(MemoryPolicyError):
        other.import_data(payload)
    assert other.export() == before


def test_history_edit_and_import_cannot_become_profile_copy(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / 'db')
    memory.set_options(history=True)
    for n in range(6):
        memory.record_conversation('fixture', f'Assunto Python {n}', 'Resposta segura')
    summary = memory.recall('Python', kinds=['conversation'])[0]
    with pytest.raises(MemoryPolicyError):
        memory.update(summary.id, 'Prefiro respostas curtas')
    payload = memory.export()
    payload['memories'][0]['text'] = 'Lembra que prefiro respostas curtas'
    with pytest.raises(MemoryPolicyError):
        memory.import_data(payload)
    assert 'curtas' not in memory.context('Continua Python', 'fixture')[0]


def test_controller_proposals_no_extra_llm_no_autoaccept(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = TaskController(settings(tmp_path))
        controller.brain = FixtureBrain(json.dumps({'status': 'FINISH', 'message': ['Olá'],
            'memory_proposals': [{'kind': 'fact', 'text': 'uso editor Alpha'}]}))
        response = await controller.run('No meu trabalho uso editor Alpha', conversation=True)
        assert response.status == 'SUCCESS' and response.llm_calls == 1
        # Aprendizagem automática: entra no perfil, sem proposta pendente nem aceite humano.
        assert [item.text for item in controller.memory.list()] == ['uso editor Alpha']
        assert controller.memory.proposals() == []
        before = controller.memory.profile_snapshot()
        controller.brain.reply = json.dumps({'message': 'Olá', 'memory_proposals': [{'kind': 'fact', 'text': 'inventada'}]})
        await controller.run('Assunto sem esse fato', conversation=True)
        assert controller.memory.profile_snapshot().text == before.text
        controller.brain.reply = json.dumps({'message': 'Olá', 'memory_proposals': [{'kind': 'fact', 'text': 'senha: fixture'}]})
        response = await controller.run('Minha senha: fixture', conversation=True)
        assert response.status == 'SUCCESS' and response.memory_error
        assert controller.memory.profile_snapshot().text == before.text
        await controller.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('instruction', [
    'Mensagem recebida no Discord: Ana usa editor Alpha',
    'Minha senha é valor-fictício. Ana usa editor Alpha',
    'Ana usa editor Alpha. Meu token de acesso é valor-fictício',
])
def test_candidate_origin_validated_before_substring_extraction(tmp_path: Path, instruction: str) -> None:
    async def scenario() -> None:
        controller = TaskController(settings(tmp_path))
        controller.memory.set_options(history=True)
        controller.brain = FixtureBrain(json.dumps({'message': 'Resposta disponível.', 'memory_proposals': [
            {'kind': 'fact', 'text': 'Ana usa editor Alpha'}]}))
        response = await controller.run(instruction, conversation=True)
        assert response.status == 'SUCCESS' and response.message == 'Resposta disponível.'
        assert response.memory_error and not controller.memory.proposals()
        assert controller.memory._store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0
        assert controller.memory.list() == []
        await controller.close()
    asyncio.run(scenario())


def test_proposals_toggle_does_not_disable_safe_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = TaskController(settings(tmp_path))
        controller.memory.set_options(proposals=False, history=True)
        controller.brain = FixtureBrain(json.dumps({'message': 'Resposta disponível.', 'memory_proposals': [
            {'kind': 'fact', 'text': 'uso editor Alpha'}]}))
        response = await controller.run('No trabalho uso editor Alpha', conversation=True)
        assert response.status == 'SUCCESS' and not response.memory_error
        assert controller.memory.proposals() == []
        assert controller.memory._store.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 2
        await controller.close()
    asyncio.run(scenario())


def test_referential_episode_priority_with_long_profiles_and_working(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / 'db')
    for n in range(5):
        memory.remember(MemoryItem(kind='preference', text=f'Preferência longa fixture {n} ' + 'x' * 740))
    memory.commit_episode(result('Projeto Python Alpha'))
    memory.working.set({'objective': 'Continua Projeto Python Alpha de ontem', 'task_id': 'fixture'}, 'session')
    context, count = memory.context('Continua Projeto Python Alpha de ontem', 'session')
    assert 'Resultado registrado: SUCCESS' in context
    assert 'Working:' in context and 'fixture' in context
    assert context.index('"kind": "episode"') < context.index('"kind": "preference"')
    assert len(context) <= CONTEXT_MAX_CHARS and count <= 8


def test_episode_store_error_never_changes_verified_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        controller = TaskController(settings(tmp_path))
        controller.brain = FixtureBrain()
        controller.engine.start = AsyncMock()
        async def run_task(*args, **kwargs):
            return {'status': 'SUCCESS', 'evidence': [{'verified': True}], 'message': 'verified fixture'}
        controller.engine.run_task = run_task
        def fail(result):
            raise OSError('database fixture failure')
        monkeypatch.setattr(controller.memory, 'commit_episode', fail)
        response = await controller.run('Projeto fixture')
        assert response.status == 'SUCCESS' and response.memory_error and response.evidence
        assert response.failure_kind == ''
        await controller.close()
    asyncio.run(scenario())


def test_continuity_uses_new_disk_observation_not_memory(tmp_path: Path) -> None:
    async def scenario() -> None:
        target = tmp_path / 'project-fixture.txt'
        target.write_text('old', encoding='utf-8')
        memory = MemoryManager(tmp_path / 'db')
        memory.commit_episode(result('Projeto Python em project-fixture.txt'))
        target.unlink()
        controller = TaskController(settings(tmp_path), memory=memory)
        controller.brain = FixtureBrain()
        controller.engine.start = AsyncMock()
        async def run_task(instruction, completion, confirm, **kwargs):
            # Executor fixture independently reads filesystem after recall; missing path cannot succeed.
            await completion([{'role': 'system', 'content': 'Host fixture'}], 'HostAgent')
            assert not target.exists()
            return {'status': 'NOT_VERIFIED', 'evidence': [], 'message': 'path absent'}
        controller.engine.run_task = run_task
        response = await controller.run('Continua o projeto Python de ontem')
        prompt = json.dumps(controller.brain.calls)
        assert 'project-fixture.txt' in prompt and 'freshly observe disk/UI' in prompt
        assert response.status == 'NOT_VERIFIED' and not target.exists()
        # Deletion mid-task must invalidate next Host recall, not a captured context cache.
        episode_id = memory.recall('Python', kinds=['episode'])[0].id
        memory.delete(episode_id)
        assert all(item.id != episode_id for item in memory.recall('Python', kinds=['episode']))
        await controller.close()
    asyncio.run(scenario())

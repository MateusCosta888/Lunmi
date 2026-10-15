from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pytest

from app.memory import MemoryItem, MemoryManager, MemoryPolicyError
from app.memory.manager import CONTEXT_MAX_CHARS


@pytest.fixture
def memory(tmp_path: Path) -> MemoryManager:
    return MemoryManager(tmp_path / 'memory.db')


def test_schema_restart_fts_and_working(tmp_path: Path) -> None:
    path = tmp_path / 'db.sqlite3'
    memory = MemoryManager(path)
    item_id = memory.remember(MemoryItem(kind='preference', text='Prefiro explicações concisas em português.'))
    memory.working.set({'objective': 'example'}, 'session')
    assert memory._store.db.execute('PRAGMA user_version').fetchone()[0] == 1
    assert memory.recall('explicacoes?!')[0].id == item_id
    assert memory.recall('" OR NOT : * ;') == []
    memory.close()
    memory = MemoryManager(path)
    assert memory.recall('portugues')[0].id == item_id
    assert memory.working.get('session') == {}
    assert memory.options() == {'enabled': True, 'episodes': True, 'proposals': True, 'history': False}


@pytest.mark.parametrize('text', [
    'Minha senha: fictional-placeholder', 'API_KEY=unit-placeholder', 'Authorization: Bearer fake',
    'Meu token de acesso é valor-ficticio', 'Meu token de atualização é valor-ficticio',
    'sk-testplaceholder12345678', 'ghp_fictionalfixture12345', 'token=fixture',
    'Mensagem recebida no Discord: exemplo privado', 'João disse: exemplo de terceiro',
    'email: conteúdo privado de teste', 'tool_output: dump fictício', '```file dump```', 'x' * 801,
])
def test_all_profile_routes_reject_before_write(memory: MemoryManager, text: str) -> None:
    good_id = memory.remember(MemoryItem(text='Prefiro exemplo curto.'))
    baseline = memory.export()
    for write in (
        lambda: memory.remember(MemoryItem(text=text)),
        lambda: memory.update(good_id, text),
        lambda: memory.propose(MemoryItem(text=text, source='system')),
        lambda: memory.import_data({**baseline, 'memories': [asdict(MemoryItem(text=text))]}),
    ):
        with pytest.raises(MemoryPolicyError) as error:
            write()
        assert text not in str(error.value)
        assert memory.export() == baseline


def test_metadata_cannot_smuggle_content(memory: MemoryManager) -> None:
    for values in [{'unknown': 'dump'}, {'conflict_key': 'password'}, {'apps': ['senha=fixture']},
                   {'llm_calls': True}, {'expires_at': 'yesterday'}, {'instruction': {'secret': 'x'}}]:
        with pytest.raises(MemoryPolicyError):
            memory.remember(MemoryItem(text='safe fixture', metadata=values))


def test_edit_delete_fts_tombstone_import_no_resurrection(memory: MemoryManager) -> None:
    item_id = memory.remember(MemoryItem(text='Uso Python em projetos.'))
    old = memory.export()
    memory.update(item_id, 'Uso Rust em projetos.')
    assert memory.recall('Python') == []
    assert memory.recall('Rust')[0].id == item_id
    memory.delete(item_id)
    assert memory.recall('Rust') == []
    assert memory.import_data(old) == 0
    assert memory.list() == []
    assert memory.list(status='deleted')[0].text == '[deleted]'


def test_import_atomic_and_versioned(memory: MemoryManager) -> None:
    memory.remember(MemoryItem(text='Original segura.'))
    payload = memory.export()
    bad = copy.deepcopy(payload)
    bad['memories'].append(asdict(MemoryItem(text='nova segura')))
    bad['memories'].append(asdict(MemoryItem(text='senha: ficticia')))
    with pytest.raises(MemoryPolicyError):
        memory.import_data(bad)
    assert memory.export() == payload
    for change in [{'version': 99}, {'sql': 'DROP TABLE memories'}, {'options': {'enabled': 'true'}}]:
        with pytest.raises(MemoryPolicyError):
            memory.import_data({**payload, **change})
    other = MemoryManager(Path(memory._store.db.execute('PRAGMA database_list').fetchone()[2] + '.other'))
    assert other.import_data(json.loads(json.dumps(payload))) == 1
    assert other.recall('Original')[0].text == 'Original segura.'


def test_bounds_options_ttl_and_wipe(memory: MemoryManager) -> None:
    for n in range(15):
        memory.remember(MemoryItem(text=f'Preferência segura {n} ' + 'x' * 300))
    context, count = memory.context('explicar', 'fixture')
    assert len(context) <= CONTEXT_MAX_CHARS and count <= 5
    assert 'hypothesis' in context and 'approvals' in context
    memory.remember(MemoryItem(text='Hoje estou no projeto temporário', kind='preference'))
    assert memory.recall('temporario')[0].kind == 'fact'
    memory.remember(MemoryItem(text='Expirada segura', metadata={'expires_at': '2000-01-01T00:00:00Z'}))
    assert memory.recall('Expirada') == []
    for limit in [-1, 101, True]:
        with pytest.raises(MemoryPolicyError):
            memory.recall('', limit=limit)
    memory.set_options(enabled=False)
    assert memory.recall('') == [] and memory.context('query', 'fixture') == ('', 0)
    with pytest.raises(MemoryPolicyError):
        memory.set_options(history='true')
    memory.wipe()
    assert memory.export()['memories'] == [] and memory.proposals() == []
    assert memory.options()['enabled'] and not memory.options()['history']


def test_concurrent_thread_access_and_close(memory: MemoryManager) -> None:
    def operation(n: int) -> None:
        item_id = memory.remember(MemoryItem(text=f'Concurrent fixture {n}'))
        assert memory._store.get(item_id).text == f'Concurrent fixture {n}'
        memory.recall('Concurrent')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(operation, range(30)))
    assert len(memory.list()) == 30
    memory.close()
    memory.close()
    with pytest.raises(RuntimeError, match='fechada'):
        memory.list()


def test_working_copy_not_disk(memory: MemoryManager) -> None:
    values = {'objective': 'Fixture safe'}
    memory.working.set(values, 'a')
    values['objective'] = 'changed'
    returned = memory.working.get('a')
    returned['objective'] = 'changed again'
    assert memory.working.get('a')['objective'] == 'Fixture safe'
    assert memory.export()['memories'] == []
    memory.working.clear('a')
    assert memory.working.get('a') == {}

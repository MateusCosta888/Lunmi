from __future__ import annotations

from pathlib import Path
import threading
from contextlib import contextmanager

import pytest

from app.memory import MemoryItem, MemoryManager, MemoryPolicyError


def test_update_reapplies_temporal_rules(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / 'db')
    item_id = memory.remember(MemoryItem(kind='preference', text='Prefiro projetos pequenos'))
    item = memory.update(item_id, 'Hoje estou no projeto Alpha')
    assert item.kind == 'fact' and item.metadata['expires_at']
    assert memory.recall('Alpha')[0].metadata['expires_at']


@pytest.mark.parametrize('text', ['Pessoa desconhecida disse: exemplo privado',
                                'Alguém escreveu: exemplo de terceiro',
                                'Transcrição de outra pessoa', 'Citação: exemplo fictício'])
def test_third_party_markers_not_example_names(tmp_path: Path, text: str) -> None:
    memory = MemoryManager(tmp_path / 'db')
    with pytest.raises(MemoryPolicyError):
        memory.remember(MemoryItem(text=text))
    assert memory.list() == []


@pytest.mark.parametrize('source', ['system', 'user_confirmed'])
@pytest.mark.parametrize('text', ['Minha religião é exemplo fictício', 'Minha religiao é exemplo fictício'])
def test_sensitive_requires_actual_explicit_request(tmp_path: Path, source: str, text: str) -> None:
    memory = MemoryManager(tmp_path / 'db')
    with pytest.raises(MemoryPolicyError):
        memory.propose(MemoryItem(text=text, source=source))
    with pytest.raises(MemoryPolicyError):
        memory.remember(MemoryItem(text=text, source=source))
    assert memory.list() == []


def test_dedup_applies_explicit_supersede_first(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / 'db')
    a = memory.remember(MemoryItem(text='Preferência Alpha', kind='preference'))
    b = memory.remember(MemoryItem(text='Preferência Beta', kind='preference'))
    assert memory.remember(MemoryItem(text='Preferência Beta', kind='preference', supersedes_id=a)) == b
    assert [item.id for item in memory.list()] == [b]
    assert memory.list(status='superseded')[0].id == a
    assert memory._store.get(b).supersedes_id == a


def test_deleted_duplicate_proposal_tombstone_survives_import(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / 'db')
    a = memory.remember(MemoryItem(text='Preferência Alpha', kind='preference'))
    p = memory.propose(MemoryItem(text='Preferência Alpha', kind='preference', source='system'))
    old = memory.export()
    memory.delete(a)
    row = memory._store.db.execute('SELECT * FROM proposals WHERE id=?', (p,)).fetchone()
    assert row['state'] == 'rejected' and row['payload_json'] == '{}'
    memory.import_data(old)
    assert memory.proposals() == [] and memory.recall('Alpha') == []
    with pytest.raises(KeyError):
        memory.accept_proposal(p)


@pytest.mark.parametrize('route,flag,table', [
    ('history', 'history', 'messages'), ('episode', 'episodes', 'episodes'),
    ('proposal', 'proposals', 'proposals'), ('history', 'enabled', 'messages'),
    ('episode', 'enabled', 'episodes'), ('proposal', 'enabled', 'proposals'),
])
def test_consent_revoked_before_write_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                 route: str, flag: str, table: str) -> None:
    from app.core.controller import TaskResult
    memory = MemoryManager(tmp_path / 'db')
    memory.set_options(history=True)
    arrived = threading.Event()
    proceed = threading.Event()
    original = memory._store.transaction
    errors = []
    writer_id = []

    @contextmanager
    def paused_transaction():
        # Pause only the outer write, after preflight reads completed. Once
        # flags=False commits, resume and acquire the real existing transaction.
        if threading.get_ident() in writer_id and not memory._store.db.in_transaction:
            import inspect
            caller = inspect.currentframe().f_back.f_back.f_code.co_name
            if caller in {'record_conversation', 'commit_episode', 'propose'}:
                arrived.set()
                assert proceed.wait(5)
        with original() as db:
            yield db

    monkeypatch.setattr(memory._store, 'transaction', paused_transaction)
    def write():
        writer_id.append(threading.get_ident())
        try:
            if route == 'history':
                memory.record_conversation('fixture', 'Assunto seguro', 'Resposta segura')
            elif route == 'episode':
                memory.commit_episode(TaskResult(instruction='Projeto fixture', status='SUCCESS', evidence=[{'verified': True}]))
            else:
                memory.propose(MemoryItem(text='Proposta segura', source='system'))
        except MemoryPolicyError:
            # Disabled proposal is an explicit API refusal; no write is permitted.
            pass
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=write)
    thread.start()
    assert arrived.wait(5)
    memory.set_options(**{flag: False})
    proceed.set()
    thread.join(5)
    assert not thread.is_alive() and not errors
    assert memory._store.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0


def test_delete_profile_preserves_unrelated_history(tmp_path: Path) -> None:
    memory = MemoryManager(tmp_path / 'db')
    profile_id = memory.remember(MemoryItem(text='Prefiro respostas curtas'))
    with memory._store.transaction() as db:
        db.execute("INSERT INTO conversations VALUES('fixture','2000-01-01T00:00:00Z','Independent')")
        db.execute("INSERT INTO messages VALUES('message','fixture','user','Assunto independente','2000-01-01T00:00:00Z')")
    memory.delete(profile_id)
    assert memory._store.db.execute('SELECT content FROM messages').fetchone()[0] == 'Assunto independente'
    assert memory.recall('curtas') == []
    assert 'curtas' not in memory.context('explique', 'fixture')[0]

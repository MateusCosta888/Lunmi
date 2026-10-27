from __future__ import annotations

import asyncio
import json
import io
import sqlite3
import time
from types import SimpleNamespace

import pytest

from app.chat_history import ChatHistory, PRIVATE
from app.bridge import protocol
from app.bridge.service import BridgeService
from app.core.controller import TaskResult
from app.memory.contracts import now
from app.memory.turns import TurnWindow
from app.settings import Settings


def turn(history: ChatHistory, task: str, user: str = 'Cor favorita azul', answer: str = 'Azul.', **extra):
    cid = history.active['id']
    message = dict(conversation_id=cid, task_id=task, created_at=now(), role='user', text=user,
                   message_id=task + ':user', state='completed')
    history.accept(message, attachments=extra.pop('attachment_turn', False))
    history.complete({**message, 'role': 'assistant', 'message_id': task + ':assistant',
                      'text': answer, **extra}, eligible=True)


def test_restart_resume_temporary_options_and_no_memory_migration(tmp_path):
    path = tmp_path / '.local/chat-history.sqlite3'
    h = ChatHistory(path)
    first = h.active['id']
    turn(h, 't1', 'A' * 100)
    assert len(h.active['title']) == 80
    h.store.rename(first, 'Projeto azul')
    h.new(True)
    turn(h, 'temporary', 'Texto efémero')
    h.store.close()
    h = ChatHistory(path)
    assert h.active['id'] == first and h.active['title'] == 'Projeto azul'
    assert len(h.active['messages']) == 2
    assert h.store.context(first) == [('A' * 100, 'Azul.')]
    h.store.set_options({'startup': 'new'})
    h.store.close()
    h = ChatHistory(path)
    assert h.active['id'] != first
    assert len(h.store.list()['conversations']) == 1
    h.store.close()
    assert not (tmp_path / '.local/memory.sqlite3').exists()


def test_off_applies_only_to_future_messages_and_sources_keep_dates(tmp_path):
    h = ChatHistory(tmp_path / 'chat.sqlite3')
    sources = [{'id': 'source1', 'title': 'Documentação', 'url': 'https://python.org/',
                'snippet': 'Biblioteca', 'engines': ['original'], 'published_at': '2024-03-01',
                'date_source': 'page', 'retrieved_at': '2025-02-01T10:00:00Z',
                'read': True, 'truncated': False, 'cached': False}]
    turn(h, 'saved', sources=sources, provider='original', model='old-model')
    h.options['enabled'] = False
    turn(h, 'off', user='Não guardar esta frase')
    page = h.store.read(h.active['id'])
    assert len(page['messages']) == 2
    assert page['messages'][1]['sources'] == sources
    assert len(h.active['messages']) == 4
    h.store.close()
    h = ChatHistory(tmp_path / 'chat.sqlite3')
    assert h.active['messages'][1]['sources'] == sources
    assert h.active['messages'][1]['provider'] == 'original'
    assert h.active['messages'][1]['model'] == 'old-model'
    h.store.close()


def test_enable_recording_does_not_copy_earlier_draft_or_title(tmp_path):
    h = ChatHistory(tmp_path / 'chat.sqlite3')
    h.options['enabled'] = False
    turn(h, 'off', user='NOME_QUE_NAO_PODE_SER_GRAVADO')
    h.options['enabled'] = True
    turn(h, 'on', user='Primeira mensagem guardada')
    assert h.store.conversation(h.active['id'])['title'] == 'Primeira mensagem guardada'
    assert 'NOME_QUE_NAO' not in json.dumps(h.store.read(h.active['id']))
    h.store.close()


def test_crash_recovers_pending_without_replay(tmp_path):
    path = tmp_path / 'chat.sqlite3'
    h = ChatHistory(path)
    cid = h.active['id']
    h.accept(dict(conversation_id=cid, task_id='crash', role='user', text='Uma pergunta',
                  created_at=now(), message_id='crash:user', state='completed'), attachments=False)
    h.store.close()
    h = ChatHistory(path)
    assert h.active['messages'][0]['state'] == 'interrupted'
    assert h.active['pairs'] == [] and h.pending == {}
    assert h.store.db.execute("SELECT count(*) FROM messages WHERE state='pending'").fetchone()[0] == 0
    h.store.close()


def test_pagination_search_delete_fts_and_idempotence(tmp_path):
    h = ChatHistory(tmp_path / 'chat.sqlite3')
    first = h.active['id']
    for i in range(56):
        turn(h, f't{i}', user=f'Assunto {i}', answer='conteúdo pesquisável órbita')
    a = h.store.read(first)
    b = h.store.read(first, a['before'])
    c = h.store.read(first, b['before'])
    assert [len(page['messages']) for page in (a, b, c)] == [50, 50, 12]
    assert len({m['message_id'] for page in (a, b, c) for m in page['messages']}) == 112
    assert h.store.list('orbita')['conversations'][0]['id'] == first
    assert h.store.list('" OR NOT *')['conversations'] == []
    message = a['messages'][-1]
    h.store.append(message)
    assert h.store.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 112
    for i in range(51):
        h.new()
        turn(h, f'other{i}')
    assert len(h.store.list()['conversations']) == 50
    assert len(h.store.list(offset=50)['conversations']) == 2
    h.store.delete(first)
    assert h.store.list('órbita')['conversations'] == []
    h.store.delete(None)
    assert h.store.db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
    assert h.store.db.execute('SELECT count(*) FROM message_fts').fetchone()[0] == 0
    h.store.close()


def test_privacy_attachment_names_only_and_failed_storage(tmp_path, monkeypatch):
    path = tmp_path / 'chat.sqlite3'
    h = ChatHistory(path)
    turn(h, 'secret', user='password: private_value', answer='Nada sensível aqui')
    turn(h, 'sensitive', user='diagnóstico de saúde pessoal')
    turn(h, 'attachment', user='Resume este ficheiro', answer='EXTRACTED_SENTINEL', attachment_turn=True)
    data = json.dumps(h.store.read(h.active['id']), ensure_ascii=False)
    assert 'private_value' not in data and 'saúde pessoal' not in data and 'EXTRACTED_SENTINEL' not in data
    assert PRIVATE in data and h.store.context(h.active['id']) == []
    monkeypatch.setattr(h.store, 'append', lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError('disk full')))
    turn(h, 'disk', user='Continuar em RAM')
    assert h.error and h.active['messages'][-1]['text'] == 'Azul.'
    h.store.close()
    bad = tmp_path / 'not-directory'
    bad.write_text('x')
    h = ChatHistory(bad / 'chat.sqlite3')
    turn(h, 'usable')
    assert h.error and len(h.active['messages']) == 2


class FakeController:
    def __init__(self, settings, **kwargs):
        self.settings = settings
        self.session_id = 'controller-session'
        self.turns = TurnWindow()
        self.engine = SimpleNamespace(set_host_pids=lambda pids: None)
        self.delay = 0
        self.failure = False
        self.cancelled = False
        self.on_public = None
        self.email_pending = {'effect': 'must not survive'}
        self.email_authorization = {'effect': 'must not survive'}

    async def run(self, instruction, **kwargs):
        await asyncio.sleep(self.delay)
        if self.cancelled:
            return TaskResult(status='CANCELLED', message='Resposta cancelada.', scope='conversation')
        if self.failure:
            raise RuntimeError('PRIVATE_INTERNAL_ERROR')
        previous = self.turns.pairs(self.session_id)
        answer = 'Anterior: ' + (previous[-1][0] if previous else 'nenhum')
        if self.on_public:
            self.on_public('response.started', {'streaming': True})
            self.on_public('response.text.delta', {'text': answer})
        self.turns.add(self.session_id, instruction, answer)
        return TaskResult(status='SUCCESS', message=answer, scope='conversation')

    def cancel(self):
        self.cancelled = True

    async def close(self):
        pass


def command(service, operation, **payload):
    return service.dispatch(protocol.Envelope(1, 'request', operation, payload))


def wait_task(service):
    for _ in range(200):
        if not service._is_task_running():
            return
        time.sleep(.01)
    raise AssertionError('Task did not terminate')


def test_lazy_handshake_hydrates_history_once_without_replaying(tmp_path, monkeypatch):
    from app.bridge.server import BridgeServer
    import app.bridge.server as server_module
    class TestService(BridgeService):
        def __init__(self, **kwargs):
            super().__init__(controller_factory=FakeController, **kwargs)
    monkeypatch.setattr(server_module, 'BridgeService', TestService)
    output = io.BytesIO()
    server = BridgeServer(stdin=io.BytesIO(), stdout=output, stderr=io.BytesIO(),
                          sandbox_dir=tmp_path, enable_service=True)
    server.handle_raw_line(json.dumps({'v': 1, 'id': 'hello', 'type': 'session.hello', 'payload': {}}).encode())
    assert server._service is None
    for request in ('read1', 'read2'):
        server.handle_raw_line(json.dumps({'v': 1, 'id': request, 'type': 'chat.history.get', 'payload': {}}).encode())
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    selected = [m for m in messages if m['type'] == 'chat.history.changed']
    assert len(selected) == 1
    assert selected[0]['payload']['reason'] == 'selected'
    assert not any(m['type'].startswith(('task.', 'response.')) for m in messages)
    server._service.close()


def test_cancellation_finalizes_once_and_cannot_restore_context(tmp_path):
    events = []
    service = BridgeService(emit=lambda *event: events.append(event), sandbox_dir=tmp_path,
                            settings=Settings(), controller_factory=FakeController)
    try:
        service.controller.delay = .1
        command(service, 'chat.send', mode='converse', text='Pedido cancelável')
        command(service, 'task.cancel')
        wait_task(service)
        messages = service.history.store.read(service.history.active['id'])['messages']
        assert len(messages) == 2 and messages[-1]['state'] == 'cancelled'
        assert service.history.store.context(service.history.active['id']) == []
        assert sum(kind == 'response.cancelled' for kind, _ in events) == 1
        assert sum(kind == 'task.finished' for kind, _ in events) == 1
    finally:
        service.close()


def test_atomic_accept_failure_leaves_no_empty_saved_conversation(tmp_path, monkeypatch):
    h = ChatHistory(tmp_path / 'chat.sqlite3')
    monkeypatch.setattr(h.store, 'append', lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError('full')))
    turn(h, 'failure')
    assert h.store.list()['conversations'] == []
    assert h.store.options()['last_id'] is None
    assert h.error and h.active['saved'] is False
    h.store.close()


def test_restored_context_uses_existing_six_pair_and_character_budget(tmp_path):
    h = ChatHistory(tmp_path / 'chat.sqlite3')
    cid = h.active['id']
    for i in range(12):
        turn(h, f't{i}', user=str(i) + 'u' * 1500, answer='a' * 1500)
    window = TurnWindow()
    for user, answer in h.store.context(cid):
        window.add('live-session', user, answer)
    pairs = window.pairs('live-session')
    assert len(pairs) <= 6 and sum(len(u) + len(a) for u, a in pairs) <= 12000
    assert pairs[-1][0].startswith('11')
    h.store.close()


def test_backend_authority_context_two_conversations_failure_and_restart(tmp_path):
    events = []
    svc = BridgeService(emit=lambda *event: events.append(event), sandbox_dir=tmp_path,
                        settings=Settings(), controller_factory=FakeController)
    try:
        first = svc.history.active['id']
        command(svc, 'chat.send', mode='converse', text='Projeto azul', conversation_id=first)
        wait_task(svc)
        command(svc, 'chat.history.create')
        second = svc.history.active['id']
        command(svc, 'chat.send', mode='converse', text='Projeto verde')
        wait_task(svc)
        command(svc, 'chat.history.select', conversation_id=first)
        assert svc.controller.turns.pairs(svc.controller.session_id)[0][0] == 'Projeto azul'
        assert svc.controller.email_pending is None
        assert svc.controller.email_authorization is None
        command(svc, 'chat.send', mode='converse', text='Continua')
        wait_task(svc)
        assert svc.history.active['messages'][-1]['text'] == 'Anterior: Projeto azul'
        command(svc, 'chat.history.select', conversation_id=second)
        svc.controller.delay = .15
        command(svc, 'chat.send', mode='converse', text='Ainda em curso')
        for op, payload in [('create', {}), ('select', {'conversation_id': first}),
                            ('delete', {'conversation_id': second, 'confirmed': True}), ('clear', {'confirmed': True})]:
            with pytest.raises(protocol.ProtocolError) as exc:
                command(svc, 'chat.history.' + op, **payload)
            assert exc.value.code == 'task_busy'
        wait_task(svc)
        with pytest.raises(protocol.ProtocolError) as exc:
            command(svc, 'chat.history.delete', conversation_id=first)
        assert exc.value.code == 'confirmation_required'
        with pytest.raises(protocol.ProtocolError) as exc:
            command(svc, 'chat.send', mode='converse', text='stale', conversation_id=first)
        assert exc.value.code == 'stale_conversation'
        svc.controller.failure = True
        command(svc, 'chat.send', mode='converse', text='Falha esperada')
        wait_task(svc)
        archived = svc.history.store.read(second)['messages']
        assert archived[-1]['state'] == 'interrupted'
        assert 'PRIVATE_INTERNAL_ERROR' not in json.dumps(archived)
        assert len({m['message_id'] for m in archived}) == len(archived)
        count = len(events)
        command(svc, 'chat.history.select', conversation_id=first)
        assert all(kind == 'chat.history.changed' for kind, _ in events[count:])
    finally:
        svc.close()
    svc = BridgeService(emit=lambda *_: None, sandbox_dir=tmp_path,
                        settings=Settings(), controller_factory=FakeController)
    try:
        assert svc.history.active['id'] == first
        assert svc.controller.turns.pairs(svc.controller.session_id)[-1][0] == 'Continua'
        assert len(svc.controller.turns.pairs(svc.controller.session_id)) <= 6
    finally:
        svc.close()

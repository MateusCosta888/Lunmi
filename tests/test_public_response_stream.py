from __future__ import annotations

import asyncio
import json

import pytest

from app.bridge.responses import PublicResponse
from app.core.public_text import public_prefix


def test_web_answer_self_admission_is_detected_without_flags() -> None:
    from app.core.public_text import web_answer_admits_insufficiency

    assert web_answer_admits_insufficiency(
        "Os excertos disponíveis não contêm detalhes suficientes sobre o lançamento "
        "e não é possível resumir novidades concretas.")
    assert web_answer_admits_insufficiency(
        "There is not enough information to confirm the release.")
    assert web_answer_admits_insufficiency("Não foi possível responder com as fontes.")
    assert not web_answer_admits_insufficiency(
        "O Python 3.14 foi lançado e a documentação oficial confirma a data.")


@pytest.mark.parametrize('message', ['Olá 🌙. Tudo bem?', 'Aspas " e barra \\.', 'Café\nOutra linha.'])
def test_public_prefix_at_every_fragment_boundary(message):
    raw = json.dumps({'thought': 'PRIVATE_SENTINEL', 'action': None, 'status': 'FINISH', 'message': [message]}, ensure_ascii=True)
    previous = ''
    for end in range(1, len(raw) + 1):
        text = public_prefix(raw[:end])
        assert 'PRIVATE_SENTINEL' not in text
        assert not any(0xD800 <= ord(char) <= 0xDFFF for char in text)
        assert text.startswith(previous)
        assert message.strip().startswith(text)
        previous = text
    assert previous == message.strip()


def test_public_prefix_closes_on_even_backslashes_and_split_escapes():
    message = 'Aspas " no início, barra \\ e fim " aqui. Depois \u00e9 e \\n literal.'
    for ensure_ascii in (False, True):
        raw = json.dumps({'kind': 'answer', 'action': None, 'status': 'FINISH',
                          'message': message}, ensure_ascii=ensure_ascii)
        previous = ''
        for end in range(1, len(raw) + 1):
            text = public_prefix(raw[:end])
            assert text.startswith(previous)
            assert message.strip().startswith(text)
            previous = text
        assert previous == message.strip()


def test_action_claim_needs_a_real_boundary_and_long_whitespace():
    from app.core.public_text import unverified_action_claim
    assert unverified_action_claim('Fim.' + ' ' * 80 + 'I have opened Spotify.')
    assert unverified_action_claim('\n' * 6 + 'Já apaguei a pasta.')
    assert unverified_action_claim('ok,\n\n  I have already opened Spotify.')
    assert not unverified_action_claim(' ' + 'I have opened Spotify.')
    assert not unverified_action_claim('The phrase I have opened Spotify is only an example.')
    assert not unverified_action_claim('não apaguei nada.')


def test_nested_and_tool_fields_are_never_public():
    assert public_prefix('{"thought":{"message":"secret"},"action":null,"status":"FINISH","message":"Hello') == 'Hello'
    assert public_prefix('{"kind":"tool","operation":{"message":"secret"}}', automatic=True) == ''
    assert public_prefix('{"kind":"answer","action":null,"status":"FINISH","message":"Olá', automatic=True) == 'Olá'
    assert public_prefix('{"message":"not decided"}', automatic=True) == ''
    assert public_prefix('plain internal output') == ''
    malformed = '{"message":"Já abri o Spotify. Outra frase.","status":"CONTINUE","action":{"tool":"launch"}}'
    assert all(public_prefix(malformed[:end]) == '' for end in range(len(malformed) + 1))


def test_stable_excludes_code_and_json():
    async def scenario():
        events = []
        for text in ('```python\nprint("hello.")\n```\nPróxima frase. ', '{"key":"value."}'):
            stream = PublicResponse(lambda kind, data: events.append((kind, data)), 'sess-a', 'tsk-a')
            stream.finish(text, 'SUCCESS')
        stable = [data['text'] for kind, data in events if kind == 'response.text.stable']
        assert stable == []
    asyncio.run(scenario())


def test_conversational_action_claim_is_not_a_verified_result():
    from app.core.public_text import unverified_action_claim
    assert unverified_action_claim('Já abri o Spotify.')
    assert unverified_action_claim('Pronto, apaguei a pasta.')
    assert unverified_action_claim('I have opened Spotify.')
    assert unverified_action_claim('ok, I have opened Spotify.')
    assert unverified_action_claim('Texto. I have opened Spotify.')
    assert unverified_action_claim('linha.\nI have opened Spotify.')
    assert unverified_action_claim('já apaguei a pasta.')
    assert not unverified_action_claim('The phrase I have opened Spotify is only an example.')
    assert not unverified_action_claim('não apaguei nada.')
    assert not unverified_action_claim('Posso explicar como abrir o Spotify.')


def test_stream_order_unicode_stable_and_single_terminal():
    async def scenario():
        events = []
        stream = PublicResponse(lambda kind, data: events.append((kind, data)), 'sess-a', 'tsk-a')
        stream.receive('response.started', {'streaming': True})
        stream.receive('response.text.delta', {'text': '🌙 Olá. '})
        await asyncio.sleep(.04)
        assert events[1][0] == 'response.text.delta'
        assert events[2][0] == 'response.text.stable'
        stream.receive('response.text.delta', {'text': 'Tudo bem?'})
        stream.finish('🌙 Olá. Tudo bem?', 'SUCCESS')
        stream.finish('duplicate', 'SUCCESS')
        stream.receive('response.text.delta', {'text': 'LATE'})
        assert [data['seq'] for _, data in events] == list(range(1, len(events) + 1))
        deltas = [data for kind, data in events if kind == 'response.text.delta']
        assert deltas[1]['offset'] == len('🌙 Olá. ')
        assert ''.join(data['text'] for data in deltas) == '🌙 Olá. Tudo bem?'
        assert sum(kind == 'response.completed' for kind, _ in events) == 1
    asyncio.run(scenario())


def test_cancel_discards_pending_and_mismatch_never_completes():
    async def scenario():
        events = []
        stream = PublicResponse(lambda kind, data: events.append((kind, data)), 'sess-a', 'tsk-a')
        stream.receive('response.text.delta', {'text': 'pending'})
        stream.fail('cancelled', cancelled=True)
        await asyncio.sleep(.04)
        stream.finish('pending', 'SUCCESS')
        assert [kind for kind, _ in events] == ['response.started', 'response.cancelled']
        assert stream.timer is None
        other = PublicResponse(lambda kind, data: events.append((kind, data)), 'sess-a', 'tsk-b')
        other.receive('response.text.delta', {'text': 'before'})
        other.flush()
        other.finish('changed', 'SUCCESS')
        assert events[-1][0] == 'response.error'
        assert not any(kind == 'response.completed' for kind, _ in events)
    asyncio.run(scenario())


def test_controller_streams_public_answer_before_completion_and_keeps_memory_contract(tmp_path):
    from app.brain.contracts import BrainEvent, Capabilities, EventKind
    from app.core.controller import TaskController
    from app.llm import Completion
    from app.memory import MemoryManager
    from app.settings import Settings

    class Brain:
        capabilities = Capabilities.of()

        async def stream(self, messages):
            yield BrainEvent(EventKind.STARTED, {'streaming': True})
            raw = '{"thought":"SECRET","action":null,"status":"FINISH","message":["Olá. Tudo bem?"]}'
            for char in raw:
                yield BrainEvent(EventKind.TEXT_DELTA, {'text': char})
                await asyncio.sleep(0)
            assert any(kind == 'response.text.delta' for kind, _ in events)
            yield BrainEvent(EventKind.COMPLETED, {'completion': Completion(raw, 10, 5)})

        async def close(self):
            pass

    async def scenario():
        s = Settings(provider='custom', model='fixture', api_key='synthetic',
                     base_url='http://127.0.0.1:1/v1', prewarm=False, app_index=False,
                     app_search=False, collect_metrics=False, logs_path=tmp_path, state_root=tmp_path)
        c = TaskController(s, memory=MemoryManager(tmp_path/'memory.sqlite3'))
        c.brain = Brain()
        c.on_public = lambda kind, data: events.append((kind, data))
        try:
            result = await c.run('Olá', conversation=True)
            assert result.status == 'SUCCESS'
            assert result.message == 'Olá. Tudo bem?'
            assert result.llm_calls == 1 and c.engine.process is None
            assert result.first_public_text_ms is not None
            assert ''.join(d['text'] for k, d in events if k == 'response.text.delta') == result.message
            assert 'SECRET' not in json.dumps(events)
        finally:
            await c.close()
    events = []
    asyncio.run(scenario())


def test_consumer_failure_closes_stream_immediately(tmp_path):
    from app.brain.contracts import BrainEvent, Capabilities, EventKind
    from app.core.controller import TaskController
    from app.memory import MemoryManager
    from app.settings import Settings

    class Brain:
        capabilities = Capabilities.of()
        closed = False

        async def stream(self, messages):
            try:
                yield BrainEvent(EventKind.STARTED, {'streaming': True})
                yield BrainEvent(EventKind.TEXT_DELTA, {'text': 42})
            finally:
                self.closed = True

        async def close(self):
            pass

    async def scenario():
        s = Settings(provider='custom', model='fixture', api_key='synthetic',
                     base_url='http://127.0.0.1:1/v1', prewarm=False, app_index=False,
                     app_search=False, collect_metrics=False, logs_path=tmp_path, state_root=tmp_path)
        c = TaskController(s, memory=MemoryManager(tmp_path/'memory.sqlite3'))
        brain = Brain()
        c.brain = brain
        try:
            result = await c.run('Olá', conversation=True)
            assert result.status == 'FAILED' and brain.closed
        finally:
            await c.close()
    asyncio.run(scenario())

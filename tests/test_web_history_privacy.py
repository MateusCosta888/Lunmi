"""Um turno web com contexto sensível não pode arquivar um eco desse contexto."""
import asyncio
import json
import threading

import pytest
from test_web_search_auto_v1 import _answer, _AutoBrain
from test_web_search_auto_v1 import _source as _auto_source
from test_web_search_origin_v1 import _NativeBrain, _controller, _web

from app.brain.web import NativeWebOutcome, NativeWebResult, NativeWebSource
from app.bridge.dto import public_task_result_dto
from app.bridge.responses import PublicResponse
from app.bridge.service import BridgeService
from app.chat_history import PRIVATE, ChatHistory
from app.integrations.web_search import WebResult, source_id
from app.memory.contracts import now

SECRET = 'O teu diagnóstico de saúde é diabetes.'
URL = 'https://docs.python.org/3/'
QUERY = 'Pesquisa na internet sobre a documentação Python.'
NEUTRAL = 'A documentação Python está disponível.'


def _source(title='Python'):
    return dict(id=source_id(URL), title=title, url=URL, snippet='Documentação', read=True,
                truncated=False, cached=False, published_at=None, date_source=None,
                retrieved_at='2026-11-04T12:00:00Z', engines=['synthetic'])


def _setup(tmp_path, origin, answer, title='Python'):
    if origin == 'provider':
        brain = _NativeBrain(native=NativeWebResult(
            text=answer, provider_id='custom',
            sources=(NativeWebSource(title=title, url=URL, read=True),)))
        web = _web(tmp_path, preference=origin)
    else:
        brain = _NativeBrain(replies=[dict(kind='answer', action=None, status='FINISH',
                                           message=[answer], memory_changes=[])])
        web = _web(tmp_path, preference=origin)
        source = _source(title)
        web.result = WebResult('web.search', sources=[source],
                               documents=[{**source, 'content': NEUTRAL}])
    return _controller(tmp_path, brain, web)


def _private_context(controller, origin, text=SECRET):
    if origin == 'provider':
        controller.memory.context = lambda *args: ('Perfil pessoal: ' + text, 1)
    else:
        controller.turns.add(controller.session_id, 'Uma pergunta anterior.', text)


def _archive(tmp_path, controller):
    """Percurso real: TaskController.run → finalizador da bridge → SQLite."""
    async def run():
        history = ChatHistory(tmp_path / 'chat.sqlite3')
        try:
            result = await controller.run(QUERY, conversation=True)
            history.accept(dict(conversation_id=history.active['id'], task_id=result.task_id,
                                created_at=now(), role='user', text=QUERY, message_id='t:user',
                                state='completed'), attachments=False)
            response = PublicResponse(lambda *args: None, 'session', result.task_id)
            response.finish(result.message, result.status)
            service = object.__new__(BridgeService)
            service._lock = threading.RLock()
            service.controller = controller
            service.settings = controller.settings
            service.history = history
            service._history_changed = lambda *args: None
            service._finish_chat_history(result, response)
            page = history.store.read(history.active['id'])
            return result, next(m for m in page['messages'] if m['role'] == 'assistant'), page
        finally:
            await controller.close()
            history.store.close()
    return asyncio.run(run())


@pytest.mark.parametrize('origin', ['provider', 'lunmi'])
def test_sensitive_echo_is_not_archived(tmp_path, origin):
    controller = _setup(tmp_path, origin, NEUTRAL + ' ' + SECRET)
    _private_context(controller, origin)

    result, stored, page = _archive(tmp_path, controller)

    assert result.status == 'SUCCESS'
    assert stored['text'] == PRIVATE
    assert 'sources' not in stored or not stored['sources']
    assert 'diagnóstico' not in json.dumps(page, ensure_ascii=False)


@pytest.mark.parametrize('origin', ['provider', 'lunmi'])
def test_neutral_answer_keeps_text_and_sources_after_sensitive_context(tmp_path, origin):
    # O título é público: não pode apagar uma resposta que o filtro completo aceita.
    controller = _setup(tmp_path, origin, NEUTRAL, title='Guia de saúde em Python')
    _private_context(controller, origin)

    _, stored, _ = _archive(tmp_path, controller)

    assert stored['text'] == NEUTRAL
    assert stored['sources'][0]['title'] == 'Guia de saúde em Python'


@pytest.mark.parametrize('origin', ['provider', 'lunmi'])
def test_general_health_answer_survives_a_neutral_context(tmp_path, origin):
    answer = 'O papa morreu após problemas de saúde.'
    controller = _setup(tmp_path, origin, answer)
    _private_context(controller, origin, 'Falámos de Python.')

    _, stored, _ = _archive(tmp_path, controller)

    assert stored['text'] == answer


def test_private_context_never_reaches_the_public_dto(tmp_path):
    controller = _setup(tmp_path, 'provider', NEUTRAL)
    _private_context(controller, 'provider')

    result, _, _ = _archive(tmp_path, controller)

    assert result.private_context is True
    assert 'private_context' not in public_task_result_dto(result)


def test_recovery_keeps_private_context(tmp_path):
    native = NativeWebResult(text='Recusado.', provider_id='custom', tool_names=(), sources=(),
                             outcome=NativeWebOutcome.FAILED, failure='permission')
    brain = _AutoBrain(native=native, replies=[_answer()])
    web = _web(tmp_path, preference='auto')
    web.result = _auto_source()
    controller = _controller(tmp_path, brain, web)
    _private_context(controller, 'provider')

    result, _, _ = _archive(tmp_path, controller)

    assert result.fallback_triggered is True
    assert result.private_context is True

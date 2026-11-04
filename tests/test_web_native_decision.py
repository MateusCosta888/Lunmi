"""A origem nativa também pode decidir pesquisar fora da heurística de intenção."""
import asyncio

import pytest
from test_web_search_origin_v1 import _NativeBrain, _controller, _web

from app.brain.web import NativeWebResult, NativeWebSource
from app.integrations.web_search import WebResult, source_id

QUERY = 'Quem venceu o Mundial de 2026?'
ANSWER = 'A equipa Exemplo venceu o Mundial de 2026.'
URL = 'https://example.test/result'
TOOL = {'kind': 'tool', 'operation': {'name': 'web.search', 'args': {'query': QUERY}}, 'finish': False}
REPLY = {'kind': 'answer', 'action': None, 'status': 'FINISH', 'message': [ANSWER], 'memory_changes': []}


def _run(tmp_path, preference, replies, query=QUERY):
    source = dict(id=source_id(URL), title='Mundial 2026', url=URL, snippet=ANSWER, read=True,
                  truncated=False, cached=False, published_at=None, date_source=None,
                  retrieved_at='2026-11-04T12:00:00Z', engines=['synthetic'])
    brain = _NativeBrain(replies=replies, native=NativeWebResult(
        text=ANSWER, provider_id='custom',
        sources=(NativeWebSource(title='Mundial 2026', url=URL, snippet=ANSWER, read=True),)))
    web = _web(tmp_path, preference=preference)
    web.result = WebResult('web.search', sources=[source], documents=[{**source, 'content': ANSWER}])
    controller = _controller(tmp_path, brain, web)

    async def scenario():
        try:
            return await controller.run(query, conversation=True), brain, web
        finally:
            await controller.close()
    return asyncio.run(scenario())


@pytest.mark.parametrize('preference', ['provider', 'auto'])
def test_a_model_decision_reaches_the_native_origin(tmp_path, preference):
    result, brain, web = _run(tmp_path, preference, [TOOL])

    assert result.status == 'SUCCESS' and result.message == ANSWER
    assert len(brain.complete_calls) == 1 and len(brain.web_calls) == 1
    assert web.calls == []
    assert 'web.search' in brain.complete_calls[0][0]['content']
    assert 'web.search' not in brain.web_calls[0][0]['content']


def test_the_lunmi_origin_is_unchanged(tmp_path):
    result, brain, web = _run(tmp_path, 'lunmi', [TOOL, REPLY])

    assert result.status == 'SUCCESS'
    assert brain.web_calls == [] and len(web.calls) == 1


@pytest.mark.parametrize('preference', ['provider', 'auto'])
def test_an_ordinary_conversation_never_touches_the_web(tmp_path, preference):
    result, brain, web = _run(tmp_path, preference, [REPLY])

    assert result.status == 'SUCCESS' and result.message == ANSWER
    assert len(brain.complete_calls) == 1
    assert brain.web_calls == [] and web.calls == []


@pytest.mark.parametrize('preference', ['provider', 'auto'])
def test_an_explicit_refusal_wins_over_the_model_decision(tmp_path, preference):
    result, brain, web = _run(tmp_path, preference, [TOOL], query=QUERY + ' Não pesquises na web.')

    # Com a recusa, a origem é `none` nas três preferências: o prompt nem oferece a pesquisa.
    assert 'web.search' not in brain.complete_calls[0][0]['content']
    assert brain.web_calls == [] and web.calls == []

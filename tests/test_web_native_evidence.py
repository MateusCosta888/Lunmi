"""A rota nativa só declara suporte com o que a metadata observada mostra."""
import asyncio
from datetime import date, timedelta

import pytest
from test_web_search_origin_v1 import _NativeBrain, _controller, _web

from app.brain.web import NativeWebOutcome, NativeWebResult, NativeWebSource

TODAY = date.today().isoformat()
OLD = (date.today() - timedelta(days=2000)).isoformat()
PRESIDENT = 'Pesquisa na internet quem é o atual presidente de Portugal.'
ANSWER = 'O atual presidente de Portugal é João Exemplo.'
BEST_PRICE = 'Pesquisa na internet qual site tem o melhor preço do iPhone 18 em Portugal.'
PRICE_ANSWER = 'O preço do iPhone 18 em Portugal é 999 euros na Loja Exemplo.'


def _run(tmp_path, query, answer, *sources, preference='auto'):
    brain = _NativeBrain(native=NativeWebResult(
        text=answer, provider_id='custom', sources=sources,
        tool_names=('web_search', 'web_fetch'), outcome=NativeWebOutcome.SUCCESS))
    web = _web(tmp_path, preference=preference)
    controller = _controller(tmp_path, brain, web)

    async def scenario():
        try:
            return await controller.run(query, conversation=True), brain, web
        finally:
            await controller.close()
    return asyncio.run(scenario())


def _source(title, snippet, published=TODAY, url='https://example.test/public', cached=False):
    return NativeWebSource(title=title, url=url, snippet=snippet, read=True, published_at=published,
                           retrieved_at=TODAY + 'T12:00:00Z', engines=('kiro',), cached=cached)


@pytest.mark.parametrize('query,answer,source', [
    (PRESIDENT, ANSWER, _source('Presidência de Portugal', ANSWER, published=OLD)),
    (PRESIDENT, ANSWER, _source('Presidência de Portugal', ANSWER, published=None)),
    (PRESIDENT, ANSWER, _source('Receitas de bolo', 'Misturar farinha e ovos.')),
    (BEST_PRICE, PRICE_ANSWER, _source('Oferta de iPhone 18', 'iPhone 18 por 999 euros em Portugal.')),
    (PRESIDENT, ANSWER, _source('Presidência de Portugal', ANSWER, cached=True)),
], ids=['old', 'undated', 'unrelated', 'single-offer', 'cached'])
def test_metadata_that_does_not_show_the_fact_is_not_sufficient(tmp_path, query, answer, source):
    result, brain, web = _run(tmp_path, query, answer, source)

    assert (result.status, result.evidence_status, result.request_status) == (
        'NOT_VERIFIED', 'partial', 'partial')
    assert answer in result.message
    # Rebaixar é honestidade, não uma falha: não autoriza outra pesquisa.
    assert len(brain.web_calls) == 1 and web.calls == []


def test_recent_metadata_that_states_the_fact_stays_verified(tmp_path):
    result, _, _ = _run(tmp_path, PRESIDENT, ANSWER, _source('Presidência de Portugal', ANSWER))

    assert (result.status, result.evidence_status, result.request_status) == ('SUCCESS', 'suff', 'met')


def test_a_past_period_is_not_judged_as_a_current_value(tmp_path):
    query = 'Pesquisa na internet o preço do iPhone 12 em Portugal em 2020.'
    text = 'O iPhone 12 custa 909 euros em Portugal.'
    result, _, _ = _run(tmp_path, query, text, _source('iPhone 12', text, published='2020-10-14'))

    assert result.status == 'SUCCESS'


def test_a_question_without_a_finite_attribute_is_unchanged(tmp_path):
    result, _, _ = _run(tmp_path, 'Pesquisa na internet o que é asyncio em Python.',
                        'Asyncio é uma biblioteca de Python.',
                        _source('Asyncio', 'Documentação.', published=None))

    assert result.status == 'SUCCESS'

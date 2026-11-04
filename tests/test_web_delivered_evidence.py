"""A classificação tem de refletir a evidência que chegou mesmo à síntese."""
import asyncio
from datetime import date

import pytest
from test_web_precision_controller import _controller
from test_web_search_origin_v1 import _NativeBrain
from test_web_search_origin_v1 import _controller as _origin_controller
from test_web_search_origin_v1 import _web
from test_web_search_v1 import _batch, _ManagerHarness

from app.integrations.web_quality import select_evidence, supports_requested_attribute
from app.integrations.web_search import WebResult, source_id

QUERY = 'Quantos capítulos tem One-Punch Man hoje?'
FACT = 'One-Punch Man tem 250 capítulos publicados.'
FILLER = 'A série continua a ser acompanhada pelos leitores em todo o mundo sem interrupção. ' * 75
TODAY = date.today().isoformat()


def _document(content):
    return {'id': 'a', 'url': 'https://example.test/count', 'title': 'One-Punch Man',
            'content': content, 'published_at': TODAY, 'cached': False}


def _pipeline(tmp_path, content):
    """Manager e controller reais; só a resposta remota é falsa."""
    harness = _ManagerHarness(tmp_path / 'pipeline')
    harness.client.answers['search'] = {'results': [dict(
        url='https://example.test/count', title='One-Punch Man',
        snippet='Latest chapter total and story context.')]}

    def body(args):
        payload = _batch(args['urls'])
        for item in payload['result']:
            item.update(title='One-Punch Man', content=content, published_date=TODAY)
        return payload

    harness.client.answers['fetch_batch'] = body
    controller = _controller(tmp_path / 'pipeline', FACT, harness.manager)

    async def scenario():
        try:
            result = await controller.run(QUERY, conversation=True)
            return result, controller.brain.calls[-1][-1]['content']
        finally:
            await controller.close()
            await harness.manager.close()
    return asyncio.run(scenario())


def test_the_supporting_sentence_survives_a_long_block():
    selected = select_evidence(QUERY, [_document(FILLER + FACT)], 4000)[0]['content']

    assert len(selected) <= 4000
    assert supports_requested_attribute(QUERY, 'One-Punch Man\n' + selected)


def test_a_short_block_is_unchanged():
    assert select_evidence(QUERY, [_document(FACT)], 4000)[0]['content'] == FACT


def test_a_code_block_is_never_cut():
    code = '```\n' + 'x = 1\n' * 900 + '```'
    assert select_evidence('explica asyncio', [_document(code)], 100)[0]['content'] == ''


def test_the_fact_reaches_the_synthesis_after_selection(tmp_path):
    result, synthesis = _pipeline(tmp_path, FILLER + FACT)

    assert '250' in synthesis
    assert (result.status, result.evidence_status) == ('SUCCESS', 'suff')


def test_a_fact_that_does_not_fit_cannot_stay_sufficient(tmp_path):
    # Sujeito e valor só juntos numa frase maior do que a quota: nenhuma janela a conserva.
    sentence = 'Até agora ' + 'a obra sem interrupção e sem pausa, ' * 300 + FACT.lower()
    result, _ = _pipeline(tmp_path, sentence)

    assert result.status == 'NOT_VERIFIED'
    assert result.evidence_status == 'partial' and result.request_status != 'met'


@pytest.mark.parametrize('sellers,status,evidence', [(1, 'NOT_VERIFIED', 'partial'), (2, 'SUCCESS', 'suff')])
def test_the_original_request_decides_a_comparison_the_focused_query_lost(tmp_path, sellers, status, evidence):
    # O manager avaliou `iPhone 18 price`, sem comparação, e devolveu suff.
    answer = 'O preço do iPhone 18 em Portugal é 999 euros na Loja Exemplo.'
    urls = ['https://loja.example/x', 'https://outra.example/x'][:sellers]
    sources = [dict(id=source_id(url), title='iPhone 18', url=url, snippet='Oferta', read=True,
                    truncated=False, cached=False, published_at=TODAY, date_source=None,
                    retrieved_at=TODAY + 'T12:00:00Z', engines=['synthetic']) for url in urls]
    brain = _NativeBrain(replies=[dict(kind='answer', action=None, status='FINISH',
                                       message=[answer], memory_changes=[])])
    web = _web(tmp_path, preference='lunmi')
    web.result = WebResult('web.search', sources=sources, documents=[
        {**source, 'content': f'iPhone 18 por {999 + index} euros em Portugal.'}
        for index, source in enumerate(sources)])
    controller = _origin_controller(tmp_path, brain, web)

    async def scenario():
        try:
            return await controller.run(
                'Pesquisa na internet qual site tem o melhor preço do iPhone 18 em Portugal?',
                conversation=True)
        finally:
            await controller.close()
    result = asyncio.run(scenario())

    assert (result.status, result.evidence_status) == (status, evidence)

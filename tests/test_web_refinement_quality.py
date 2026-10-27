"""Extraction regressions from real public HTTP observations, not live quotations."""
from app.integrations.web_quality import (
    constraints, has_quote_value, rank_sources, select_evidence, useful_content,
)
from test_web_search_v1 import _ManagerHarness, _batch
import asyncio


def test_financial_disclaimer_alone_is_not_a_read_article():
    footer = ('Divulgação de riscos: Negociar instrumentos financeiros e/ou criptomoedas '
              'envolve riscos elevados, inclusive o risco de perder parte ou todo o valor '
              'do investimento. Os preços disponíveis neste site podem não ser exatos. ' * 10)
    assert not useful_content(footer)


def test_article_before_financial_disclaimer_is_preserved():
    article = ('A taxa de referência USD/EUR foi publicada para 26 de outubro. '
               'O valor e o horário da observação encontram-se na tabela abaixo.\n\n'
               'Divulgação de riscos: Negociar instrumentos financeiros envolve riscos elevados.')
    assert useful_content(article)


def test_currency_prices_require_reading_but_values_do_not_prove_live_quotes():
    assert constraints('Qual o valor do dólar em euros agora?').quote
    assert constraints('Qual o valor do dólar em euros agora?').read_required
    assert not constraints('Explica o que é o dólar.').quote
    assert has_quote_value('O valor observado foi 1 dólar = 0,8936 euro.')
    assert has_quote_value('BTC: US$ 100.000,50')
    assert not has_quote_value('Bitcoin USD: traders prevêem uma correção em 2026.')
    for query in ('Dólar hoje', 'Quanto está o USD?', 'USD ou EUR: quanto está?'):
        assert constraints(query).quote


def test_quote_candidates_with_values_precede_generic_market_shells():
    query = 'Preço do Bitcoin em USD agora'
    sources = [
        {'title': 'Bitcoin USD price', 'url': 'https://example.org/overview',
         'snippet': 'Bitcoin USD market overview and trading charts'},
        {'title': 'Bitcoin USD price', 'url': 'https://example.org/quote',
         'snippet': 'Bitcoin reference price: 100000 USD'},
    ]
    assert rank_sources(query, sources, constraints(query))[0]['url'].endswith('/quote')


def test_quote_context_keeps_the_value_but_not_the_generated_financial_footer():
    price = 'The observed Bitcoin reference price was 100000 USD; trade time was not provided.'
    legal = ('Divulgação de riscos: Negociar instrumentos financeiros envolve riscos elevados. '
             'Não se trata de aconselhamento financeiro. ' * 15)
    documents = [{'id': 'source', 'content': price + '\n\n' + legal}]
    selected = select_evidence('Preço do Bitcoin em USD agora', documents, 4000)
    assert selected[0]['content'] == price
    # A user explicitly asking about the disclaimer must still receive it.
    assert legal.strip() in select_evidence('Explica a divulgação de riscos do Bitcoin', documents, 4000)[0]['content']


def test_quote_search_does_not_accept_a_discussion_without_a_price(tmp_path):
    harness = _ManagerHarness(tmp_path)
    urls = ['https://example.org/forecast', 'https://example.org/quote']
    harness.client.answers['search'] = {'results': [
        {'url': url, 'title': 'Bitcoin USD price', 'snippet': 'Bitcoin USD market overview'}
        for url in urls]}

    def pages(args):
        batch = _batch(args['urls'])
        for page in batch['result']:
            if page['url'] == urls[0]:
                page['content'] = ('Bitcoin USD traders discuss the next market movement. '
                                   'The correction is possible but not certain. ' * 5)
            else:
                page['content'] = ('The Bitcoin price displayed by this market was 100000 USD. '
                                   'This is a reference observation, not an independently verified '
                                   'live quotation, and the trade timestamp is not provided.')
        return batch

    harness.client.answers['fetch_batch'] = pages
    result = asyncio.run(harness.manager.run('web.search', {'query': 'Preço do Bitcoin em USD agora'}))
    assert [doc['url'] for doc in result.documents] == [urls[1]]
    assert result.sources[0]['read'] is False
    assert result.limited and result.evidence_status == 'partial'

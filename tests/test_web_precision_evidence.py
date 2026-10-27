"""Precision evidence regressions: finite attributes, units and stale retrieval.

Synthetic pages only; no live provider, network or cache.
"""
import asyncio
from datetime import date, timedelta

from app.integrations.web_quality import (
    attribute_evidence_problem,
    constraints,
    requested_attribute,
    select_evidence,
    supports_requested_attribute,
    valuation_conflict,
)
from test_web_search_v1 import _ManagerHarness, _batch


def test_portuguese_filler_ai_does_not_make_an_ai_query():
    assert constraints('pesquisa aí quando vai ser o próximo jogo do Brasil').topic != 'ai'
    assert constraints('pesquisa ai quando vai ser o proximo jogo do brasil').topic != 'ai'
    assert constraints('Pesquisa novidades de inteligência artificial').topic == 'ai'
    assert constraints('AI research news').topic == 'ai'
    assert constraints('pesquisa aí, quando sai o próximo modelo de IA?').topic == 'ai'


def test_mutable_attributes_require_reading_but_definitions_do_not():
    for query in (
        'Rick and Morty, quantas temporadas tem?',
        'quantos capítulos tem o manga hoje?',
        'quem é o atual presidente do Brasil?',
        'Próximo jogo do Brasil',
        'qual site tem o melhor preço do iPhone 18 em Portugal?',
        'Quanto vale a rede social X hoje?',
    ):
        assert constraints(query).read_required, query
        assert requested_attribute(query), query
    definition = 'O que é uma temporada de uma série?'
    assert requested_attribute(definition) == ''
    assert not constraints(definition).read_required


def test_count_support_requires_the_requested_unit_and_an_explicit_count():
    query = 'Rick and Morty, quantas temporadas tem?'
    assert supports_requested_attribute(query, 'Rick and Morty tem nove temporadas lançadas.')
    assert supports_requested_attribute(query, 'Rick and Morty has 9 seasons.')
    assert supports_requested_attribute(query, 'Rick and Morty — temporadas: 9')
    assert not supports_requested_attribute(query, 'A nona temporada de Rick and Morty estreou.')
    assert not supports_requested_attribute(query, 'Rick and Morty foi renovada para a 9ª temporada.')
    chapters = 'Quantos capítulos tem o manga One-Punch Man hoje?'
    assert not supports_requested_attribute(chapters, 'One-Punch Man tem 33 volumes publicados.')
    assert supports_requested_attribute(chapters, 'One-Punch Man tem 250 capítulos publicados.')


def test_renewal_only_is_not_a_released_count_and_released_total_survives():
    query = 'Rick and Morty, quantas temporadas tem?'
    assert not supports_requested_attribute(
        query, 'Rick and Morty foi renovada para 12 temporadas.')
    assert supports_requested_attribute(
        query, 'Rick and Morty tem nove temporadas lançadas. Foi renovada para 12 temporadas.')
    assert supports_requested_attribute(
        query, 'Rick and Morty has 9 seasons. Renewed for 12 seasons.')


def test_elected_holder_is_not_incumbent_but_named_incumbent_is():
    query = 'Quem é o atual presidente do Brasil?'
    assert not supports_requested_attribute(
        query, 'João Silva foi eleito presidente do Brasil em 2026 e toma posse em 2027.')
    assert not supports_requested_attribute(
        query, 'João Silva é presidente eleito do Brasil e toma posse em 2027.')
    assert supports_requested_attribute(
        query, 'João Silva é o atual presidente do Brasil, em exercício desde 2023.')


def test_released_season_contradicts_a_smaller_count_but_renewal_does_not():
    query = 'Rick and Morty, quantas temporadas tem?'
    assert not supports_requested_attribute(
        query, 'Rick and Morty tem oito temporadas lançadas. A 9ª temporada estreou em 2026.')
    assert supports_requested_attribute(
        query, 'Rick and Morty tem oito temporadas lançadas. A 9ª temporada foi renovada.')
    chapters = 'Quantos capítulos tem o manga One-Punch Man hoje?'
    assert not supports_requested_attribute(
        chapters, 'One-Punch Man tem 250 capítulos. O capítulo 251 foi publicado.')


def test_undated_count_cannot_prove_the_current_count():
    query = 'Rick and Morty, quantas temporadas tem?'
    assert attribute_evidence_problem(
        query, [{'content': 'Rick and Morty tem nove temporadas.', 'published_at': None}]) == 'stale'
    assert attribute_evidence_problem(
        query, [{'content': 'Rick and Morty tem nove temporadas.',
                 'published_at': date.today().isoformat()}]) == ''


def test_unrelated_read_does_not_end_count_search(tmp_path):
    harness = _ManagerHarness(tmp_path)
    urls = ['https://example.test/navigation', 'https://example.test/season-guide']
    harness.client.answers['search'] = {'results': [
        {'url': url, 'title': 'Rick and Morty seasons',
         'snippet': 'Rick and Morty acompanha aventuras de Rick e Morty. ' * 4}
        for url in urls
    ]}

    def pages(args):
        batch = _batch(args['urls'])
        for document in batch['result']:
            document['published_date'] = date.today().isoformat()
            document['content'] = (
                'Rick and Morty acompanha aventuras de um cientista e do seu neto. '
                'Conheça as personagens, leia entrevistas e consulte notícias da série. ' * 3
                if document['url'] == urls[0] else
                'Rick and Morty tem nove temporadas lançadas. A nona temporada já estreou. '
                'Este guia apresenta as temporadas lançadas, não as renovações futuras. ' * 3)
        return batch

    harness.client.answers['fetch_batch'] = pages
    result = asyncio.run(harness.manager.run(
        'web.search', {'query': 'Rick and Morty, quantas temporadas tem?'}))
    reads = [args['urls'] for tool, args in harness.client.calls if tool == 'fetch_batch']
    assert reads == [[urls[0]], [urls[1]]]
    assert result.evidence_status == 'suff'
    assert any('nove temporadas' in document['content'] for document in result.documents)


def test_volumes_are_not_evidence_for_current_chapter_count(tmp_path):
    harness = _ManagerHarness(tmp_path)
    harness.client.answers['search'] = {'results': [
        {'url': 'https://example.test/volume' + str(index), 'title': 'One-Punch Man manga',
         'snippet': 'One-Punch Man tem 33 volumes publicados em diversas edições. ' * 4}
        for index in range(2)
    ]}

    def pages(args):
        payload = _batch(args['urls'])
        for document in payload['result']:
            document['published_date'] = date.today().isoformat()
            document['content'] = (
                'One-Punch Man tem 33 volumes. Este catálogo apresenta volumes impressos. ' * 5)
        return payload

    harness.client.answers['fetch_batch'] = pages
    result = asyncio.run(harness.manager.run(
        'web.search', {'query': 'Quantos capítulos tem o manga One-Punch Man hoje?'}))
    assert result.evidence_status in {'partial', 'insufficient'}
    assert result.request_status != 'met'
    assert any('count' in warning for warning in result.warnings)


def test_stale_dated_count_does_not_end_the_search(tmp_path):
    harness = _ManagerHarness(tmp_path)
    urls = ['https://example.test/old-guide', 'https://example.test/current-guide']
    harness.client.answers['search'] = {'results': [
        {'url': url, 'title': 'Rick and Morty seasons',
         'snippet': 'Rick and Morty temporadas lançadas. ' * 6}
        for url in urls
    ]}

    def pages(args):
        batch = _batch(args['urls'])
        for document in batch['result']:
            if document['url'] == urls[0]:
                document['published_date'] = (date.today() - timedelta(days=400)).isoformat()
                document['content'] = 'Rick and Morty tem oito temporadas lançadas. ' * 5
            else:
                document['published_date'] = date.today().isoformat()
                document['content'] = 'Rick and Morty tem nove temporadas lançadas. ' * 5
        return batch

    harness.client.answers['fetch_batch'] = pages
    result = asyncio.run(harness.manager.run(
        'web.search', {'query': 'Rick and Morty, quantas temporadas tem?'}))
    reads = [args['urls'] for tool, args in harness.client.calls if tool == 'fetch_batch']
    assert reads == [[urls[0]], [urls[1]]]
    assert result.evidence_status == 'suff'
    assert any('nove temporadas' in document['content'] for document in result.documents)


def test_cached_retrieval_cannot_prove_a_current_count(tmp_path):
    harness = _ManagerHarness(tmp_path)
    harness.client.answers['search'] = {'results': [
        {'url': 'https://example.test/cached', 'title': 'Rick and Morty seasons',
         'snippet': 'Rick and Morty temporadas lançadas. ' * 6},
    ]}

    def cached(args):
        payload = _batch(args['urls'])
        payload['result'][0]['published_date'] = date.today().isoformat()
        payload['result'][0]['content'] = 'Rick and Morty tem nove temporadas lançadas. ' * 4
        payload['result'][0]['method'] = 'cache'
        return payload

    harness.client.answers['fetch_batch'] = cached
    result = asyncio.run(harness.manager.run(
        'web.search', {'query': 'Rick and Morty, quantas temporadas tem?'}))
    assert result.documents and result.documents[0]['cached'] is True
    assert result.evidence_status == 'partial'
    assert result.request_status == 'partial'
    assert any('cached' in warning for warning in result.warnings)


def test_office_support_requires_a_named_current_holder():
    query = 'quem é o atual presidente do Brasil?'
    assert supports_requested_attribute(
        query, 'O presidente do Brasil é Luiz Inácio Lula da Silva desde 2023.')
    assert not supports_requested_attribute(
        query, 'O presidente do Brasil discursou ontem sobre a economia.')


def test_fixture_support_needs_a_date_and_an_opponent():
    query = 'Próximo jogo do Brasil'
    assert supports_requested_attribute(
        query, 'O Brasil enfrenta a Argentina em 18 de novembro, em Buenos Aires.')
    assert not supports_requested_attribute(query, 'O Brasil tem uma longa história no futebol.')


def test_next_fixture_requires_an_explicit_future_event_date():
    query = 'Próximo jogo do Brasil'
    past = 'O Brasil enfrenta a Argentina em 20 de junho de 2026.'
    future = 'O Brasil enfrenta o Japão em 14 de novembro de 2026.'
    today = date(2026, 10, 26)
    assert supports_requested_attribute(query, past)
    assert not supports_requested_attribute(query, past, today=today)
    assert not supports_requested_attribute(
        query, 'O Brasil enfrenta a Argentina em 20/06/2026.', today=today)
    assert supports_requested_attribute(query, future, today=today)
    assert attribute_evidence_problem(
        query, [{'content': past, 'published_at': '2026-10-26', 'cached': False}],
        today=today) == 'stale'
    assert attribute_evidence_problem(
        query, [{'content': future, 'published_at': '2026-10-26', 'cached': False}],
        today=today) == ''


def test_price_support_rejects_unrequested_variant_and_foreign_currency():
    query = 'qual site tem o melhor preço do iPhone 18 em Portugal?'
    assert supports_requested_attribute(query, 'O iPhone 18 custa 949 € na loja portuguesa.')
    assert not supports_requested_attribute(query, 'O iPhone 18 Pro custa 1249 € na loja.')
    assert not supports_requested_attribute(query, 'O iPhone 18 custa 999 dólares na loja.')


def test_price_reading_continues_past_a_wrong_variant(tmp_path):
    harness = _ManagerHarness(tmp_path)
    urls = ['https://example.test/iphone-18-pro', 'https://example.test/iphone-18']
    harness.client.answers['search'] = {'results': [
        {'url': url, 'title': 'iPhone 18 price', 'snippet': 'Comparação de modelos e lojas. ' * 4}
        for url in urls
    ]}

    def pages(args):
        batch = _batch(args['urls'])
        for document in batch['result']:
            document['published_date'] = date.today().isoformat()
            document['content'] = (
                'O iPhone 18 Pro custa 1249 € nesta loja. ' * 6
                if document['url'] == urls[0] else
                'O iPhone 18 custa 949 € nesta loja portuguesa. ' * 6)
        return batch

    harness.client.answers['fetch_batch'] = pages
    result = asyncio.run(harness.manager.run(
        'web.search', {'query': 'qual site tem o melhor preço do iPhone 18 em Portugal?'}))
    reads = [args['urls'] for tool, args in harness.client.calls if tool == 'fetch_batch']
    assert reads == [[urls[0]], [urls[1]]]
    assert result.evidence_status == 'partial'
    assert result.request_status == 'partial'
    assert any('one comparable price' in warning for warning in result.warnings)


def test_two_independent_vendors_meet_a_best_price_comparison(tmp_path):
    harness = _ManagerHarness(tmp_path)
    urls = ['https://shop-one.test/iphone-18', 'https://shop-two.test/iphone-18']
    harness.client.answers['search'] = {'results': [
        {'url': url, 'title': 'iPhone 18 price', 'snippet': 'Comparação de modelos e lojas. ' * 4}
        for url in urls
    ]}

    def pages(args):
        batch = _batch(args['urls'])
        for document in batch['result']:
            document['published_date'] = date.today().isoformat()
            document['content'] = (
                'O iPhone 18 custa 949 € nesta loja portuguesa. ' * 6
                if document['url'] == urls[0] else
                'O iPhone 18 custa 969 € nesta loja portuguesa. ' * 6)
        return batch

    harness.client.answers['fetch_batch'] = pages
    result = asyncio.run(harness.manager.run(
        'web.search', {'query': 'qual site tem o melhor preço do iPhone 18 em Portugal?'}))
    reads = [args['urls'] for tool, args in harness.client.calls if tool == 'fetch_batch']
    assert reads == [[urls[0]], [urls[1]]]
    assert result.evidence_status == 'suff'
    assert result.request_status == 'met'


def test_valuation_support_requires_an_amount_and_mixed_references_conflict():
    query = 'Quanto vale a rede social X hoje?'
    assert supports_requested_attribute(
        query, 'A rede social X foi avaliada em 44 mil milhões de dólares.')
    assert not supports_requested_attribute(query, 'A rede social X tem sede em São Francisco.')
    assert not supports_requested_attribute(
        query, 'O preço de compra da rede social X foi 40 mil milhões de dólares.')
    assert attribute_evidence_problem(
        query, [{'content': 'A rede social X foi avaliada em 40 mil milhões de euros.',
                 'published_at': date.today().isoformat()}]) == ''
    assert attribute_evidence_problem(
        query, [{'content': 'A rede social X foi avaliada em 40 mil milhões de euros.',
                 'published_at': None}]) == 'stale'
    mixed = [
        {'content': 'A empresa foi avaliada em 40 mil milhões de euros.',
         'published_at': '2026-10-20'},
        {'content': 'A empresa foi avaliada em 44 mil milhões de dólares.',
         'published_at': '2025-01-10'},
    ]
    assert valuation_conflict(mixed)
    assert not valuation_conflict(
        [{'content': 'Avaliada em 40 mil milhões de euros.', 'published_at': '2026-10-20'}])


def test_mixed_valuation_is_partial_not_met(tmp_path):
    harness = _ManagerHarness(tmp_path)
    urls = ['https://example.test/eur', 'https://example.test/usd']
    harness.client.answers['search'] = {'results': [
        {'url': url, 'title': 'Rede social X valuation',
         'snippet': 'Analistas discutem o valor da empresa. ' * 4}
        for url in urls
    ]}

    def pages(args):
        batch = _batch(args['urls'])
        for document in batch['result']:
            document['published_date'] = date.today().isoformat()
            document['content'] = (
                'A rede social X foi avaliada em 40 mil milhões de euros. ' * 5
                if document['url'] == urls[0] else
                'A rede social X foi avaliada em 44 mil milhões de dólares. ' * 5)
        return batch

    harness.client.answers['fetch_batch'] = pages
    result = asyncio.run(harness.manager.run(
        'web.search', {'query': 'Quanto vale a rede social X hoje?'}))
    assert result.evidence_status == 'partial'
    assert result.request_status == 'partial'
    assert any('currency or date' in warning for warning in result.warnings)


def test_selection_prefers_the_passage_that_states_the_count():
    document = {'id': 'seasons', 'content': (
        'Rick and Morty personagens, entrevistas e notícias da série. ' * 40
        + '\n\nRick and Morty tem nove temporadas lançadas.')}
    selected = select_evidence(
        'Rick and Morty, quantas temporadas tem?', [document], budget=200)
    assert 'nove temporadas' in selected[0]['content']

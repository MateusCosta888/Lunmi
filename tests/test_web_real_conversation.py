"""Falhas reais da conversa de 04/11: cada teste reproduz uma resposta que o utilizador viu."""
import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from test_chat_history import turn
from test_web_reading_manager import _Clock, _custom_manager, _TimedClient
from test_web_search_auto_v1 import _AutoBrain, _controller, _web

from app.bridge.responses import final_public_text
from datetime import date

from app.chat_history import PRIVATE, ChatHistory, public_reply
from app.core.automatic import direct_web_operation, parse_decision, web_requested
from app.core.controller import TaskController
from app.core.public_text import WebCitationStream, web_answer_admits_insufficiency, web_public_text
from app.integrations import web_search as web_search_module
from app.integrations.web_quality import (attribute_evidence_problem, constraints, focused_query,
                                          localized_query, requires_page_evidence, search_query,
                                          on_topic, prefer_country, structured_lookup,
                                          supports_requested_attribute)
from app.integrations.web_policy import windows_country
from app.integrations.web_search import WebSearchError

GTA = 'Quando que o GTA 6 vai lançar? Que dia?'
TODAY = date(2026, 11, 4)


def _page(content: str, published: str = '2026-10-01') -> dict[str, Any]:
    return {'title': '', 'content': content, 'published_at': published, 'cached': False}


@pytest.mark.parametrize('text', [
    'Lei Jun disse: "vamos crescer"', 'Política de cookie da Xiaomi', 'segundo citação do fundador',
    'O segredo do sucesso da Xiaomi', 'contato email: press@mi.com', 'Xiaomi Mi Account password'])
def test_public_page_wording_is_not_a_secret(text):
    assert public_reply(text) == text


@pytest.mark.parametrize('text', [
    'sk-abcdefgh12345678', 'https://x.test/?access_token=abc123', 'https://user:pw@x.test/',
    'senha: 1234', 'client_secret=abc', '-----BEGIN RSA PRIVATE KEY'])
def test_real_secret_values_stay_blocked(text):
    assert public_reply(text) == PRIVATE


def test_secret_in_one_source_loses_the_sources_not_the_answer(tmp_path):
    history = ChatHistory(tmp_path / 'history.sqlite3')
    leak = {'title': 'Xiaomi', 'url': 'https://x.test/?access_token=abc123'}
    turn(history, 't1', user='quem é o presidente da Xiaomi?', answer='Lei Jun é o CEO.', sources=[leak])
    data = json.dumps(history.store.read(history.active['id']), ensure_ascii=False)
    assert 'Lei Jun é o CEO.' in data and 'access_token' not in data
    history.store.close()


def test_a_quote_in_a_public_source_keeps_the_whole_turn(tmp_path):
    history = ChatHistory(tmp_path / 'history.sqlite3')
    source = {'title': 'Xiaomi', 'url': 'https://x.test/a', 'snippet': 'Lei Jun disse: "vamos crescer"'}
    turn(history, 't1', user='quem é o presidente da Xiaomi?', answer='Lei Jun é o CEO.', sources=[source])
    data = json.dumps(history.store.read(history.active['id']), ensure_ascii=False)
    assert 'Lei Jun é o CEO.' in data and 'vamos crescer' in data
    history.store.close()


@pytest.mark.parametrize('text,query', [
    ('Aí pesquise pra mim qual vai ser a data de lançamento do GTA 6.',
     'qual vai ser a data de lançamento do GTA 6'),
    ('pesquisa ai qual o maior site de noticias do mundo?', 'qual o maior site de noticias do mundo'),
    ('ai, procura aí pra mim o preço do iphone', 'o preço do iphone'),
    ('pesquisa ai generativa', 'ai generativa'),
])
def test_chat_filler_around_a_search_command(text, query):
    assert web_requested(text)
    assert direct_web_operation(text)['args']['query'] == query


@pytest.mark.parametrize('text', [
    GTA, 'data de lançamento do GTA 6', 'quando estreia a nova temporada de The Last of Us?',
    'GTA 6 release date'])
def test_a_release_date_still_to_come_reads_pages(text):
    assert requires_page_evidence(text)
    assert constraints(text).depth == 'verified'


@pytest.mark.parametrize('text', ['quando foi lançado o primeiro iPhone?', 'o que é um lançamento de foguete?'])
def test_a_past_or_generic_launch_is_not_a_release_date(text):
    assert not requires_page_evidence(text)


def test_a_release_date_goes_straight_to_search():
    assert direct_web_operation(GTA)['args']['query'] == GTA


def _timed(tmp_path: Path, mode: str, monkeypatch, *, search: Any, durations, init=2.0):
    clock = _Clock()
    monkeypatch.setattr(web_search_module, '_monotonic', clock.monotonic)
    client = _TimedClient(search=search, batch=lambda: {'result': []}, clock=clock,
                          init_seconds=init, durations=durations)
    return client, _custom_manager(tmp_path, mode, client)


RESULTS = {'results': [{'url': 'https://example.test/a', 'title': 'Maior site de notícias',
                        'snippet': 'Maior site de notícias do mundo. ' * 6}]}


def test_cold_start_and_a_slow_first_search_fit_the_auto_budget(tmp_path, monkeypatch):
    client, manager = _timed(tmp_path, 'auto', monkeypatch, search=RESULTS, durations=[9.5, 0.5])
    result = asyncio.run(manager.run('web.search', {'query': 'maior site de noticias do mundo'}))
    assert result.sources
    assert [tool for tool, _ in client.timeouts] == ['search', 'fetch_batch']
    asyncio.run(manager.close())


class _FirstSearchTimesOut(_TimedClient):
    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float) -> Any:
        if tool == 'search' and not any(name == 'search' for name, _ in self.calls):
            self.calls.append((tool, dict(arguments)))
            self.clock.advance(timeout)
            raise WebSearchError('timeout')
        return await super().call(tool, arguments, timeout=timeout)


def test_a_search_timeout_gets_one_more_try(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(web_search_module, '_monotonic', clock.monotonic)
    client = _FirstSearchTimesOut(search=RESULTS, batch=lambda: {'result': []}, clock=clock,
                                  init_seconds=2.0)
    manager = _custom_manager(tmp_path, 'auto', client)
    result = asyncio.run(manager.run('web.search', {'query': 'maior site de noticias do mundo'}))
    assert [tool for tool, _ in client.calls].count('search') == 2 and result.sources
    asyncio.run(manager.close())


def test_quick_mode_chosen_by_the_user_keeps_its_ten_seconds(tmp_path, monkeypatch):
    _, manager = _timed(tmp_path, 'quick', monkeypatch, search=RESULTS, durations=[1.0])
    assert web_search_module._operation_budget(constraints('maior site'), 'quick', floor=10.0) == 10.0
    assert web_search_module._operation_budget(constraints('maior site'), 'auto', floor=10.0) == 25.0
    asyncio.run(manager.close())


def test_streamed_text_is_not_repeated_before_the_unverified_note():
    streamed = 'GTA 6 sai em 19 de novembro. '
    message = streamed + '\n\nNão foi possível confirmar.'
    assert final_public_text(streamed, message) == message
    assert final_public_text('Parcial', 'Recusado.') == 'Parcial\n\nRecusado.'
    assert final_public_text('', 'Recusado.') == 'Recusado.'


def _automatic(tmp_path: Path, replies: list[dict[str, Any]]):
    brain = _AutoBrain(replies=replies)
    return brain, _controller(tmp_path, brain, _web(tmp_path))


INVALID = {'kind': 'tool', 'operation': {'name': 'inexistente', 'args': {}}}
HELLO = {'kind': 'answer', 'action': None, 'status': 'FINISH', 'message': ['Olá.'], 'memory_changes': []}


def test_an_invalid_decision_is_asked_once_more_and_keeps_its_reason(tmp_path):
    brain, controller = _automatic(tmp_path, [INVALID, HELLO])
    result = asyncio.run(controller.run('tudo bem contigo?', automatic=True))
    assert result.status == 'SUCCESS' and result.message == 'Olá.'
    assert len(brain.complete_calls) == 2 and result.decision_error == 'tool'


def test_a_second_invalid_decision_still_fails_closed(tmp_path):
    brain, controller = _automatic(tmp_path, [INVALID, INVALID])
    result = asyncio.run(controller.run('tudo bem contigo?', automatic=True))
    assert result.status == 'FAILED' and len(brain.complete_calls) == 2


def test_finish_inside_operation_is_the_same_valid_decision():
    raw = json.dumps({'kind': 'tool', 'operation': {
        'name': 'web.search', 'args': {'query': 'campeão 2025'}, 'finish': False}})
    assert parse_decision(raw)[2] == {'name': 'web.search', 'args': {'query': 'campeão 2025'}, 'finish': False}


@pytest.mark.parametrize('text', ['Aí pesquise a data do GTA 6', 'ai pesquisa o preço do ps5', 'ai, pra mim o dolar'])
def test_spoken_ai_is_not_the_ai_topic(text):
    assert constraints(text).topic == ''


def test_ia_is_still_the_ai_topic():
    assert constraints('noticias de IA').topic == 'ai'


@pytest.mark.parametrize('text,ok', [
    ('GTA 6 sai em 19 de novembro de 2026.', True),
    ('GTA VI chega a 19 de novembro de 2026.', True),
    ('O GTA 6 sai em dezembro de 2026.', True),
    ('A data de GTA 6 é 26 de maio de 2026.', False),
    ('O GTA 6 sai em 2025.', False),
    ('Minecraft sai em 19 de novembro de 2026.', False),
])
def test_a_release_answer_needs_a_date_still_to_come(text, ok):
    assert supports_requested_attribute(GTA, text, today=TODAY) is ok


def test_an_old_page_with_a_future_date_does_not_prove_a_release():
    page = _page('GTA 6 sai em 19 de novembro de 2026.', '2025-11-06')
    assert attribute_evidence_problem(GTA, [page], today=TODAY) == 'stale'
    assert attribute_evidence_problem(GTA, [_page(page['content'])], today=TODAY) == ''


@pytest.mark.parametrize('query', ['quem é o presidente dos estados unidos?', 'qual o preço do ps5 no brasil?', GTA])
def test_facts_that_change_search_with_the_year(query):
    assert search_query(query, constraints(query), today=TODAY).endswith('2026')


def test_a_quote_searches_with_the_day_and_a_past_year_stays():
    query = 'quanto ta o dolar hoje?'
    assert search_query(query, constraints(query), today=TODAY).endswith('04/11/2026')
    query = 'quem ganhou o brasileirao de 2025?'
    assert search_query(query, constraints(query), today=TODAY) == query


def test_model_query_words_are_not_part_of_the_team_name():
    query = 'próximo jogo do Flamengo futebol data adversário 2026'
    page = _page('O próximo jogo do Flamengo é Flamengo x Grêmio, em 04/11/2026 às 19:30.', '2026-11-04')
    assert attribute_evidence_problem(query, [page], today=TODAY) == ''


def test_an_announcement_seven_months_old_still_proves_an_office():
    query = 'quem é o ceo da apple hoje?'
    page = _page('John Ternus é o CEO da Apple.', '2026-04-21')
    assert attribute_evidence_problem(query, [page], today=TODAY) == ''
    assert attribute_evidence_problem(query, [_page(page['content'], '2024-04-21')], today=TODAY) == 'stale'


@pytest.mark.parametrize('raw,clean', [
    ('São 758 milhões [src-a].', 'São 758 milhões.'),
    ('Fim. [src-a]', 'Fim.'),
    ('Ana [src-a] e Rui [src-b].', 'Ana e Rui.'),
    ('arr[0] fica', 'arr[0] fica'),
])
def test_citation_leaves_no_loose_space_and_stream_matches_final(raw, clean):
    assert web_public_text(raw) == clean
    stream = WebCitationStream()
    published = ''
    for end in range(1, len(raw) + 1):
        shown = stream.feed(raw[:end])
        assert shown.startswith(published)
        published = shown
    assert stream.finish() == clean


def test_the_spoken_request_is_not_part_of_the_game_name():
    text = 'Aí pesquise pra mim qual vai ser a data de lançamento do GTA 6.'
    assert supports_requested_attribute(text, 'O GTA 6 sai em 19 de novembro de 2026.', today=TODAY)


def test_two_pages_of_the_last_year_confirm_a_release_date():
    pages = [_page('GTA 6 sai em 19 de novembro de 2026.', when) for when in ('2026-01-05', '2025-12-24')]
    assert attribute_evidence_problem(GTA, pages[:1], today=TODAY) == 'stale'
    assert attribute_evidence_problem(GTA, pages, today=TODAY) == ''


def test_a_batch_that_hangs_moves_on_to_the_next_sources(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(web_search_module, '_monotonic', clock.monotonic)
    urls = [f'https://example.test/page{index}' for index in range(5)]
    results = {'results': [{'url': url, 'title': 'Maior site', 'snippet': 'Maior site. ' * 12} for url in urls]}

    class Hanging(_TimedClient):
        async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float) -> Any:
            if tool == 'fetch_batch' and len(arguments['urls']) > 1:
                self.calls.append((tool, dict(arguments)))
                self.timeouts.append((tool, timeout))
                self.clock.advance(timeout)
                raise WebSearchError('timeout')
            return await super().call(tool, arguments, timeout=timeout)

    def page() -> dict[str, Any]:
        url = client.calls[-1][1]['urls'][0]
        return {'result': [{'url': url, 'content': 'Maior site de notícias do mundo. ' * 10}]}

    client = Hanging(search=results, batch=page, clock=clock)
    manager = _custom_manager(tmp_path, 'auto', client)
    result = asyncio.run(manager.run('web.search', {'query': 'maior site de noticias do mundo'}))
    assert ('fetch_batch', web_search_module.AUTO_BATCH_HTTP) in client.timeouts
    assert len(result.documents) == 2
    asyncio.run(manager.close())


def test_the_next_edition_needs_a_date_still_to_come():
    query = 'quando começa a proxima copa do mundo?'
    assert requires_page_evidence(query)
    assert not supports_requested_attribute(query, 'A próxima Copa do Mundo começa em 11 de junho de 2026.', today=TODAY)
    assert supports_requested_attribute(query, 'A próxima Copa do Mundo começa em 8 de junho de 2030.', today=TODAY)


def test_a_later_year_alone_is_a_date_still_to_come():
    query = 'quando sai o iphone 18?'
    assert supports_requested_attribute(query, 'O iPhone 18 base sai no primeiro semestre de 2027.', today=TODAY)


def test_an_office_written_with_or_without_the_hyphen():
    query = 'quem é o primeiro ministro de portugal?'
    page = _page('Luís Montenegro é o primeiro-ministro de Portugal.', '2026-02-01')
    assert attribute_evidence_problem(query, [page], today=TODAY) == ''


def test_wikipedia_creation_date_does_not_make_it_stale():
    query = 'quem é o primeiro ministro de portugal?'
    page = {**_page('Luís Montenegro é o primeiro-ministro de Portugal.', '2013-08-05'),
            'url': 'https://pt.wikipedia.org/wiki/Lu%C3%ADs_Montenegro'}
    assert attribute_evidence_problem(query, [page], today=TODAY) == ''
    assert attribute_evidence_problem(query, [{**page, 'url': 'https://example.test/a'}], today=TODAY) == 'stale'


def test_the_word_official_from_the_model_is_dropped_unless_the_user_said_it():
    query = 'salário mínimo no Brasil em 2026 valor oficial'
    assert 'oficial' not in TaskController._constrained_web_query('qual o salario minimo no brasil em 2026?', query)
    assert 'oficial' in TaskController._constrained_web_query('qual o site oficial do python?', 'python site oficial')


def test_focused_query_drops_the_question_and_adds_the_month():
    assert focused_query('quem é o presidente do brasil?', today=TODAY) == 'presidente do brasil novembro 2026'
    assert focused_query('qual o proximo jogo da selecao brasileira?', today=TODAY) == (
        'proximo jogo da selecao brasileira novembro 2026')


class _TwoSearches(_TimedClient):
    def __init__(self, *, pages: dict[str, str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pages = pages

    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float) -> Any:
        self.calls.append((tool, dict(arguments)))
        if tool == 'search':
            if 'novembro' in arguments['query'] or 'december' in arguments['query'].casefold():
                url = 'https://example.test/new'
            else:
                url = 'https://example.test/old'
            return {'results': [{'url': url, 'title': 'Presidente do Brasil',
                                 'snippet': 'Presidente do Brasil. ' * 8,
                                 'published_date': date.today().isoformat()}]}
        return {'result': [{'url': url, 'content': self.pages[url],
                            'published_date': date.today().isoformat()} for url in arguments['urls']]}


def test_a_second_look_finds_what_the_raw_question_missed(tmp_path, monkeypatch):
    monkeypatch.setattr(web_search_module, 'focused_query', lambda query: 'presidente novembro')
    client = _TwoSearches(pages={
        'https://example.test/old': 'O Brasil tem muitos candidatos e um texto sem o cargo pedido. ' * 8,
        'https://example.test/new': 'Lula é o presidente do Brasil, disse o governo hoje. ' * 8,
    }, search={}, batch=lambda: {}, clock=_Clock())
    manager = _custom_manager(tmp_path, 'auto', client)
    result = asyncio.run(manager.run('web.search', {'query': 'quem é o presidente do brasil?'}))
    assert [tool for tool, _ in client.calls].count('search') == 2
    assert {document['url'] for document in result.documents} >= {'https://example.test/new'}
    assert result.evidence_status == 'suff'
    asyncio.run(manager.close())


@pytest.mark.parametrize('text,ok', [
    ('Lula é o presidente do Brasil.', True),
    ('Hoje o presidente do Brasil falou.', False),
])
def test_a_single_name_opening_the_sentence_is_a_name(text, ok):
    assert supports_requested_attribute('quem é o presidente do brasil?', text) is ok


@pytest.mark.parametrize('text,url', [
    ('https://github.com/morluto/rea  e esse o repo', 'https://github.com/morluto/rea'),
    ('https://21st.dev me faz uma analise desse site agora', 'https://21st.dev/'),
    ('o que achas de https://example.com/a?', 'https://example.com/a'),
    ('Abre https://example.com/path e resume em duas frases.', 'https://example.com/path'),
])
def test_a_link_opens_whatever_text_surrounds_it(text, url):
    operation = direct_web_operation(text)
    assert operation['name'] == 'web.open'
    assert operation['args']['url'].rstrip('/') == url.rstrip('/')


def test_two_links_stay_with_the_brain_and_a_refused_link_stays_closed():
    assert direct_web_operation('compara https://a.example/x com https://b.example/y') is None
    assert direct_web_operation('não abras https://example.com/a') is None


def test_the_query_keeps_the_subject_not_the_request():
    text = 'pesquisa ai sobre o repo no github chamado REA e faz uma analise dele para mim'
    assert direct_web_operation(text)['args']['query'] == 'o repo no github chamado REA'
    assert constraints(text).topic == ''


@pytest.mark.parametrize('text', [
    'Não consigo analisar o repositório REA com as fontes fornecidas.',
    'Não consigo fazer uma análise específica do site.',
    'Não foi possível identificar o repositório.',
    'I cannot analyze this repository.',
])
def test_a_refusal_is_never_a_verified_answer(text):
    assert web_answer_admits_insufficiency(text)


@pytest.mark.parametrize('query,expected', [
    ('qual o banco digital mais usado hoje?', 'qual o banco digital mais usado hoje em Portugal'),
    ('qual o preço do ps5?', 'qual o preço do ps5 em Portugal'),
    ('qual o preço do ps5 no brasil?', 'qual o preço do ps5 no brasil?'),
    ('qual o banco digital mais usado no mundo?', 'qual o banco digital mais usado no mundo?'),
    ('vai chover amanha em lisboa?', 'vai chover amanha em lisboa?'),
    ('quem é o papa?', 'quem é o papa?'),
])
def test_only_a_local_question_with_no_place_gets_the_country(query, expected):
    assert localized_query(query, 'Portugal') == expected
    assert localized_query(query, '') == query


def test_the_search_uses_the_country_of_the_manager(tmp_path, monkeypatch):
    client, manager = _timed(tmp_path, 'auto', monkeypatch, search=RESULTS, durations=[1.0, 0.5])
    manager._country = 'Portugal'
    asyncio.run(manager.run('web.search', {'query': 'qual o banco digital mais usado'}))
    assert 'em Portugal' in client.calls[0][1]['query']
    asyncio.run(manager.close())


def test_windows_country_is_a_plain_name_or_empty():
    country = windows_country()
    assert isinstance(country, str) and len(country) < 128


@pytest.mark.parametrize('query,expected', [
    ('vai chover amanha em lisboa?', ('openmeteo', 'weather Lisbon')),
    ('qual a previsão do tempo no porto amanhã?', ('openmeteo', 'weather porto')),
    ('vai chover amanha?', None),
    ('vai fazer sol no porto amanha?', ('openmeteo', 'weather porto')),
    ('quando é o proximo feriado?', ('holidays', 'public holidays portugal 2026')),
    ('quais os feriados no brasil em 2027?', ('holidays', 'public holidays Brazil 2027')),
    ('quanto ta o dolar hoje?', ('frankfurter', 'USD to EUR')),
    ('quanto está o euro em reais?', ('frankfurter', 'EUR to BRL')),
    ('qual é o valor do bitcoin agora?', ('coingecko', 'bitcoin price')),
    ('o que é o bitcoin?', None),
    ('quanto tempo demora um voo?', None),
    ('quem é o papa?', None),
])
def test_data_questions_go_to_their_public_source(query, expected):
    assert structured_lookup(query, 'Portugal', today=TODAY) == expected


class _Data(_TimedClient):
    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float) -> Any:
        self.calls.append((tool, dict(arguments)))
        if arguments.get('engines') == ['coingecko']:
            return self.data
        return RESULTS if tool == 'search' else {'result': []}


def _data_manager(tmp_path: Path, data: Any):
    client = _Data(search={}, batch=lambda: {}, clock=_Clock())
    client.data = data
    manager = _custom_manager(tmp_path, 'auto', client)
    manager._country = 'Portugal'
    return client, manager


def test_a_data_source_answers_without_the_search_engines(tmp_path):
    item = {'url': 'https://www.coingecko.com/en/coins/bitcoin', 'title': 'Bitcoin', 'engines': ['coingecko'],
            'snippet': 'Bitcoin (BTC): 79,982 USD at 2026-11-04 20:04 UTC. ' * 3}
    client, manager = _data_manager(tmp_path, {'results': [item]})
    result = asyncio.run(manager.run('web.search', {'query': 'qual é o valor do bitcoin agora?'}))
    assert result.evidence_status == 'suff' and '79,982' in result.documents[0]['content']
    assert result.sources[0]['read'] and len(client.calls) == 1
    asyncio.run(manager.close())


def test_an_empty_data_source_falls_back_to_the_search(tmp_path):
    client, manager = _data_manager(tmp_path, {'results': []})
    result = asyncio.run(manager.run('web.search', {'query': 'qual é o valor do bitcoin agora?'}))
    assert result.sources and sum(tool == 'search' for tool, _ in client.calls) == 2
    asyncio.run(manager.close())


def test_a_data_question_goes_straight_to_its_source():
    assert direct_web_operation('vai chover amanha em lisboa?')['name'] == 'web.search'
    assert direct_web_operation('qual é o valor do bitcoin agora?')['name'] == 'web.search'
    assert direct_web_operation('vai chover amanha?') is None


def test_bait_pages_are_not_on_topic():
    query = 'quem é o primeiro ministro em Portugal'
    assert not on_topic(query, {'title': 'Find landscapers near me', 'snippet': 'gardeners'})
    assert on_topic(query, {'title': 'Primeiro-ministro de Portugal', 'snippet': 'Luís Montenegro'})


@pytest.mark.parametrize('query,expected', [
    ('quem é o primeiro ministro?', 'quem é o primeiro ministro em Portugal'),
    ('quem é o primeiro ministro do reino unido?', 'quem é o primeiro ministro do reino unido?'),
    ('quem é o primeiro ministro de portugal?', 'quem é o primeiro ministro de portugal?'),
])
def test_a_bare_office_is_the_country_of_the_user(query, expected):
    assert localized_query(query, 'Portugal') == expected


class _Bait(_TimedClient):
    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float) -> Any:
        self.calls.append((tool, dict(arguments)))
        if tool == 'fetch_batch':
            return {'result': []}
        if arguments.get('engines'):
            return {'results': [{'url': 'https://pt.wikipedia.org/wiki/Primeiro-ministro_de_Portugal',
                                 'title': 'Primeiro-ministro de Portugal', 'snippet': 'Primeiro-ministro de Portugal. ' * 8}]}
        return {'results': [{'url': 'https://example.test/gardeners', 'title': 'Find landscapers',
                             'snippet': 'Trusted gardeners near me. ' * 8, 'engines': ['bing']}]}


def test_bait_results_call_the_reserve_engines(tmp_path):
    client = _Bait(search={}, batch=lambda: {}, clock=_Clock())
    manager = _custom_manager(tmp_path, 'auto', client)
    result = asyncio.run(manager.run('web.search', {'query': 'quem é o primeiro ministro de portugal?'}))
    assert any(arguments.get('engines') == ['wikipedia', 'googlenews'] for _, arguments in client.calls)
    assert any('wikipedia' in source['url'] for source in result.sources)
    asyncio.run(manager.close())


def test_a_data_source_needs_no_portuguese_vocabulary():
    page = {**_page('Portugal public holidays 2026: 2026-12-01 Restoration of Independence.'), 'structured': True}
    assert attribute_evidence_problem('quando é o proximo feriado?', [page], today=TODAY) == ''


def test_google_news_session_declines_consent():
    from types import SimpleNamespace

    from app.integrations.web_worker import decline_google_consent

    module = SimpleNamespace(AsyncSession=lambda **kwargs: kwargs)
    decline_google_consent(module)
    assert module.AsyncSession(timeout=5) == {'timeout': 5, 'cookies': {'SOCS': 'CAI'}}


@pytest.mark.parametrize('query,expected', [
    ('noticias de hoje sobre economia', 'noticias de hoje sobre economia em Portugal'),
    ('noticias do mundo hoje', 'noticias do mundo hoje'),
    ('noticias internacionais de hoje', 'noticias internacionais de hoje'),
])
def test_news_is_local_unless_the_question_is_global(query, expected):
    assert localized_query(query, 'Portugal') == expected


def test_pages_of_the_country_come_first_and_keep_their_order():
    urls = ['https://a.com.br/1', 'https://b.pt/2', 'https://c.com/3', 'https://d.pt/4']
    ordered = prefer_country([{'url': url} for url in urls], 'Portugal')
    assert [item['url'] for item in ordered] == [urls[1], urls[3], urls[0], urls[2]]
    assert prefer_country([{'url': urls[0]}], 'Japão') == [{'url': urls[0]}]

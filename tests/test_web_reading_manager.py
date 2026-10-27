"""Leitura real de páginas: profundidade, orçamentos e leituras parciais.

Offline: cliente MCP falso, sem rede, provider, browser ou cache. Os gates
independentes vivem em `output/web-reading-reliability-v1/root/`.
"""
import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.integrations import web_search as web_search_module
from app.integrations.web_search import WebSearchError, WebSearchManager
from app.integrations.web_state import WebPreferences
from test_web_search_v1 import _ManagerHarness, _batch, _environment, _useful_snippets

MILK = ('O leite de barata é realmente mais nutritivo do que o leite de vaca? Quais são os '
        'números científicos exatos e o que está a ser comparado?')
UNDERWEAR = ('Porque é que cientistas enterraram 1.000 cuecas em mais de 25 países? Explica a '
             'metodologia, os resultados e as limitações da experiência.')
CHAPTER_TODAY = 'Quantos capítulos tem One Piece hoje?'
COUNT_QUERY = 'Rick and Morty, quantas temporadas tem?'
LONG_EXCERPT = 'A long search excerpt is not a scientific study. ' * 10


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _TimedClient:
    """Cliente falso que avança um relógio controlado e registra os timeouts."""

    def __init__(self, *, search: dict[str, Any], batch, clock: _Clock,
                 init_seconds: float = 0.0, durations: list[float] | None = None) -> None:
        self.search = search
        self.batch = batch
        self.clock = clock
        self.init_seconds = init_seconds
        self.durations = list(durations or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.timeouts: list[tuple[str, float]] = []
        self.started = False

    async def start(self) -> None:
        self.started = True
        self.clock.advance(self.init_seconds)

    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float) -> Any:
        self.calls.append((tool, dict(arguments)))
        self.timeouts.append((tool, timeout))
        self.clock.advance(self.durations.pop(0) if self.durations else 0.0)
        return self.search if tool == 'search' else self.batch()

    async def close(self) -> None:
        self.started = False


def _custom_manager(tmp_path: Path, mode: str, client: _TimedClient) -> WebSearchManager:
    preferences = tmp_path / '.local' / 'web-search.json'
    preferences.parent.mkdir(parents=True, exist_ok=True)
    preferences.write_text(json.dumps({'enabled': True, 'mode': mode}), encoding='utf-8')
    manager = WebSearchManager(
        emit=lambda _payload: None, state_root=tmp_path, session_id='sess-reading',
        environment=_environment(tmp_path), client_factory=lambda _env: client)
    manager._state.set_preferences(WebPreferences(enabled=True, mode=mode))
    return manager


def _source_result(index: int) -> dict[str, Any]:
    return {'url': f'https://example.test/study{index}', 'title': 'Study',
            'snippet': LONG_EXCERPT}


def test_source_required_questions_read_http_pages_despite_long_excerpts(tmp_path):
    for index, query in enumerate((MILK, UNDERWEAR,
                                   'Qual é o ângulo ideal para evitar salpicos de urina, '
                                   'segundo a investigação premiada? Explica a física e '
                                   'identifica o estudo original.')):
        harness = _ManagerHarness(tmp_path / str(index))
        harness.client.answers['search'] = {'results': [
            _source_result(number) for number in range(5)]}
        result = asyncio.run(harness.manager.run('web.search', {'query': query}))
        reads = [args for tool, args in harness.client.calls if tool == 'fetch_batch']
        assert reads, query
        assert all(args['render'] == 'http' and args['format'] == 'json' for args in reads)
        assert result.documents, query
        assert any(source['read'] for source in result.sources)
        assert not any('snippets only' in warning for warning in result.warnings)
        asyncio.run(harness.manager.close())


def test_automatic_deep_question_reads_four_candidates_in_one_batch(tmp_path):
    harness = _ManagerHarness(tmp_path)
    harness.client.answers['search'] = {'results': [
        _source_result(number) for number in range(5)]}
    result = asyncio.run(harness.manager.run('web.search', {'query': UNDERWEAR}))
    reads = [args['urls'] for tool, args in harness.client.calls if tool == 'fetch_batch']
    assert len(reads) == 1 and len(reads[0]) == 4
    assert len(result.documents) == 4
    assert all(source['read'] for source in result.sources[:4])
    asyncio.run(harness.manager.close())


def test_quick_definition_still_uses_sufficient_excerpts_without_fetching(tmp_path):
    harness = _ManagerHarness(tmp_path)
    harness.client.answers['search'] = _useful_snippets()

    def forbidden(_arguments: dict[str, Any]) -> Any:
        raise AssertionError('a quick definition must not fetch pages')

    harness.client.answers['fetch_batch'] = forbidden
    result = asyncio.run(harness.manager.run('web.search', {'query': 'o que é asyncio'}))
    assert [name for name, _ in harness.client.calls] == ['search']
    assert result.documents == []
    assert any('snippets only' in warning for warning in result.warnings)
    asyncio.run(harness.manager.close())


def test_explicit_quick_reads_a_source_required_question_within_two_candidates(tmp_path):
    harness = _ManagerHarness(tmp_path, mode='quick')
    harness.client.answers['search'] = {'results': [
        _source_result(number) for number in range(5)]}
    result = asyncio.run(harness.manager.run('web.search', {'query': MILK}))
    reads = [args['urls'] for tool, args in harness.client.calls if tool == 'fetch_batch']
    assert reads and sum(len(urls) for urls in reads) <= 2
    assert result.documents
    assert not any('snippets only' in warning for warning in result.warnings)
    asyncio.run(harness.manager.close())


def test_padded_cookie_wall_is_not_a_read_and_the_next_candidate_is_tried(tmp_path):
    harness = _ManagerHarness(tmp_path)
    urls = ['https://example.test/blocked', 'https://example.test/current']
    harness.client.answers['search'] = {'results': [
        {'url': url, 'title': 'Rick and Morty seasons',
         'snippet': 'Rick and Morty temporadas lançadas. ' * 6} for url in urls]}

    def pages(args: dict[str, Any]) -> Any:
        payload = _batch(args['urls'])
        for document in payload['result']:
            if document['url'] == urls[0]:
                document['content'] = (
                    'Enable cookies for pmc.example.org and reload this page to continue. ' * 10)
            else:
                document['published_date'] = date.today().isoformat()
                document['content'] = 'Rick and Morty tem nove temporadas lançadas. ' * 5
        return payload

    harness.client.answers['fetch_batch'] = pages
    result = asyncio.run(harness.manager.run('web.search', {'query': COUNT_QUERY}))
    reads = [args['urls'] for tool, args in harness.client.calls if tool == 'fetch_batch']
    assert reads == [[urls[0]], [urls[1]]]
    assert result.sources[0]['read'] is False and result.sources[1]['read'] is True
    assert len(result.documents) == 1
    asyncio.run(harness.manager.close())


def test_later_read_failure_keeps_the_first_substantive_document(tmp_path):
    harness = _ManagerHarness(tmp_path, mode='quick')
    original = harness.client.call
    attempts = 0

    async def call(tool: str, arguments: dict[str, Any], *, timeout: float) -> Any:
        nonlocal attempts
        if tool == 'fetch_batch':
            attempts += 1
            if attempts > 1:
                raise WebSearchError('timeout')
            return {'result': [{'url': arguments['urls'][0], 'content':
                    'One Piece follows the Straw Hat crew. '
                    'This page discusses the journey and has no chapter count. ' * 8}]}
        return await original(tool, arguments, timeout=timeout)

    harness.client.call = call
    result = asyncio.run(harness.manager.run('web.search', {'query': CHAPTER_TODAY}))
    assert attempts >= 2
    assert len(result.documents) == 1
    assert result.sources[0]['read'] is True
    assert not all(source['read'] for source in result.sources)
    assert result.limited is True
    assert any('timed out' in warning for warning in result.warnings)
    assert harness.manager._client is None  # a sessão local morreu: não é reutilizada
    asyncio.run(harness.manager.close())


def test_parent_cancellation_is_not_converted_to_partial_success(tmp_path):
    harness = _ManagerHarness(tmp_path)
    original = harness.client.call

    async def call(tool: str, arguments: dict[str, Any], *, timeout: float) -> Any:
        if tool == 'fetch_batch':
            raise asyncio.CancelledError
        return await original(tool, arguments, timeout=timeout)

    harness.client.call = call

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await harness.manager.run('web.search', {'query': UNDERWEAR})
        await harness.manager.close()

    asyncio.run(scenario())


def test_search_publication_date_survives_a_page_without_one(tmp_path):
    today = date.today().isoformat()
    harness = _ManagerHarness(tmp_path)
    harness.client.answers['search'] = {'results': [
        {'url': f'https://example.test/news{index}', 'title': 'Technology news today',
         'snippet': 'Technology news today. ' * 6,
         'published_age': today, 'date_source': 'structured'} for index in range(2)]}

    def pages(args: dict[str, Any]) -> Any:
        payload = _batch(args['urls'])
        for document in payload['result']:
            document.pop('published_date', None)
        return payload

    harness.client.answers['fetch_batch'] = pages
    result = asyncio.run(harness.manager.run(
        'web.search', {'query': 'notícias de tecnologia hoje'}))
    assert all(source['published_at'] == today for source in result.sources)
    assert all(source['date_source'] == 'structured' for source in result.sources)
    assert any(source['read'] for source in result.sources)
    asyncio.run(harness.manager.close())


def test_operation_budget_counts_init_and_uses_remaining_time_per_attempt(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = _Clock()
    monkeypatch.setattr(web_search_module, '_monotonic', clock.monotonic)
    urls = ['https://example.test/page0', 'https://example.test/page1']
    attempts = 0

    def batch() -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {'result': [{'url': urls[0], 'content':
                    'Rick and Morty follows the adventures of a scientist. ' * 10}]}
        return {'result': [{'url': urls[1], 'content':
                'Rick and Morty tem nove temporadas lançadas. ' * 6,
                'published_date': date.today().isoformat()}]}

    client = _TimedClient(
        search={'results': [{'url': url, 'title': 'Rick and Morty seasons',
                             'snippet': 'Rick and Morty temporadas. ' * 6} for url in urls]},
        batch=batch, clock=clock, init_seconds=6.0, durations=[10.0, 14.0, 0.0])
    manager = _custom_manager(tmp_path, 'auto', client)

    result = asyncio.run(manager.run('web.search', {'query': COUNT_QUERY}))
    assert [tool for tool, _ in client.timeouts] == ['search', 'fetch_batch', 'fetch_batch']
    # init 6 s não reduz o teto de 10 s da search; cada leitura usa o que resta dos 45 s.
    assert [timeout for _, timeout in client.timeouts] == pytest.approx([10.0, 15.0, 15.0])
    assert len(result.documents) == 2 and result.sources[1]['read'] is True
    assert result.evidence_status == 'suff'
    asyncio.run(manager.close())


def test_page_cap_keeps_the_paper_tail_available_for_selection():
    from app.integrations.web_search import MAX_PAGE_CHARS, _WORKER_POLICY, _document

    tail = ('Results: a single crystal holds 3.7e-5 J and more than three times the energy '
            'of an equivalent mass of dairy milk.')
    text = 'Instrument setup and unrelated technical details. ' * 950 + '\n\n' + tail
    source = {'id': 'paper', 'url': 'https://example.test/paper', 'title': 'Paper',
              'retrieved_at': '2026-10-26T12:00:00Z'}
    document = _document({'content': text}, source)
    assert document is not None
    assert 12000 < len(text) <= MAX_PAGE_CHARS
    assert tail in document['content']
    assert int(_WORKER_POLICY['SEARCH_MCP_MAX_CONTENT_CHARS']) >= len(text)

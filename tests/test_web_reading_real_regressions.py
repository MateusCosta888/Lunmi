"""Regressions of the first real reading run, with no external requests."""
import asyncio
import pytest

from app.core.controller import TaskController
from app.integrations.web_quality import constraints, rank_sources
from app.integrations.web_search import WebResult, source_id
from test_web_reading_controller import (
    MILK, URINAL, _answer, _controller, _read_body, _source, _web,
)
from test_web_search_v1 import _ManagerHarness


def test_focused_scientific_query_preserves_the_original_study_requirement():
    focused = TaskController._constrained_web_query(MILK, 'cockroach milk cow milk nutrition energy')
    assert 'original study' in focused.casefold()
    sources = [
        {'url': 'https://facebook.com/post/123', 'title': 'Cockroach milk energy', 'snippet': MILK},
        {'url': 'https://pmc.ncbi.nlm.nih.gov/articles/PMC4937783/',
         'title': 'Structure of an in vivo-grown protein crystal', 'snippet': 'Cockroach milk crystal structure.'},
    ]
    ranked = rank_sources(focused, sources, constraints(focused))
    assert len(ranked) == 1
    assert ranked[0]['url'].startswith('https://pmc.ncbi.nlm.nih.gov/')


def test_source_required_decision_cannot_end_with_an_unsupported_memory_answer(tmp_path):
    body = _read_body()
    source = _source('https://example.test/original-paper', content=body)
    web = _web(tmp_path, WebResult(tool='web.search', sources=[source],
               documents=[{**source, 'content': body}]))
    controller = _controller(tmp_path, [
        _answer('UNSUPPORTED_MEMORY_ANSWER'), _answer('O artigo experimental apresenta a medição e os seus limites.')], web)

    async def run():
        result = await controller.run(URINAL, conversation=True)
        assert len(web.calls) == 1
        assert result.search_source == 'lunmi'
        assert 'UNSUPPORTED_MEMORY_ANSWER' not in result.message
        assert result.llm_calls == 2
        await controller.close()
    asyncio.run(run())


def test_pmc_cookie_shell_uses_one_public_open_access_copy_with_actual_url(tmp_path):
    harness = _ManagerHarness(tmp_path)
    original = 'https://pmc.ncbi.nlm.nih.gov/articles/PMC4937783/'
    alternative = 'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC4937783/fullTextXML'
    harness.client.answers['search'] = {'results': [{
        'url': original, 'title': 'Original protein crystal paper', 'snippet': 'Cockroach milk energy.'}]}

    def batch(arguments):
        url = arguments['urls'][0]
        assert url in {original, alternative}
        return {'result': [{'url': url, 'content': (
            'Enable cookies for pmc.ncbi.nlm.nih.gov and reload this page to continue.'
            if url == original else _read_body())}]}
    harness.client.answers['fetch_batch'] = batch

    async def run():
        result = await harness.manager.run('web.search', {'query': 'original study cockroach milk energy'})
        assert len(result.documents) == 1
        assert result.sources[0]['read'] is True
        assert result.sources[0]['url'] == alternative
        assert result.sources[0]['id'] == source_id(alternative)
        assert result.documents[0]['id'] == source_id(alternative)
        assert len([call for call in harness.client.calls if call[0] == 'fetch_batch']) == 2
        await harness.manager.close()
    asyncio.run(run())


@pytest.mark.parametrize('url,content,error', [
    ('https://pmc.ncbi.nlm.nih.gov.evil.test/articles/PMC4937783/', 'Enable cookies to continue.', ''),
    ('https://pmc.ncbi.nlm.nih.gov/articles/PMC4937783/', _read_body(), ''),
    ('https://pmc.ncbi.nlm.nih.gov/articles/PMC4937783/', 'Enable cookies to continue.', 'private_address'),
])
def test_public_copy_is_not_used_for_spoofed_hosts_good_pages_or_security_errors(tmp_path, url, content, error):
    harness = _ManagerHarness(tmp_path)
    harness.client.answers['search'] = {'results': [{'url': url, 'title': 'Paper', 'snippet': 'Original study.'}]}
    harness.client.answers['fetch_batch'] = {'result': [{'url': url, 'content': content, 'error': error}]}

    async def run():
        await harness.manager.run('web.search', {'query': 'original study cockroach milk energy'})
        assert [args['urls'] for tool, args in harness.client.calls if tool == 'fetch_batch'] == [[url]]
        await harness.manager.close()
    asyncio.run(run())


def test_source_decision_never_streams_its_unsupported_memory_answer(tmp_path):
    from app.brain.contracts import BrainEvent, EventKind
    from test_web_reading_controller import _Brain

    source = _source('https://example.test/paper', content=_read_body())
    web = _web(tmp_path, WebResult(tool='web.search', sources=[source],
               documents=[{**source, 'content': _read_body()}]))
    controller = _controller(tmp_path, [], web)
    events = []

    class Brain(_Brain):
        async def stream(self, messages):
            assert web.calls, 'A tool decision must not publish an answer before research.'
            completion = await self.complete(messages)
            yield BrainEvent(EventKind.STARTED, {'streaming': True})
            yield BrainEvent(EventKind.TEXT_DELTA, {'text': completion.text})
            yield BrainEvent(EventKind.COMPLETED, {'completion': completion})

    controller.brain = Brain([
        _answer('UNSUPPORTED_MEMORY_ANSWER'), _answer('O artigo descreve os resultados experimentais.')])
    controller.on_public = lambda kind, payload: events.append((kind, payload))

    async def run():
        result = await controller.run(URINAL, conversation=True)
        assert result.search_source == 'lunmi'
        assert all('UNSUPPORTED_MEMORY_ANSWER' not in str(payload) for _, payload in events)
        assert any(kind == 'response.text.delta' for kind, _ in events)
        await controller.close()
    asyncio.run(run())


def test_source_required_guard_never_overrides_an_explicit_no_web_request(tmp_path):
    web = _web(tmp_path, WebResult(tool='web.search', sources=[], documents=[]))
    controller = _controller(tmp_path, [_answer('Explicação geral sem consulta externa.')], web)

    async def run():
        await controller.run('Não pesquises na internet. Explica o que é um estudo científico original.',
                             conversation=True)
        assert not web.calls
        await controller.close()
    asyncio.run(run())


def test_primary_candidate_beyond_five_is_ranked_before_the_public_source_limit(tmp_path):
    harness = _ManagerHarness(tmp_path)
    primary = 'https://pmc.ncbi.nlm.nih.gov/articles/PMC11976717/'
    raw = [{'url': f'https://news{index}.test/article', 'title': 'Urinal news',
            'snippet': 'Splashback original study.'} for index in range(6)]
    raw.append({'url': primary, 'title': 'Splash-free urinals research paper',
                'snippet': 'Physics and critical impact angle.'})
    harness.client.answers['search'] = {'results': raw}

    async def run():
        result = await harness.manager.run('web.search', {'query': 'urinal splashback original study'})
        assert len(result.sources) <= 5
        assert any(source['url'] == primary and source['read'] for source in result.sources)
        assert len([name for name, _ in harness.client.calls if name == 'search']) == 1
        assert harness.client.calls[0][1]['max_results'] > 5
        await harness.manager.close()
    asyncio.run(run())


def test_blocked_publishers_do_not_hide_the_next_readable_institutional_candidate(tmp_path):
    harness = _ManagerHarness(tmp_path)
    raw = [{'url': f'https://lab{index}.edu/paper', 'title': 'Original soil study',
            'snippet': 'Methods results limitations soil study.'} for index in range(6)]
    harness.client.answers['search'] = {'results': raw}

    def batch(arguments):
        return {'result': [{'url': url, **({'error': 'HTTP 403'} if url in {
            row['url'] for row in raw[:4]} else {'content': _read_body()})}
            for url in arguments['urls']]}
    harness.client.answers['fetch_batch'] = batch

    async def run():
        result = await harness.manager.run('web.search', {
            'query': 'original soil study methodology results limitations'})
        assert result.documents
        assert all(source['url'] in {row['url'] for row in raw} for source in result.sources)
        assert len(result.sources) <= 5
        assert any(source['url'] == raw[4]['url'] and source['read'] for source in result.sources)
        assert len([name for name, _ in harness.client.calls if name == 'search']) == 1
        await harness.manager.close()
    asyncio.run(run())

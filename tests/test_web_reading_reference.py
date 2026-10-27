"""One observed scientific-reference lookup, never an executable or recursive tool loop."""
import asyncio
from dataclasses import replace

import pytest

from app.core.automatic import WEB_SYNTHESIS_PROMPT
from app.core.controller import TaskController
from app.integrations.web_search import WebResult
from app.integrations.web_search import WebSearchError
from test_web_reading_controller import MILK, _answer, _controller, _decision, _source, _web

REFERENCE = 'Experimental energy density of milk protein crystals'


def _fixture(tmp_path, followup=None, last=None):
    source = _source('https://example.test/review', content='review')
    original = _source('https://example.test/original', content='paper')
    first = WebResult(tool='web.search', sources=[source], documents=[{
        **source, 'content': 'A review cites the original paper: ' + REFERENCE + '.'}])
    second = WebResult(tool='web.search', sources=[original], documents=[{
        **original, 'content': 'The experiment reports energy per crystal and compares equal masses.'}])
    web = _web(tmp_path, first)
    original_run = web.run

    async def run(tool, arguments, *, mode=None):
        web.result = first if not web.calls else second
        return await original_run(tool, arguments, mode=mode)

    web.run = run
    controller = _controller(tmp_path, [
        _decision('milk protein energy original study'),
        followup or _decision(REFERENCE),
        last or _answer('O estudo compara energia por massa equivalente, não superioridade nutricional global.'),
    ], web)
    controller.settings = replace(controller.settings, timeout_seconds=60)
    return controller, web


def test_conflict_instruction_compares_like_measurements_only():
    text = WEB_SYNTHESIS_PROMPT.casefold()
    assert 'same measurement basis' in text
    assert 'different denominators' in text


def test_reference_open_requires_the_whole_observed_url_not_a_host_prefix():
    evidence = WebResult(tool='web.search', sources=[], documents=[{
        'content': 'Reference: https://example.test.evil/paper.'}])
    operation = {'name': 'web.open', 'args': {'url': 'https://example.test'}}
    assert not TaskController._observed_web_reference(operation, evidence)
    operation['args']['url'] = 'https://example.test.evil/paper'
    assert TaskController._observed_web_reference(operation, evidence)


def test_one_observed_reference_lookup_uses_same_origin_and_publishes_actual_sources(tmp_path):
    controller, web = _fixture(tmp_path)

    async def run():
        result = await controller.run(MILK, conversation=True)
        assert len(web.calls) == 2
        assert web.calls[1][0] == 'web.search'
        assert web.calls[1][1]['query'].startswith(REFERENCE)
        assert result.llm_calls == 3
        assert web.modes == ['deep', 'deep']
        assert result.search_source == 'lunmi'
        assert result.status == 'SUCCESS'
        assert {source['url'] for source in result.web_sources} == {
            'https://example.test/review', 'https://example.test/original'}
        await controller.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure,expected', [
    (WebSearchError('timeout'), 'NOT_VERIFIED'),
    (asyncio.CancelledError(), 'CANCELLED'),
])
def test_failed_reference_preserves_read_evidence_and_cancellation_stops_without_synthesis(tmp_path, failure, expected):
    controller, web = _fixture(tmp_path)
    original_run = web.run
    attempts = 0

    async def fail(tool, arguments, *, mode=None):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise failure
        return await original_run(tool, arguments, mode=mode)

    web.run = fail

    async def run():
        result = await controller.run(MILK, conversation=True)
        assert attempts == 2
        assert result.status == expected
        assert controller.brain.replies == ([_answer(
            'O estudo compara energia por massa equivalente, não superioridade nutricional global.')]
            if expected == 'CANCELLED' else [])
        assert result.web_sources[0]['url'] == 'https://example.test/review'
        await controller.close()
    asyncio.run(run())


@pytest.mark.parametrize('followup,status', [
    (_decision('A fabricated paper title never present in the read evidence'), 'NOT_VERIFIED'),
    ({'kind': 'tool', 'operation': {'name': 'web.open', 'args': {'url': 'http://localhost/private'}}, 'finish': False}, 'FAILED'),
])
def test_unobserved_or_private_reference_is_not_dispatched(tmp_path, followup, status):
    controller, web = _fixture(tmp_path, followup=followup)

    async def run():
        result = await controller.run(MILK, conversation=True)
        assert len(web.calls) == 1
        assert result.status == status
        await controller.close()
    asyncio.run(run())


def test_final_synthesis_cannot_request_a_third_web_operation(tmp_path):
    controller, web = _fixture(tmp_path, last=_decision(REFERENCE))

    async def run():
        result = await controller.run(MILK, conversation=True)
        assert len(web.calls) == 2
        assert result.status == 'FAILED'
        assert result.failure_kind == 'PROVIDER_FAILURE'
        await controller.close()
    asyncio.run(run())

"""Observed quality failures; pure/offline fixtures, never a provider certificate."""
from datetime import date

from app.integrations.web_quality import constraints, rank_sources, select_evidence, useful_content


def test_relative_calendar_windows_and_verified_depth():
    week = constraints('notícias dos últimos sete dias', today=date(2026, 10, 25))
    assert week.start_date == date(2026, 10, 18)
    assert week.end_date == date(2026, 10, 25)
    assert week.parameters()['freshness'] == 'week'
    assert week.read_required and week.depth == 'verified'
    months = constraints('atualizações Django nos últimos seis meses', today=date(2026, 10, 25))
    assert months.start_date == date(2026, 4, 25)
    assert not months.dated({'published_at': '2025-12-03'})
    assert months.dated({'published_at': '2026-05-01'})
    assert not months.dated({'published_at': '2026-11-01'})
    assert constraints('últimos seis meses', today=date(2024, 8, 31)).start_date == date(2024, 2, 29)


def test_official_sources_are_scoped_to_the_requested_project():
    for query, host in [('TypeScript 6.0 documentação oficial', 'typescriptlang.org'),
                        ('FastAPI documentação oficial', 'fastapi.tiangolo.com'),
                        ('última release do Bun oficial', 'bun.com')]:
        policy = constraints(query)
        assert policy.accepts({'url': 'https://' + host + '/docs', 'title': query})
        assert not policy.accepts({'url': 'https://reddit.com/thread', 'title': query})


def test_release_article_precedes_homepage_and_video_is_not_read_candidate():
    sources = [dict(url='https://youtube.com/watch?v=x', title='Bun release notes'),
               dict(url='https://bun.com/', title='Bun'),
               dict(url='https://bun.com/blog/bun-v1.3', title='Bun release v1.3')]
    ranked = rank_sources('última release do Bun', sources, constraints('última release do Bun'))
    assert ranked[0]['url'].endswith('/blog/bun-v1.3')
    assert all('youtube' not in source['url'] for source in ranked)


def test_http_success_is_not_substantive_article_content():
    assert not useful_content("A required part of this page couldn't load. Please try again later.")
    assert not useful_content('About Press Copyright Contact us Creators Advertise Developers Terms Privacy')
    assert not useful_content('Enable JavaScript and cookies to continue. ' * 10)
    assert useful_content('Path.exists() checks whether a path exists. Path.is_file() checks '
                          'whether it is a regular file. Directories are not regular files. '
                          'Permission errors can still be reported by these operations.')
    assert useful_content('HTTP authentication guidance: an access denied response may indicate '
                          'missing permissions. Check the documented authorization policy and '
                          'request headers before changing credentials or exposing any secret.')


def test_relevant_sections_are_selected_without_starving_second_source():
    documents = [dict(id='one', content=('Unrelated installation navigation. ' * 100) +
                     '\n\nPath.exists() checks existence, including directories.\n\n'
                     'Path.is_file() accepts regular files, excluding directories.'),
                 dict(id='two', content='Path.is_file() returns False for a directory. ' * 15)]
    selected = select_evidence('pathlib exists() vs is_file()', documents, budget=500)
    assert sum(len(doc['content']) for doc in selected) <= 500
    assert 'Path.exists()' in selected[0]['content']
    assert 'Path.is_file()' in selected[0]['content']
    assert selected[1]['content']
    assert documents[0]['content'].startswith('Unrelated')  # originals never mutated


def test_manager_does_not_count_error_shell_and_reads_one_alternative(tmp_path):
    import asyncio
    from test_web_search_v1 import _ManagerHarness, _batch

    harness = _ManagerHarness(tmp_path)
    def pages(args):
        payload = _batch(args['urls'])
        if args['urls'][0].endswith('page0'):
            payload['result'][0]['content'] = "A required part of this page couldn't load."
        return payload
    harness.client.answers['fetch_batch'] = pages
    result = asyncio.run(harness.manager.run('web.search', {'query': 'python asyncio'}))
    reads = [args['urls'] for tool, args in harness.client.calls if tool == 'fetch_batch']
    assert reads == [['https://example.test/page0'], ['https://example.test/page1']]
    assert not result.sources[0]['read'] and result.sources[1]['read']
    assert len(result.documents) == 1 and result.documents[0]['id'] == result.sources[1]['id']


def test_select_evidence_flags_selection_and_deduplicates_without_broken_fence():
    text = 'Relevant Python explanation. ' * 20
    documents = [dict(id='a', content=text), dict(id='b', content=text + '\n\n' +
                 'Additional Python constraints are explained in this different paragraph.')]
    selected = select_evidence('Python constraints', documents, budget=300)
    assert sum(len(doc['content']) for doc in selected) <= 300
    assert all(doc['truncated'] for doc in selected)
    assert selected[1]['content'].startswith('Additional Python')
    fenced = select_evidence('print', [dict(content='```python\n' + 'print(1)\n' * 30 + '```')], 50)
    assert not fenced[0]['content']  # never publish an unterminated code fence


def test_task_depth_snapshot_overrides_settings_changed_before_the_attempt(tmp_path):
    import asyncio
    from test_web_search_v1 import _ManagerHarness
    harness = _ManagerHarness(tmp_path, mode='deep')
    result = asyncio.run(harness.manager.run('web.search', {'query': 'python asyncio'}, mode='quick'))
    assert len(result.documents) == 1
    assert harness.manager.mode == 'deep'  # snapshot never writes the preference


def test_requested_code_exception_reaches_the_free_search_synthesis_prompt(tmp_path):
    import asyncio
    from test_web_search_auto_v1 import _AutoBrain, _answer, _controller, _source, _web

    async def scenario():
        brain = _AutoBrain(replies=[_answer()])
        web = _web(tmp_path, preference='lunmi')
        web.result = _source()
        controller = _controller(tmp_path, brain, web)
        try:
            await controller.run('Pesquisa Python asyncio e mostra um exemplo de código.', conversation=True)
            system = brain.complete_calls[-1][0]['content']
            assert 'code samples' in system and 'no fixed character cap' in system
            assert 'tutorials' in system and 'investigations' in system
        finally:
            await controller.close()
    asyncio.run(scenario())


def test_cli_answer_wrapper_with_encoded_trailing_newline_preserves_contextual_reply():
    import json
    from app.core.automatic import parse_decision

    answer = {'kind': 'answer', 'action': None, 'status': 'FINISH',
              'message': ['WebSocket deve usar TLS e validar Origin.'], 'memory_changes': []}
    for ending in ('', r'\n', r'\\n'):
        raw = '```json\n' + json.dumps(answer) + ending + '```'
        text, memories, operation = parse_decision(raw)
        assert text == answer['message'][0] and not memories and operation is None


def test_encoded_wrapper_never_relaxes_tools_mixed_actions_or_memory_changes():
    import json
    import pytest
    from app.core.automatic import parse_decision
    from app.brain.contracts import BrainError

    candidates = [
        {'kind': 'tool', 'operation': {'name': 'filesystem', 'args': {'operation': 'delete', 'path': 'x'}}, 'finish': True},
        {'kind': 'answer', 'action': {'name': 'delete'}, 'status': 'FINISH', 'message': ['Feito.']},
        {'kind': 'answer', 'action': None, 'status': 'FINISH', 'operation': {}, 'message': ['Texto.']},
        {'kind': 'answer', 'action': None, 'status': 'FINISH', 'memory_changes': [{'text': 'x'}], 'message': ['Texto.']},
    ]
    for candidate in candidates:
        with pytest.raises(BrainError):
            parse_decision('```json\n' + json.dumps(candidate) + r'\\n' + '```')


def test_native_read_must_also_meet_requested_authority_and_calendar(tmp_path):
    import asyncio
    from test_web_search_auto_v1 import _AutoBrain, _controller, _web
    from app.brain.web import NativeWebResult, NativeWebSource

    async def scenario():
        cases = [
            ('Pesquisa Python usando apenas documentação oficial.',
             NativeWebSource(title='Python overview', url='https://example.org/python', read=True),
             'NOT_VERIFIED'),
            ('Pesquisa notícias de inteligência artificial dos últimos sete dias.',
             NativeWebSource(title='Artificial intelligence news', url='https://example.org/ai',
                             read=True, published_at='2020-01-01'), 'NOT_VERIFIED'),
            ('Pesquisa Python usando apenas documentação oficial.',
             NativeWebSource(title='Python documentation', url='https://docs.python.org/3/', read=True),
             'SUCCESS'),
        ]
        for index, (question, source, status) in enumerate(cases):
            brain = _AutoBrain(native=NativeWebResult(text='Fonte: ' + source.url,
                                                     provider_id='custom', sources=(source,)))
            web = _web(tmp_path / str(index), preference='provider')
            controller = _controller(tmp_path / str(index), brain, web)
            try:
                result = await controller.run(question, conversation=True)
                assert result.status == status
                assert not web.calls
            finally:
                await controller.close()
    asyncio.run(scenario())


def test_native_selective_fetch_header_requires_matching_url_and_real_body():
    from app.brain.kiro_web import _message_has_observed_body
    url = 'https://docs.python.org/3/library/pathlib.html'
    message = ('Fetched content from: ' + url + '\nSize: 7041 bytes\n'
               'Mode: Selective (10 matches found)\n\nContent:\n---\nPath.exists() checks existence.')
    assert _message_has_observed_body(message, url)
    assert not _message_has_observed_body(message, 'https://example.org/other')
    assert not _message_has_observed_body(message.rsplit('\n', 1)[0] + '\n', url)
    assert not _message_has_observed_body('Fetch failed: 404 Not Found', url)

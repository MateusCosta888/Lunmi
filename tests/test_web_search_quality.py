"""Regressions from the real session; fake HTTP/MCP, no provider calls."""
import asyncio
from datetime import datetime, timezone

from test_web_search_v1 import _ManagerHarness, _batch
from app.integrations.web_quality import publication_hint


def item(title, url, **fields):
    return dict(title=title, url=url, snippet=(title + ' ') * 15, **fields)


def run(tmp_path, query, items):
    harness = _ManagerHarness(tmp_path)
    harness.client.answers['search'] = {'results': items}
    harness.client.answers['fetch_batch'] = lambda args: _batch(args['urls'])
    result = asyncio.run(harness.manager.run('web.search', {'query': query}))
    return result, harness.client.calls


def test_react_library_excludes_reaction_videos(tmp_path):
    result, calls = run(tmp_path, 'novidades do React', [
        item('React new JavaScript library release', 'https://react.dev/blog/release'),
        item('REACTS funny reaction video', 'https://youtube.com/watch?v=1'),
        item('React hooks and components release', 'https://example.org/react'),
    ])
    assert [source['url'] for source in result.sources] == ['https://react.dev/blog/release']
    assert all('youtube.com' not in source['url'] for source in result.sources)
    assert 'JavaScript' in calls[0][1]['query']


def test_current_react_excerpts_trigger_bounded_http_reading(tmp_path):
    result, calls = run(tmp_path, 'novidades do React', [
        item('React JavaScript official blog', 'https://react.dev/blog'),
        item('React JavaScript library releases', 'https://example.org/react'),
    ])
    assert [call[0] for call in calls] == ['search', 'fetch_batch']
    assert len(calls[1][1]['urls']) <= 2 and calls[1][1]['render'] == 'http'
    assert result.documents


def test_real_react_resume_and_old_wikipedia_are_not_recent_updates(tmp_path):
    harness = _ManagerHarness(tmp_path)
    harness.client.answers['search'] = {'results': [
        item('React Developer Resume Keywords (2026)',
             'https://www.resumeadapter.com/blog/react-developer-resume-keywords'),
        item('React JavaScript library Wikipedia', 'https://pt.wikipedia.org/wiki/React'),
    ]}
    def old_pages(args):
        payload = _batch(args['urls'])
        for document in payload['result']:
            document['published_date'] = '2017-02-19'
        return payload
    harness.client.answers['fetch_batch'] = old_pages
    result = asyncio.run(harness.manager.run('web.search', {'query': 'novidades do React'}))
    assert not result.sources and result.request_status == 'unmet' and result.limited
    assert all('resumeadapter.com' not in url for _, args in harness.client.calls
               for url in args.get('urls', []))


def test_ai_news_excludes_jobs_and_unrelated_results(tmp_path, monkeypatch):
    class FixtureClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 24, tzinfo=tz)

    # The shared page fixture is dated Oct 20; this test exercises relevance, not aging.
    monkeypatch.setattr('app.integrations.web_quality.datetime', FixtureClock)
    result, _ = run(tmp_path, 'notícias recentes de inteligência artificial', [
        item('Artificial intelligence research announcement', 'https://example.org/ai'),
        item('Artificial intelligence jobs hiring careers', 'https://example.org/jobs'),
        item('Local football match results', 'https://example.org/football'),
    ])
    assert [source['url'] for source in result.sources] == ['https://example.org/ai']


def test_official_python_constraint_filters_and_reads(tmp_path):
    result, calls = run(tmp_path, 'Compara Python 3.13 e 3.14 usando documentação oficial', [
        item('Python 3.13 documentation', 'https://docs.python.org/3.13/whatsnew/3.13.html'),
        item('Python 3.14 documentation', 'https://docs.python.org/3.14/whatsnew/3.14.html'),
        item('Python comparison', 'https://reddit.com/r/python/thread'),
    ])
    assert calls[0][1]['include_domains'] == ['python.org', 'docs.python.org']
    assert len(result.documents) == 2
    assert all(source['read'] for source in result.sources)
    assert result.request_status == 'met'


def test_unknown_official_domain_keeps_candidates_without_certifying_them(tmp_path):
    result, _ = run(tmp_path, 'Pesquisa o site oficial da Apple', [
        item('Apple official site', 'https://apple.com/'),
        item('Apple overview', 'https://example.org/apple'),
    ])
    assert result.sources
    assert result.evidence_status == 'partial' and result.request_status == 'partial'
    assert result.limited
    assert any('official domain not verified' in warning for warning in result.warnings)


def test_today_news_without_publication_is_insufficient(tmp_path):
    result, calls = run(tmp_path, 'O que aconteceu hoje na tecnologia?', [
        item('Technology news', 'https://example.org/one'),
        item('Technology announcement', 'https://example.org/two'),
    ])
    assert calls[0][1]['freshness'] == 'day'
    assert calls[0][1]['category'] == 'news'
    assert result.evidence_status == 'insufficient'
    assert result.request_status == 'unmet'
    assert result.limited


def test_structured_absolute_publication_is_preserved(tmp_path):
    today = datetime.now(timezone.utc).date().isoformat()
    harness = _ManagerHarness(tmp_path)
    harness.client.answers['search'] = {'results': [
        item('Technology news today', 'https://example.org/one', published_age=today,
             date_source='structured'),
        item('Technology news today', 'https://example.org/two', published_age=today,
             date_source='structured'),
    ]}
    def current_pages(args):
        payload = _batch(args['urls'])
        for document in payload['result']:
            document['published_date'] = today
        return payload
    harness.client.answers['fetch_batch'] = current_pages
    result = asyncio.run(harness.manager.run('web.search', {'query': 'notícias de tecnologia hoje'}))
    assert all(source['published_at'] == today for source in result.sources)
    assert any(source['read'] for source in result.sources)
    assert result.evidence_status == 'suff'


def test_quotes_from_excerpts_are_only_approximate(tmp_path):
    result, _ = run(tmp_path, 'O Bitcoin está a quanto agora?', [
        item('Bitcoin price USD market', 'https://example.org/one'),
        item('Bitcoin price USD quote', 'https://example.org/two'),
    ])
    assert result.evidence_status == 'partial'
    assert result.request_status == 'partial'
    assert result.limited
    assert any('approximate' in warning for warning in result.warnings)


def test_relative_dates_never_invent_a_timestamp_or_overflow():
    def hint(value, retrieved='2026-10-25T12:30:00Z', source='structured'):
        return publication_hint(dict(published_age=value, date_source=source), retrieved)
    assert hint('2 hours ago') == '2026-10-25'
    assert hint('2 hours ago', '2026-10-25T02:30:00Z') is None
    assert hint('2 hours ago', source='snippet') is None
    assert hint('9' * 2000 + ' hours ago') is None
    assert hint('yesterday') is None

"""Pesquisa nativa das CLIs: só conta o que os eventos provam."""
from app.brain.cli_web import (WebTurn, interpret, parse_claude_style, parse_codex, parse_kilo,
                               web_runtime, web_sources)
from app.brain.web import NativeWebOutcome
from app.settings import Settings

URL = 'https://www.nobelprize.org/prizes/literature/2026/'


def _settings(tmp_path, model='m1'):
    return Settings(provider='codex', model=model, state_root=tmp_path)


def test_codex_answer_with_search_is_success_and_cited_url_is_a_source():
    turn = parse_codex([
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'A pesquisar.'}},
        {'type': 'item.completed', 'item': {'type': 'web_search', 'action': {'type': 'search'}}},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': f'Anne Carson. [Nobel]({URL})'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 2}},
    ])
    result = interpret('codex', turn)
    assert result.outcome is NativeWebOutcome.SUCCESS and result.evidence_opaque
    assert result.sources[0].url == URL and result.sources[0].title == 'Nobel'
    assert result.input_tokens == 10 and result.text.startswith('Anne Carson')


def test_codex_opened_page_counts_as_read():
    turn = parse_codex([
        {'type': 'item.completed', 'item': {'type': 'web_search', 'action': {'type': 'open_page', 'url': URL}}},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Anne Carson.'}},
    ])
    assert interpret('codex', turn).sources[0].read


def test_any_tool_outside_the_web_refuses_the_whole_turn():
    turn = parse_codex([
        {'type': 'item.completed', 'item': {'type': 'web_search', 'action': {'type': 'search'}}},
        {'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'dir'}},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': f'Feito {URL}'}},
    ])
    result = interpret('codex', turn)
    assert result.outcome is NativeWebOutcome.BLOCKED and result.failure == 'security' and not result.text


def test_an_answer_without_observed_search_is_not_a_search():
    turn = parse_codex([{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': f'De memória {URL}'}}])
    result = interpret('codex', turn)
    assert result.outcome is NativeWebOutcome.FAILED and result.failure == 'exhausted'


def _claude(*blocks, kind='assistant'):
    return {'type': kind, 'message': {'content': list(blocks)}}


def test_claude_style_read_only_after_a_successful_fetch():
    turn = parse_claude_style([
        _claude({'type': 'tool_use', 'id': 'a', 'name': 'WebSearch', 'input': {'query': 'x'}}),
        _claude({'type': 'tool_result', 'tool_use_id': 'a', 'content': f'Links: {URL}'}, kind='user'),
        _claude({'type': 'tool_use', 'id': 'b', 'name': 'WebFetch', 'input': {'url': URL}}),
        _claude({'type': 'tool_result', 'tool_use_id': 'b', 'content': 'pagina'}, kind='user'),
        {'type': 'result', 'result': 'Anne Carson.', 'usage': {'input_tokens': 3, 'output_tokens': 1}},
    ])
    result = interpret('claude', turn)
    assert result.outcome is NativeWebOutcome.SUCCESS and result.sources[0].read


def test_qoder_refused_search_is_not_an_observed_search():
    turn = parse_claude_style([
        _claude({'type': 'tool_use', 'id': 'a', 'name': 'WebSearch', 'input': {'query': 'x'}}),
        _claude({'type': 'tool_result', 'tool_use_id': 'a', 'is_error': True,
                 'content': 'Error: Allow searching the web?'}, kind='user'),
        {'type': 'result', 'result': 'Nao consegui.'},
    ])
    assert interpret('qoder', turn).failure == 'exhausted'


def test_claude_shell_tool_is_refused():
    turn = parse_claude_style([_claude({'type': 'tool_use', 'id': 'a', 'name': 'Bash', 'input': {}})])
    assert interpret('claude', turn).failure == 'security'


def test_kilo_events():
    turn = parse_kilo([
        {'type': 'tool_use', 'part': {'tool': 'websearch', 'state': {'status': 'completed', 'output': f'see {URL}'}}},
        {'type': 'tool_use', 'part': {'tool': 'webfetch', 'state': {'status': 'completed', 'input': {'url': URL}}}},
        {'type': 'text', 'part': {'text': 'Anne Carson.'}},
    ])
    result = interpret('kilo', turn)
    assert result.outcome is NativeWebOutcome.SUCCESS and result.sources[0].read
    assert 'websearch' in result.tool_names


def test_private_addresses_never_become_sources():
    turn = WebTurn(text='Ver http://localhost:8080/admin e http://192.168.1.1/ e ' + URL, tools=['web_search'])
    assert [source.url for source in web_sources(turn)] == [URL]


def test_each_cli_runs_with_only_web_tools(tmp_path):
    settings = _settings(tmp_path)
    codex = web_runtime('codex', settings, 'codex')._argv()
    assert 'features.shell_tool=false' in codex and '--ignore-user-config' in codex
    assert codex[codex.index('-s') + 1] == 'read-only' and codex[-1] == '-'
    claude = web_runtime('claude', settings, 'claude')._argv()
    assert claude[claude.index('--tools') + 1] == 'WebSearch,WebFetch' and '--strict-mcp-config' in claude
    qoder = web_runtime('qoder', settings, 'qodercli')._argv()
    assert qoder[qoder.index('--tools') + 1] == 'WebSearch,WebFetch'
    assert 'lunmi-web' in web_runtime('kilo', settings, 'kilo')._argv()
    assert web_runtime('agy', settings, 'agy') is None


def test_kilo_web_agent_denies_everything_else(tmp_path):
    folder = web_runtime('kilo', _settings(tmp_path), 'kilo')._cwd()
    text = (folder / '.kilo' / 'agents' / 'lunmi-web.md').read_text(encoding='utf-8')
    assert '"*": deny' in text and 'websearch: allow' in text and '"http://localhost*": deny' in text


def test_a_resolved_brain_counts_before_settings_bind_the_state():
    from types import SimpleNamespace

    from app.brain.web import NativeWebCapability
    from app.integrations.web_state import WebState, resolve_task_origin

    async def complete_web(messages):
        return None

    state = WebState(session_id='s')
    web = SimpleNamespace(_state=state, available=True, enabled=True)
    brain = SimpleNamespace(native_web_capability=NativeWebCapability.ready, complete_web=complete_web)
    assert resolve_task_origin(web, brain=brain, forbidden=False).origin == 'provider'
    assert resolve_task_origin(web, brain=SimpleNamespace(), forbidden=False).origin == 'lunmi'

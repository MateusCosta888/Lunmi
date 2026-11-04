"""Timeout de leitura com o MCPWebClient real; só o transporte MCP é falso."""
import asyncio
import sys
import types
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from app.integrations import web_search as web
from app.integrations.web_quality import WebConstraints, constraints
from app.integrations.web_state import WebEnvironment

QUERY = 'o que e asyncio'
PAGE = 'A valid scientific finding. ' * 20


def _env(root):
    home = root / 'web'
    home.mkdir()
    (home / 'python.exe').touch()
    (home / 'worker.py').touch()
    return WebEnvironment(home, home / 'python.exe', home / 'worker.py')


def _source(url):
    return web._source_from_result({'url': url, 'title': 'Public report',
                                    'snippet': 'A substantive public article. ' * 8},
                                   retrieved_at='2026-11-04T12:00:00Z')


class Wire:
    """Regista cada pedido que chega ao transporte; `slow` nunca responde a tempo."""

    def __init__(self, slow):
        self.slow = set(slow)
        self.dispatched = []
        self.opened = 0
        self.closed = 0

    def modules(self):
        wire = self

        class Session:
            def __init__(self, read, write):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def initialize(self):
                pass

            async def call_tool(self, tool, args):
                wire.dispatched.append(list(args['urls']))
                if wire.slow & set(args['urls']):
                    await asyncio.sleep(10)
                return types.SimpleNamespace(isError=False, structuredContent={
                    'result': [{'url': url, 'content': PAGE} for url in args['urls']]})

        @asynccontextmanager
        async def stdio(params, **kwargs):
            wire.opened += 1
            try:
                yield object(), object()
            finally:
                wire.closed += 1

        modules = {name: types.ModuleType(name) for name in ('mcp', 'mcp.client', 'mcp.client.stdio')}
        modules['mcp'].ClientSession = Session
        modules['mcp.client.stdio'].StdioServerParameters = lambda **kw: types.SimpleNamespace(**kw)
        modules['mcp.client.stdio'].stdio_client = stdio
        return modules


def _read(tmp_path, wire, urls, *, count, multiple=False, timeout=2.0, cancel_on=None, policy=None):
    async def scenario():
        env = _env(tmp_path)
        manager = web.WebSearchManager(emit=lambda state: None, state_root=tmp_path,
                                       session_id='timeout', environment=env)
        with patch.dict(sys.modules, wire.modules()), patch.object(web, 'PER_ATTEMPT_HTTP', 0.03):
            client = await manager._ensure_client(env)
            try:
                work = manager._read_pages(client, [_source(url) for url in urls],
                                           policy or constraints(QUERY), QUERY, count, multiple,
                                           timeout, web._monotonic(), env=env)
                if cancel_on is None:
                    return await work
                task = asyncio.ensure_future(work)
                while len(wire.dispatched) < cancel_on:
                    await asyncio.sleep(0.001)
                task.cancel()
                return await task
            finally:
                await manager.close()
    return asyncio.run(scenario())


def _urls(wire):
    return [url for request in wire.dispatched for url in request]


def test_the_next_candidate_is_read_after_a_slow_page(tmp_path):
    slow, fast = 'https://example.test/slow', 'https://example.test/valid'
    wire = Wire([slow])

    documents, warnings, failed = _read(tmp_path, wire, [slow, fast], count=2)

    assert _urls(wire) == [slow, fast]
    assert [document['url'] for document in documents] == [fast]
    assert warnings and failed is False
    assert wire.opened == wire.closed == 2


def test_an_expired_batch_is_consumed_and_never_sent_again(tmp_path):
    slow, quick, other, fresh = (f'https://example.test/{name}'
                                 for name in ('slow', 'quick', 'other', 'fresh'))
    wire = Wire([slow])

    documents, _, _ = _read(tmp_path, wire, [slow, quick, other, fresh], count=3, multiple=True,
                            policy=WebConstraints(read_required=True))

    # Um lote pode ter arrancado todos os pedidos: falta de resposta não prova que não foram.
    assert wire.dispatched == [[slow, quick, other], [fresh]]
    assert len(_urls(wire)) == len(set(_urls(wire)))
    assert [document['url'] for document in documents] == [fresh]


def test_an_expired_batch_without_new_candidates_ends_with_what_was_read(tmp_path):
    slow, quick = 'https://example.test/slow', 'https://example.test/quick'
    wire = Wire([slow])

    documents, warnings, failed = _read(tmp_path, wire, [slow, quick], count=2, multiple=True)

    assert wire.dispatched == [[slow, quick]]
    assert documents == [] and warnings and failed is True
    assert wire.opened == wire.closed == 1


def test_the_deadline_is_not_renewed_to_restart_the_client(tmp_path):
    slow, fast = 'https://example.test/slow', 'https://example.test/valid'
    wire = Wire([slow])

    documents, warnings, failed = _read(tmp_path, wire, [slow, fast], count=2, timeout=0.02)

    assert _urls(wire) == [slow]
    assert documents == [] and failed is True and warnings


def test_cancellation_during_recovery_leaves_no_transport_open(tmp_path):
    slow, other = 'https://example.test/slow', 'https://example.test/also-slow'
    wire = Wire([slow, other])

    with pytest.raises(asyncio.CancelledError):
        _read(tmp_path, wire, [slow, other], count=2, cancel_on=2)

    assert wire.opened == wire.closed

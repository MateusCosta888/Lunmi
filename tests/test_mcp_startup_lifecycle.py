"""MCP startup ownership and late scheduling, using only barriers and fake transports."""
from __future__ import annotations

import asyncio
import sys
import threading
import types
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.integrations import web_search as web
from app.integrations.web_state import WebEnvironment


@pytest.mark.parametrize("cancel", [True, False], ids=["cancelled", "failed"])
def test_manager_closes_a_starting_client_exactly_once(tmp_path: Path, cancel: bool) -> None:
    class StartingClient(web.MCPWebClient):
        def __init__(self, environment):
            super().__init__(environment)
            self.entered = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()
            self.closes = 0

        def _start_blocking(self):
            self.entered.set()
            try:
                assert self.release.wait(2), "Fixture startup was not released"
                raise web.WebSearchError("failed")
            finally:
                self.finished.set()

        def _close_blocking(self):
            self.closes += 1
            self.release.set()

    async def scenario():
        env = WebEnvironment(tmp_path, tmp_path / "fake-python.exe", tmp_path / "fake-worker.py")
        client = StartingClient(env)
        manager = web.WebSearchManager(emit=lambda state: None, state_root=tmp_path,
                                       session_id="fixture", environment=env,
                                       client_factory=lambda environment: client)
        task = asyncio.create_task(manager.run("web.search", {"query": "fixture query"}))
        try:
            assert await asyncio.to_thread(client.entered.wait, 1)
            assert manager._client is client
            if cancel:
                task.cancel()
            else:
                client.release.set()
            with pytest.raises(asyncio.CancelledError if cancel else web.WebSearchError):
                await task
            assert await asyncio.to_thread(client.finished.wait, 1)
            assert client.closes == 1 and manager._client is None
            await manager.close()
            assert client.closes == 1
        finally:
            client.release.set()
            await asyncio.to_thread(client.finished.wait, 1)
            await manager.close()

    asyncio.run(scenario())


def test_start_queued_after_close_cannot_create_an_owner(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_thread(**kwargs):
        pytest.fail("A closed client must not create an owner thread")

    client = web.MCPWebClient(WebEnvironment(tmp_path, tmp_path / "python", tmp_path / "worker"))
    client._close_blocking()
    monkeypatch.setattr(web.threading, "Thread", forbidden_thread)
    with pytest.raises(web.WebSearchError, match="failed"):
        client._start_blocking()


@pytest.mark.parametrize("close_first", [True, False], ids=["late-owner-refused", "normal-start"])
def test_delayed_owner_does_not_reopen_stdio_after_close(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close_first: bool) -> None:
    real_thread = threading.Thread
    queued = threading.Event()
    owners, opened, outcomes = [], [], []

    class DelayedOwner:
        def __init__(self, *, target, args, **kwargs):
            self.target, self.args, self.live = target, args, False
            owners.append(self)

        def start(self):
            self.live = True
            queued.set()

        def join(self, timeout):
            pass

        def is_alive(self):
            return self.live

    class Session:
        def __init__(self, read, write): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def initialize(self): pass

    @asynccontextmanager
    async def stdio(params, **kwargs):
        opened.append("fixture transport")
        yield object(), object()

    for name in ("mcp", "mcp.client", "mcp.client.stdio"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["mcp"].ClientSession = Session
    sys.modules["mcp.client.stdio"].StdioServerParameters = lambda **kwargs: types.SimpleNamespace(**kwargs)
    sys.modules["mcp.client.stdio"].stdio_client = stdio
    monkeypatch.setattr(web, "_worker_environment", lambda environment: ({}, tmp_path))
    monkeypatch.setattr(web.threading, "Thread", DelayedOwner)
    client = web.MCPWebClient(WebEnvironment(tmp_path, tmp_path / "python", tmp_path / "worker"),
                              idle_seconds=0.01, init_timeout=0.1)

    def start_waiter():
        try:
            client._start_blocking()
            outcomes.append("ready")
        except web.WebSearchError as exc:
            outcomes.append(exc.code)

    waiter = real_thread(target=start_waiter, daemon=True)
    waiter.start()
    try:
        assert queued.wait(1)
        assert client._thread_loop is None and client._queue is None and client._owner is None
        if close_first:
            client._close_blocking()
            assert client._thread is None
        try:
            owners[0].target(*owners[0].args)
        finally:
            owners[0].live = False
        waiter.join(1)
        assert not waiter.is_alive(), "Readiness waiter was left pending"
        assert len(opened) == (0 if close_first else 1)
        assert outcomes == (["failed"] if close_first else ["ready"])
    finally:
        if client._ready is not None and not client._ready.done():
            client._ready.set_exception(web.WebSearchError("failed"))
        waiter.join(1)
        client._close_blocking()

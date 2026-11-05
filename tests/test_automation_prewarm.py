"""A primeira ação não deve pagar o arranque do worker de automação (92 s num i3, 05/11)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.bridge.service import BridgeService


def owner(*, running: bool = False, closed: bool = False, prewarm: bool = True, sandbox: Any = None,
          fail: bool = False) -> Any:
    started: list[str] = []

    async def start() -> None:
        started.append("start")
        if fail:
            raise RuntimeError("no worker")

    loop = asyncio.new_event_loop()
    service = BridgeService.__new__(BridgeService)
    service._loop = loop
    service._closed = closed
    service.sandbox_dir = sandbox
    service.settings = SimpleNamespace(prewarm=prewarm)
    service.controller = SimpleNamespace(engine=SimpleNamespace(start=start))
    service._is_task_running = lambda: running  # type: ignore[method-assign]
    return service, loop, started


def run(service: Any, loop: asyncio.AbstractEventLoop) -> None:
    service.schedule_automation_prewarm(0)
    loop.call_later(0.05, loop.stop)
    loop.run_forever()
    loop.close()


def test_the_worker_starts_in_the_background_after_opening() -> None:
    service, loop, started = owner()
    run(service, loop)
    assert started == ["start"]


def test_a_failed_prewarm_stays_quiet_for_the_first_action_to_retry() -> None:
    service, loop, started = owner(fail=True)
    run(service, loop)
    assert started == ["start"] and service._automation_prewarm.exception() is None


def test_nothing_starts_during_a_task_after_closing_or_in_a_sandbox(tmp_path: Path) -> None:
    for kwargs in ({"running": True}, {"closed": True}, {"prewarm": False}, {"sandbox": tmp_path}):
        service, loop, started = owner(**kwargs)
        run(service, loop)
        assert started == []

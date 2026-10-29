"""Diagnostic disk failures preserve the real result; no provider or worker runs."""
from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.brain.contracts import BrainError, Capabilities, ErrorKind
from app.core.controller import TaskController
from app.llm import Completion
from app.memory import MemoryManager
from app.settings import Settings


@pytest.mark.parametrize("fail_brain", [False, True], ids=["success", "failed"])
def test_log_write_failure_preserves_result_without_replay(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_brain: bool) -> None:
    class Brain:
        capabilities = Capabilities.of(max_safe_prompt_chars=24000)
        calls = 0

        async def complete(self, messages):
            self.calls += 1
            if fail_brain:
                raise BrainError(ErrorKind.CONFIGURATION, "Fixture provider refused.")
            return Completion(json.dumps({"status": "FINISH", "message": "Fixture reply."}))

        async def close(self):
            pass

    original_open = Path.open
    log_attempts = []

    def disk_full(path, *args, **kwargs):
        if path == tmp_path / "logs" / "tasks.jsonl":
            log_attempts.append(path)
            raise OSError(errno.ENOSPC, "Fixture disk full")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", disk_full)
    settings = Settings(provider="custom", api_key="synthetic", model="fixture",
                        base_url="http://127.0.0.1:1/v1", prewarm=False, fast_path=False,
                        app_index=False, app_search=False, collect_metrics=False,
                        logs_path=tmp_path / "logs", output_path=tmp_path / "output", state_root=tmp_path)
    states = []
    controller = TaskController(settings, on_state=states.append,
                                memory=MemoryManager(tmp_path / "memory.sqlite3"))
    brain = Brain()
    controller.brain = brain
    controller.engine.start = AsyncMock(side_effect=AssertionError("No real worker"))

    async def scenario():
        try:
            result = await controller.run("Explain a star.", conversation=True)
            assert result.status == ("FAILED" if fail_brain else "SUCCESS")
            assert result.message == ("Fixture provider refused." if fail_brain else "Fixture reply.")
            assert states[-1] == ("Failed" if fail_brain else "Completed")
            assert result.actions_executed == 0 and brain.calls == 1
            assert len(log_attempts) == 1
            assert not controller.active and controller.current_task is None
            controller.engine.start.assert_not_awaited()
        finally:
            await controller.close()

    asyncio.run(scenario())

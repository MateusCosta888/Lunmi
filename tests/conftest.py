from __future__ import annotations

from pathlib import Path

import pytest

from app.memory import MemoryManager


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Existing controller tests must never create/open the user's real memory database."""
    original = MemoryManager.__init__
    opened: list[MemoryManager] = []

    def initialize(self: MemoryManager, path: Path | None = None) -> None:
        original(self, path if path is not None else tmp_path / 'controller-memory.sqlite3')
        opened.append(self)

    monkeypatch.setattr(MemoryManager, '__init__', initialize)
    yield
    for memory in opened:
        memory.close()

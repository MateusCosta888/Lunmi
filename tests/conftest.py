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


@pytest.fixture(autouse=True)
def _no_real_recycle_bin(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests delete for real but never fill the developer's Recycle Bin; one test uses it."""
    if request.node.get_closest_marker("real_recycle_bin"):
        return
    import shutil

    from app.computer import windows_tools

    def remove(path):  # type: ignore[no-untyped-def]
        shutil.rmtree(path) if path.is_dir() else path.unlink()

    monkeypatch.setattr(windows_tools, "_to_recycle_bin", remove)

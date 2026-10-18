from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.computer.windows_tools import WindowsTools, executable_path


def test_filesystem_operations_have_real_postconditions(tmp_path: Path) -> None:
    async def scenario() -> None:
        approvals: list[str] = []
        async def approve(description: str) -> bool:
            approvals.append(description)
            return True

        tools = WindowsTools(object(), approve)
        folder = tmp_path / "Projeto"
        assert (await tools.filesystem("mkdir", str(folder)))["verified"]
        source = folder / "teste.txt"
        text = "Olá — texto literal ^+%{}\nSegunda linha"
        assert (await tools.filesystem("create_file", str(source), text=text))["verified"]
        assert source.read_text(encoding="utf-8") == text
        assert (await tools.filesystem("list", str(folder)))["entries"] == [
            {"name": "teste.txt", "directory": False}]
        assert (await tools.filesystem("search", str(folder), pattern="*.txt"))["entries"] == [str(source)]
        assert (await tools.filesystem("exists", str(source)))["exists"]
        assert (await tools.filesystem("metadata", str(source)))["size"] == len(source.read_bytes())
        copied = folder / "copia.txt"
        assert (await tools.filesystem("copy", str(source), str(copied)))["verified"]
        assert copied.read_bytes() == source.read_bytes()
        copy_folder = tmp_path / "Copias"
        copy_folder.mkdir()
        copy_result = await tools.filesystem("copy", str(source), str(copy_folder))
        assert copy_result["destination"] == str(copy_folder / source.name)
        assert copy_result["source_sha256"] == copy_result["destination_sha256"]
        assert (copy_folder / source.name).read_bytes() == source.read_bytes()
        moved = folder / "movido.txt"
        assert (await tools.filesystem("move", str(copied), str(moved)))["verified"]
        assert not copied.exists() and moved.read_bytes() == source.read_bytes()
        renamed = folder / "documento.txt"
        assert (await tools.filesystem("rename", str(moved), str(renamed)))["verified"]
        assert not moved.exists() and renamed.read_bytes() == source.read_bytes()
        assert approvals == []
        assert (await tools.filesystem("delete", str(renamed)))["verified"]
        assert not renamed.exists() and len(approvals) == 1
        assert (await tools.filesystem("create_file", str(source), text="Novo texto"))["verified"]
        assert len(approvals) == 2
        assert source.read_text(encoding="utf-8") == "Novo texto"
        assert (await tools.filesystem("exists", str(renamed)))["exists"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["create_file", "copy", "move", "rename", "delete"])
def test_filesystem_denial_preserves_source_and_destination(tmp_path: Path, operation: str) -> None:
    async def scenario() -> None:
        approvals: list[str] = []
        async def deny(description: str) -> bool:
            approvals.append(description)
            return False
        tools = WindowsTools(object(), deny)
        source, target = tmp_path / "origem.txt", tmp_path / "destino.txt"
        source.write_text("origem", encoding="utf-8")
        target.write_text("destino", encoding="utf-8")
        with pytest.raises(PermissionError):
            await tools.filesystem(operation, str(source), str(target), text="não autorizado")
        assert source.read_text(encoding="utf-8") == "origem"
        assert target.read_text(encoding="utf-8") == "destino"
        assert len(approvals) == 1 and tools.cancelled
        assert tools.evidence == []
    asyncio.run(scenario())


def test_copy_to_directory_denial_preserves_the_actual_target(tmp_path: Path) -> None:
    async def scenario() -> None:
        asked: list[str] = []
        async def deny(message: str) -> bool:
            asked.append(message)
            return False
        source = tmp_path / "documento.txt"
        folder = tmp_path / "destino"
        folder.mkdir()
        source.write_bytes(b"origem")
        target = folder / source.name
        target.write_bytes(b"usuario")
        with pytest.raises(PermissionError):
            await WindowsTools(object(), deny).filesystem("copy", str(source), str(folder))
        assert str(target) in asked[0]
        assert target.read_bytes() == b"usuario" and source.read_bytes() == b"origem"
    asyncio.run(scenario())


def test_executable_path_uses_windows_app_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import app.computer.windows_tools as module
    executable = tmp_path / "common-win32.exe"
    executable.write_bytes(b"test-owned-not-executed")
    class Key:
        def __enter__(self) -> Key:
            return self
        def __exit__(self, *args: object) -> None:
            pass
    monkeypatch.setattr(module.shutil, "which", lambda target: None)
    monkeypatch.setattr(module.winreg, "OpenKey", lambda *args: Key())
    monkeypatch.setattr(module.winreg, "QueryValueEx", lambda *args: (str(executable), 1))
    assert executable_path("common-win32.exe") == str(executable)
    assert executable_path("common-win32.exe --flag") is None


def test_verified_close_is_terminal_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.computer.windows_tools as module
    monkeypatch.setattr(module.user32, "IsWindow", lambda handle: 0)
    async def deny(_: str) -> bool:
        return False
    state = type("State", (), {"selected_app_window": type("Window", (), {"handle": 17})()})()
    tools = WindowsTools(state, deny)
    tools.evidence.append({"operation": "close", "handle": 17, "verified": True})
    tools.pending_ui = True
    async def scenario() -> None:
        result = await tools.server().call_tool("verify_window", {})
        data = result.structured_content or result.data
        assert data["verified"] and data["closed"] and not tools.pending_ui
        with pytest.raises(Exception, match="Janela selecionada"):
            await tools.server().call_tool("verify_window", {"expected_text": "texto"})
    asyncio.run(scenario())


def test_create_file_revalidates_the_approved_target_before_writing(tmp_path: Path) -> None:
    target = tmp_path / "synthetic.txt"
    target.write_text("before", encoding="utf-8")
    tools = WindowsTools.__new__(WindowsTools)
    tools.evidence = []

    async def approve(_: str) -> bool:
        target.write_text("concurrent-change", encoding="utf-8")
        return True

    tools.approval = approve

    async def scenario() -> None:
        with pytest.raises((PermissionError, ValueError)):
            await tools.filesystem("create_file", str(target), text="replacement")
        assert target.read_text(encoding="utf-8") == "concurrent-change"

    asyncio.run(scenario())


def test_delete_revalidates_a_replaced_directory(tmp_path: Path) -> None:
    target = tmp_path / "approved-directory"
    target.mkdir()
    moved = tmp_path / "original-moved"
    tools = WindowsTools.__new__(WindowsTools)
    tools.evidence = []

    async def approve(_: str) -> bool:
        target.rename(moved)
        target.mkdir()
        (target / "new-unapproved.txt").write_text("new object", encoding="utf-8")
        return True

    tools.approval = approve

    async def scenario() -> None:
        with pytest.raises((PermissionError, ValueError)):
            await tools.filesystem("delete", str(target))
        assert (target / "new-unapproved.txt").is_file()

    asyncio.run(scenario())


def test_generic_overwrite_does_not_inherit_the_one_mib_document_limit(tmp_path: Path) -> None:
    target = tmp_path / "large.txt"
    target.write_text("x" * (1_048_576 + 1), encoding="utf-8")
    tools = WindowsTools.__new__(WindowsTools)
    tools.evidence = []

    async def approve(_: str) -> bool:
        return True

    tools.approval = approve

    async def scenario() -> None:
        result = await tools.filesystem("create_file", str(target), text="replacement")
        assert result["verified"]
        assert target.read_text(encoding="utf-8") == "replacement"

    asyncio.run(scenario())


class _RecordingScan:
    """Wrap a real scandir iterator so the test can prove it was closed."""

    def __init__(self, iterator: object) -> None:
        self._iterator = iterator
        self.closed = False

    def __enter__(self) -> _RecordingScan:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    def __iter__(self) -> _RecordingScan:
        return self

    def __next__(self) -> object:
        return next(self._iterator)  # type: ignore[call-overload]

    def close(self) -> None:
        self.closed = True
        self._iterator.close()  # type: ignore[attr-defined]


def _listing_tool() -> WindowsTools:
    tools = WindowsTools.__new__(WindowsTools)
    tools.evidence = []
    return tools


def test_list_stops_at_the_result_limit_and_reports_partial_coverage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.computer.windows_tools as module
    for index in range(40):
        (tmp_path / f"item-{index}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(module, "FILESYSTEM_RESULT_LIMIT", 10)
    opened: list[_RecordingScan] = []
    real_scandir = module.os.scandir

    def recording(path: str) -> _RecordingScan:
        scan = _RecordingScan(real_scandir(path))
        opened.append(scan)
        return scan

    monkeypatch.setattr(module.os, "scandir", recording)
    result = asyncio.run(_listing_tool().filesystem("list", str(tmp_path)))
    assert len(result["entries"]) == 10
    assert result["visited"] == 10            # the work stopped with the answer
    assert result["complete"] is False and result["truncated"] is True
    assert result["limit_reason"] == "result_limit"
    assert result["verified"] is True         # the query ran; coverage is partial
    assert opened and all(scan.closed for scan in opened)


def test_search_without_matches_stops_at_the_visit_budget(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.computer.windows_tools as module
    for index in range(80):
        (tmp_path / f"noise-{index}.dat").write_text("x", encoding="utf-8")
    monkeypatch.setattr(module, "FILESYSTEM_VISIT_LIMIT", 25)
    opened: list[_RecordingScan] = []
    real_scandir = module.os.scandir

    def recording(path: str) -> _RecordingScan:
        scan = _RecordingScan(real_scandir(path))
        opened.append(scan)
        return scan

    monkeypatch.setattr(module.os, "scandir", recording)
    result = asyncio.run(_listing_tool().filesystem("search", str(tmp_path), pattern="*.txt"))
    assert result["entries"] == []
    assert result["visited"] == 25            # non-matching entries counted
    assert result["complete"] is False and result["truncated"] is True
    assert result["limit_reason"] == "visit_limit"
    assert result["verified"] is True
    assert opened and all(scan.closed for scan in opened)


def test_search_stops_at_the_time_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.computer.windows_tools as module
    for index in range(20):
        (tmp_path / f"item-{index}.dat").write_text("x", encoding="utf-8")
    monkeypatch.setattr(module, "FILESYSTEM_SCAN_SECONDS", .6)
    ticks = {'now': 0.0}

    def clock() -> float:
        ticks['now'] += .25
        return ticks['now']

    monkeypatch.setattr(module, "_fs_now", clock)
    result = asyncio.run(_listing_tool().filesystem("search", str(tmp_path), pattern="*.txt"))
    assert result["entries"] == []
    assert result["visited"] < 20
    assert result["complete"] is False and result["truncated"] is True
    assert result["limit_reason"] == "time_limit"


def test_small_listing_and_search_declare_exhaustive_coverage(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.log").write_text("b", encoding="utf-8")
    tools = _listing_tool()
    listing = asyncio.run(tools.filesystem("list", str(tmp_path)))
    assert len(listing["entries"]) == 2
    assert listing["complete"] is True and listing["truncated"] is False
    assert listing["limit_reason"] == "" and listing["visited"] == 2
    search = asyncio.run(tools.filesystem("search", str(tmp_path), pattern="*.txt"))
    assert search["entries"] == [str(tmp_path / "a.txt")]
    assert search["complete"] is True and search["truncated"] is False
    assert search["limit_reason"] == "" and search["visited"] == 2


def _make_directory_link(link: Path, target: Path) -> bool:
    if os.name == "nt":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True)
        return done.returncode == 0
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        return False
    return True


def test_search_never_follows_a_directory_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.txt").write_text("não deve aparecer", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    (root / "visible.txt").write_text("visível", encoding="utf-8")
    if not _make_directory_link(root / "linked", outside):
        pytest.skip("junction/symlink não autorizado neste ambiente")
    result = asyncio.run(_listing_tool().filesystem("search", str(root), pattern="*.txt"))
    assert result["entries"] == [str(root / "visible.txt")]
    assert str(outside / "hidden.txt") not in result["entries"]
    assert result["complete"] is True


def test_an_unreadable_subtree_is_reported_as_partial_coverage(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.computer.windows_tools as module
    (tmp_path / "visible.txt").write_text("visível", encoding="utf-8")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "extra.txt").write_text("inacessível", encoding="utf-8")
    real_scandir = module.os.scandir

    def guarded(path: str) -> object:
        if Path(path) == locked:
            raise PermissionError("synthetic denial")
        return real_scandir(path)

    monkeypatch.setattr(module.os, "scandir", guarded)
    result = asyncio.run(_listing_tool().filesystem("search", str(tmp_path), pattern="*.txt"))
    assert result["entries"] == [str(tmp_path / "visible.txt")]
    assert result["complete"] is False and result["truncated"] is False
    assert result["limit_reason"] == "access_limited"
    assert result["errors"] == 1


def test_a_cancelled_search_keeps_no_partial_success(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.computer.windows_tools as module
    for index in range(20):
        (tmp_path / f"item-{index}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(module, "FILESYSTEM_YIELD_EVERY", 1)
    real_scandir = module.os.scandir

    class SlowScan(_RecordingScan):
        def __next__(self) -> object:
            time.sleep(.005)
            return super().__next__()

    monkeypatch.setattr(module.os, "scandir", lambda path: SlowScan(real_scandir(path)))
    tools = _listing_tool()

    async def scenario() -> None:
        task = asyncio.create_task(tools.filesystem("search", str(tmp_path), pattern="*.txt"))
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert tools.evidence == []

    asyncio.run(scenario())


def test_a_partial_listing_message_never_reads_as_exhaustive(
        monkeypatch: pytest.MonkeyPatch) -> None:
    vendor = Path(__file__).resolve().parents[1] / "vendor" / "UFO"
    monkeypatch.syspath_prepend(str(vendor))
    monkeypatch.chdir(vendor)
    from app.computer.ufo_runtime import UFORuntime
    partial = UFORuntime.bootstrap_message("filesystem", {
        "operation": "search", "entries": [], "complete": False, "truncated": True,
        "visited": 5000, "limit_reason": "visit_limit", "errors": 0, "verified": True})
    assert "cobertura parcial" in partial
    assert "5000" in partial
    assert "não prova que não existe" in partial
    complete = UFORuntime.bootstrap_message("filesystem", {
        "operation": "list", "entries": [{"name": "a.txt", "directory": False}],
        "complete": True, "truncated": False, "visited": 1, "limit_reason": "",
        "errors": 0, "verified": True})
    assert "cobertura parcial" not in complete
    assert "verificada" in complete


def test_unreadable_entry_metadata_is_never_traversed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.computer.windows_tools as module
    opened: list[str] = []

    def denied_stat(**kwargs: object) -> object:
        raise PermissionError("synthetic metadata denial")

    class Scan:
        def __init__(self, values: object) -> None:
            self.values = iter(values)

        def __iter__(self) -> Scan:
            return self

        def __next__(self) -> object:
            return next(self.values)

        def __enter__(self) -> Scan:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    def scan(path: str) -> Scan:
        opened.append(str(path))
        child = tmp_path / "unproven-child"
        values = [SimpleNamespace(name="unknown", path=str(child),
                                  is_dir=lambda **kwargs: True,
                                  is_symlink=lambda: False, stat=denied_stat)]
        return Scan(values if len(opened) == 1 else [])

    monkeypatch.setattr(module.os, "scandir", scan)
    result = asyncio.run(module._scan_filesystem(tmp_path, "no-match"))
    assert opened == [str(tmp_path)]
    assert result["complete"] is False and result["errors"] == 1
    assert result["limit_reason"] == "access_limited"

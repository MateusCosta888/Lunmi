from __future__ import annotations

import asyncio
from pathlib import Path

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

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.controller import TaskController
from app.llm import Plan, UnsupportedTask
from app.safety import SafetyError, check_approved_file, file_version, save_target, verify_saved_file
from app.settings import Settings
from app.computer.worker import literal_keys


@pytest.mark.parametrize("operation,text,path", [
    ("open", "unexpected", ""), ("open", "", "hello.txt"),
    ("write", "", ""), ("write", "hi", "hello.txt"),
    ("save", "hi", ""), ("save", "", "hello.txt"), ("write", "\x00", ""),
])
def test_incoherent_plan_rejected(operation: str, text: str, path: str) -> None:
    with pytest.raises(UnsupportedTask):
        Plan(supported=True, application="notepad", operation=operation, reason="", text=text, save_path=path).authorize()


@pytest.mark.parametrize("name", ["../outside.txt", "CON.txt", "aux.txt", "hello.txt:stream",
    "danger.exe", "trailing.txt ", "missing/hello.txt", "hello?.txt", "a\x00.txt"])
def test_save_paths_fail_closed(tmp_path: Path, name: str) -> None:
    with pytest.raises(SafetyError):
        save_target(tmp_path, name)


def test_relative_save_path(tmp_path: Path) -> None:
    assert save_target(tmp_path, "hello.txt") == tmp_path / "hello.txt"


def test_changed_file_requires_new_approval(tmp_path: Path) -> None:
    path = tmp_path / "hello.txt"
    path.write_text("old", encoding="utf-8")
    approved = file_version(path)
    assert check_approved_file(tmp_path, str(path), approved) == path
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(SafetyError):
        check_approved_file(tmp_path, str(path), approved)


def test_hard_linked_file_cannot_be_overwritten(tmp_path: Path) -> None:
    original = tmp_path / "original.txt"
    alias = tmp_path / "alias.txt"
    original.write_text("shared user data", encoding="utf-8")
    try:
        alias.hardlink_to(original)
    except OSError as exc:
        pytest.skip(f"Filesystem does not permit hard links here: {exc}")
    with pytest.raises(SafetyError, match="hard links"):
        file_version(alias)


def test_newly_created_file_not_implicitly_authorized(tmp_path: Path) -> None:
    path = tmp_path / "hello.txt"
    path.write_text("user-owned", encoding="utf-8")
    with pytest.raises(SafetyError):
        check_approved_file(tmp_path, str(path), None)


def test_file_content_not_exists_alone_proves_success(tmp_path: Path) -> None:
    path = tmp_path / "hello.txt"
    path.write_bytes(b"wrong")
    assert not verify_saved_file(path, "Hello World")["file_verified"]
    path.write_bytes("Olá\r\nWorld".encode("utf-8-sig"))
    assert verify_saved_file(path, "Olá\nWorld")["file_verified"]


def test_literal_keys_never_execute_document_hotkeys() -> None:
    assert literal_keys("^a{DELETE}+%()\n\t") == "{^}a{{}DELETE{}}{+}{%}{(}{)}{ENTER}{TAB}"


def test_overwrite_denial_does_not_start_computer(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "hello.txt"
        path.write_text("user-owned", encoding="utf-8")
        requests: list[str] = []
        async def deny(message: str) -> bool:
            requests.append(message)
            return False
        c = TaskController(Settings(output_path=tmp_path, logs_path=tmp_path, collect_metrics=False), confirm=deny)
        plan = Plan(supported=True, application="notepad", operation="save", text="Hello World", save_path="hello.txt", reason="")
        result = await c.run("test", engine_only=True, engine_plan=plan)
        assert result.status == "CANCELLED"
        assert result.actions == result.steps == 0
        assert str(path) in requests[0]
        assert "SHA256" in requests[0]
        assert path.read_text() == "user-owned"
        assert c.engine.process is None
        await c.close()
    asyncio.run(scenario())


def test_step_limit_blocks_entire_plan(tmp_path: Path) -> None:
    async def scenario() -> None:
        c = TaskController(Settings(output_path=tmp_path, logs_path=tmp_path, max_steps=1, collect_metrics=False))
        plan = Plan(supported=True, application="notepad", operation="write", text="Hello", reason="")
        result = await c.run("test", engine_only=True, engine_plan=plan)
        assert result.status == "FAILED"
        assert result.actions == 0
        assert c.engine.process is None
    asyncio.run(scenario())

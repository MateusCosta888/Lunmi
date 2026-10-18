"""The authorisation policy: what is asked about, in which mode, and why.

Measured 2026-10-09 (the reported CASO A, Steam): confirming a search with Enter asked for
authorisation, so a normal task turned into a dialog — and when the answer did not travel,
the search never happened. The opposite mistake is worse: publishing text in a message
composer must keep asking, in both modes. These tests hold the three levels apart, and the
mode's only lever: it answers the question the runtime could not decide by itself.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# The runtime belongs to the pinned UFO revision; importing it is what needs it on the path.
sys.path.insert(0, str(ROOT / "vendor" / "UFO"))

from app import settings as settings_module  # noqa: E402
from app.computer.approval import (  # noqa: E402
    AUTO,
    HIGH,
    LOW,
    MANUAL,
    UNKNOWN,
    Verdict,
    click_verdict,
    keyboard_verdict,
    set_text_verdict,
    submit_keys,
    uia_action_verdict,
    unknown_tool_verdict,
)
from app.settings import Settings, _env_flag, _load_approval, save_auto_approve  # noqa: E402


@contextlib.contextmanager
def runtime_module() -> Iterator[Any]:
    """Import the real runtime the way the worker does: with UFO's own config as cwd.

    The pinned revision loads its configuration relative to the working directory, so the
    import only succeeds from there. Nothing below calls ``__init__`` of the runtime, so
    the directory only matters for the import itself.
    """
    original = os.getcwd()
    os.chdir(ROOT / "vendor" / "UFO")
    try:
        import app.computer.ufo_runtime as module
        yield module
    finally:
        os.chdir(original)


def test_the_default_is_manual() -> None:
    assert Settings().auto_approve is False


def test_only_high_asks_in_both_modes_and_unknown_only_asks_when_manual() -> None:
    assert Verdict(HIGH).needs_consent(MANUAL) and Verdict(HIGH).needs_consent(AUTO)
    assert not Verdict(LOW).needs_consent(MANUAL) and not Verdict(LOW).needs_consent(AUTO)
    assert Verdict(UNKNOWN).needs_consent(MANUAL) is True
    assert Verdict(UNKNOWN).needs_consent(AUTO) is False


@pytest.mark.parametrize("keys", ["{ENTER}", "{VK_RETURN}", "enter", "\n"])
def test_enter_is_recognised_in_every_spelling_it_arrives_in(keys: str) -> None:
    assert submit_keys(keys)


def test_enter_in_a_query_field_is_normal_and_is_never_asked_about() -> None:
    """The reported case: a search confirmation is not a risky action."""
    verdict = keyboard_verdict(keys="{ENTER}", control_type="Edit", control_label="Search")
    assert verdict.level == LOW
    assert not verdict.needs_consent(MANUAL) and not verdict.needs_consent(AUTO)


def test_enter_that_confirms_text_this_task_typed_is_normal_even_in_an_unnamed_field() -> None:
    verdict = keyboard_verdict(keys="{ENTER}", control_type="Edit", control_label="",
                              typed_into_control=True)
    assert verdict.level == LOW and not verdict.needs_consent(MANUAL)


def test_enter_in_a_message_composer_asks_in_both_modes() -> None:
    """Typing into a composer and pressing Enter publishes: auto-approval does not cover it."""
    for kind, label in (("Document", ""), ("Edit", "Message"), ("Edit", "Escreva uma mensagem")):
        verdict = keyboard_verdict(keys="{ENTER}", control_type=kind, control_label=label,
                                   typed_into_control=True)
        assert verdict.level == HIGH, (kind, label)
        assert verdict.needs_consent(AUTO) and verdict.needs_consent(MANUAL)


def test_enter_in_an_unidentified_field_is_not_decidable() -> None:
    verdict = keyboard_verdict(keys="{ENTER}", control_type="Edit", control_label="")
    assert verdict.level == UNKNOWN
    assert verdict.needs_consent(MANUAL) and not verdict.needs_consent(AUTO)


@pytest.mark.parametrize("keys", ["^s", "^+s", "^w", "^q", "^x", "%{f4}", "{DELETE}"])
def test_keys_that_save_close_or_delete_ask_in_both_modes(keys: str) -> None:
    verdict = keyboard_verdict(keys=keys, control_type="Edit", control_label="Documento",
                              typed_into_control=True)
    assert verdict.level == HIGH and verdict.needs_consent(AUTO)


@pytest.mark.parametrize("keys", ["^a", "^c", "^f", "^n", "{TAB}", "{ESC}", "{DOWN}"])
def test_reading_and_navigating_keys_are_normal(keys: str) -> None:
    assert keyboard_verdict(keys=keys, control_type="Edit", control_label="").level == LOW


@pytest.mark.parametrize("keys", ["^v", "^p", "{F5}"])
def test_shortcuts_without_a_known_effect_are_answered_by_the_mode(keys: str) -> None:
    verdict = keyboard_verdict(keys=keys, control_type="Edit", control_label="")
    assert verdict.level == UNKNOWN and not verdict.needs_consent(AUTO)


def test_typing_text_is_normal() -> None:
    assert keyboard_verdict(keys="gta 5", control_type="Edit", control_label="").level == LOW


def test_a_control_that_calls_itself_destructive_is_sensitive() -> None:
    for label in ("Excluir", "Enviar", "Salvar", "Fechar", "Delete this"):
        assert click_verdict(control_label=label).level == HIGH, label


def test_a_plain_navigation_click_is_normal_and_a_setting_click_is_not() -> None:
    assert click_verdict(control_label="Pesquisar", control_type="Button").level == LOW
    assert click_verdict(control_label="Página inicial", control_type="Hyperlink").level == LOW
    for kind in ("CheckBox", "RadioButton", "Slider", "Spinner"):
        assert click_verdict(control_label="Ativar", control_type=kind).level == HIGH, kind


def test_interacting_with_a_command_tool_is_sensitive_however_it_is_done(
        monkeypatch: pytest.MonkeyPatch) -> None:
    with runtime_module() as module:
        for name in ("cmd.exe", "powershell.exe", "regedit.exe"):
            runtime = runtime_with(module, process=name, monkeypatch=monkeypatch)
            assert runtime._risk_verdict("keyboard_input", {"keys": "dir"}).level == HIGH
            assert runtime._risk_verdict("click_input", {}).level == HIGH


def test_uia_operations_are_read_reveal_select_or_change() -> None:
    assert uia_action_verdict(operation="expand").level == LOW
    assert uia_action_verdict(operation="scroll", control_type="List").level == LOW
    assert uia_action_verdict(operation="select", control_type="TabItem").level == LOW
    assert uia_action_verdict(operation="select", control_type="ListItem").level == LOW
    assert uia_action_verdict(operation="toggle", control_type="CheckBox").level == HIGH
    assert uia_action_verdict(operation="select", control_type="ComboBox").level == UNKNOWN
    assert uia_action_verdict(operation="", control_type="Button").level == UNKNOWN


@pytest.mark.parametrize("operation", ["invoke", "default_action", "click_input"])
def test_native_dispatch_cannot_bypass_sensitive_control_labels(operation: str) -> None:
    with runtime_module() as module:
        runtime = runtime_with(module, control_type="Button", label="Enviar")
        verdict = runtime._risk_verdict("uia_action", {"id": "1", "name": "Enviar", "operation": operation})
        assert verdict.level == HIGH and verdict.needs_consent(AUTO)


def test_writing_asks_only_when_it_replaces_content_this_task_did_not_create() -> None:
    assert set_text_verdict(replaces_content=True, owned=False).level == HIGH
    assert set_text_verdict(replaces_content=True, owned=True).level == LOW
    assert set_text_verdict(replaces_content=False, owned=False).level == LOW


@pytest.mark.parametrize("label, keys", [("Excluir", "x"), ("Enviar", "%e")])
def test_menu_access_keys_cannot_bypass_sensitive_labels(label: str, keys: str) -> None:
    with runtime_module() as module:
        runtime = runtime_with(module, control_type="MenuItem", label=label)
        verdict = runtime._risk_verdict("keyboard_input", {"id": "1", "name": label, "keys": keys})
        assert verdict.level == HIGH and verdict.needs_consent(AUTO)


@pytest.mark.parametrize("operation", ["keyboard_input", "click_input"])
def test_generic_wrapper_preserves_observed_menu_risk(operation: str) -> None:
    with runtime_module() as module:
        runtime = runtime_with(module, control_type="Custom", label="Excluir")
        runtime.tools.uia = SimpleNamespace(references={
            "1": SimpleNamespace(meta={"type": "MenuItem", "name": "Excluir"})})
        verdict = runtime._risk_verdict(
            operation if operation == "keyboard_input" else "uia_action",
            {"id": "1", "name": "Excluir", "keys": "x", "operation": operation})
        assert verdict.level == HIGH and verdict.needs_consent(AUTO)


def test_an_unknown_tool_is_not_decidable_instead_of_silently_allowed() -> None:
    verdict = unknown_tool_verdict("new_tool")
    assert verdict.level == UNKNOWN and verdict.needs_consent(MANUAL)
    assert not verdict.needs_consent(AUTO)


def test_tools_with_their_own_consent_are_not_re_asked_here() -> None:
    with runtime_module() as module:
        runtime = runtime_with(module)
        for name in ("filesystem", "window_action", "launch_application", "uia_verify", "texts"):
            assert runtime._risk_verdict(name, {}).level == LOW, name


def runtime_with(module: Any, *, process: str | None = None, window: bool = True,
                 written: bool = False, control_type: str = "Edit",
                 label: str = "", text: str = "",
                 monkeypatch: pytest.MonkeyPatch | None = None) -> Any:
    """A runtime assembled for one decision: no UFO process, no desktop.

    ``psutil_name`` is the only thing that reaches out of the process, so it is patched
    (never left patched) when a test needs a specific application identity.
    """
    control = SimpleNamespace(element_info=SimpleNamespace(control_type=control_type),
                              get_value=lambda: text)
    window_object = (SimpleNamespace(handle=7, process_id=lambda: 0) if window else None)
    runtime = module.UFORuntime.__new__(module.UFORuntime)
    runtime.state = SimpleNamespace(control_dict={"1": control}, selected_app_window=window_object)
    runtime.tools = SimpleNamespace(written_controls={"1"} if written else set(),
                                    owned_documents={})
    if process is not None:
        assert monkeypatch is not None, "patching psutil_name needs a monkeypatch fixture"
        monkeypatch.setattr(module, "psutil_name", lambda _: process)
    return runtime


def test_the_verdict_composition_records_what_was_decided_and_whether_it_asked() -> None:
    with runtime_module() as module:
        runtime = runtime_with(module, written=True)
        runtime.approval_mode = AUTO
        asyncio.run(runtime.check_risk(SimpleNamespace(tool_name="keyboard_input",
                                                      parameters={"id": "1", "name": "",
                                                                  "keys": "{ENTER}"})))
        assert runtime.approvals == [{"tool": "keyboard_input", "level": LOW,
                                      "reason": "Enter confirma o texto já digitado neste campo",
                                      "asked": False, "mode": AUTO, "identifier": "1"}]


def test_a_sensitive_action_asks_even_under_auto_approval() -> None:
    with runtime_module() as module:
        from app.computer.windows_tools import WindowsTools
        runtime = runtime_with(module)
        runtime.approval_mode = AUTO
        asked: list[str] = []

        async def confirm(message: str) -> bool:
            asked.append(message)
            return True

        runtime.tools = WindowsTools(runtime.state, confirm)
        asyncio.run(runtime.check_risk(SimpleNamespace(tool_name="keyboard_input",
                                                      parameters={"id": "1", "name": "",
                                                                  "keys": "^s"})))
        assert len(asked) == 1 and "salvar/fechar/excluir" in asked[0]


def test_the_mode_is_persisted_and_survives_a_reload(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module, "ROOT", tmp_path)
    assert _load_approval() is False
    save_auto_approve(True)
    assert _load_approval() is True
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("AUTO_APPROVE", raising=False)
    assert Settings.load().auto_approve is True
    # An explicit environment flag wins over the saved preference, in both directions.
    monkeypatch.setenv("AUTO_APPROVE", "false")
    assert Settings.load().auto_approve is False
    assert _env_flag("AUTO_APPROVE", True) is False
    assert _env_flag("AUTO_APPROVE_ABSENT", True) is True


def test_a_broken_preference_file_never_widens_what_runs_without_asking(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module, "ROOT", tmp_path)
    (tmp_path / ".local").mkdir()
    for content in ("{", '{"auto_approve": "yes"}', "[]", '{"auto_approve": false}'):
        (tmp_path / ".local/approval.json").write_text(content, encoding="utf-8")
        assert _load_approval() is False, content


def test_the_controller_switches_modes_and_saves_the_choice(tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_module, "ROOT", tmp_path)
    from app.core.controller import TaskController
    controller = TaskController(Settings(state_root=tmp_path))
    assert controller.approval_mode == MANUAL
    controller.set_auto_approve(True)
    assert controller.approval_mode == AUTO and controller.settings.auto_approve is True
    assert _load_approval() is True
    controller.set_auto_approve(False)
    assert controller.approval_mode == MANUAL and _load_approval() is False

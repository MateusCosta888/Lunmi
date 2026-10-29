"""Task-card preference at the real parse/save/bridge boundary, without starting workers."""
from pathlib import Path

import pytest

from app.bridge.protocol import ProtocolError
from app.bridge.service import BridgeService
from app.ui import ui_preferences


def prefs_service(tmp_path: Path) -> BridgeService:
    # These handlers only depend on prefs/prefs_path; no provider, thread or personal data.
    service = BridgeService.__new__(BridgeService)
    service.prefs_path = tmp_path / "lunmi-ui.json"
    service.prefs = ui_preferences.parse({"theme": "light", "reduce_motion": True, "onboarding_done": True})
    return service


def test_task_result_card_is_off_for_new_and_legacy_preferences() -> None:
    assert ui_preferences.DEFAULT.show_task_results is False
    assert ui_preferences.parse({"theme": "light"}).show_task_results is False


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_tolerant_disk_reader_requires_literal_true(value: object) -> None:
    assert ui_preferences.parse({"show_task_results": value}).show_task_results is False


def test_task_card_roundtrip_and_unrelated_updates_preserve_other_preferences(tmp_path: Path) -> None:
    service = prefs_service(tmp_path)
    assert service._handle_ui_preferences_get()["show_task_results"] is False
    service.prefs = ui_preferences.parse({"theme": "light", "reduce_motion": True, "onboarding_done": True})
    saved = service._handle_ui_preferences_set({"show_task_results": True})
    assert saved["show_task_results"] is True
    assert saved["reduce_motion"] is True and saved["onboarding_done"] is True
    assert service._handle_ui_preferences_get()["show_task_results"] is True
    service._handle_ui_preferences_set({"theme": "redvelvet"})
    assert service._handle_ui_preferences_get()["show_task_results"] is True
    service._handle_ui_preferences_set({"show_task_results": False})
    assert ui_preferences.load(service.prefs_path).show_task_results is False


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_bridge_rejects_non_boolean_without_changing_saved_value(tmp_path: Path, value: object) -> None:
    service = prefs_service(tmp_path)
    assert ui_preferences.save(service.prefs_path, service.prefs)
    before = service.prefs_path.read_bytes()
    with pytest.raises(ProtocolError) as error:
        service._handle_ui_preferences_set({"show_task_results": value})
    assert error.value.code == "invalid_payload"
    assert service.prefs_path.read_bytes() == before


def test_failed_write_does_not_claim_that_the_card_was_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = prefs_service(tmp_path)
    monkeypatch.setattr(ui_preferences, "save", lambda *_args: False)
    with pytest.raises(ProtocolError) as error:
        service._handle_ui_preferences_set({"show_task_results": True})
    assert error.value.code == "io_error"
    assert service.prefs.show_task_results is False

from app.computer.approval import AUTO, HIGH, LOW, MANUAL, UNKNOWN, click_verdict, keyboard_verdict
from app.computer.verification import covers_dispatch, unresolved_evidence
import pytest


@pytest.mark.parametrize("label", ["Guardar", "Eliminar", "Guardar como…", "Eliminar mensagem"])
def test_portuguese_sensitive_buttons_always_ask(label):
    verdict = click_verdict(control_label=label, control_type="Button")
    assert verdict.level == HIGH
    assert verdict.needs_consent(AUTO) and verdict.needs_consent(MANUAL)


def test_unknown_button_is_not_proven_safe_by_an_unrecognised_label():
    verdict = click_verdict(control_label="Detalhes", control_type="Button")
    assert verdict.level == UNKNOWN and verdict.needs_consent(MANUAL)


@pytest.mark.parametrize("keys", ["{TAB}^p", "{ESC}{F5}", "{DOWN}%x"])
def test_navigation_does_not_hide_an_unknown_shortcut(keys):
    assert keyboard_verdict(keys=keys).level == UNKNOWN


def test_navigation_sequence_and_sensitive_mixed_sequence():
    assert keyboard_verdict(keys="{TAB}{DOWN}").level == LOW
    assert keyboard_verdict(keys="{TAB}^s").level == HIGH


def test_changed_expected_substring_is_not_a_changed_result():
    old = dict(operation="uia_verify", handle=5, runtime_id=[2], property="text",
               observed="83", expected_text="8", verified=True)
    action = dict(operation="invoke", handle=5, runtime_id=[1], verified=False,
                  needs_postcondition=True)
    proof = {**old, "expected_text": "83"}
    assert not covers_dispatch(action, proof, history=(old,))
    assert unresolved_evidence([old, action, proof]) == [action]


def test_same_control_real_state_change_still_proves_action():
    action = dict(operation="invoke", handle=5, runtime_id=[1], verified=False,
                  needs_postcondition=True, before={"expanded": 0})
    proof = dict(operation="uia_verify", handle=5, runtime_id=[1], property="expanded",
                 observed=1, verified=True)
    assert unresolved_evidence([action, proof]) == []


def test_worker_flags_follow_settings_not_stale_parent_environment():
    from app.computer.ufo_engine import build_worker_env
    from app.settings import Settings
    parent = {"COMPUTER_WRITE_MACRO": "1", "COMPUTER_DIRECT_APP": "1"}
    env = build_worker_env(Settings(computer_write_macro=False, computer_direct_app=False), environ=parent)
    assert env["COMPUTER_WRITE_MACRO"] == env["COMPUTER_DIRECT_APP"] == "0"
    enabled = build_worker_env(Settings(computer_write_macro=True, computer_direct_app=True), environ={})
    assert enabled["COMPUTER_WRITE_MACRO"] == enabled["COMPUTER_DIRECT_APP"] == "1"

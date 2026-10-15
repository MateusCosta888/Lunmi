import pytest

from app.core.controller import unresolved_evidence
from app.computer.batches import NoProgressGuard


def test_preconditions_and_window_selection_do_not_prove_a_dispatch() -> None:
    action = {"operation": "invoke", "handle": 10, "verified": False, "needs_postcondition": True}
    for proof in (
        {"operation": "select_application_window"},
        {"operation": "uia_verify", "property": "focused"},
        {"operation": "uia_verify", "property": "enabled"},
        {"operation": "verify_window"},
        {"operation": "verify_window", "active": True},
    ):
        assert unresolved_evidence([action, {**proof, "handle": 10, "verified": True}]) == [action]


def test_submission_requires_proof_outside_the_submitted_field() -> None:
    action = {"operation": "keyboard_input", "handle": 10, "id": "old", "runtime_id": [1, 2],
              "submitted": True, "verified": False, "needs_postcondition": True}
    proof = {"operation": "uia_verify", "handle": 10, "id": "new", "runtime_id": [1, 2],
             "property": "text", "verified": True}
    assert unresolved_evidence([action, proof]) == [action]
    assert unresolved_evidence([action, {**proof, "runtime_id": [3, 4]}]) == []
    window_read = {"operation": "verify_window", "handle": 10, "verified": True,
                   "expected_text": "Michael Jackson", "control_type": "Edit"}
    assert unresolved_evidence([action, window_read]) == [action]


def test_failed_dispatch_records_cannot_reset_the_repetition_limit() -> None:
    guard = NoProgressGuard(limit=3)
    signature = ("expand", "same-live-control")
    for count in range(1, 4):
        guard.after_batch(signature, failed=True, evidence_count=count)
        guard.after_batch(("uia_observe", "region"), failed=False, evidence_count=count)
    assert guard.before_batch(signature)


def test_precondition_read_does_not_erase_failed_content_verification() -> None:
    failed = {"operation": "uia_verify", "handle": 10, "property": "text", "expected_text": "result",
              "expected_state": -1, "verified": False}
    focus = {**failed, "property": "focused", "expected_state": 1, "verified": True}
    assert unresolved_evidence([failed, focus]) == [failed]
    assert unresolved_evidence([failed, {**failed, "id": "result-child", "verified": True}]) == []
    assert unresolved_evidence([failed, {**failed, "expected_text": "other result", "verified": True}]) == [failed]
    failed_window = {"operation": "verify_window", "handle": 10, "verified": False, "expected_text": "result"}
    for proof in ({"active": True}, {}):
        assert unresolved_evidence([failed_window, {**proof, "operation": "verify_window", "handle": 10,
                                                   "verified": True}]) == [failed_window]


def test_later_foreground_read_can_resolve_a_failed_foreground_goal() -> None:
    failed = {"operation": "verify_window", "handle": 10, "verified": False,
              "expected_active": True, "active": False}
    assert unresolved_evidence([failed, {**failed, "verified": True, "active": True}]) == []
    assert unresolved_evidence([failed, {"operation": "verify_window", "handle": 10, "verified": True}]) == [failed]


@pytest.mark.parametrize("invalid", ["none", "negative_index", "bad_index", "inverted", "read_index", "identity", "handle", "early", "one_dispatch", "uncertain_only", "unassociated"])
def test_only_proven_associated_popup_recovery_retires_a_negative_read(invalid):
    native = {"operation": "invoke", "handle": 10, "runtime_id": [1], "executed": True,
              "verified": False, "needs_postcondition": True, "before": {"expanded": 0}}
    negative = {"operation": "uia_verify", "handle": 10, "runtime_id": [1], "property": "expanded",
                "expected_state": 1, "observed": 0, "verified": False}
    click = {**native, "operation": "click_input", "recovery_of": 0, "recovery_read": 1}
    positive = {"operation": "uia_verify", "handle": 10, "runtime_id": [2], "property": "name", "verified": True}
    evidence = [native, negative, click, positive]
    if invalid == "negative_index":
        click["recovery_of"] = -1
    elif invalid == "bad_index":
        click["recovery_of"] = 100
    elif invalid == "inverted":
        click["recovery_of"] = 2
    elif invalid == "read_index":
        click["recovery_read"] = 0
    elif invalid == "identity":
        negative["runtime_id"] = [3]
    elif invalid == "handle":
        negative["handle"] = 11
    elif invalid == "early":
        evidence = [native, negative, positive, click]
    elif invalid == "one_dispatch":
        positive.update(runtime_id=[1], property="toggle", observed=0)
        click["before"] = {"toggle": 0}
    elif invalid == "uncertain_only":
        click["dispatch_uncertain"] = True
        evidence.pop()
    elif invalid == "unassociated":
        click.pop("recovery_of")
    pending = unresolved_evidence(evidence)
    if invalid == "none":
        assert pending == []
    else:
        assert negative in pending
    assert negative["verified"] is False

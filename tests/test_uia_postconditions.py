import hashlib

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
    from app.computer.verification import issue_association
    query = "same"
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    typed = {"operation": "set_edit_text", "handle": 10, "id": "old", "runtime_id": [1, 2],
             "verified": True, "length": len(query), "text_sha256": digest}
    action = {"operation": "keyboard_input", "handle": 10, "id": "old", "runtime_id": [1, 2],
              "submitted": True, "verified": False, "needs_postcondition": True}
    proof = {"operation": "uia_verify", "handle": 10, "id": "new", "runtime_id": [3, 4],
             "property": "text", "verified": True, "results_verified": True,
             "workflow": "spotify_search", "query_length": len(query), "query_sha256": digest}
    proof["association"] = issue_association(proof, digest)
    assert unresolved_evidence([typed, action, proof]) == []
    # A different live control with only a verified read is not an association.
    assert unresolved_evidence([typed, action, {**proof, "association": ""}]) == [action]
    # A marker is not a token issued by the live workflow, even in this window.
    assert unresolved_evidence([typed, action, {**proof, "association": "forged"}]) == [action]
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


@pytest.mark.parametrize("invalid", ["none", "negative_index", "bad_index", "inverted", "read_index", "identity", "handle", "early", "one_dispatch", "uncertain_only", "unassociated", "label_only"])
def test_only_proven_associated_popup_recovery_retires_a_negative_read(invalid):
    native = {"operation": "invoke", "handle": 10, "runtime_id": [1], "executed": True,
              "verified": False, "needs_postcondition": True, "before": {"expanded": 0}}
    negative = {"operation": "uia_verify", "handle": 10, "runtime_id": [1], "property": "expanded",
                "expected_state": 1, "observed": 0, "verified": False}
    click = {**native, "operation": "click_input", "recovery_of": 0, "recovery_read": 1}
    positive = {"operation": "uia_verify", "handle": 10, "runtime_id": [1],
                "control_type": "MenuItem", "property": "expanded", "expected_state": 1,
                "observed": 1, "verified": True}
    evidence = [native, negative, click, positive]
    if invalid not in {"none", "one_dispatch"}:
        # These cases break the recovery record; give them an unrelated positive
        # read so a legitimate same-control retry cannot mask the invalid link.
        positive.clear()
        positive.update({"operation": "uia_verify", "handle": 10, "runtime_id": [2],
                         "control_type": "Pane", "property": "name", "observed": "About",
                         "expected_text": "About", "verified": True})
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
    elif invalid == "label_only":
        positive.clear()
        positive.update({"operation": "uia_verify", "handle": 10, "runtime_id": [2],
                         "control_type": "Pane", "property": "name", "observed": "About",
                         "expected_text": "About", "verified": True})
    pending = unresolved_evidence(evidence)
    if invalid == "none":
        assert pending == []
    else:
        assert negative in pending
    assert negative["verified"] is False


def test_a_button_click_requires_more_than_an_unassociated_display_read() -> None:
    def click() -> dict:
        return {"operation": "click_input", "handle": 10, "runtime_id": [1], "verified": False,
                "needs_postcondition": True}

    def display(text: str) -> dict:
        return {"operation": "uia_verify", "handle": 10, "runtime_id": [2], "property": "name",
                "expected_text": text, "verified": True}

    first = click()
    assert unresolved_evidence([first, display("Display is 1")]) == [first]
    # The display already showed this at its last read: nothing proves this click.
    again = click()
    assert unresolved_evidence([display("Display is 7"), again, display("Display is 7")]) == [again]
    other_window = click()
    assert unresolved_evidence([other_window, {**display("Display is 1"), "handle": 11}]) == [other_window]


def test_enter_requires_a_result_association_not_just_another_control() -> None:
    enter = {"operation": "keyboard_input", "handle": 10, "runtime_id": [1], "submitted": True,
             "verified": False, "needs_postcondition": True}
    bar = {"operation": "uia_verify", "handle": 10, "runtime_id": [1], "property": "text",
           "expected_text": "wikipedia.org", "verified": True}
    page = {"operation": "uia_verify", "handle": 10, "runtime_id": [2], "property": "name",
            "expected_text": "Wikipedia, a enciclopédia livre", "verified": True}
    assert unresolved_evidence([enter, bar]) == [enter]
    assert unresolved_evidence([enter, page]) == [enter]


def test_save_requires_content_proof_not_just_a_matching_title() -> None:
    """A title identifies a file; the original click may not have saved its content."""
    save = {"operation": "click_input", "handle": 10, "runtime_id": [5], "verified": False,
            "needs_postcondition": True}
    before = {"operation": "verify_window", "handle": 10, "title": "*Untitled - Notepad", "verified": True}
    titled = {"operation": "verify_window", "handle": 10, "title": "lunmi-teste.txt - Notepad",
              "expected_text": "lunmi-teste.txt", "verified": True}
    assert unresolved_evidence([before, save, titled]) == [save]
    # The name typed in the dialog matches the window's text, but not its title: no proof.
    in_dialog = {**titled, "title": "*Untitled - Notepad"}
    assert unresolved_evidence([before, save, in_dialog]) == [save]
    # A title that already named the file before the click proves nothing about this click.
    stale = {**before, "title": "lunmi-teste.txt - Notepad"}
    assert unresolved_evidence([stale, save, titled]) == [save]


def test_a_failed_text_check_is_answered_by_the_same_words_read_as_name() -> None:
    failed = {"operation": "uia_verify", "handle": 10, "runtime_id": [7], "property": "text",
              "expected_text": "Wikipedia", "verified": False}
    by_name = {**failed, "property": "name", "verified": True}
    assert unresolved_evidence([failed, by_name]) == []
    assert unresolved_evidence([failed, {**by_name, "expected_text": "Outra"}]) == [failed]


def test_a_full_read_with_the_failed_lines_answers_a_partial_verify() -> None:
    """Measured 01/11: the line was appended and read back, but the earlier verify of just
    "comprar pão" stayed pending forever and the finished task stalled."""
    failed = {"operation": "uia_verify", "id": "uia-2-7", "runtime_id": [42, 1], "handle": 10,
              "control_type": "Document", "property": "text", "expected_text": "comprar pão",
              "expected_state": -1, "verified": False}
    full = {**failed, "id": "uia-3-7", "expected_text": "lista de compras: arroz\ncomprar pão", "verified": True}
    assert unresolved_evidence([failed, full]) == []
    poem = {**failed, "expected_text": "O Mar"}
    assert unresolved_evidence([poem, {**full, "expected_text": "O Mar\n\nO mar canta devagar,"}]) == []
    # Not a whole line, another control or an unverified read: still pending.
    display = {**failed, "expected_text": "84"}
    assert unresolved_evidence([display, {**full, "expected_text": "184"}]) == [display]
    assert unresolved_evidence([display, {**full, "property": "name", "expected_text": "Display is 84"}]) == []
    saved = {**failed, "expected_text": "guardado"}
    assert unresolved_evidence([saved, {**full, "expected_text": "não guardado"}]) == [saved]
    assert unresolved_evidence([failed, {**full, "runtime_id": [42, 2]}]) == [failed]
    assert unresolved_evidence([failed, {**full, "verified": False}]) == [failed, {**full, "verified": False}]


def test_a_label_asked_of_one_control_is_answered_by_another_that_shows_it() -> None:
    """Measured 01/11: "Wikipedia" asked of Edge's address bar, then verified as the page
    heading "Wikipedia A Enciclopédia Livre"; the failed read stalled a navigated task."""
    asked = {"operation": "uia_verify", "runtime_id": [1, 1], "handle": 10, "control_type": "Edit",
             "property": "text", "expected_text": "Wikipedia", "verified": False}
    heading = {"operation": "uia_verify", "runtime_id": [1, 2], "handle": 10, "control_type": "Text",
               "property": "text", "expected_text": "Wikipedia A Enciclopédia Livre", "verified": True}
    assert unresolved_evidence([asked, heading]) == [asked]  # an editor's content is its own
    label = {**asked, "control_type": "Text"}
    assert unresolved_evidence([label, heading]) == []
    assert unresolved_evidence([label, {**heading, "expected_text": "Não é Wikipedia"}]) == [label]
    assert unresolved_evidence([label, {**heading, "handle": 11}]) == [label]


def test_an_unnamed_page_container_is_a_label_but_an_editor_is_not() -> None:
    """Measured 01/11: "Wikipedia" asked of Edge's unnamed WebView Document, then verified as
    the page title; the container is not an editor, Notepad's Document is."""
    container = {"operation": "uia_verify", "runtime_id": [1, 1], "handle": 10, "control_type": "Document",
                 "content_field": False, "property": "text", "expected_text": "Wikipedia", "verified": False}
    page = {**container, "runtime_id": [1, 2], "verified": True}
    assert unresolved_evidence([container, page]) == []
    editor = {**container, "content_field": True}
    assert unresolved_evidence([editor, page]) == [editor]

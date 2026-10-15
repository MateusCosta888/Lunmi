from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.computer.uia import NativeBackend, UIAError

from test_uia import engine, identifier, node

ROOT = Path(__file__).resolve().parents[1]


def _runtime_module(monkeypatch: Any) -> Any:
    """Import the worker-side runtime the way the worker itself sees it.

    The pinned vendor config loads from relative paths, so the import (like the worker
    boot) runs with ``vendor/UFO`` as the working directory. Scoped to the test.
    """
    monkeypatch.chdir(ROOT / "vendor" / "UFO")
    monkeypatch.syspath_prepend(str(ROOT / "vendor" / "UFO"))
    import app.computer.ufo_runtime as runtime_module
    return runtime_module


def test_reuse_checks_selected_window_before_returning_cached_controls() -> None:
    instance, backend = engine(node("Old window", "Window", children=(node("Old field"),)))
    backend.key = (20, 2, 200.0)
    backend.tree = node("New window", "Window", children=(node("New field"),))
    observed = instance.observe(reuse=True)
    assert not observed.get("reused")
    assert any(row["name"] == "New field" for row in observed["elements"])


def test_region_snapshot_cannot_be_reused_as_a_whole_window_snapshot() -> None:
    region = node("Region", "Pane", children=(node("Inner"),))
    instance, _ = engine(node("Window", "Window", children=(region, node("Outside"))))
    instance.observe(root_id=identifier(instance, "Region"))
    observed = instance.observe(reuse=True)
    assert not observed["root_id"]
    assert any(row["name"] == "Outside" for row in observed["elements"])


def test_unreadable_depth_frontier_never_certifies_completeness() -> None:
    frontier = node("Frontier", "Pane")
    instance, backend = engine(node("Window", "Window", children=(frontier,)))
    original = backend.children

    def children(element: Any) -> Any:
        if element is frontier:
            raise RuntimeError("provider unavailable")
        return original(element)

    backend.children = children
    observed = instance.observe(depth=1)
    assert observed["truncated"] and not observed["complete"] and observed["errors"]
    assert instance.resolve(query="Absent")["status"] == "incomplete"


def test_virtual_container_without_realized_children_does_not_prove_absence() -> None:
    instance, backend = engine(node("Items", "List", patterns=("ItemContainer",)))
    found = instance.resolve(query="Unrealized item")
    assert found["status"] == "incomplete" and found["virtualized"]
    assert backend.mutation_count == 0


def test_edit_exact_content_cannot_be_verified_by_substring_fallback() -> None:
    instance, backend = engine(node("Field", "Edit", text="Michael Jackson bonus", patterns=("Value",)))
    proof = instance.verify(identifier(instance, "Field"), "Field", expected_text="Michael Jackson")
    assert not proof["verified"]
    assert backend.mutation_count == 0


def test_writing_can_change_a_dynamic_result_tree() -> None:
    field = node("Query", "Edit", patterns=("Value",))
    window = node("Window", "Window", children=(field,))
    instance, backend = engine(window)
    original = backend.call

    def call(*args: Any) -> None:
        original(*args)
        window["children"].append(node("New result", "DataItem"))

    backend.call = call
    instance.set_text(identifier(instance, "Query"), "Query", "query", replace=True)
    found = instance.resolve(query="New result")
    assert found["status"] == "unique"
    assert backend.mutation_count == 1


def test_context_filters_combine_provider_and_state_evidence() -> None:
    first = node("Same", "Edit", aid="a", patterns=("Value",), enabled=False)
    second = node("Same", "Edit", aid="b", patterns=("Value",))
    instance, backend = engine(node("Window", "Window", children=(first, second)))
    found = instance.resolve(query="Same", control_type="Edit", automation_id="b",
                             framework="fixture", class_name="Edit", enabled=True,
                             offscreen=False, pattern="Value")
    assert found["status"] == "unique" and found["candidates"][0]["automation_id"] == "b"
    assert backend.mutation_count == 0


def test_progressive_observation_expands_a_region_without_losing_window_context() -> None:
    region = node("Work area", "Pane", children=(node("Level 1", "Pane", children=(
        node("Level 2", "Pane", children=(node("Deep field", "Edit", patterns=("Value",)),)),)),))
    instance, _ = engine(node("Window", "Window", children=(region, node("Toolbar", "Pane"))))
    instance.invalidate_tree()
    first = instance.observe_progressive()
    assert first["stage"] == "top" and first["truncated"] and not first["complete"]
    assert not any(row["name"] == "Deep field" for row in first["elements"])
    root_id = identifier(instance, "Work area")
    expanded = instance.observe(root_id=root_id)
    assert expanded["complete"]
    assert instance.resolve(query="Deep field", root_id=root_id)["status"] == "unique"
    assert any(row["name"] == "Toolbar" for row in instance.elements)
    target = identifier(instance, "Deep field")
    following = instance.observe_progressive()
    assert any(row["id"] == target for row in following["elements"])
    assert target in instance.references and not following["complete"]
    assert following["stage"] == "expanded"


def test_progressive_reuse_preserves_an_explicit_deeper_window_observation() -> None:
    instance, _ = engine(node("Window", "Window", children=(node("Button"),)))
    observed = instance.observe_progressive()
    assert observed["reused"] and observed["depth"] == 16


def test_uncertain_write_cannot_be_replayed_even_after_new_observation() -> None:
    instance, backend = engine(node("Field", "Edit", patterns=("Value",)))
    backend.corrupt = True
    instance._wait = lambda condition, timeout_ms=1000: condition()
    with pytest.raises(UIAError, match="verification_failed"):
        instance.set_text(identifier(instance, "Field"), "Field", "literal")
    instance.observe()
    with pytest.raises(UIAError, match="mutation_unverified"):
        instance.set_text(identifier(instance, "Field"), "Field", "literal")
    assert backend.mutation_count == 1


def test_already_selected_control_does_not_count_as_an_executed_action() -> None:
    target = node("Item", "ListItem", patterns=("SelectionItem",))
    target["selected"] = True
    instance, backend = engine(target)
    result = instance.action(identifier(instance, "Item"), "Item", "select")
    assert result["verified"] and result["already_satisfied"]
    assert result["executed"] is False and backend.mutation_count == 0


def test_native_menu_pattern_requires_foreground_before_dispatch() -> None:
    from types import SimpleNamespace
    backend = NativeBackend.__new__(NativeBackend)
    order = []
    backend.metadata = lambda element: {"type": "MenuItem"}
    backend.pattern = lambda element, pattern: SimpleNamespace(Expand=lambda: order.append("dispatch"))
    backend.atomic = lambda target, window, method, arguments: getattr(target, method)()
    backend._granted = lambda window: order.append("foreground") or True
    backend.call(object(), object(), "ExpandCollapse", "Expand")
    assert order == ["foreground", "dispatch"]
    order.clear()
    backend.FOCUS_ATTEMPTS = 1
    backend._granted = lambda window: False
    with pytest.raises(UIAError, match="focus_lost"):
        backend.call(object(), object(), "ExpandCollapse", "Expand")
    assert not order


def test_late_write_confirmation_does_not_append_the_text_a_second_time() -> None:
    field = node("Field", "Edit", patterns=("Value",))
    instance, backend = engine(field)
    backend.corrupt = True
    instance._wait = lambda condition, timeout_ms=1000: condition()
    with pytest.raises(UIAError, match="verification_failed"):
        instance.set_text(identifier(instance, "Field"), "Field", "literal")
    field["text"] = "literal"
    instance.observe()
    outcome = instance.set_text(identifier(instance, "Field"), "Field", "literal")
    assert outcome["verified"] and not outcome["executed"] and outcome["already_satisfied"]
    assert field["text"] == "literal" and backend.mutation_count == 1


def test_legacy_default_action_is_dispatch_only_and_requires_provider_support() -> None:
    instance, backend = engine(node("Open", "MenuItem", patterns=("LegacyIAccessible",)))
    backend.state = lambda *args: "Open"
    outcome = instance.action(identifier(instance, "Open"), "Open", "default_action")
    assert outcome["executed"] and not outcome["verified"] and outcome["needs_postcondition"]
    assert backend.mutation_count == 1
    backend.state = lambda *args: ""
    instance.evidence.clear()
    with pytest.raises(UIAError, match="unsupported_pattern"):
        instance.action(identifier(instance, "Open"), "Open", "default_action")
    assert backend.mutation_count == 1


def test_retry_signature_uses_live_identity_instead_of_snapshot_generation() -> None:
    from types import SimpleNamespace
    from app.computer.batches import action_signature
    first = SimpleNamespace(tool_type="action", tool_name="uia_action",
                            parameters={"id": "uia-1-25", "name": "Help", "operation": "expand"})
    second = SimpleNamespace(tool_type="action", tool_name="uia_action",
                             parameters={"id": "uia-2-25", "name": "Help", "operation": "expand"})
    identity = ((10, 42, 1.0), (1, 10, 25))
    assert action_signature([first], {"uia-1-25": identity}) == action_signature([second], {"uia-2-25": identity})


def test_state_pattern_without_effect_is_a_named_failure() -> None:
    instance, backend = engine(node("Help", "MenuItem", patterns=("ExpandCollapse",)))
    backend.call = lambda *args: None
    instance._wait = lambda condition: condition()
    outcome = instance.action(identifier(instance, "Help"), "Help", "expand")
    assert outcome["failure_kind"] == "UIA_ACTION_NO_EFFECT"
    assert not outcome["verified"] and outcome["needs_postcondition"]


def test_observing_again_does_not_authorize_a_duplicate_invoke() -> None:
    instance, backend = engine(node("Open", patterns=("Invoke",)))
    instance.action(identifier(instance, "Open"), "Open", "invoke")
    instance.observe()
    with pytest.raises(UIAError, match="mutation_unverified"):
        instance.action(identifier(instance, "Open"), "Open", "invoke")
    assert backend.mutation_count == 1


@pytest.mark.parametrize("first,second", [("invoke", "invoke"), ("invoke", "default_action"),
                                         ("keyboard", "keyboard"), ("invoke", "keyboard"),
                                         ("keyboard", "invoke"), ("expand", "expand"),
                                         ("expand", "invoke"), ("invoke", "expand")])
def test_provider_error_after_dispatch_does_not_authorize_replay(first, second) -> None:
    instance, backend = engine(node("Target", "Edit",
                                    patterns=("Invoke", "LegacyIAccessible", "ExpandCollapse")))
    def fails(*args):
        backend.mutation_count += 1
        raise UIAError("execution_failed", "provider acted then failed")
    backend.call = fails
    backend.keys = fails
    def run(operation):
        if operation == "keyboard":
            return instance.keyboard(identifier(instance, "Target"), "Target", "{ENTER}")
        return instance.action(identifier(instance, "Target"), "Target", operation)
    with pytest.raises(UIAError, match="mutation_unverified"):
        run(first)
    instance.observe()
    with pytest.raises(UIAError, match="mutation_unverified"):
        run(second)
    assert backend.mutation_count == 1 and instance.evidence[0]["dispatch_uncertain"]


def test_escape_recovers_without_authorizing_another_activation() -> None:
    instance, backend = engine(node("Target", "Button", patterns=("Invoke",)))

    def uncertain(*args: Any) -> None:
        backend.mutation_count += 1
        raise UIAError("execution_failed", "provider acted then failed")

    backend.keys = uncertain
    with pytest.raises(UIAError, match="mutation_unverified"):
        instance.keyboard(identifier(instance, "Target"), "Target", "{ENTER}")
    instance.observe()
    backend.keys = lambda *args: None
    result = instance.keyboard(identifier(instance, "Target"), "Target", "{ESC}")
    assert result["recovery"] and not result["verified"]
    with pytest.raises(UIAError, match="mutation_unverified"):
        instance.action(identifier(instance, "Target"), "Target", "invoke")
    with pytest.raises(UIAError, match="mutation_unverified"):
        instance.keyboard(identifier(instance, "Target"), "Target", "{ESC}{ENTER}")
    assert backend.mutation_count == 1


def test_unchanged_menu_state_or_name_does_not_prove_invoke() -> None:
    from app.computer.verification import unresolved_evidence
    instance, _ = engine(node("Help", "MenuItem", patterns=("Invoke", "ExpandCollapse")))
    outcome = instance.action(identifier(instance, "Help"), "Help", "invoke")
    for prop, text, value in (("expanded", "", 0), ("name", "Help", -1)):
        proof = instance.verify(identifier(instance, "Help"), "Help", property_name=prop,
                                expected_text=text, expected_state=value)
        assert proof["verified"]
        assert unresolved_evidence([outcome, proof]) == [outcome]


@pytest.mark.parametrize("uncertain", [False, True])
def test_menu_keyboard_requires_an_observed_change_before_replay(uncertain: bool) -> None:
    from app.computer.verification import unresolved_evidence
    menu = node("Help", "MenuItem", patterns=("Invoke", "ExpandCollapse"))
    instance, backend = engine(menu)

    def send(*args: Any) -> None:
        backend.mutation_count += 1
        if uncertain:
            raise RuntimeError("provider failed after possible dispatch")

    backend.keys = send
    if uncertain:
        with pytest.raises(UIAError, match="mutation_unverified"):
            instance.keyboard(identifier(instance, "Help"), "Help", "%h")
    else:
        instance.keyboard(identifier(instance, "Help"), "Help", "%h")
    outcome = instance.evidence[-1]
    for prop, text, value in (("expanded", "", 0), ("name", "Help", -1)):
        proof = instance.verify(identifier(instance, "Help"), "Help", property_name=prop,
                                expected_text=text, expected_state=value)
        assert proof["verified"]
        instance.evidence.append(proof)
        assert unresolved_evidence(instance.evidence) == [outcome]
    with pytest.raises(UIAError, match="mutation_unverified"):
        instance.action(identifier(instance, "Help"), "Help", "invoke")
    assert backend.mutation_count == 1
    backend.call(menu, instance.state.selected_app_window, "ExpandCollapse", "Expand")
    changed = instance.verify(identifier(instance, "Help"), "Help", property_name="expanded",
                              expected_state=1)
    instance.evidence.append(changed)
    assert changed["verified"] and unresolved_evidence(instance.evidence) == []
    instance.action(identifier(instance, "Help"), "Help", "invoke")
    assert backend.mutation_count == 3


def test_window_shortcut_cannot_use_different_field_ids_to_replay() -> None:
    instance, backend = engine(node("Window", "Window", children=(node("A", "Edit"), node("B", "Edit"))))
    backend.keys = lambda *args: pytest.fail("field ID must never dispatch a window shortcut")
    for name in ("A", "B"):
        with pytest.raises(UIAError, match="unsafe_context"):
            instance.keyboard(identifier(instance, name), name, "^n", control_focus=False)


def test_window_shortcut_requires_a_live_focused_descendant(monkeypatch) -> None:
    from types import SimpleNamespace
    from app.computer import windows_tools
    window = SimpleNamespace(handle=10)
    backend = NativeBackend.__new__(NativeBackend)
    focused = object()
    calls = []
    backend.client = SimpleNamespace(GetFocusedElement=lambda: focused)
    backend.activate = lambda window: None
    backend.belongs = lambda element, window: element is focused
    backend.atomic = lambda *args: calls.append(args)
    monkeypatch.setattr(windows_tools, "foreground_handle", lambda: 10)
    backend.keys(object(), window, {"type": "Window"}, "^n")
    assert len(calls) == 1 and calls[0][0] is window
    for keys in ("{ENTER}", "hello", "^ahello", "^({ENTER})"):
        with pytest.raises(UIAError, match="unsafe_context"):
            backend.keys(object(), window, {"type": "Window"}, keys)
    assert len(calls) == 1
    backend.belongs = lambda *args: False
    with pytest.raises(UIAError, match="focus_lost"):
        backend.keys(object(), window, {"type": "Window"}, "^n")
    assert len(calls) == 1


def _cut_row_fixture(second_row: bool = False) -> tuple[Any, Any]:
    """The live shape: a results row whose navigable control is inside it.

    Window -> DataGrid -> DataItem 'Michael Jackson' (Invoke) -> Group (not actionable)
    -> DataItem (SelectionItem) -> Hyperlink 'Michael Jackson' (Invoke).
    ``second_row`` adds a sibling row the request also names, for the coverage cases.
    """
    from test_uia import Backend

    link = node("Michael Jackson", "Hyperlink", patterns=("Invoke",))
    inner = node("", "Group", patterns=("ScrollItem",), children=(
        node("Michael Jackson", "DataItem", patterns=("SelectionItem",), children=(link,)),))
    card = node("Michael Jackson", "DataItem", patterns=("Invoke", "ScrollItem"),
                children=(inner,))
    rows = (card,) if not second_row else (
        card, node("Michael Jackson ao vivo", "DataItem", patterns=("Invoke",)))
    root = node("Window", "Window", children=(
        node("Search results", "DataGrid", patterns=("Scroll",), children=rows),
        node("Unrelated", "Button", patterns=("Invoke",))))
    from app.computer.uia import UIAEngine

    backend = Backend(root)
    state = SimpleNamespace(selected_app_window=SimpleNamespace(handle=10), control_dict={})
    return UIAEngine(state, backend=backend), backend


def test_the_row_the_request_names_is_read_before_its_list_is_sent(monkeypatch: Any) -> None:
    """A-02: a truncated read cannot carry a row's interior, so the request's own row is
    the region to read — and reading it is not an action.

    Measured on Spotify (`logs/_a02_boundary_probe.json`): the artist card sits at depth 15
    and the Hyperlink that opens the artist page at 18, while a depth-16 read stops at
    level 16 — and a deeper read does not reach it either, because level 17 alone (312
    rows) already exceeds the 600-node budget (`logs/_a02_depth_budget_probe.json`). The
    agent's own expansions missed that row (`logs/_a02_find_probe.json`); reading the row
    exposes its interior (13 rows) while dispatching nothing.
    """
    module = _runtime_module(monkeypatch)

    instance, backend = _cut_row_fixture()
    observed = instance.observe(depth=2)
    assert observed["truncated"] and not observed["complete"]
    assert not any(item["type"] == "Hyperlink" for item in observed["elements"])
    runtime = module.UFORuntime.__new__(module.UFORuntime)
    runtime.tools = SimpleNamespace(uia=instance)
    runtime.task_keywords = ("michael", "jackson")
    reads_before = backend.calls
    expanded = module.UFORuntime.read_named_regions(runtime)
    assert expanded and any(item["type"] == "Hyperlink" for item in instance.elements)
    assert backend.calls > reads_before and backend.mutation_count == 0


def _runtime_for(module: Any, instance: Any, keywords: tuple[str, ...]) -> Any:
    runtime = module.UFORuntime.__new__(module.UFORuntime)
    runtime.tools = SimpleNamespace(uia=instance)
    runtime.task_keywords = keywords
    return runtime


def test_a_region_is_not_read_for_a_request_that_does_not_name_it(monkeypatch: Any) -> None:
    """The expansion follows the request's own words, and a covered window needs none."""
    module = _runtime_module(monkeypatch)

    instance, backend = _cut_row_fixture()
    instance.observe(depth=2)
    reads_before = backend.calls
    assert module.UFORuntime.read_named_regions(_runtime_for(module, instance, ("orcamento",))) == []
    assert backend.calls == reads_before
    complete, backend = _cut_row_fixture()
    complete.observe()
    assert complete.last_observation["window_complete"]
    reads_before = backend.calls
    named = _runtime_for(module, complete, ("michael", "jackson"))
    assert module.UFORuntime.read_named_regions(named) == []
    assert backend.calls == reads_before


def test_a_completed_region_read_does_not_hide_an_uncovered_window(monkeypatch: Any) -> None:
    """A scoped read reports ``complete`` for its own root, never for the window.

    Measured in the A-02 probe: after one region read, `truncated` went False while the
    union stayed `complete: False`. The expansion gate has to read the window's coverage,
    or one successful region read would end the search for the row that still holds the
    target.
    """
    module = _runtime_module(monkeypatch)

    instance, _ = _cut_row_fixture(second_row=True)
    instance.observe(depth=2)
    scoped = instance.observe(root_id=identifier(instance, "Michael Jackson"))
    assert scoped["complete"] and not scoped["truncated"]
    assert not scoped["window_complete"] and not instance.last_observation["window_complete"]
    runtime = _runtime_for(module, instance, ("michael", "jackson"))
    rows = {str(row["id"]): row for row in instance.elements}
    # The window is still partial, so the row that is not covered yet is read: one
    # completed region read must not end the search for the whole window.
    assert [rows[item]["name"] for item in module.UFORuntime.read_named_regions(runtime)] == [
        "Michael Jackson ao vivo"]


def test_a_row_already_read_is_not_read_again_and_a_failure_is_recorded(monkeypatch: Any) -> None:
    """Off screen, already complete and overlapping rows are not paid for twice."""
    module = _runtime_module(monkeypatch)
    from app.computer.uia import UIAError

    nested = node("Michael Jackson", "DataItem", patterns=("Invoke",), children=(
        node("Michael Jackson", "Group", patterns=("ScrollItem",), children=(
            node("Inner", "Pane", patterns=("Invoke",)),)),))
    offscreen = node("Jackson Michael", "DataItem", patterns=("Invoke",))
    offscreen["meta"]["offscreen"] = True
    root = node("Window", "Window", children=(nested, offscreen,
                                                node("Filler", "Pane", patterns=("ScrollItem",))))
    from test_uia import Backend

    from app.computer.uia import UIAEngine

    backend = Backend(root)
    state = SimpleNamespace(selected_app_window=SimpleNamespace(handle=10), control_dict={})
    instance = UIAEngine(state, backend=backend)
    # Depth 2 exposes the descendant as well, so the ancestor/descendant pair is real.
    instance.observe(depth=2)
    assert [row["name"] for row in instance.elements].count("Michael Jackson") == 2
    runtime = _runtime_for(module, instance, ("michael", "jackson"))
    first = module.UFORuntime.read_named_regions(runtime)
    rows = {str(row["id"]): row for row in instance.elements}
    # The outer row only: the off screen row is skipped and the row inside it is not read
    # as a second region of the same tree.
    assert [rows[item]["name"] for item in first] == ["Michael Jackson"]
    assert runtime.region_expansion_errors == []
    # The off screen row and the row already read are not candidates again.
    assert module.UFORuntime.read_named_regions(runtime) == []
    # Two new rows appear (new evidence): the failing one is recorded and its slot goes to
    # the next row instead of disappearing.
    failing = node("Jackson Live", "DataItem", patterns=("Invoke",))
    working = node("Jackson Night", "DataItem", patterns=("Invoke",))
    root["children"].extend([failing, working])
    instance.invalidate_tree()
    instance.observe(depth=2)
    original = instance.observe
    failing_id = str(next(row["id"] for row in instance.elements if row["name"] == "Jackson Live"))

    def refuse(*, root_id: str = "", **kwargs: Any) -> Any:
        if root_id == failing_id:
            raise UIAError("stale_reference", "fixture")
        return original(root_id=root_id, **kwargs)

    instance.observe = refuse  # type: ignore[method-assign]
    read = module.UFORuntime.read_named_regions(runtime)
    names = {str(rows[item]["name"]) for item in read} if all(item in rows for item in read) else set()
    names = names or {str(next(row["name"] for row in instance.elements if str(row["id"]) == item))
                      for item in read}
    # The row inside the read region is never a second region, and the failed row's slot is
    # used by the next row instead of being lost.
    assert "Jackson Night" in names and "Jackson Live" not in names
    assert runtime.region_expansion_errors == [{"id": failing_id, "code": "stale_reference"}]


def test_the_region_attempt_budget_is_local_and_bounded(monkeypatch: Any) -> None:
    """A window full of stale rows cannot turn one collection into an unbounded read loop."""
    module = _runtime_module(monkeypatch)
    from app.computer.uia import UIAEngine, UIAError
    from test_uia import Backend

    rows = [node(f"Michael Jackson {index}", "DataItem", patterns=("Invoke",),
                 children=(node("Inner", "Pane", patterns=("Invoke",)),)) for index in range(6)]
    backend = Backend(node("Window", "Window", children=tuple(rows)))
    state = SimpleNamespace(selected_app_window=SimpleNamespace(handle=10), control_dict={})
    instance = UIAEngine(state, backend=backend)
    assert instance.observe(depth=1)["truncated"]  # the window still has unread interior
    runtime = _runtime_for(module, instance, ("michael", "jackson"))
    original = instance.observe

    def refuse(*, root_id: str = "", **kwargs: Any) -> Any:
        raise UIAError("stale_reference", "fixture")

    instance.observe = refuse  # type: ignore[method-assign]
    assert module.UFORuntime.read_named_regions(runtime) == []
    # Four attempts (the local budget), never one per matching row.
    assert len(runtime.region_expansion_errors) == 4
    instance.observe = original  # type: ignore[method-assign]


def test_a_partial_region_is_read_again_only_with_new_evidence(monkeypatch: Any) -> None:
    """A region that reads partially returns the same read again: dedup until evidence."""
    module = _runtime_module(monkeypatch)

    instance, _ = _cut_row_fixture()
    instance.observe(depth=2)
    runtime = _runtime_for(module, instance, ("michael", "jackson"))
    runtime.region_expansion_nodes = 1  # the read cannot finish: the region stays partial
    first = module.UFORuntime.read_named_regions(runtime)
    assert first and first[0] not in instance.complete_regions
    assert module.UFORuntime.read_named_regions(runtime) == []
    # A dispatch may have changed the tree: the same region is worth reading again.
    instance.evidence.append({"operation": "invoke", "handle": 10, "executed": True,
                              "verified": False, "needs_postcondition": True})
    assert module.UFORuntime.read_named_regions(runtime) == first


def test_region_reads_and_errors_belong_to_one_task(monkeypatch: Any) -> None:
    """The region record is per task, like every other metric a worker carries over."""
    module = _runtime_module(monkeypatch)

    instance, _ = _cut_row_fixture()
    instance.observe(depth=2)
    runtime = _runtime_for(module, instance, ("michael", "jackson"))
    module.UFORuntime.read_named_regions(runtime)
    runtime.region_expansion_errors.append({"id": "uia-1-1", "code": "stale_reference"})
    assert runtime.regions_read and runtime.region_expansion_errors
    runtime.tools = SimpleNamespace(uia=instance, evidence=[], owned_documents={},
                                   written_controls=set(), pending_ui=True, cancelled=False,
                                   waits_ms=0.0, wait_polls=0, launch_failfast_saved_ms=0.0,
                                   verify_failfast_saved_ms=0.0, selected_window=None,
                                   shadow_wait_saved_ms=0.0)
    runtime.state = SimpleNamespace(selected_app_window=object(), control_dict={})
    module.UFORuntime._reset_tools(runtime)
    assert runtime.regions_read == {} and runtime.region_expansion_errors == []


def test_native_pending_dispatch_blocks_explicit_geometry_before_activation() -> None:
    instance, backend = engine(node("Help", "MenuItem", patterns=("Invoke",)))
    instance.action(identifier(instance, "Help"), "Help", "invoke")
    instance.observe()
    with pytest.raises(UIAError, match="mutation_unverified"):
        instance.action(identifier(instance, "Help"), "Help", "click_input")
    assert backend.mutation_count == 1 and backend.atomics == [] and backend.activations == 0


def test_ordinary_ambiguous_error_after_possible_input_remains_uncertain() -> None:
    instance, backend = engine(node("Help", "MenuItem", patterns=("Invoke",)))
    def send(*args: Any) -> None:
        backend.mutation_count += 1
        raise UIAError("ambiguous", "provider failed after input")
    backend.keys = send
    with pytest.raises(UIAError, match="mutation_unverified"):
        instance.keyboard(identifier(instance, "Help"), "Help", "%h")
    with pytest.raises(UIAError, match="mutation_unverified"):
        instance.action(identifier(instance, "Help"), "Help", "click_input")
    assert backend.mutation_count == 1 and instance.evidence[-1]["dispatch_uncertain"]


@pytest.mark.parametrize("uncertain", [False, True])
def test_keyboard_records_dispatch_even_when_it_destroys_the_selected_window(uncertain: bool) -> None:
    instance, backend = engine(node("Dialog", "Window"))
    class Window:
        gone = False
        @property
        def handle(self) -> int:
            if self.gone:
                raise RuntimeError("window destroyed after input")
            return 10
    window = Window()
    instance.state.selected_app_window = window
    target = identifier(instance, "Dialog")
    runtime_id = list(instance.references[target].meta["runtime_id"])
    def send(*args: Any) -> None:
        window.gone = True
        if uncertain:
            raise RuntimeError("provider failed after closing")
    backend.keys = send
    if uncertain:
        with pytest.raises(UIAError, match="mutation_unverified"):
            instance.keyboard(target, "Dialog", "{ESC}")
    else:
        result = instance.keyboard(target, "Dialog", "{ESC}")
        assert result is instance.evidence[-1]
    result = instance.evidence[-1]
    assert result["handle"] == 10 and result["runtime_id"] == runtime_id
    assert not result["verified"] and result["recovery"] and result["needs_postcondition"]
    assert result.get("dispatch_uncertain", False) is uncertain


def _pending_popup(operation="invoke"):
    item = node("Help", "MenuItem", patterns=("Invoke", "ExpandCollapse"))
    parent = node("Application", "MenuBar", children=(item,))
    instance, backend = engine(parent)
    backend.client = SimpleNamespace(ControlViewWalker=SimpleNamespace(GetParentElement=lambda element: parent))
    if operation == "default_action":
        item["meta"]["patterns"].append("LegacyIAccessible")
        state = backend.state
        backend.state = lambda element, pattern, prop: "Open" if prop == "CurrentDefaultAction" else state(element, pattern, prop)
        instance.observe()
    instance.action(identifier(instance, "Help"), "Help", operation)
    instance.observe()
    proof = instance.verify(identifier(instance, "Help"), "Help", property_name="expanded", expected_state=1)
    instance.evidence.append(proof)
    return instance, backend, item, proof


@pytest.mark.parametrize("operation", ["invoke", "default_action"])
def test_normal_popup_no_effect_allows_one_explicit_alternative_with_new_proof(operation):
    from app.computer.verification import unresolved_evidence
    instance, backend, item, negative = _pending_popup(operation)
    assert not negative["verified"] and negative["observed"] == 0
    result = instance.action(identifier(instance, "Help"), "Help", "click_input")
    assert result["recovery_of"] == 0 and result["recovery_read"] == 1
    assert instance.evidence[1] is negative and not negative["verified"]
    assert len(unresolved_evidence(instance.evidence)) == 3
    instance.observe()
    with pytest.raises(UIAError, match="mutation_unverified"):
        instance.action(identifier(instance, "Help"), "Help", "click_input")
    assert len(backend.atomics) == 1
    positive = {"operation": "uia_verify", "handle": 10, "runtime_id": [99],
                "property": "name", "observed": "About", "verified": True}
    instance.evidence.append(positive)
    assert unresolved_evidence(instance.evidence) == []
    assert not negative["verified"]


@pytest.mark.parametrize("invalid", ["missing", "before", "handle", "identity", "uncertain", "expanded", "unknown", "submenu", "native", "right", "double"])
def test_popup_recovery_does_not_authorize_unsafe_replays(invalid):
    instance, backend, item, proof = _pending_popup()
    if invalid == "missing":
        instance.evidence.pop()
    elif invalid == "before":
        instance.evidence.reverse()
    elif invalid == "handle":
        proof["handle"] = 11
    elif invalid == "identity":
        proof["runtime_id"] = [99]
    elif invalid == "uncertain":
        instance.evidence[0]["dispatch_uncertain"] = True
    elif invalid == "expanded":
        item["expand"] = 1
    elif invalid == "unknown":
        item["expand"] = None
    elif invalid == "submenu":
        backend.tree["meta"]["type"] = "Menu"
    with pytest.raises(UIAError, match="mutation_unverified"):
        if invalid == "native":
            instance.action(identifier(instance, "Help"), "Help", "invoke")
        elif invalid in {"right", "double"}:
            instance.click(identifier(instance, "Help"), "Help", button="right" if invalid == "right" else "left",
                           double=invalid == "double")
        else:
            instance.action(identifier(instance, "Help"), "Help", "click_input")
    assert backend.atomics == [] and backend.activations == 0


@pytest.mark.parametrize("kind", ["Menu", "MenuBar"])
@pytest.mark.parametrize("keys", ["{ENTER}", "text", "%a"])
def test_menu_container_input_is_a_preflight_refusal(kind, keys):
    from app.computer.uia import NativeBackend, UIAPreconditionError
    instance, backend = engine(node("Container", kind))
    backend.keys = lambda *args: NativeBackend.keys(backend, *args)
    with pytest.raises(UIAPreconditionError, match="unsafe_context"):
        instance.keyboard(identifier(instance, "Container"), "Container", keys)
    assert instance.evidence == [] and backend.atomics == [] and backend.focus_calls == 0


@pytest.mark.parametrize("kind", ["Menu", "MenuBar"])
def test_menu_container_escape_remains_an_isolated_recovery(kind):
    from app.computer.uia import NativeBackend
    instance, backend = engine(node("Container", kind))
    backend.keys = lambda *args: NativeBackend.keys(backend, *args)
    result = instance.keyboard(identifier(instance, "Container"), "Container", "{ESC}")
    assert result["recovery"] and not result["verified"]
    assert len(backend.atomics) == 1


def test_a_completed_popup_recovery_cannot_mask_a_later_attempt_in_the_same_ledger():
    instance, backend, item, negative = _pending_popup()
    instance.action(identifier(instance, "Help"), "Help", "click_input")
    instance.evidence.append({"operation": "uia_verify", "handle": 10, "runtime_id": [99],
                              "property": "name", "observed": "About", "verified": True})
    instance.observe()
    instance.action(identifier(instance, "Help"), "Help", "invoke")
    native_index = len(instance.evidence) - 1
    instance.observe()
    proof = instance.verify(identifier(instance, "Help"), "Help", property_name="expanded", expected_state=1)
    instance.evidence.append(proof)
    result = instance.action(identifier(instance, "Help"), "Help", "click_input")
    assert result["recovery_of"] == native_index and len(backend.atomics) == 2
    instance.observe()
    with pytest.raises(UIAError, match="mutation_unverified"):
        instance.action(identifier(instance, "Help"), "Help", "click_input")

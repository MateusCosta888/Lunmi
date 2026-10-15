from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.computer.uia import NativeBackend, UIAEngine, UIAError, normalized


def node(name: str, kind: str = "Button", *, aid: str = "", patterns: tuple[str, ...] = (),
         children: tuple[dict[str, Any], ...] = (), enabled: bool = True,
         text: str = "", protected: bool = False) -> dict[str, Any]:
    return {"meta": {"name": name, "type": kind, "automation_id": aid, "runtime_id": (id(name), id(children)),
                     "pid": 1, "rect": [0, 0, 100, 100], "class_name": kind, "framework": "fixture",
                     "enabled": enabled, "offscreen": False, "focusable": True, "focused": False,
                     "protected": protected, "patterns": list(patterns), "native_handle": 0},
            "children": list(children), "text": text, "alive": True, "selected": False,
            "toggle": 0, "expand": 0, "scroll_x": 0, "scroll_y": 0}


class Backend:
    calls = 0
    key = (10, 1, 100.0)
    mutation_count = 0
    readable = True
    corrupt = False

    def __init__(self, root: dict[str, Any]) -> None:
        self.tree = root
        self.pattern_calls: list[tuple[str, str]] = []
        self.atomics: list[tuple[str, Any]] = []
        self.clicked: list[str] = []
        self.focus_calls = 0
        self.activations = 0

    def window_key(self, window: Any) -> Any:
        return self.key

    def root(self, window: Any) -> Any:
        self.calls += 1
        return self.tree

    def metadata(self, element: Any) -> Any:
        return dict(element["meta"])

    def refresh(self, element: Any) -> Any:
        if not element["alive"]:
            raise RuntimeError("gone")
        return element

    def children(self, element: Any) -> Any:
        self.calls += 1
        return element["children"]

    def belongs(self, element: Any, window: Any) -> bool:
        return element["alive"]

    def wrap(self, element: Any, meta: Any) -> Any:
        return element

    def content(self, element: Any, meta: Any) -> Any:
        if meta["protected"]:
            raise UIAError("protected", "refused")
        return {"readable": self.readable, "text": element["text"], "truncated": False, "source": "Value"}

    def writable(self, element: Any, meta: Any) -> str:
        return "Value"

    def activate(self, window: Any) -> None:
        self.activations += 1

    def focus(self, element: Any, window: Any, meta: Any) -> None:
        self.focus_calls += 1
        if not meta["focusable"]:
            raise UIAError("not_focusable", "fixture nunca segura foco de teclado")

    def atomic(self, target: Any, window: Any, method: str, arguments: Any) -> Any:
        self.atomics.append((method, arguments))
        if method == "click_input":
            self.clicked.append(target["meta"]["name"])
        return {}

    def call(self, element: Any, window: Any, pattern: str, method: str, *args: Any) -> None:
        self.pattern_calls.append((pattern, method))
        self.mutation_count += 1
        if method == "SetValue":
            element["text"] = str(args[0]) + ("corrupt" if self.corrupt else "")
        elif method == "Select":
            element["selected"] = True
        elif method == "Toggle":
            element["toggle"] = 1 - element["toggle"]
        elif method == "Expand":
            element["expand"] = 1
        elif method == "Collapse":
            element["expand"] = 0
        elif method == "Scroll":
            horizontal, vertical = args
            if horizontal != 2:
                element["scroll_x"] += 1 if horizontal == 4 else -1
            if vertical != 2:
                element["scroll_y"] += 1 if vertical == 4 else -1

    def state(self, element: Any, pattern: str, property_name: str) -> Any:
        return element[{"CurrentIsSelected": "selected", "CurrentToggleState": "toggle",
                        "CurrentExpandCollapseState": "expand", "CurrentHorizontalScrollPercent": "scroll_x",
                        "CurrentVerticalScrollPercent": "scroll_y"}[property_name]]


def engine(root: Any) -> tuple[UIAEngine, Backend]:
    backend = Backend(root)
    state = SimpleNamespace(selected_app_window=SimpleNamespace(handle=10), control_dict={})
    result = UIAEngine(state, backend=backend)
    result.observe()
    return result, backend


def identifier(instance: UIAEngine, name: str) -> str:
    return next(item["id"] for item in instance.elements if item["name"] == name)


def test_duplicate_names_need_context_not_first_match() -> None:
    left = node("Esquerda", "Pane", children=(node("Adicionar", aid="left"),))
    right = node("Direita", "Pane", children=(node("Adicionar", aid="right"),))
    instance, backend = engine(node("Window", "Window", children=(left, right)))
    assert instance.resolve(query="Adicionar")["status"] == "ambiguous"
    assert instance.resolve(query="Adicionar", root_id=identifier(instance, "Direita"))["candidates"][0]["automation_id"] == "right"
    assert backend.mutation_count == 0


def test_partial_observation_never_proves_unique_or_absence() -> None:
    instance, _ = engine(node("Root", children=(node("A"), node("B"))))
    result = instance.observe(max_nodes=2)
    assert result["truncated"] and not result["complete"]
    assert instance.resolve(query="A")["status"] == "incomplete"
    assert instance.resolve(query="Missing")["status"] == "incomplete"


@pytest.mark.parametrize("change", ["name", "runtime_id", "automation_id", "pid", "type"])
def test_identity_change_is_refused(change: str) -> None:
    child = node("Original", patterns=("Invoke",))
    instance, backend = engine(node("Root", children=(child,)))
    ref = identifier(instance, "Original")
    child["meta"][change] = (99,) if change == "runtime_id" else "changed"
    with pytest.raises(UIAError, match="stale_reference"):
        instance.action(ref, "Original", "invoke")
    assert backend.mutation_count == 0


def test_wrong_name_is_not_a_warning_after_execution() -> None:
    instance, backend = engine(node("Original", patterns=("Invoke",)))
    with pytest.raises(UIAError, match="name_mismatch"):
        instance.action(identifier(instance, "Original"), "Another", "invoke")
    assert backend.mutation_count == 0


def test_gone_or_recycled_window_refused() -> None:
    child = node("Control", patterns=("Invoke",))
    instance, backend = engine(child)
    ref = identifier(instance, "Control")
    backend.key = (10, 1, 101.0)
    with pytest.raises(UIAError, match="window_changed"):
        instance.action(ref, "Control", "invoke")
    backend.key = (10, 1, 100.0)
    child["alive"] = False
    with pytest.raises(UIAError, match="stale_reference"):
        instance.action(ref, "Control", "invoke")
    assert backend.mutation_count == 0


@pytest.mark.parametrize("protected,enabled,code", [(True, True, "protected"), (False, False, "disabled")])
def test_disabled_and_protected_controls_refused(protected: bool, enabled: bool, code: str) -> None:
    instance, backend = engine(node("Control", patterns=("Invoke",), protected=protected, enabled=enabled))
    with pytest.raises(UIAError, match=code):
        instance.action(identifier(instance, "Control"), "Control", "invoke")
    assert backend.mutation_count == 0


def test_text_exact_unicode_multiline_and_empty_replacement() -> None:
    field = node("Accessible name is not content", "Edit", text="old", patterns=("Value",))
    instance, backend = engine(field)
    ref = identifier(instance, field["meta"]["name"])
    name = field["meta"]["name"]
    assert instance.set_text(ref, name, "ação 😀\n  +^%{}", replace=True)["verified"]
    assert field["text"] == "ação 😀\n  +^%{}"
    assert instance.set_text(ref, name, " next")["verified"]
    assert field["text"].endswith(" next")
    assert instance.set_text(ref, name, "", replace=True)["verified"]
    assert backend.mutation_count == 3


def test_no_accessible_content_does_not_fallback_to_name() -> None:
    instance, backend = engine(node("Expected text", "Edit", patterns=("Value",)))
    backend.readable = False
    ref = identifier(instance, "Expected text")
    with pytest.raises(UIAError, match="unreadable_text"):
        instance.set_text(ref, "Expected text", "Expected text")
    assert backend.mutation_count == 0


def test_failed_verification_never_replays_write() -> None:
    instance, backend = engine(node("Field", "Edit", patterns=("Value",)))
    backend.corrupt = True
    instance._wait = lambda condition, timeout_ms=1000: condition()
    with pytest.raises(UIAError, match="verification_failed"):
        instance.set_text(identifier(instance, "Field"), "Field", "literal")
    assert backend.mutation_count == 1
    assert instance.evidence[-1]["verified"] is False


def test_invoke_dispatch_does_not_claim_task_success() -> None:
    instance, backend = engine(node("Open", patterns=("Invoke",)))
    evidence = instance.action(identifier(instance, "Open"), "Open", "invoke")
    assert evidence["executed"] and not evidence["verified"] and evidence["needs_postcondition"]
    assert backend.mutation_count == 1


@pytest.mark.parametrize("pattern,operation", [("SelectionItem", "select"), ("Toggle", "toggle"),
                                               ("ExpandCollapse", "expand")])
def test_state_patterns_verified_and_idempotent(pattern: str, operation: str) -> None:
    instance, backend = engine(node("Target", patterns=(pattern,)))
    ref = identifier(instance, "Target")
    assert instance.action(ref, "Target", operation, value=1)["verified"]
    assert instance.action(ref, "Target", operation, value=1)["verified"]
    assert backend.mutation_count == 1


def test_a_read_above_the_maximum_is_served_at_the_maximum() -> None:
    """A-02: an over-asked read must not cost the round it was needed for.

    Measured on the Spotify results page (`logs/_a02_find_probe.json`): the AppAgent asked
    to expand the region that lacked its target with depth=20 and max_nodes=1000 — both
    above the engine's maxima — and the round ended in `invalid_arguments` while the target
    stayed unread. A read is bounded by the engine either way, so the maximum is served
    instead of refused; a value below the minimum cannot be served by any read and stays
    refused.
    """
    instance, backend = engine(node("Target"))
    served = instance.observe(depth=20, max_nodes=1000, timeout_ms=9000)
    assert (served["depth"], served["max_nodes"]) == (16, 600)
    assert served["elements"]
    for kwargs in ({"depth": 0}, {"max_nodes": 0}, {"timeout_ms": 0}):
        with pytest.raises(UIAError, match="invalid_arguments"):
            instance.observe(**kwargs)
    assert backend.mutation_count == 0


def test_unsupported_action_refused() -> None:
    instance, backend = engine(node("Target"))
    with pytest.raises(UIAError, match="unsupported_pattern"):
        instance.action(identifier(instance, "Target"), "Target", "invoke")
    assert backend.mutation_count == 0


def test_subtree_expansion_replaces_old_descendants_and_keeps_context() -> None:
    nested = node("Region", "Pane", children=(node("Target"),))
    instance, _ = engine(node("Root", "Window", children=(nested,)))
    root = identifier(instance, "Region")
    old = identifier(instance, "Target")
    instance.observe(root_id=root)
    assert sum(item["name"] == "Target" for item in instance.elements) == 1
    assert instance.resolve(query="Target", root_id=root)["status"] == "unique"
    with pytest.raises(UIAError, match="stale_reference"):
        instance.reference(old)


def test_independent_verification_does_not_accept_name_as_content() -> None:
    instance, _ = engine(node("Expected", "Edit", text="other", patterns=("Value",)))
    ref = identifier(instance, "Expected")
    assert not instance.verify(ref, "Expected", expected_text="Expected")["verified"]
    assert instance.verify(ref, "Expected", expected_text="Expected", property_name="name")["verified"]
    assert instance.verify(ref, "Expected", expected_text="other")["verified"]


def test_the_controls_own_content_is_answered_before_anything_under_it() -> None:
    instance, _ = engine(node("Field", "Edit", text="Michael Jackson", patterns=("Value",)))
    outcome = instance.verify(identifier(instance, "Field"), "Field", expected_text="Michael Jackson")
    assert outcome["verified"] and outcome["source"] == "Value"


def test_verification_reads_the_label_a_chromium_row_renders_underneath_it() -> None:
    """Measured 2026-10-09 on Spotify's own search results.

    The result row (``DataItem`` "Michael Jackson") and every node under it answered
    ``LegacyIAccessible`` with an empty value while the artist's name existed only as their
    accessible Name. A search that had plainly worked could not be confirmed through that
    control, and the run ended as "sucesso não confirmado".
    """
    row = node("Michael Jackson", "DataItem", children=(
        node("Michael Jackson", "Group", children=(node("Michael Jackson", "Hyperlink"),)),
        node("Artista", "DataItem"),
        node("Mais opções para Michael Jackson", "DataItem",
             children=(node("Mais opções para Michael Jackson", "Button"),))))
    instance, backend = engine(row)
    ref = identifier(instance, "Michael Jackson")
    outcome = instance.verify(ref, "Michael Jackson", expected_text="Michael Jackson")
    assert outcome["verified"] and outcome["source"] == "rendered_subtree"
    assert outcome["observed"]["rendered_nodes"] > 0
    # A label this row does not render under it is not verified, and nothing is touched.
    assert not instance.verify(ref, "Michael Jackson", expected_text="Billie Jean")["verified"]
    assert backend.mutation_count == 0


def test_a_controls_own_label_is_never_its_content() -> None:
    """The identity the caller already asserted cannot be the proof it is asking for."""
    instance, _ = engine(node("Michael Jackson", "DataItem"))
    ref = identifier(instance, "Michael Jackson")
    assert not instance.verify(ref, "Michael Jackson", expected_text="Michael Jackson")["verified"]
    # An empty field is still provable as empty through the same reading.
    assert instance.verify(ref, "Michael Jackson", expected_text="")["verified"]


def test_verification_never_reads_a_protected_control_underneath() -> None:
    row = node("Conta", "DataItem", children=(node("Senha secreta", "Edit", protected=True),))
    instance, _ = engine(row)
    outcome = instance.verify(identifier(instance, "Conta"), "Conta", expected_text="Senha secreta")
    assert not outcome["verified"] and outcome["observed"]["rendered_nodes"] == 1


@pytest.mark.parametrize("direction,key,expected", [("down", "scroll_y", 1), ("up", "scroll_y", -1),
                                                     ("right", "scroll_x", 1), ("left", "scroll_x", -1)])
def test_scroll_uses_correct_small_amount_enums(direction: str, key: str, expected: int) -> None:
    target = node("List", "List", patterns=("Scroll",))
    instance, backend = engine(target)
    assert instance.action(identifier(instance, "List"), "List", "scroll", direction=direction)["verified"]
    assert target[key] == expected and backend.mutation_count == 1


def test_native_text_reader_never_reads_name_or_protected_data() -> None:
    from app.computer.uia import NativeBackend
    backend = NativeBackend.__new__(NativeBackend)
    backend.pattern = lambda element, name: SimpleNamespace(CurrentValue="", DocumentRange=SimpleNamespace(
        GetText=lambda length: "content"))
    assert backend.content(object(), {"protected": False, "patterns": ["Value"]})["text"] == ""
    assert backend.content(object(), {"protected": False, "patterns": ["Text"]})["text"] == "content"
    assert not backend.content(object(), {"protected": False, "patterns": []})["readable"]
    with pytest.raises(UIAError, match="protected"):
        backend.content(object(), {"protected": True, "patterns": ["Value"]})


def test_keyboard_text_fallback_blocks_enter_and_tab() -> None:
    instance, backend = engine(node("Editor", "Document", patterns=("Text",)))
    backend.writable = lambda element, meta: "keyboard"
    with pytest.raises(UIAError, match="unsafe_text_fallback"):
        instance.set_text(identifier(instance, "Editor"), "Editor", "line1\nline2")
    assert backend.mutation_count == 0


def test_normalization_preserves_whitespace() -> None:
    assert normalized("  a\r\nb\r\n\n") == "  a\nb\n\n"


# --- FASE B: deeper observation and the Control/Raw View decision -------------------


class _ViewBackend(NativeBackend):
    """A NativeBackend with canned trees, so the view decision is testable without COM."""

    trees: dict[str, Any] = {}
    # Which view served each read, so a follow-up operation's backend is observable.
    used: list[str] = []

    def __init__(self, *, use_cache: bool = True, view: str = "control") -> None:
        self.use_cache = use_cache
        self.view = view
        self.calls = 0
        self.tree = _ViewBackend.trees[view]

    def content(self, element: Any, meta: Any) -> Any:
        _ViewBackend.used.append(self.view)
        return {"readable": True, "text": element.get("text", ""), "truncated": False,
                "source": "Value"}

    def window_key(self, window: Any) -> Any:
        return (10, 1, 100.0)

    def root(self, window: Any) -> Any:
        self.calls += 1
        return self.tree

    def metadata(self, element: Any) -> Any:
        return dict(element["meta"])

    def children(self, element: Any) -> Any:
        self.calls += 1
        return element["children"]

    def wrap(self, element: Any, meta: Any) -> Any:
        return element

    def belongs(self, element: Any, window: Any) -> bool:
        return element["alive"]

    def refresh(self, element: Any) -> Any:
        return element


def _thin_control_tree() -> Any:
    # Two panes, one actionable control: the Spotify-in-Control-View shape.
    return node("Root", "Window", children=(
        node("Pane", "Pane", patterns=("ScrollItem",), children=(
            node("Start", "Button", patterns=("Invoke",)),)),))


def _rich_raw_tree() -> Any:
    """A well-populated window: ten actionable controls. At or above THIN_ACTIONABLE."""
    return node("Root", "Window", children=(
        node("Search", "Edit", patterns=("Value", "Text")),
        node("Play", "Button", patterns=("Invoke",)),
        node("Home", "Button", patterns=("Invoke",)),
        node("Next", "Button", patterns=("Invoke",)),
        node("Previous", "Button", patterns=("Invoke",)),
        node("Shuffle", "Button", patterns=("Toggle",)),
        node("Repeat", "Button", patterns=("Toggle",)),
        node("Mute", "Button", patterns=("Invoke",)),
        node("List", "List", patterns=("ItemContainer", "Scroll"), children=(
            node("Track", "ListItem", patterns=("SelectionItem",)),)),
        node("Volume", "Slider", patterns=("Value",)),
    ))


def _count(root: Any) -> int:
    return 1 + sum(_count(child) for child in root["children"])


def _view_engine(monkeypatch: pytest.MonkeyPatch, control: Any, raw: Any) -> UIAEngine:
    _ViewBackend.trees = {"control": control, "raw": raw}
    monkeypatch.setattr("app.computer.uia.NativeBackend", _ViewBackend)
    state = SimpleNamespace(selected_app_window=SimpleNamespace(handle=10), control_dict={})
    return UIAEngine(state)


def test_a_thin_control_view_escalates_to_the_richer_raw_view(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured failure mode: a Chromium host hides its real controls from Control View."""
    instance = _view_engine(monkeypatch, _thin_control_tree(), _rich_raw_tree())
    observed = instance.observe()
    assert observed["view"] == "raw"
    assert observed["views_tried"] == ["control", "raw"]
    assert observed["attempts"]["control"]["actionable"] == 1
    assert observed["attempts"]["raw"]["actionable"] > 1
    # The committed rows are the rich tree's, and they resolve normally.
    assert instance.resolve(query="Search")["status"] == "unique"
    assert len(instance.elements) == _count(_rich_raw_tree())


def test_metrics_count_raw_collection_even_when_control_view_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = _view_engine(monkeypatch, _thin_control_tree(), node("Root", "Window"))
    observed = instance.observe()
    assert observed["view"] == "control" and observed["views_tried"] == ["control", "raw"]
    assert observed["controls_inspected"] == sum(a["elements"] for a in observed["attempts"].values())
    assert observed["native_calls_total"] == sum(a["native_calls"] for a in observed["attempts"].values())


def test_a_rich_control_view_is_kept_and_the_raw_view_is_not_even_read(
        monkeypatch: pytest.MonkeyPatch) -> None:
    instance = _view_engine(monkeypatch, _rich_raw_tree(), _rich_raw_tree())
    observed = instance.observe()
    assert observed["view"] == "control"
    assert observed["views_tried"] == ["control"]
    assert observed["attempts"].keys() == {"control"}


def test_an_explicit_view_is_never_second_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = _view_engine(monkeypatch, _thin_control_tree(), _rich_raw_tree())
    observed = instance.observe(view="control")
    assert observed["view"] == "control" and observed["views_tried"] == ["control"]
    assert observed["actionable"] == 1
    assert instance.resolve(query="Search")["status"] == "not_found"


def test_an_unknown_view_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = _view_engine(monkeypatch, _thin_control_tree(), _rich_raw_tree())
    with pytest.raises(UIAError, match="invalid_arguments"):
        instance.observe(view="everything")


def test_deeper_defaults_reach_a_tree_the_shallow_default_missed(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Depth is a perception budget: measured trees saturate at 12, not at 8."""
    deep = node("Window", "Window", children=tuple(
        node(f"Nivel {index}", "Pane", children=(node(f"Botao {index}", "Button", patterns=("Invoke",)),))
        for index in range(12)))
    instance = _view_engine(monkeypatch, deep, deep)
    shallow = instance.observe(depth=1, max_nodes=64)
    assert shallow["truncated"] and shallow["actionable"] == 0
    deep_observed = instance.observe()
    assert not deep_observed["truncated"] and deep_observed["actionable"] == 12


def test_operations_follow_the_view_the_element_was_read_from(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A Raw View element is acted on through the Raw View backend.

    On a Chromium/Electron window the observation escalates to the Raw View, and the
    recorded ``view`` is what keeps every follow-up read on the same tree instead of
    comparing one provider's element against the other's. Measured as the shape of the
    Spotify/VS Code trees; resolved in one place (``_referenced``).
    """
    _ViewBackend.used = []
    instance = _view_engine(monkeypatch, _thin_control_tree(), _rich_raw_tree())
    assert instance.observe()["view"] == "raw"
    reference = next(item["id"] for item in instance.elements if item["name"] == "Search")
    instance.text(reference, "Search")
    assert _ViewBackend.used == ["raw"]


def test_scoped_observation_never_proves_absence_or_uniqueness() -> None:
    """A filtered list is a targeting aid, not a complete region (FASE 2.1)."""
    window = node("Window", "Window", children=(node("Editar", "Edit", patterns=("Value",)),
                                                node("Botão", "Button", patterns=("Invoke",))))
    instance, backend = engine(window)
    scoped = instance.observe(only_types=("Edit",))
    assert scoped["scoped"] and not scoped["complete"]
    assert [item["type"] for item in scoped["elements"]] == ["Edit"]
    # Neither a name inside the filter nor one outside it can be certified from here.
    assert instance.resolve(query="Editar")["status"] == "incomplete"
    assert instance.resolve(query="Botão")["status"] == "incomplete"
    assert backend.mutation_count == 0
    complete = instance.observe()
    assert not complete["scoped"] and complete["complete"]
    assert instance.resolve(query="Editar")["status"] == "unique"
    assert instance.resolve(query="Botão")["status"] == "unique"


def test_snapshot_is_reused_until_the_tree_is_invalidated() -> None:
    """FASE 2.2: a batch reuses the snapshot; navigation invalidates it."""
    field = node("Campo", "Edit", text="", patterns=("Value",))
    open_button = node("Abrir", "Button", patterns=("Invoke",))
    instance, backend = engine(node("Window", "Window", children=(field, open_button)))
    calls = backend.calls
    assert instance.resolve(query="Campo")["status"] == "unique"
    assert backend.calls == calls  # finding candidates did not rewalk the tree
    instance.set_text(identifier(instance, "Campo"), "Campo", "texto", replace=True)
    assert instance.resolve(query="Campo")["status"] == "unique"
    assert backend.calls > calls  # a write can populate search results or validation controls
    calls = backend.calls
    instance.action(identifier(instance, "Abrir"), "Abrir", "invoke")
    assert instance.tree_invalidated
    assert instance.resolve(query="Campo")["status"] == "unique"
    assert backend.calls > calls  # navigation invalidated the snapshot


def test_collection_is_metered_and_reuse_is_counted() -> None:
    """FASE 2.1 measurement: collect_ms/collect_calls/collect_reused are real numbers."""
    instance, backend = engine(node("Window", "Window", children=(node("A"),)))
    assert instance.collect_calls == 1 and instance.collect_ms > 0
    calls = backend.calls
    cached = instance.observe(reuse=True)
    assert cached["reused"] and cached["observation_ms"] == 0.0
    assert instance.collect_reused == 1 and backend.calls == calls
    instance.invalidate_tree()
    fresh = instance.observe(reuse=True)
    assert "reused" not in fresh
    assert instance.collect_calls == 2 and backend.calls > calls


def test_actionable_counts_only_patterns_a_user_can_act_on(monkeypatch: pytest.MonkeyPatch) -> None:
    tree = node("Window", "Window", children=(
        node("Label", "Text"),
        node("Button", "Button", patterns=("Invoke",)),
        node("Slider", "Slider", patterns=("Scroll",)),
        node("Stale", "Pane", patterns=("LegacyIAccessible",))))
    instance = _view_engine(monkeypatch, tree, tree)
    assert instance.observe(view="control")["actionable"] == 2


def test_a_scoped_expansion_reaches_the_next_round() -> None:
    """A-02: an expanded region must survive into the next round's control list.

    Measured on Spotify (`logs/_a02_loop_repro.json`): the round's cold pass lists 9
    generic Chrome panes, the AppAgent expands the window pane with `types=...`, and
    `observe_progressive` skipped the union because that reading was scoped, so the next
    prompt got the same 9 panes again — 19 identical expansions and PLANNING_FAILURE
    (`logs/uia-v2-app-20261011-212837.json`). A scoped reading is incomplete and must stay
    reported as such, but dropping it from the next prompt turns one narrowed read into a
    dead end.
    """
    deeper = node("Fundo", "Pane", children=(node("Campo", "Edit", patterns=("Value",)),))
    root = node("Window", "Window", children=(
        node("A", "Pane", children=(node("B", "Pane", children=(deeper,)),)),))
    backend = Backend(root)
    state = SimpleNamespace(selected_app_window=SimpleNamespace(handle=10), control_dict={})
    instance = UIAEngine(state, backend=backend)  # cold: the round's own progressive pass
    top = instance.observe_progressive()
    assert top["stage"] == "top"
    assert "Campo" not in [item["name"] for item in top["elements"]]
    expanded = instance.observe(root_id=identifier(instance, "B"), only_types=("Edit",))
    assert expanded["scoped"] and [item["name"] for item in expanded["elements"]] == ["B", "Campo"]
    following = instance.observe_progressive()
    assert following["stage"] == "expanded" and following["reused"]
    assert "Campo" in [item["name"] for item in following["elements"]]
    assert following["scoped"] is True and following["complete"] is False


@pytest.mark.parametrize("pattern, operation", [("Invoke", "invoke"), ("ExpandCollapse", "expand")])
def test_menu_items_preserve_functional_native_patterns(pattern: str, operation: str) -> None:
    item = node("Command", "MenuItem", patterns=(pattern,))
    item["meta"]["offscreen"] = True
    instance, backend = engine(item)
    result = instance.click(identifier(instance, "Command"), "Command")
    assert result["operation"] == operation
    assert backend.pattern_calls and backend.clicked == []


def test_menu_without_native_activation_uses_live_geometry_without_control_focus() -> None:
    item = node("Help", "MenuItem", patterns=("LegacyIAccessible",))
    item["meta"]["focusable"] = False
    instance, backend = engine(item)
    result = instance.click(identifier(instance, "Help"), "Help")
    assert backend.pattern_calls == []
    assert backend.atomics == [("click_input", {"button": "left", "double": False})]
    assert backend.activations == 1 and backend.focus_calls == 0
    assert result["operation"] == "click_input" and result["needs_postcondition"]


@pytest.mark.parametrize("access, keys, parent_type, valid, duplicate, offscreen, foreground", [
    ("Alt+a", "%a", "MenuBar", True, False, False, 10),
    ("s", "s", "Menu", True, False, False, 10),
    ("s", "s", "MenuBar", False, False, False, 10),
    ("Alt+a", "%x", "MenuBar", False, False, False, 10),
    ("Alt+a{ENTER}", "%a{ENTER}", "MenuBar", False, False, False, 10),
    ("", "s", "Menu", False, False, False, 10),
    ("", "{ESC}", "Menu", True, False, False, 10),
    ("s", "s", "Menu", False, True, False, 10),
    ("s", "s", "Menu", False, False, True, 10),
    ("s", "s", "Menu", False, False, False, 99),
])
def test_menu_keyboard_uses_only_published_keys_in_the_observed_context(
        monkeypatch, access: str, keys: str, parent_type: str, valid: bool,
        duplicate: bool, offscreen: bool, foreground: int) -> None:
    from app.computer import windows_tools

    backend = NativeBackend.__new__(NativeBackend)
    backend.client = SimpleNamespace(ControlViewWalker=SimpleNamespace(GetParentElement=lambda element: "parent"),
                                     CompareElements=lambda left, right: left == right)
    backend.refresh = lambda element: element
    backend.metadata = lambda element: ({"type": parent_type, "offscreen": offscreen} if element == "parent"
                                       else {"type": "MenuItem", "access_key": access})
    backend.children = lambda parent: ["item", "other"] if duplicate else ["item"]
    backend.activate = lambda window: None
    sent = []
    backend.atomic = lambda target, window, method, args: sent.append((target, method, args))
    backend.focus = lambda *args: pytest.fail("Menu mnemonic must not focus an unfocusable MenuItem")
    monkeypatch.setattr(windows_tools, "foreground_handle", lambda: foreground)
    window = SimpleNamespace(handle=10)
    meta = {"type": "MenuItem", "access_key": access}
    if valid:
        backend.keys("item", window, meta, keys)
        assert len(sent) == 1 and sent[0][0] is window
        assert sent[0][1] == "type_keys" and sent[0][2]["keys"] == keys
        assert sent[0][2]["vk_packet"] is False
    else:
        with pytest.raises(UIAError):
            backend.keys("item", window, meta, keys)
        assert sent == []


def test_menu_keyboard_refreshes_the_uncached_walker_parent(monkeypatch) -> None:
    from app.computer import windows_tools
    backend = NativeBackend.__new__(NativeBackend)
    cached_parent = object()
    parent = SimpleNamespace(BuildUpdatedCache=lambda request: cached_parent)
    backend.use_cache, backend.cache, backend.calls = True, object(), 0
    backend.client = SimpleNamespace(ControlViewWalker=SimpleNamespace(GetParentElement=lambda element: parent),
                                     CompareElements=lambda left, right: left == right)

    def metadata(element):
        if element is parent:
            raise RuntimeError("walker parent has no requested cache")
        return ({"type": "MenuBar", "offscreen": False} if element is cached_parent
                else {"type": "MenuItem", "access_key": "Alt+a"})

    backend.metadata = metadata
    backend.children = lambda element: ["item"]
    backend.activate = lambda window: None
    sent = []
    backend.atomic = lambda target, window, method, args: sent.append(args)
    monkeypatch.setattr(windows_tools, "foreground_handle", lambda: 10)
    backend.keys("item", SimpleNamespace(handle=10), {"type": "MenuItem", "access_key": "Alt+a"}, "%a")
    assert len(sent) == 1 and sent[0]["keys"] == "%a"


def test_explicit_live_click_preserves_native_defaults_and_requires_postcondition() -> None:
    item = node("Help", "MenuItem", patterns=("Invoke", "ExpandCollapse"))
    item["meta"]["focusable"] = False
    instance, backend = engine(item)
    result = instance.action(identifier(instance, "Help"), "Help", "click_input")
    assert backend.pattern_calls == [] and backend.focus_calls == 0
    assert backend.activations == 1
    assert backend.atomics == [("click_input", {"button": "left", "double": False})]
    assert result["executed"] and not result["verified"] and result["needs_postcondition"]
    with pytest.raises(UIAError, match="mutation_unverified"):
        instance.action(identifier(instance, "Help"), "Help", "invoke")
    assert len(backend.atomics) == 1


def test_ambiguous_menu_key_is_a_preflight_refusal_not_an_uncertain_dispatch() -> None:
    item = node("Help", "MenuItem", patterns=("Invoke", "ExpandCollapse"))
    item["meta"]["access_key"] = "Alt+a"
    other = node("File", "MenuItem")
    other["meta"]["access_key"] = "Alt+a"
    parent = node("Bar", "MenuBar", children=(item, other))
    instance, backend = engine(parent)
    backend.client = SimpleNamespace(ControlViewWalker=SimpleNamespace(GetParentElement=lambda element: parent),
                                     CompareElements=lambda left, right: left is right)
    backend.keys = lambda *args: NativeBackend.keys(backend, *args)
    with pytest.raises(UIAError, match="^ambiguous:"):
        instance.keyboard(identifier(instance, "Help"), "Help", "%a")
    assert instance.evidence == [] and backend.atomics == [] and backend.activations == 0
    result = instance.action(identifier(instance, "Help"), "Help", "click_input")
    assert result["executed"] and not result["verified"]
    assert len(backend.atomics) == 1

"""Window-scoped UIA tools for the existing UFO AppAgent, not another planner.

Native CacheRequest batches properties within each provider call. Snapshots are
never postconditions: resolve/act re-read live identity, membership and state.
Source: https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading
Source: https://github.com/microsoft/winappCli/blob/main/docs/ui-automation.md
Source: https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationelement-get_currentaccesskey
"""
from __future__ import annotations

import hashlib
import time
import re
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from app.computer.controls import fold
from app.computer.approval import submit_keys
from app.computer.verification import covers_dispatch, recovered_dispatch_indices

PATTERNS = ("Invoke", "Value", "Text", "Selection", "SelectionItem", "Toggle",
            "ExpandCollapse", "Scroll", "ScrollItem", "ItemContainer", "VirtualizedItem",
            "Window", "LegacyIAccessible")
PROPERTIES = {
    "name": "Name", "automation_id": "AutomationId", "runtime_id": "RuntimeId",
    "type": "ControlType", "class_name": "ClassName", "framework": "FrameworkId",
    "pid": "ProcessId", "rect": "BoundingRectangle", "enabled": "IsEnabled",
    "offscreen": "IsOffscreen", "focusable": "IsKeyboardFocusable", "focused": "HasKeyboardFocus",
    "protected": "IsPassword", "native_handle": "NativeWindowHandle", "access_key": "AccessKey",
}
MAX_REFERENCES = 1600
MAX_TEXT = 8000
# How much of a control's subtree one verification may read when the control's own content
# cannot answer (``UIAEngine._rendered_text``). Bounded because this happens at the moment
# a postcondition is checked, not as a second observation: a handful of native calls.
RENDERED_DEPTH = 3
RENDERED_NODES = 60
# Measured on real windows (logs/uia-diagnose.json): depth 8 was heavily truncated
# (Discord 10 vs 31 elements), so the budget was 12. Measured again 2026-10-09 on the same
# machine, at depth 12 Spotify's Web Player stayed out of reach: the Chromium/CEF shell was
# read (197 elements, 58 actionable, search field absent), while depth 16 reached the
# application's own controls (325 elements, 148 actionable) with its search field at depth
# 13. An application's content sits deeper than its window frame, so the budget has to
# cover a frame plus a page: the *context* budget stays separate and narrow
# (app/computer/controls.py ranks what is sent to the model).
# The maxima any caller may ask for. A request above them is served *at* the maximum
# instead of being refused: the read is bounded by the engine either way, while refusing
# costs the round it was asked in. Measured 2026-10-11 (A-02, Spotify): the AppAgent
# expanded the region that lacked its target with depth=20/max_nodes=1000 — the documented
# remedy — and the round ended in `invalid_arguments` with the target still unread
# (`logs/_a02_find_probe.json`).
MAX_DEPTH = 16
MAX_NODES = 600
MAX_TIMEOUT_MS = 5000
DEFAULT_DEPTH = MAX_DEPTH
DEFAULT_MAX_NODES = MAX_NODES
# Patterns that mean "a user could act on this element".
ACTIONABLE_PATTERNS = frozenset({"Invoke", "Value", "Toggle", "SelectionItem",
                                 "ExpandCollapse", "Scroll"})
# Below this many actionable controls the Control View is the thin-tree case: measured on
# Spotify it exposed 5 usable controls where the Raw View exposed 45 (183 vs 27 elements).
THIN_ACTIONABLE = 8


class UIAError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class UIAPreconditionError(UIAError):
    """A local guard refused before any input or focus operation was attempted."""


def normalized(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


VIEWS = ("control", "raw")


class NativeBackend:
    """One backend: the COM/UIA interfaces already used by pywinauto and UFO.

    ``view`` selects which tree the provider is asked for. The Control View hides
    elements that do not implement UIA properly, which is why it can be almost empty
    for a Chromium/Electron host; the Raw View still walks them. Both are the same
    automation tree, so a raw element is still a real element of the same window.
    """

    def __init__(self, *, use_cache: bool = True, view: str = "control") -> None:
        self.use_cache = use_cache
        self.view = view if view in VIEWS else "control"
        from pywinauto.uia_defines import IUIA
        self.uia = IUIA()
        self.client = self.uia.iuia
        self.dll = self.uia.UIA_dll
        self.condition = (self.client.RawViewCondition if self.view == "raw"
                          else self.client.ControlViewCondition)
        self.cache = self.client.CreateCacheRequest()
        self.cache.TreeScope = self.uia.tree_scope["element"]
        self.cache.TreeFilter = self.condition
        for name in (*PROPERTIES.values(), *(f"Is{pattern}PatternAvailable" for pattern in PATTERNS)):
            self.cache.AddProperty(getattr(self.dll, f"UIA_{name}PropertyId"))
        self.calls = 0

    def window_key(self, window: Any) -> tuple[int, int, float]:
        import psutil
        from app.computer.windows_tools import user32, window_pid
        handle = int(window.handle)
        if not user32.IsWindow(handle):
            raise UIAError("window_gone", "A janela selecionada não existe.")
        pid = window_pid(handle)
        return handle, pid, psutil.Process(pid).create_time()

    def root(self, window: Any) -> Any:
        self.calls += 1
        return (self.client.ElementFromHandleBuildCache(int(window.handle), self.cache) if self.use_cache
                else self.client.ElementFromHandle(int(window.handle)))

    def refresh(self, element: Any) -> Any:
        self.calls += 1
        return element.BuildUpdatedCache(self.cache) if self.use_cache else element

    def metadata(self, element: Any) -> dict[str, Any]:
        read = element.GetCachedPropertyValue if self.use_cache else element.GetCurrentPropertyValue
        if not self.use_cache:
            self.calls += len(PROPERTIES) + len(PATTERNS)
        result = {key: read(getattr(self.dll, f"UIA_{name}PropertyId"))
                  for key, name in PROPERTIES.items()}
        # A provider may answer None for ControlType. An unknown type is "Custom", never a
        # failed read of the whole subtree.
        result["type"] = self.uia.known_control_type_ids.get(int(result["type"] or 0), "Custom")
        result["runtime_id"] = tuple(result["runtime_id"] or ())
        result["rect"] = list(result["rect"] or ())
        for key in ("enabled", "offscreen", "focusable", "focused", "protected"):
            result[key] = bool(result[key])
        result["patterns"] = [pattern for pattern in PATTERNS if read(
            getattr(self.dll, f"UIA_Is{pattern}PatternAvailablePropertyId"))]
        # A provider can put a password into its Name. Never deliver it to a model.
        if result["protected"]:
            result["name"] = "[protected]"
        return result

    def children(self, element: Any) -> list[Any]:
        self.calls += 1
        array = (element.FindAllBuildCache(self.uia.tree_scope["children"], self.condition,
                                          self.cache) if self.use_cache
                 else element.FindAll(self.uia.tree_scope["children"], self.condition))
        # FindAll is native and cannot be interrupted by a Python deadline. The worker
        # remains the hard timeout boundary; cap materialization, not just prompt size.
        return [array.GetElement(i) for i in range(min(array.Length, MAX_REFERENCES + 1))]

    def belongs(self, element: Any, window: Any) -> bool:
        root = self.root(window)
        walker = (self.client.RawViewWalker if self.view == "raw" else self.client.ControlViewWalker)
        current = element
        for _ in range(64):
            if not current:
                return False
            self.calls += 1
            if self.client.CompareElements(current, root):
                return True
            current = walker.GetParentElement(current)
        return False

    def wrap(self, element: Any, meta: dict[str, Any]) -> Any:
        """A pywinauto wrapper for one observed element, never at the cost of the read.

        Some pywinauto specializations (ToolBar, for one) build a Win32 sub-wrapper from
        the element's native handle, so an element the provider exposes without one —
        common in a Chromium/Electron window — raised InvalidWindowHandle and took the
        whole observation with it (measured on Spotify's toolbar). The generic UIA
        wrapper still offers focus, keys and clicks, so the read survives and only the
        specialization is lost.
        """
        from pywinauto.controls.uiawrapper import UIAWrapper
        from pywinauto.uia_element_info import UIAElementInfo
        info = UIAElementInfo(element, cache_enable=True)
        info._cached_name = meta["name"]
        info._cached_control_type = meta["type"]
        info._cached_class_name = meta["class_name"]
        info._cached_visible = not meta["offscreen"]
        info._cached_handle = meta["native_handle"]
        try:
            return UIAWrapper(info)
        except Exception:
            info._cached_control_type = "Custom"
            return UIAWrapper(info)

    def pattern(self, element: Any, name: str) -> Any:
        from pywinauto.uia_defines import get_elem_interface
        self.calls += 1
        return get_elem_interface(element, name)

    def content(self, element: Any, meta: dict[str, Any]) -> dict[str, Any]:
        if meta["protected"]:
            raise UIAError("protected", "Leitura de campo protegido bloqueada.")
        for pattern in ("Value", "Text", "LegacyIAccessible"):
            if pattern not in meta["patterns"]:
                continue
            interface = self.pattern(element, pattern)
            if pattern == "Text":
                value = str(interface.DocumentRange.GetText(MAX_TEXT + 1))
            else:
                value = str(interface.CurrentValue)
            # Empty values are still readable; no fallback to the accessible Name.
            return {"readable": True, "text": normalized(value[:MAX_TEXT]),
                    "source": pattern, "truncated": len(value) > MAX_TEXT}
        return {"readable": False, "text": "", "source": "", "truncated": False}

    def writable(self, element: Any, meta: dict[str, Any]) -> str:
        if "Value" in meta["patterns"]:
            if self.pattern(element, "Value").CurrentIsReadOnly:
                raise UIAError("read_only", "O campo não permite escrita.")
            return "Value"
        if "LegacyIAccessible" in meta["patterns"]:
            legacy = self.pattern(element, "LegacyIAccessible")
            # ROLE_SYSTEM_TEXT and absence of STATE_SYSTEM_READONLY.
            if legacy.CurrentRole == 42 and not legacy.CurrentState & 0x40:
                return "LegacyIAccessible"
        if "Text" in meta["patterns"] and meta["focusable"]:
            readonly = self.pattern(element, "Text").DocumentRange.GetAttributeValue(
                self.dll.UIA_IsReadOnlyAttributeId)
            if readonly is False or readonly == 0:
                return "keyboard"
        raise UIAError("not_editable", "Nenhum mecanismo de edição comprovado neste controle.")

    def atomic(self, target: Any, window: Any, method: str, arguments: dict[str, Any]) -> Any:
        from ufo.automator.ui_control.controller import AtomicCommand, ControlReceiver
        # UFO's constructor unconditionally focuses and waits 10 s. Preconditions and
        # explicit focus are handled here; pattern-only calls must not inject input.
        receiver = ControlReceiver.__new__(ControlReceiver)
        receiver.control, receiver.application = target, window
        result = AtomicCommand(receiver, method, arguments).execute()
        if isinstance(result, str) and (result.startswith("An error occurred")
                                       or "doesn't have a method named" in result):
            raise UIAError("execution_failed", "UFO AtomicCommand recusou a operação; não repetir.")
        return result

    def call(self, element: Any, window: Any, pattern: str, method: str, *arguments: Any) -> Any:
        # Win32 menu proxies can inject keyboard input inside their native pattern.
        # Acquire the observed window before dispatch, never retry the mutation.
        if self.metadata(element)["type"] == "MenuItem":
            self.activate(window)
        interface = self.pattern(element, pattern)

        class PatternTarget:
            def apply(self) -> Any:
                return getattr(interface, method)(*arguments)

        return self.atomic(PatternTarget(), window, "apply", {})

    # A foreground change is not instantaneous, and the first request can be refused while
    # the window that holds the keyboard is still finishing its own activation. Measured
    # 2026-10-09 in the chat ("pesquisa por michel jackson no spotify"): the AppAgent had
    # typed the query into Spotify's search field and the Enter that submits it failed with
    # `focus_lost` on the single attempt, so the step was lost and the search was never
    # submitted; the same keystroke from a plain console found the foreground immediately.
    # The grace is spent only when the window does not come forward at once.
    FOCUS_ATTEMPTS = 3
    FOCUS_RETRY_SECONDS = 0.2

    def _granted(self, window: Any) -> bool:
        """Whether this window is the foreground window, asking for it once."""
        from app.computer.windows_tools import foreground_handle, foreground_window
        if foreground_window(int(window.handle)):
            return True
        return foreground_handle() == int(window.handle)

    def activate(self, window: Any) -> None:
        for attempt in range(self.FOCUS_ATTEMPTS):
            if self._granted(window):
                break
            if attempt + 1 == self.FOCUS_ATTEMPTS:
                raise UIAError("focus_lost", "Janela não recebeu o primeiro plano.")
            time.sleep(self.FOCUS_RETRY_SECONDS)

    def focus(self, element: Any, window: Any, meta: dict[str, Any]) -> None:
        from app.computer.windows_tools import foreground_handle
        if not meta["focusable"] or meta["offscreen"]:
            raise UIAError("not_focusable", "Controle não pode receber teclado neste estado.")
        self.activate(window)
        wrapper = self.wrap(element, meta)
        self.atomic(wrapper, window, "set_focus", {})
        deadline = time.perf_counter() + self.FOCUS_ATTEMPTS * self.FOCUS_RETRY_SECONDS
        while True:
            if foreground_handle() == int(window.handle) and wrapper.has_keyboard_focus():
                return
            if time.perf_counter() >= deadline:
                raise UIAError("focus_lost", "Foco de teclado não corresponde ao controle.")
            time.sleep(self.FOCUS_RETRY_SECONDS)

    def keys(self, element: Any, window: Any, meta: dict[str, Any], keys: str) -> None:
        target = window
        if meta["type"] in {"Menu", "MenuBar"} and keys.casefold() not in {"{esc}", "{escape}"}:
            raise UIAPreconditionError("unsafe_context", "Entrada em menu exige o item de destino, não o container.")
        if meta["type"] == "MenuItem":
            if keys.casefold() not in {"{esc}", "{escape}"}:
                # AccessKey is provider data; never derive a mnemonic from the label.
                access = re.fullmatch(r"(Alt\+)?([A-Za-z0-9])", str(meta.get("access_key", "")), re.IGNORECASE)
                expected = ("%" if access and access[1] else "") + (access[2] if access else "")
                if not access or keys.casefold() != expected.casefold():
                    raise UIAPreconditionError("unsafe_context", "Tecla não corresponde ao AccessKey publicado do menu.")
                parent = self.client.ControlViewWalker.GetParentElement(element)
                if parent:
                    parent = self.refresh(parent)
                parent_meta = self.metadata(parent) if parent else {}
                if parent_meta.get("type") != ("MenuBar" if access[1] else "Menu") \
                        or parent_meta.get("offscreen") or meta.get("offscreen"):
                    raise UIAPreconditionError("unsafe_context", "AccessKey exige barra ou submenu observado correspondente.")
                siblings = self.children(parent)
                if len(siblings) > MAX_REFERENCES:
                    raise UIAPreconditionError("incomplete", "Menu parcial não comprova unicidade do AccessKey.")
                matches = [item for item in siblings if str(self.metadata(item).get("access_key", "")).casefold()
                           == str(meta["access_key"]).casefold()]
                if len(matches) != 1 or not self.client.CompareElements(matches[0], element):
                    raise UIAPreconditionError("ambiguous", "AccessKey não identifica um único item deste menu.")
            from app.computer.windows_tools import foreground_handle
            self.activate(window)
            if foreground_handle() != int(window.handle):
                raise UIAError("focus_lost", "Atalho de menu recusado fora da janela selecionada.")
        elif meta["type"] == "Window":
            if submit_keys(keys) or not re.fullmatch(
                    r"[\^%+]+(?:[a-zA-Z0-9]|\{[A-Za-z0-9_]+\})|\{(?:ESC|TAB|UP|DOWN|LEFT|RIGHT|HOME|END|PGUP|PGDN|F\d{1,2})\}",
                    keys, flags=re.IGNORECASE):
                raise UIAError("unsafe_context", "Texto/Enter exige o controle de destino, não a janela.")
            from app.computer.windows_tools import foreground_handle
            self.activate(window)
            focused = self.client.GetFocusedElement()
            if not focused or not self.belongs(focused, window) or foreground_handle() != int(window.handle):
                raise UIAError("focus_lost", "Atalho recusado: foco fora da janela selecionada.")
        else:
            self.focus(element, window, meta)
            target = self.wrap(element, meta)
        self.atomic(target, window, "type_keys",
                    {"keys": keys, "pause": 0.02, "with_spaces": True, "vk_packet": meta["type"] != "MenuItem",
                     "set_foreground": False})

    def state(self, element: Any, pattern: str, property_name: str) -> Any:
        return getattr(self.pattern(element, pattern), property_name)


@dataclass
class Reference:
    element: Any
    meta: dict[str, Any]
    window_key: Any
    # Which tree the element came from; a raw element must be refreshed through the same
    # view it was read from, or its identity check would compare different providers.
    view: str = "control"


class UIAEngine:
    def __init__(self, state: Any, *, backend: Any = None,
                 evidence: list[dict[str, Any]] | None = None) -> None:
        self.state = state
        self.backend = backend
        self.backends: dict[str, Any] = {}
        self.evidence = evidence if evidence is not None else []
        self.references: dict[str, Reference] = {}
        self.elements: list[dict[str, Any]] = []
        self.generation = 0
        self.window_key: Any = None
        self.last_observation: dict[str, Any] = {}
        self.complete_regions: set[str] = set()
        self.dirty = True
        # FASE 2 measurement: collection cost is the dominant UIA cost, so it is metered
        # at the one place every observation goes through, and reused observations say so
        # instead of disappearing into the total.
        self.collect_ms = 0.0
        self.collect_calls = 0
        self.collect_reused = 0
        self.controls_inspected = 0
        # FASE 2: a snapshot stays usable for *finding* candidates until an operation that
        # can change the tree happens. Identity is still re-read live before every action
        # (``reference``), so a stale snapshot cannot authorize a stale action — it can only
        # offer a candidate whose live check then fails and asks for a fresh observation.
        self.tree_invalidated = True
        # Which control types the current snapshot was narrowed to (empty = complete list).
        self.scoped_types: tuple[str, ...] = ()
        # Lives through re-observation/window rebinding; cleared only between tasks.
        self.unverified_writes: dict[Any, tuple[str, bool, str]] = {}

    def _backend(self, view: str = "control") -> Any:
        """The backend for one view. An injected control backend is always reused."""
        if view == "control" and self.backend is not None:
            return self.backend
        instance = self.backends.get(view)
        if instance is None:
            # Keep the caller's transport mode: a diagnostic run that asked for live
            # properties must not silently get a cached Raw View alongside it.
            instance = NativeBackend(view=view, use_cache=getattr(self.backend, "use_cache", True))
            self.backends[view] = instance
        return instance

    def reset(self) -> None:
        self.references.clear()
        self.elements.clear()
        self.state.control_dict = {}
        self.window_key = None
        self.last_observation = {}
        self.complete_regions.clear()
        self.dirty = True
        self.tree_invalidated = True
        self.scoped_types = ()

    def invalidate_tree(self) -> None:
        """The tree may have changed: the next resolve re-observes instead of reusing.

        Called after a failed round and after any operation whose effect on the tree the
        engine cannot bound (navigation, menu, dialog). Content-only writes do not call
        it: the element list still names the same controls and every action re-reads their
        live identity anyway.
        """
        self.tree_invalidated = True
        self.dirty = True

    def _window(self) -> Any:
        window = self.state.selected_app_window
        if window is None:
            raise UIAError("no_window", "Selecione a janela pelo HostAgent primeiro.")
        return window

    @staticmethod
    def actionable(items: list[tuple[Any, dict[str, Any], int, int]]) -> int:
        """How many collected elements a user could actually act on."""
        return sum(1 for _, meta, _, _ in items if set(meta["patterns"]) & ACTIONABLE_PATTERNS)

    def _walk(self, backend: Any, root: Any, *, depth: int, max_nodes: int,
              timeout_ms: int) -> dict[str, Any]:
        """Collect one view's subtree breadth-first. Nothing is committed here.

        Parents are recorded positionally: a child can only be dequeued after its parent
        was appended, so the parent's index is always smaller than the child's.
        """
        started = time.perf_counter()
        initial_calls = backend.calls
        queue = deque([(root, -1, 0)])
        items: list[tuple[Any, dict[str, Any], int, int]] = []
        errors: list[str] = []
        truncated = False
        while queue:
            if len(items) >= max_nodes or (time.perf_counter() - started) * 1000 >= timeout_ms:
                truncated = True
                break
            element, parent_index, level = queue.popleft()
            try:
                meta = backend.metadata(element)
            except Exception as exc:
                errors.append(getattr(exc, "code", type(exc).__name__))
                truncated = True
                continue
            position = len(items)
            items.append((element, meta, parent_index, level))
            if level < depth:
                try:
                    children = backend.children(element)
                except Exception as exc:
                    errors.append(getattr(exc, "code", type(exc).__name__))
                    truncated = True
                    continue
                if len(children) > max_nodes:
                    truncated = True
                queue.extend((child, position, level + 1) for child in children[:max_nodes])
            else:
                try:
                    if backend.children(element):
                        truncated = True
                except Exception as exc:
                    errors.append(getattr(exc, "code", type(exc).__name__))
                    truncated = True
        return {"items": items, "errors": errors, "truncated": truncated,
                "walk_ms": (time.perf_counter() - started) * 1000,
                "native_calls": backend.calls - initial_calls}

    def _summarize(self, walk: dict[str, Any]) -> dict[str, Any]:
        """Per-view audit of one observation: what each tree offered and what it cost."""
        return {"elements": len(walk["items"]), "actionable": self.actionable(walk["items"]),
                "truncated": bool(walk["truncated"]), "walk_ms": round(walk["walk_ms"], 1),
                "native_calls": walk["native_calls"]}

    def _thin(self, walk: dict[str, Any]) -> bool:
        """Whether the Control View is the thin-tree case that hides real controls."""
        return bool(walk["truncated"]) or self.actionable(walk["items"]) < THIN_ACTIONABLE

    def observe_progressive(self) -> dict[str, Any]:
        """Start with window context; the AppAgent expands an observed region as needed.

        A region the agent already expanded survives into the following round as window
        context plus that region, never as a complete window. Refusing to carry a scoped
        expansion is what turned one Spotify run into a loop (measured 2026-10-11, A-02):
        the agent expanded the window pane with ``types=...``, the next prompt got the same
        shallow list again, and it repeated the identical expansion 19 times until
        PLANNING_FAILURE (`logs/_a02_loop_repro.json`).
        """
        previous = self.last_observation
        if previous and not self.tree_invalidated:
            key = self._backend(previous["view"]).window_key(self._window())
            if key == self.window_key and previous.get("root_id"):
                # The union includes window context and an expanded region, not a
                # complete window. Every later action still refreshes its reference.
                self.collect_reused += 1
                self.last_observation = {**previous, "elements": list(self.elements),
                                         "complete": False, "reused": True, "stage": "expanded"}
                return self.last_observation
            if key == self.window_key:
                return self.observe(depth=previous["depth"], max_nodes=previous["max_nodes"],
                                    view=previous["view"], reuse=True)
        # Deliberate depth truncation is not evidence that Control View is insufficient.
        # Region expansion uses auto and can ask the same provider for Raw View.
        observed = self.observe(depth=3, max_nodes=80, view="control")
        observed["stage"] = "top"
        return observed

    def observe(self, *, root_id: str = "", depth: int = DEFAULT_DEPTH,
                max_nodes: int = DEFAULT_MAX_NODES, timeout_ms: int = 1500,
                view: str = "auto", only_types: tuple[str, ...] = (),
                reuse: bool = False) -> dict[str, Any]:
        """Read the selected window (or an observed subtree) in one bounded pass.

        ``view="auto"`` starts from the Control View and escalates to the Raw View only
        when that pass is thin or truncated, keeping whichever tree exposes more
        actionable controls. Settled on the same windows in one run: Spotify 5 -> 58
        actionable (the Raw View won), Discord 14 with no escalation at all, and VS Code
        4 -> 115 where the *Control* View won because its own tree is the richer one.

        ``only_types`` narrows the *returned* list to the requested control types (the
        AppAgent's targeted mode: Edit, Button, ListItem, TreeItem, ...) and the result is
        marked ``scoped``/incomplete: a filtered list never proves absence or uniqueness,
        so ``resolve`` answers ``incomplete`` instead of ``not_found``/``unique`` until a
        complete observation exists. ``reuse`` returns the last snapshot when the tree has
        not been invalidated since it was read; it never skips the live identity check an
        action performs.

        Budgets are clamped to the maxima rather than refused: asking for a deeper or
        larger read is answered with the deepest and largest one the engine performs
        (``MAX_DEPTH``/``MAX_NODES``/``MAX_TIMEOUT_MS``), because no read is unbounded and
        a dropped round cannot be recovered. A value below the minimum is malformed and
        stays refused.
        """
        if depth < 1 or max_nodes < 1 or timeout_ms < 50:
            raise UIAError("invalid_arguments", "Limites: depth 1..16, nodes 1..600, timeout 50..5000 ms.")
        depth, max_nodes = min(depth, MAX_DEPTH), min(max_nodes, MAX_NODES)
        timeout_ms = min(timeout_ms, MAX_TIMEOUT_MS)
        if view not in (*VIEWS, "auto"):
            raise UIAError("invalid_arguments", "view deve ser auto, control ou raw.")
        wanted = tuple(sorted({str(item).strip().casefold() for item in only_types
                               if str(item).strip()}))
        window = self._window()
        requested = "control" if view == "auto" else view
        key = self._backend(requested).window_key(window)
        if reuse and not root_id and wanted == self.scoped_types and self.elements \
                and not self.tree_invalidated and self.last_observation \
                and key == self.window_key and not self.last_observation.get("root_id") \
                and (view == "auto" or self.last_observation.get("view") == view) \
                and self.last_observation.get("depth") == depth \
                and self.last_observation.get("max_nodes") == max_nodes:
            self.collect_reused += 1
            return {**self.last_observation, "reused": True, "observation_ms": 0.0}
        root_parent = ""
        if not root_id:
            requested = "control" if view == "auto" else view
            key = self._backend(requested).window_key(window)
            if self.window_key != key:
                self.reset()
                self.window_key = key
            self.references.clear()
            self.elements.clear()
            self.complete_regions.clear()
            self.state.control_dict = {}
            root = self._backend(requested).root(window)
            locked_view = requested
        else:
            reference = self.references.get(root_id)
            if reference is None:
                raise UIAError("stale_reference", "ID não pertence à observação atual; reinspecione.")
            key = reference.window_key
            if self._backend(reference.view).window_key(window) != key:
                raise UIAError("window_changed", "A janela mudou desde a observação.")
            root, _ = self.reference(root_id)
            locked_view = reference.view if view == "auto" else view
            self.complete_regions.discard("")
            root_row = next(item for item in self.elements if item["id"] == root_id)
            root_parent = root_row["parent"]
            removed = {root_id}
            for item in self.elements:
                if item["parent"] in removed:
                    removed.add(item["id"])
            self.elements = [item for item in self.elements if item["id"] not in removed]
            for identifier in removed:
                self.references.pop(identifier, None)
                self.state.control_dict.pop(identifier, None)
                self.complete_regions.discard(identifier)
        if len(self.references) + max_nodes > MAX_REFERENCES:
            raise UIAError("reference_budget", "Reinspecione a janela para liberar referências antigas.")
        started = time.perf_counter()
        backend = self._backend(locked_view)
        walked = self._walk(backend, root, depth=depth, max_nodes=max_nodes, timeout_ms=timeout_ms)
        chosen_view, chosen, tried = locked_view, walked, [locked_view]
        attempts = {locked_view: self._summarize(walked)}
        # Escalation needs a second, real provider view. A substituted backend is what the
        # caller handed us, and the engine never brings a different provider in behind it.
        if view == "auto" and locked_view == "control" and isinstance(backend, NativeBackend) \
                and self._thin(walked):
            raw_backend = self._backend("raw")
            raw_root = raw_backend.root(window) if not root_id else root
            raw = self._walk(raw_backend, raw_root, depth=depth, max_nodes=max_nodes,
                             timeout_ms=timeout_ms)
            tried.append("raw")
            attempts["raw"] = self._summarize(raw)
            if self.actionable(raw["items"]) > self.actionable(walked["items"]):
                chosen_view, chosen, backend = "raw", raw, raw_backend
        self.generation += 1
        rows: list[dict[str, Any]] = []

        def identifier_of(index: int) -> str:
            return root_id if root_id and index == 0 else f"uia-{self.generation}-{index + 1}"

        for index, (element, meta, parent_index, level) in enumerate(chosen["items"]):
            identifier = identifier_of(index)
            row = {**meta, "id": identifier,
                   "parent": root_parent if parent_index < 0 else identifier_of(parent_index),
                   "depth": level}
            # Keep provider identity internal, not in the LLM representation.
            row.pop("runtime_id", None)
            row.pop("native_handle", None)
            self.references[identifier] = Reference(element, dict(meta), key, chosen_view)
            self.state.control_dict[identifier] = backend.wrap(element, meta)
            if not wanted or (root_id and index == 0) \
                    or str(meta.get("type", "")).casefold() in wanted:
                rows.append(row)
        self.elements.extend(rows)
        self.dirty = False
        if not root_id:
            # A full observation is the new baseline for both reuse and scoping. A
            # subtree observation extends the old snapshot and must not clear an
            # invalidation raised elsewhere in the window.
            self.tree_invalidated = False
            self.scoped_types = wanted
        errors = chosen["errors"]
        truncated = bool(chosen["truncated"])
        actionable = self.actionable(chosen["items"])
        observed = {"elements": rows, "truncated": truncated, "errors": errors[:10],
                    "coverage": "empty" if not rows else "sparse" if actionable < THIN_ACTIONABLE else "rich",
                    "window": {"handle": int(window.handle), "pid": key[1]},
                    "controls_inspected": sum(attempt["elements"] for attempt in attempts.values()),
                    "depth": depth, "max_nodes": max_nodes, "generation": self.generation, "root_id": root_id,
                    "scoped": bool(wanted), "view": chosen_view, "views_tried": tried, "attempts": attempts,
                    "actionable": actionable,
                    "raw_gain": (self.actionable(chosen["items"]) - self.actionable(walked["items"])
                                 if chosen_view == "raw" else 0),
                    "native_calls": chosen["native_calls"],
                    "native_calls_total": sum(attempt["native_calls"] for attempt in attempts.values()),
                    "walk_ms": chosen["walk_ms"],
                    "observation_ms": (time.perf_counter() - started) * 1000,
                    # ``scoped`` presentation is never a complete certification of the
                    # window: only a full, untruncated read can answer absence/unicity.
                    "complete": not truncated and not errors and not wanted}
        self.collect_ms += (time.perf_counter() - started) * 1000
        self.collect_calls += 1
        self.controls_inspected += observed["controls_inspected"]
        self.last_observation = observed
        if observed["complete"] and not wanted:
            self.complete_regions.add(root_id)
        else:
            # A scoped list is never a complete region: it cannot certify absence or
            # uniqueness, exactly like a truncated walk.
            self.complete_regions.discard(root_id)
        # Whether the *window* was read completely. A region read reports ``complete`` for
        # its own root, so the next decision needs the window's coverage separately: a
        # scoped read that completed must not read as "the window is covered"
        # (measured 2026-10-11, A-02: ``truncated`` went False while the union stayed
        # incomplete).
        observed["window_complete"] = "" in self.complete_regions
        if not rows and not wanted:
            raise UIAError("observation_failed", "Nenhum elemento pôde ser lido; não prova ausência.")
        return observed

    def _referenced(self, identifier: str, name: str | None = None, *,
                    enabled: bool = False) -> tuple[Any, dict[str, Any], Any]:
        """One reference, with the backend it must be used with.

        ``reference`` re-reads through the view the element came from. Every operation then
        has to keep using *that* backend, or it would compare live properties from one
        provider against an element the other provider produced — which is exactly what the
        recorded ``view`` exists to prevent. The Control View and the Raw View are two trees
        of the same window, so this is not cosmetic: on a Chromium/Electron window the
        observation escalates to the Raw View and every follow-up reads that tree.
        """
        element, meta = self.reference(identifier, name, enabled=enabled)
        return element, meta, self._backend(self.references[identifier].view)

    def reference(self, identifier: str, name: str | None = None, *, enabled: bool = False) -> tuple[Any, dict[str, Any]]:
        ref = self.references.get(identifier)
        if ref is None:
            raise UIAError("stale_reference", "ID não pertence à observação atual; reinspecione.")
        backend = self._backend(ref.view)
        window = self._window()
        if backend.window_key(window) != ref.window_key:
            raise UIAError("window_changed", "A janela selecionada mudou desde a observação.")
        try:
            element = backend.refresh(ref.element)
            current = backend.metadata(element)
            identity = ("runtime_id", "automation_id", "name", "type", "pid")
            if any(current[key] != ref.meta[key] for key in identity):
                raise UIAError("stale_reference", "A identidade do controle mudou; reinspecione.")
            if not backend.belongs(element, window):
                raise UIAError("stale_reference", "O controle não pertence mais à janela selecionada.")
        except UIAError:
            raise
        except Exception as exc:
            raise UIAError("stale_reference", "Provider não confirmou referência viva; reinspecione.") from exc
        if name is not None and name != current["name"]:
            raise UIAError("name_mismatch", "ID e nome não correspondem; nenhuma ação executada.")
        if current["protected"]:
            raise UIAError("protected", "Interação com campo protegido bloqueada.")
        if enabled and not current["enabled"]:
            raise UIAError("disabled", "O controle está desativado.")
        return element, current

    def resolve(self, *, query: str = "", automation_id: str = "", control_type: str = "",
                root_id: str = "", exact: bool = True, framework: str = "", class_name: str = "",
                pattern: str = "", enabled: bool | None = None, offscreen: bool | None = None,
                focused: bool | None = None) -> dict[str, Any]:
        if not (query or automation_id or control_type):
            raise UIAError("invalid_arguments", "Informe nome, AutomationId ou tipo.")
        if any(len(value) > 512 for value in (query, automation_id, control_type, root_id, framework, class_name, pattern)):
            raise UIAError("invalid_arguments", "Filtro muito grande.")
        if not self.elements or self.tree_invalidated:
            self.observe(root_id=root_id)
        if root_id:
            self.reference(root_id)
        candidates = []
        by_id = {item["id"]: item for item in self.elements}
        for item in self.elements:
            if automation_id and item["automation_id"] != automation_id:
                continue
            if control_type and item["type"].casefold() != control_type.casefold():
                continue
            if query and (fold(item["name"]) != fold(query) if exact else fold(query) not in fold(item["name"])):
                continue
            if root_id:
                ancestor = item.get("parent", "")
                while ancestor and ancestor != root_id:
                    ancestor = by_id.get(ancestor, {}).get("parent", "")
                if ancestor != root_id:
                    continue
            # An unreadable candidate cannot silently disappear and turn ambiguity into
            # uniqueness. Fail and reinspect instead of choosing another similar control.
            _, live = self.reference(item["id"])
            if framework and live["framework"].casefold() != framework.casefold():
                continue
            if class_name and live["class_name"].casefold() != class_name.casefold():
                continue
            if pattern and pattern not in live["patterns"]:
                continue
            if any(expected is not None and live[key] != expected
                   for key, expected in (("enabled", enabled), ("offscreen", offscreen), ("focused", focused))):
                continue
            candidates.append(item)
        complete = root_id in self.complete_regions or "" in self.complete_regions
        virtualized = any(set(row.get("patterns", [])) & {"ItemContainer", "VirtualizedItem"}
                          for row in self.elements if not root_id or row["id"] == root_id
                          or self._under(row, root_id, by_id))
        virtual_roots = {row["id"] for row in self.elements
                         if set(row.get("patterns", [])) & {"ItemContainer", "VirtualizedItem"}}
        hidden_matches_possible = virtualized and (not candidates or any(
            row["id"] in virtual_roots or any(self._under(row, container, by_id) for container in virtual_roots)
            for row in candidates))
        complete = complete and not hidden_matches_possible
        status = "ambiguous" if len(candidates) > 1 else "unique" if candidates and complete else (
            "incomplete" if not complete else "not_found")
        return {"status": status, "count": len(candidates), "candidates": candidates[:20],
                "complete": complete, "virtualized": virtualized, "mutation": False}

    @staticmethod
    def _under(row: dict[str, Any], root_id: str, by_id: dict[str, dict[str, Any]]) -> bool:
        ancestor = row.get("parent", "")
        seen: set[str] = set()
        while ancestor and ancestor not in seen:
            if ancestor == root_id:
                return True
            seen.add(ancestor)
            ancestor = by_id.get(ancestor, {}).get("parent", "")
        return False

    def verify(self, identifier: str, name: str, *, expected_text: str = "", property_name: str = "text",
               expected_state: int = -1) -> dict[str, Any]:
        element, meta, backend = self._referenced(identifier, name)
        observed: Any
        source = ""
        if property_name == "name":
            if not expected_text:
                raise UIAError("invalid_arguments", "Nome esperado precisa ser explícito.")
            observed = meta["name"]
            verified = observed == expected_text
        elif property_name == "text":
            content = backend.content(element, meta)
            verified = bool(content["readable"] and not content["truncated"]
                            and content["text"] == normalized(expected_text))
            observed = {"readable": content["readable"], "length": len(content["text"])}
            source = content["source"]
            # A rendered label is not always content. When the control's own reading cannot
            # answer, the text it renders through its subtree is what the user sees, so it
            # is collected before this verification is reported as unproven.
            if not verified and expected_text and not content["truncated"] \
                    and meta["type"] not in {"Edit", "Document", "ComboBox"} \
                    and "Value" not in meta["patterns"]:
                rendered = self._rendered_text(backend, element, meta, content)
                observed = {**observed, "rendered_length": len(rendered["text"]),
                            "rendered_nodes": rendered["nodes"],
                            "rendered_truncated": rendered["truncated"]}
                verified = normalized(expected_text) in rendered["text"]
                if verified:
                    source = "rendered_subtree"
        elif property_name in {"enabled", "focused"}:
            if expected_state not in {0, 1}:
                raise UIAError("invalid_arguments", "Estado booleano exige 0 ou 1.")
            observed = meta[property_name]
            verified = observed == bool(expected_state)
        else:
            props = {"selected": ("SelectionItem", "CurrentIsSelected"),
                     "toggle": ("Toggle", "CurrentToggleState"),
                     "expanded": ("ExpandCollapse", "CurrentExpandCollapseState")}
            if property_name not in props or expected_state not in {0, 1, 2, 3}:
                raise UIAError("invalid_arguments", "Postcondição/estado UIA inválido.")
            pattern, prop = props[property_name]
            if pattern not in meta["patterns"]:
                raise UIAError("unsupported_pattern", "Provider não oferece padrão para esta prova.")
            observed = backend.state(element, pattern, prop)
            verified = observed == expected_state
        if verified and property_name == "text" and source != "rendered_subtree":
            write_key = (self.references[identifier].window_key, tuple(meta["runtime_id"]))
            pending = self.unverified_writes.get(write_key)
            if pending and normalized(expected_text) == pending[2]:
                self.unverified_writes.pop(write_key)
        return {"operation": "uia_verify", "id": identifier, "handle": int(self._window().handle),
                "runtime_id": list(meta["runtime_id"]), "control_type": meta["type"],
                "property": property_name, "observed": observed, "verified": verified,
                "expected_text": expected_text, "expected_state": expected_state,
                "source": source, "verified_by": "independent_live_read"}

    def _children(self, backend: Any, element: Any) -> list[Any]:
        """A node's children, or none: a subtree a provider refuses to open proves nothing."""
        try:
            return list(backend.children(element))
        except Exception:  # noqa: BLE001 - an unreadable branch is not an absent one
            return []

    def _rendered_text(self, backend: Any, element: Any, meta: dict[str, Any],
                       content: dict[str, Any]) -> dict[str, Any]:
        """The text this control *renders*, read from it and from its bounded subtree.

        A Chromium/Electron accessibility tree does not publish a list row's label as
        Value/Text content. Measured 2026-10-09 on Spotify's own search results: the result
        row (``DataItem`` "Michael Jackson") and every node underneath it answer
        ``LegacyIAccessible`` with an *empty* value, and the label exists only as the
        accessible Name of those nodes — so ``uia_verify`` on the row could never confirm a
        search that had plainly worked, and the run ended as "sucesso não confirmado".
        ``content`` is the control's own reading, already taken by the caller.

        The control's *own* Name is deliberately not collected: ``_referenced`` already
        asserted it as identity, so using it would make the verification its own premise.
        A descendant's Name is different — in that tree it *is* the rendered label of that
        descendant, which is what the user reads on screen. Read-only, and never through a
        protected control.
        """
        parts = [content["text"]] if content["readable"] and content["text"] else []
        truncated = bool(content["truncated"])
        nodes = 0
        queue: deque[tuple[Any, int]] = deque((child, 1) for child in
                                              self._children(backend, element)[:RENDERED_NODES])
        while queue:
            node, level = queue.popleft()
            if nodes >= RENDERED_NODES:
                truncated = True
                break
            nodes += 1
            try:
                node_meta = backend.metadata(node)
                if node_meta.get("protected"):
                    continue
                node_content = backend.content(node, node_meta)
            except Exception:  # noqa: BLE001 - a node that cannot be read proves nothing
                continue
            if node_content.get("readable") and node_content.get("text"):
                parts.append(node_content["text"])
            if node_content.get("truncated"):
                truncated = True
            if node_meta.get("name"):
                parts.append(node_meta["name"])
            if level < RENDERED_DEPTH:
                room = RENDERED_NODES - nodes - len(queue)
                if room <= 0:
                    truncated = True
                    break
                queue.extend((child, level + 1)
                             for child in self._children(backend, node)[:room])
        return {"text": normalized("\n".join(parts)), "nodes": nodes, "truncated": truncated}

    def keyboard(self, identifier: str, name: str, keys: str, *, control_focus: bool = True) -> dict[str, Any]:
        if not isinstance(keys, str) or not 1 <= len(keys) <= 512:
            raise UIAError("invalid_arguments", "Sequência de teclado inválida.")
        element, meta, backend = self._referenced(identifier, name, enabled=True)
        recovery = keys.casefold() in {"{esc}", "{escape}"}
        if not recovery:
            self._check_pending_dispatch(meta, "keyboard_input")
        if not control_focus:
            root = backend.root(self._window())
            if meta["type"] != "Window" or tuple(meta["runtime_id"]) != tuple(backend.metadata(root)["runtime_id"]):
                raise UIAError("unsafe_context", "Atalho de janela exige o ID da janela selecionada.")
        before = self._activation_state(backend, element, meta) if meta["type"] == "MenuItem" else {}
        try:
            backend.keys(element, self._window(), meta, keys)
        except UIAPreconditionError:
            raise
        except Exception as exc:
            self._record("keyboard_input", identifier, verified=False, executed=False,
                         dispatch_uncertain=True, needs_postcondition=True,
                         submitted=submit_keys(keys), recovery=recovery, before=before)
            raise UIAError("mutation_unverified", "Falha durante entrada; verifique o estado antes de repetir.") from exc
        return self._record("keyboard_input", identifier, verified=False, needs_postcondition=True,
                            submitted=submit_keys(keys), recovery=recovery, before=before)

    def _check_pending_dispatch(self, meta: dict[str, Any], operation: str,
                                recovery_of: int = -1) -> None:
        activation = {"invoke", "default_action", "click_input", "keyboard_input", "expand", "collapse"}
        recovered = recovered_dispatch_indices(self.evidence)
        for index, action in enumerate(self.evidence):
            if index == recovery_of:
                continue
            same_operation = action.get("operation") == operation or (
                operation in activation and action.get("operation") in activation)
            if (same_operation and action.get("handle") == int(self._window().handle)
                    and action.get("runtime_id") == list(meta["runtime_id"])
                    and not action.get("recovery")
                    and action.get("needs_postcondition") and not action.get("verified")
                    and index not in recovered
                    and not any(covers_dispatch(action, proof, history=tuple(self.evidence[:index]))
                                for proof in self.evidence[index + 1:])):
                raise UIAError("mutation_unverified", "Ação anterior sem pós-condição; repetição recusada.")

    def _menu_recovery(self, element: Any, meta: dict[str, Any], backend: Any) -> dict[str, Any]:
        if meta["type"] != "MenuItem" or "ExpandCollapse" not in meta["patterns"]:
            return {}
        handle = int(self._window().handle)
        identity = list(meta["runtime_id"])
        recovered = recovered_dispatch_indices(self.evidence)
        for index, action in enumerate(self.evidence):
            if (action.get("operation") not in {"invoke", "default_action"}
                    or action.get("executed") is not True or action.get("dispatch_uncertain")
                    or action.get("verified") or not action.get("needs_postcondition")
                    or action.get("handle") != handle or action.get("runtime_id") != identity
                    or action.get("before", {}).get("expanded") != 0
                    or index in recovered
                    or any(covers_dispatch(action, proof) for proof in self.evidence[index + 1:])):
                continue
            for read_index in range(index + 1, len(self.evidence)):
                proof = self.evidence[read_index]
                if (proof.get("operation") == "uia_verify" and proof.get("verified") is False
                        and proof.get("handle") == handle and proof.get("runtime_id") == identity
                        and proof.get("property") == "expanded" and proof.get("expected_state") == 1
                        and proof.get("observed") == 0):
                    parent = backend.client.ControlViewWalker.GetParentElement(element)
                    if not parent or backend.metadata(backend.refresh(parent)).get("type") != "MenuBar":
                        return {}
                    if backend.state(element, "ExpandCollapse", "CurrentExpandCollapseState") == 0:
                        return {"recovery_of": index, "recovery_read": read_index}
        return {}

    @staticmethod
    def _activation_state(backend: Any, element: Any, meta: dict[str, Any]) -> dict[str, Any]:
        before = {"name": meta["name"]}
        for prop, pattern, attribute in (("expanded", "ExpandCollapse", "CurrentExpandCollapseState"),
                                         ("selected", "SelectionItem", "CurrentIsSelected"),
                                         ("toggle", "Toggle", "CurrentToggleState")):
            if pattern in meta["patterns"]:
                before[prop] = backend.state(element, pattern, attribute)
        return before

    def _record(self, operation: str, identifier: str, *, verified: bool,
                invalidates_tree: bool = True, **details: Any) -> dict[str, Any]:
        reference = self.references.get(identifier)
        # Dispatch can destroy its window; record the identity validated before input.
        handle = int(reference.window_key[0]) if reference else int(self._window().handle)
        result = {"operation": operation, "id": identifier, "handle": handle,
                  "executed": True, "verified": verified, **details}
        if reference:
            result["runtime_id"] = list(reference.meta["runtime_id"])
        self.evidence.append(result)
        self.dirty = True
        if invalidates_tree:
            # The operation may have changed the tree (navigation, menu, dialog, list
            # realization). The next resolve re-observes instead of reusing candidates.
            self.tree_invalidated = True
        return result

    def _wait(self, condition: Callable[[], bool], timeout_ms: int = 1000) -> bool:
        deadline = time.perf_counter() + timeout_ms / 1000
        interval = 0.05
        while True:
            if condition():
                return True
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return False
            time.sleep(min(interval, remaining))
            interval = min(0.2, interval * 1.5)

    def text(self, identifier: str, name: str) -> dict[str, Any]:
        element, meta, backend = self._referenced(identifier, name)
        return backend.content(element, meta)

    def set_text(self, identifier: str, name: str, text: str, *, replace: bool = False) -> dict[str, Any]:
        if not isinstance(text, str) or len(text) > 4000 or any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            raise UIAError("invalid_text", "Texto inválido ou maior que 4000 caracteres.")
        window = self._window()
        element, meta, backend = self._referenced(identifier, name, enabled=True)
        before = backend.content(element, meta)
        if not before["readable"] or before["truncated"]:
            raise UIAError("unreadable_text", "Conteúdo completo indisponível; escrita segura não comprovada.")
        write_key = (self.references[identifier].window_key, tuple(meta["runtime_id"]))
        pending = self.unverified_writes.get(write_key)
        if pending:
            if (text, replace) == pending[:2] and before["text"] == pending[2]:
                self.unverified_writes.pop(write_key)
                return self._record("set_edit_text", identifier, verified=True, executed=False,
                                    already_satisfied=True, length=len(text),
                                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                    comparison="exact_content")
            raise UIAError("mutation_unverified", "Escrita anterior sem prova; não repetir nem acrescentar texto.")
        mechanism = backend.writable(element, meta)
        expected = normalized(text) if replace else before["text"] + normalized(text)
        if mechanism == "keyboard":
            if any(char in text for char in "\r\n\t"):
                raise UIAError("unsafe_text_fallback", "Campo sem setter nativo: Enter/Tab podem enviar ou sair do campo.")
            from app.computer.worker import literal_keys
        self.unverified_writes[write_key] = (text, replace, expected)
        try:
            if mechanism == "keyboard":
                backend.keys(element, window, meta, ("^a" if replace else "^{END}") + literal_keys(text))
            else:
                backend.call(element, window, mechanism, "SetValue", expected)
        except Exception as exc:
            self._record("set_edit_text", identifier, verified=False, executed=False,
                         dispatch_uncertain=True, needs_postcondition=True)
            raise UIAError("mutation_unverified", "Provider falhou durante escrita; leia/verifique antes de continuar.") from exc
        self.dirty = True

        def matches() -> bool:
            current, metadata = self.reference(identifier, name)
            content = backend.content(current, metadata)
            return bool(content["readable"] and not content["truncated"] and content["text"] == expected)

        try:
            verified = self._wait(matches)
        except UIAError:
            verified = False
        # TextChanged can populate results, validation labels or virtualized rows.
        # Identity checks protect actions, but an old tree cannot prove their absence.
        result = self._record("set_edit_text", identifier, verified=verified,
                              pattern=mechanism, length=len(text),
                              text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                              comparison="exact_content")
        if not verified:
            raise UIAError("verification_failed", "Texto aplicado mas conteúdo não confirmado; não repetir escrita.")
        self.unverified_writes.pop(write_key, None)
        return result

    def action(self, identifier: str, name: str, operation: str, *, value: int = -1,
               direction: str = "down", text: str = "") -> dict[str, Any]:
        if operation == "click_input":
            return self.click(identifier, name, prefer_native=False, recover_menu=True)
        window = self._window()
        element, meta, backend = self._referenced(identifier, name, enabled=True)
        if operation in {"invoke", "default_action", "expand", "collapse"}:
            self._check_pending_dispatch(meta, operation)
        if operation == "select_text":
            if "Text" not in meta["patterns"] or not text or len(text) > 4000:
                raise UIAError("unsupported_pattern", "Seleção exige TextPattern e texto explícito.")
            document = backend.pattern(element, "Text").DocumentRange
            target = document.FindText(text, False, False)
            if not target:
                raise UIAError("not_found", "Trecho não encontrado no texto acessível.")
            following = document.Clone()
            following.MoveEndpointByRange(0, target, 1)
            if following.FindText(text, False, False):
                raise UIAError("ambiguous", "Trecho repetido; refine o texto antes de selecionar.")
            backend.atomic(target, window, "Select", {})
            selected = backend.pattern(element, "Text").GetSelection()
            verified = (selected.Length == 1 and normalized(selected.GetElement(0).GetText(4001)) == normalized(text))
            return self._record(operation, identifier, verified=verified, pattern="Text", selection_length=len(text))
        if operation == "find_item":
            if "ItemContainer" not in meta["patterns"] or not text or len(text) > 512:
                raise UIAError("unsupported_pattern", "Busca virtual exige ItemContainer e nome exato.")
            container = backend.pattern(element, "ItemContainer")
            item = container.FindItemByProperty(None, backend.dll.UIA_NamePropertyId, text)
            if not item:
                raise UIAError("not_found", "Item não encontrado pelo provider do container.")
            if container.FindItemByProperty(item, backend.dll.UIA_NamePropertyId, text):
                raise UIAError("ambiguous", "Mais de um item virtual possui esse nome.")
            # A placeholder must be realized before reading its ordinary properties.
            # Source: Microsoft Working with Virtualized Items. No mutation retry.
            realized = False
            try:
                item = backend.refresh(item)
                item_meta = backend.metadata(item)
            except Exception as exc:
                # Only UIA_E_ELEMENTNOTAVAILABLE identifies a possible virtualized
                # placeholder. An unrelated provider/read error never authorizes Realize.
                if getattr(exc, "hresult", 0) != -2147220991:
                    raise UIAError("provider_error", "Falha de leitura não prova item virtualizado.") from exc
                backend.call(item, window, "VirtualizedItem", "Realize")
                realized = True
                item = backend.refresh(item)
                item_meta = backend.metadata(item)
            if not backend.belongs(item, window) or item_meta["protected"]:
                raise UIAError("invalid_item", "Provider retornou item fora da janela ou protegido.")
            if len(self.references) >= MAX_REFERENCES:
                raise UIAError("reference_budget", "Reinspecione antes de buscar mais itens.")
            item_id = f"uia-item-{self.generation}-{len(self.references) + 1}"
            row = {**item_meta, "id": item_id, "parent": identifier, "depth": meta.get("depth", 0) + 1}
            row.pop("runtime_id", None)
            row.pop("native_handle", None)
            self.references[item_id] = Reference(item, dict(item_meta), backend.window_key(window),
                                                 self.references[identifier].view)
            self.state.control_dict[item_id] = backend.wrap(item, item_meta)
            self.elements.append(row)
            return {"operation": operation, "item": row, "realized": realized,
                    "executed": realized, "verified": item_meta["name"] == text, "needs_postcondition": realized}
        operations = {"invoke": ("Invoke", "Invoke"), "default_action": ("LegacyIAccessible", "DoDefaultAction"),
                      "select": ("SelectionItem", "Select"),
                      "toggle": ("Toggle", "Toggle"), "expand": ("ExpandCollapse", "Expand"),
                      "collapse": ("ExpandCollapse", "Collapse"), "scroll_into_view": ("ScrollItem", "ScrollIntoView"),
                      "realize": ("VirtualizedItem", "Realize")}
        if operation == "focus":
            backend.focus(element, window, meta)
            return self._record(operation, identifier, verified=True,
                                invalidates_tree=False,
                                verified_by="foreground_and_keyboard_focus")
        if operation == "scroll":
            if "Scroll" not in meta["patterns"] or direction not in {"up", "down", "left", "right"}:
                raise UIAError("unsupported_pattern", "ScrollPattern/direção indisponível.")
            before = (backend.state(element, "Scroll", "CurrentHorizontalScrollPercent"),
                      backend.state(element, "Scroll", "CurrentVerticalScrollPercent"))
            # ScrollAmount: SmallDecrement=1, NoAmount=2, SmallIncrement=4.
            amount = 1 if direction in {"up", "left"} else 4
            backend.call(element, window, "Scroll", "Scroll",
                         amount if direction in {"left", "right"} else 2,
                         amount if direction in {"up", "down"} else 2)
            after = (backend.state(element, "Scroll", "CurrentHorizontalScrollPercent"),
                     backend.state(element, "Scroll", "CurrentVerticalScrollPercent"))
            return self._record(operation, identifier, verified=after != before, pattern="Scroll", changed=after != before,
                                needs_postcondition=True)
        if operation not in operations:
            raise UIAError("invalid_operation", "Operação UIA não reconhecida.")
        pattern, method = operations[operation]
        if pattern not in meta["patterns"]:
            raise UIAError("unsupported_pattern", f"Provider não oferece {pattern}Pattern.")
        if operation == "default_action" and not backend.state(element, pattern, "CurrentDefaultAction"):
            raise UIAError("unsupported_pattern", "Provider não publica uma ação padrão acessível.")
        expected: Any = None
        prop = ""
        if operation in {"expand", "collapse"}:
            prop, expected = "CurrentExpandCollapseState", 1 if operation == "expand" else 0
        elif operation == "select":
            prop, expected = "CurrentIsSelected", True
        elif operation == "toggle":
            if value not in {0, 1, 2}:
                raise UIAError("invalid_arguments", "Toggle requer estado desejado 0, 1 ou 2; sem ciclos cegos.")
            prop, expected = "CurrentToggleState", value
            current = backend.state(element, pattern, prop)
            # One mutation max. A tri-state whose next state is not requested is refused.
            if current != value and (current + 1) % 3 != value and not (current == 1 and value == 0):
                raise UIAError("unsafe_toggle", "Estado exige várias alternâncias; não executar sequência cega.")
        unchanged = bool(prop and backend.state(element, pattern, prop) == expected)
        before_state = self._activation_state(backend, element, meta) if operation in {"invoke", "default_action"} else {}
        if not unchanged:
            try:
                backend.call(element, window, pattern, method)
            except Exception as exc:
                self._record(operation, identifier, verified=False, executed=False,
                             dispatch_uncertain=True, needs_postcondition=True, pattern=pattern, before=before_state)
                raise UIAError("mutation_unverified", "Pattern falhou durante execução; não repetir sem prova.") from exc
        self.dirty = True
        verified = False
        if prop:
            verified = self._wait(lambda: backend.state(self.reference(identifier, name)[0], pattern, prop) == expected)
        elif operation in {"scroll_into_view", "realize"}:
            verified = not self._referenced(identifier, name)[1]["offscreen"]
        # Invoking is dispatch, not evidence of the semantic task's completion.
        return self._record(operation, identifier, verified=verified, pattern=pattern,
                            before=before_state,
                            already_satisfied=unchanged, executed=not unchanged,
                            failure_kind="UIA_ACTION_NO_EFFECT" if prop and not verified else "",
                            needs_postcondition=operation in {"invoke", "default_action"} or not verified)

    def click(self, identifier: str, name: str, *, button: str = "left", double: bool = False,
              prefer_native: bool = True, recover_menu: bool = False) -> dict[str, Any]:
        element, meta, backend = self._referenced(identifier, name, enabled=True)
        if button not in {"left", "right"}:
            raise UIAError("invalid_arguments", "Use botão left ou right.")
        if prefer_native and button == "left" and not double:
            for pattern, operation in (("Invoke", "invoke"), ("SelectionItem", "select"),
                                       ("ExpandCollapse", "expand")):
                if pattern in meta["patterns"]:
                    return self.action(identifier, name, operation)
        if meta["offscreen"]:
            raise UIAError("offscreen", "Controle fora da área visível; scroll/reinspecione antes.")
        recovery = self._menu_recovery(element, meta, backend) if recover_menu and button == "left" and not double else {}
        self._check_pending_dispatch(meta, "click_input", recovery.get("recovery_of", -1))
        # Geometry comes from the live control, never guessed absolute coordinates.
        if meta["type"] == "MenuItem":
            backend.activate(self._window())
        else:
            backend.focus(element, self._window(), meta)
        before_state = self._activation_state(backend, element, meta)
        try:
            backend.atomic(backend.wrap(element, meta), self._window(), "click_input",
                           {"button": button, "double": double})
        except Exception as exc:
            self._record("click_input", identifier, verified=False, executed=False,
                         dispatch_uncertain=True, needs_postcondition=True, pattern="input_fallback",
                         before=before_state, **recovery)
            raise UIAError("mutation_unverified", "Clique falhou durante execução; verifique antes de repetir.") from exc
        return self._record("click_input", identifier, verified=False, needs_postcondition=True,
                            pattern="input_fallback", before=before_state, **recovery)

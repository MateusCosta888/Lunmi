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
from app.computer.approval import normalize_keys, submit_keys
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
BROWSER_PROCESSES = frozenset({"msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe", "opera.exe"})
# A page load: measured 1-3 s for wikipedia.org; slow sites stay unproven, never failed.
NAVIGATION_TIMEOUT_MS = 8000
# Calculator redraws its display within ~100 ms; a key that changes nothing (C on 0) stays unproven.
KEYPAD_TIMEOUT_MS = 1500
# A dialog (Save As, Open) appears within ~300 ms of its shortcut or button.
WINDOW_CHANGE_TIMEOUT_MS = 1200
# A clicked link or suggestion starts its navigation quickly; buttons that do not navigate wait this long.
CLICK_NAVIGATION_TIMEOUT_MS = 3000
# An arithmetic expression for Calculator, in type_keys syntax: + and % are escaped as {+} {%}.
CALCULATOR_KEYS = re.compile(r"(?=.*[0-9])(?:[0-9.,*/=()\-]|\{\+\}|\{%\}|\{ENTER\})+")
# Keys that only move the caret or extend a selection: in a text field they change no content.
CARET_KEYS = re.compile(r"(?:[\^+]*\{(?:END|HOME|LEFT|RIGHT|UP|DOWN|PGUP|PGDN)(?: \d{1,2})?\})+", re.IGNORECASE)


def navigated_to(typed: str, url: str) -> bool:
    """Whether the page now shown is where the address-bar text leads.

    An address must land on its host (or a subdomain: wikipedia.org -> www.wikipedia.org)
    and under its path. Text that is not an address is a search: any new page is its result.
    """
    from urllib.parse import urlsplit
    if not is_address(typed):
        return True
    text = typed.strip()
    wanted = urlsplit(text if "://" in text else "http://" + text)
    reached = urlsplit(url)
    host, landed = (wanted.hostname or "").removeprefix("www."), (reached.hostname or "").removeprefix("www.")
    path = wanted.path.rstrip("/").casefold()
    return (bool(host) and (landed == host or landed.endswith("." + host))
            and reached.path.casefold().startswith(path))


def same_address(left: str, right: str) -> bool:
    """One address written two ways: scheme, a leading www. and a final slash are optional."""
    from urllib.parse import urlsplit

    def key(text: str) -> tuple[str, str, str]:
        text = text.strip()
        parts = urlsplit(text if "://" in text else "http://" + text)
        return ((parts.hostname or "").removeprefix("www."), parts.path.rstrip("/"), parts.query)
    return bool(key(left)[0]) and key(left) == key(right)


def is_address(typed: str) -> bool:
    text = typed.strip()
    return bool(text) and not any(char.isspace() for char in text) and (
        "." in text or text.startswith("localhost"))


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
        # Every refusal here comes before any key or click: no effect to verify, so it must
        # not leave an uncertain dispatch that blocks the rest of the task.
        from app.computer.windows_tools import foreground_handle
        if not meta["focusable"] or meta["offscreen"]:
            raise UIAPreconditionError("not_focusable", "Controle não pode receber teclado neste estado.")
        self.activate(window)
        wrapper = self.wrap(element, meta)
        try:
            self.atomic(wrapper, window, "set_focus", {})
        except UIAError as exc:
            raise UIAPreconditionError("focus_lost", "Não foi possível focar o controle; nada foi escrito.") from exc
        deadline = time.perf_counter() + self.FOCUS_ATTEMPTS * self.FOCUS_RETRY_SECONDS
        while True:
            if foreground_handle() == int(window.handle) and wrapper.has_keyboard_focus():
                return
            if time.perf_counter() >= deadline:
                raise UIAPreconditionError("focus_lost", "Foco de teclado não corresponde ao controle.")
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
                raise UIAPreconditionError("focus_lost", "Atalho de menu recusado fora da janela selecionada.")
        elif meta["type"] == "Window":
            # Windows Calculator reads typed expressions from its window; only arithmetic
            # keys, and only where its display exists to prove what they did.
            calculator = bool(CALCULATOR_KEYS.fullmatch(keys)) and self.has_automation_id(element, "CalculatorResults")
            if not calculator and submit_keys(keys) or not calculator and not re.fullmatch(
                    r"[\^%+]+(?:[a-zA-Z0-9]|\{[A-Za-z0-9_]+\})|\{(?:ESC|TAB|UP|DOWN|LEFT|RIGHT|HOME|END|PGUP|PGDN|F\d{1,2})\}",
                    keys, flags=re.IGNORECASE):
                raise UIAPreconditionError("unsafe_context", "Texto/Enter exige o controle de destino, não a janela; use control_focus=true num botão ou campo da aplicação.")
            from app.computer.windows_tools import foreground_handle
            self.activate(window)
            focused = self.client.GetFocusedElement()
            if not focused or not self.belongs(focused, window) or foreground_handle() != int(window.handle):
                raise UIAPreconditionError("focus_lost", "Atalho recusado: foco fora da janela selecionada.")
        else:
            self.focus(element, window, meta)
            target = self.wrap(element, meta)
        self.atomic(target, window, "type_keys",
                    {"keys": keys, "pause": 0.02, "with_spaces": True, "vk_packet": meta["type"] != "MenuItem",
                     "set_foreground": False})

    def has_automation_id(self, element: Any, automation_id: str) -> bool:
        try:
            self.calls += 1
            return bool(element.FindFirst(self.uia.tree_scope["descendants"], self.client.CreatePropertyCondition(
                self.dll.UIA_AutomationIdPropertyId, automation_id)))
        except Exception:  # noqa: BLE001 - unreadable: not proven to be Calculator
            return False

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
        # IDs of earlier observations -> (window, identity). Every action regenerates the IDs,
        # and a model that reused one lost a round per key (measured: Calculator 01/11).
        self.retired: dict[str, tuple[Any, tuple[Any, ...], Reference, Any]] = {}
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
        self.retired.clear()
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
            self._retire()
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

    @staticmethod
    def _identity(meta: dict[str, Any]) -> tuple[Any, ...]:
        return (tuple(meta["runtime_id"]), meta["automation_id"], meta["name"], meta["type"], meta["pid"])

    def _retire(self) -> None:
        for identifier, ref in self.references.items():
            if ref.meta.get("runtime_id"):
                self.retired[identifier] = (ref.window_key, self._identity(ref.meta), ref,
                                            self.state.control_dict.get(identifier))
        while len(self.retired) > MAX_REFERENCES * 4:
            self.retired.pop(next(iter(self.retired)))

    def _revive(self, identifier: str) -> Reference | None:
        """An old ID still names its control when the current observation has that very
        control: same window and the same identity ``reference`` already demands."""
        retired = self.retired.get(identifier)
        if retired is None:
            return None
        for current_id, ref in self.references.items():
            if ref.window_key == retired[0] and self._identity(ref.meta) == retired[1]:
                self.references[identifier] = ref
                self.state.control_dict[identifier] = self.state.control_dict.get(current_id)
                return ref
        # Not in this (possibly partial) observation: the old element itself, which
        # ``reference`` re-reads live and must still match identity and window (measured
        # 01/11: Edge's address bar was missing from the next step's top controls).
        ref = retired[2]
        self.references[identifier] = ref
        self.state.control_dict[identifier] = retired[3]
        return ref

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
        # Every refusal here happens before any input: a precondition, never an uncertain
        # dispatch (measured: a stale ID on Enter blocked an Edge task until its timeout).
        ref = self.references.get(identifier) or self._revive(identifier)
        if ref is None:
            raise UIAPreconditionError("stale_reference", "ID não pertence à observação atual; reinspecione.")
        backend = self._backend(ref.view)
        window = self._window()
        if backend.window_key(window) != ref.window_key:
            raise UIAPreconditionError("window_changed", "A janela selecionada mudou desde a observação.")
        try:
            element = backend.refresh(ref.element)
            current = backend.metadata(element)
            identity = ("runtime_id", "automation_id", "name", "type", "pid")
            if any(current[key] != ref.meta[key] for key in identity):
                raise UIAPreconditionError("stale_reference", "A identidade do controle mudou; reinspecione.")
            if not backend.belongs(element, window):
                raise UIAPreconditionError("stale_reference", "O controle não pertence mais à janela selecionada.")
        except UIAError:
            raise
        except Exception as exc:
            raise UIAPreconditionError("stale_reference", "Provider não confirmou referência viva; reinspecione.") from exc
        if name is not None and name != current["name"]:
            raise UIAPreconditionError("name_mismatch", "ID e nome não correspondem; nenhuma ação executada.")
        if current["protected"]:
            raise UIAPreconditionError("protected", "Interação com campo protegido bloqueada.")
        if enabled and not current["enabled"]:
            raise UIAPreconditionError("disabled", "O controle está desativado.")
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
        elif property_name == "text" and meta.get("automation_id") == "RootWebArea":
            # A Chromium page's Value is its URL, never its content (measured 01/11: "Wikipedia"
            # asked of the page read the URL, failed, and stalled a navigated task). Its text is
            # what the page shows: the title in Name, then the rendered page.
            wanted = normalized(expected_text).strip()
            observed = {"name": meta["name"]}
            verified = bool(wanted) and re.search(
                r"(?<!\w)" + re.escape(wanted) + r"(?!\w)", normalized(meta["name"])) is not None
            source = "page_title"
            if not verified and wanted:
                rendered = self._rendered_text(backend, element, meta, {"readable": False, "text": "",
                                                                          "truncated": False})
                verified, source = wanted in rendered["text"], "rendered_subtree"
        elif property_name == "text":
            content = backend.content(element, meta)
            verified = bool(content["readable"] and not content["truncated"]
                            and content["text"] == normalized(expected_text))
            observed = {"readable": content["readable"], "length": len(content["text"])}
            source = content["source"]
            if (not verified and content["readable"] and meta["type"] == "Edit"
                    and is_address(content["text"]) and is_address(expected_text)
                    and same_address(content["text"], expected_text)):
                # The same address written two ways (measured 01/11: Edge shows
                # "https://www.wikipedia.org" for "https://www.wikipedia.org/").
                verified, source = True, "same_address"
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
                elif (not content["text"] and not rendered["text"] and meta["type"] in {"Text", "Hyperlink"}
                        and re.search(r"(?<!\w)" + re.escape(normalized(expected_text).strip()) + r"(?!\w)",
                                      normalized(meta["name"]))):
                    # A static Text/Hyperlink shows exactly its Name (UIA defines it so); the same
                    # live read property_name='name' would give. Measured 01/11: a page heading's
                    # text verify failed on wording and stalled a navigated Edge task.
                    verified, source = True, "text_control_name"
                elif not content["text"] and not rendered["text"] and meta["name"]:
                    # A label such as Calculator's display publishes its value only as Name.
                    # Not proof by itself (Name is the identity just asserted): it tells the
                    # agent which live read can prove it.
                    observed = {**observed, "name": meta["name"],
                                "hint": "No readable text; verify property_name='name' with the full live Name."}
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
        # Whether this control holds user content (an editor), not a label or a page container.
        content_field = meta["type"] in {"Edit", "ComboBox"} or (
            meta["type"] == "Document" and meta.get("automation_id") != "RootWebArea"
            and bool({"Value", "Text"} & set(meta["patterns"])))
        return {"operation": "uia_verify", "id": identifier, "handle": int(self._window().handle),
                "runtime_id": list(meta["runtime_id"]), "control_type": meta["type"],
                "content_field": content_field,
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
        keys = normalize_keys(keys)
        element, meta, backend = self._referenced(identifier, name, enabled=True)
        recovery = keys.casefold() in {"{esc}", "{escape}"}
        if not recovery:
            self._check_pending_dispatch(meta, "keyboard_input")
        if not control_focus:
            root = backend.root(self._window())
            # A UWP app (Calculator) shows its content in an inner Window under the frame;
            # an arithmetic expression may go to that one too (measured 01/11).
            inner_ok = bool(CALCULATOR_KEYS.fullmatch(keys))
            if meta["type"] != "Window" or (not inner_ok and tuple(meta["runtime_id"])
                                            != tuple(backend.metadata(root)["runtime_id"])):
                raise UIAPreconditionError("unsafe_context", "Atalho de janela exige o ID da janela selecionada (na Calculadora, a expressão inteira, ex. 12*7=, vai para esse ID); para escrever texto use control_focus=true no campo.")
        before = self._activation_state(backend, element, meta) if meta["type"] == "MenuItem" else {}
        navigation = submit_keys(keys) and meta["type"] in {"Edit", "ComboBox"} and self._browser()
        display_before = self._calculator_display(backend) if CALCULATOR_KEYS.fullmatch(keys) else None
        windows_before = (None if submit_keys(keys) or recovery or display_before is not None
                          else self._process_windows())
        typed, page_before = "", ""
        if navigation:
            try:
                typed = backend.content(element, meta)["text"].strip()
                page_before = self._page_url(backend)
            except Exception:  # noqa: BLE001 - no baseline, no navigation proof; the key still goes
                navigation = False
        try:
            backend.keys(element, self._window(), meta, keys)
        except UIAPreconditionError:
            raise
        except Exception as exc:
            self._record("keyboard_input", identifier, verified=False, executed=False,
                         dispatch_uncertain=True, needs_postcondition=True,
                         submitted=submit_keys(keys), recovery=recovery, before=before)
            raise UIAError("mutation_unverified", "Falha durante entrada; verifique o estado antes de repetir.") from exc
        if meta["type"] in {"Edit", "Document"} and CARET_KEYS.fullmatch(keys):
            # Measured 01/11: Ctrl+End before appending stayed "unproven" forever and blocked
            # every later write; moving the caret has no effect to prove.
            return self._record("keyboard_input", identifier, verified=True, recovery=recovery,
                                before=before, verified_by="caret_only", submitted=False)
        if navigation and typed:
            arrived: list[str] = []

            def loaded() -> bool:
                url = self._page_url(backend)
                # The same page again (Enter on the address already shown) proves nothing.
                if url and url != page_before and navigated_to(typed, url):
                    arrived.append(url)
                    return True
                return False

            already = False
            try:
                if not self._wait(loaded, NAVIGATION_TIMEOUT_MS) and is_address(typed):
                    # Measured: Edge already on Wikipedia. Reloading the address it shows
                    # leaves it there; after the whole wait, that page is the requested one.
                    current = self._page_url(backend)
                    if current and current == page_before and navigated_to(typed, current):
                        arrived.append(current)
                        already = True
            except Exception:  # noqa: BLE001 - an unreadable page leaves the Enter unproven
                arrived.clear()
            if arrived:
                # The executor read the page itself: the Enter's own effect, not another label.
                return self._record("keyboard_input", identifier, verified=True, submitted=True,
                                    recovery=recovery, before=before, workflow="browser_navigate",
                                    url=arrived[0], verified_by="page_document_url",
                                    already_shown=already)
        if display_before is not None:
            shown: list[tuple[str, str]] = []

            def displayed() -> bool:
                now = self._calculator_display(backend)
                if now is not None and now != display_before:
                    shown.append(now)
                    return True
                return False

            # Typed keys arrive one by one; wait for the display to settle on the last of them.
            if self._wait(displayed, KEYPAD_TIMEOUT_MS):
                time.sleep(0.3)
                final = self._calculator_display(backend) or shown[0]
                return self._record("keyboard_input", identifier, verified=True, submitted=False,
                                    recovery=recovery, before=before, workflow="calculator_display",
                                    display=final[0], expression=final[1], verified_by="calculator_display")
        changed = self._window_change(windows_before)
        if changed:
            return self._record("keyboard_input", identifier, verified=True, submitted=False,
                                recovery=recovery, before=before, **changed)
        return self._record("keyboard_input", identifier, verified=False, needs_postcondition=True,
                            submitted=submit_keys(keys), recovery=recovery, before=before)

    def _page_before(self, backend: Any) -> str | None:
        """The page URL before a click in a browser; None outside one or when unreadable."""
        if not self._browser():
            return None
        try:
            return self._page_url(backend)
        except Exception:  # noqa: BLE001 - no baseline, no navigation proof
            return None

    def _navigation(self, backend: Any, before: str | None) -> dict[str, Any]:
        """A new page right after a click: the click navigated (measured 01/11: Edge's
        address suggestion was clicked and the task stayed uncertain at Wikipedia)."""
        if before is None:
            return {}
        seen: list[str] = []

        def moved() -> bool:
            url = self._page_url(backend)
            if url and url != before:
                seen.append(url)
                return True
            return False

        try:
            self._wait(moved, CLICK_NAVIGATION_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - an unreadable page leaves the click unproven
            return {}
        return ({"workflow": "browser_navigate", "url": seen[0], "verified_by": "page_document_url"}
                if seen else {})

    def _process_windows(self) -> dict[int, str] | None:
        """The visible windows of the selected window's process, by handle."""
        from app.computer.windows_tools import user32, window_pid, windows
        try:
            handle = int(self._window().handle)
            pid = window_pid(handle) if user32.IsWindow(handle) else 0
            return {item["handle"]: item["title"] for item in windows() if item["pid"] == pid} if pid else None
        except Exception:  # noqa: BLE001 - no snapshot, no window-change proof
            return None

    def _window_change(self, before: dict[int, str] | None) -> dict[str, Any]:
        """A window of the same process that appeared or closed right after the input.

        The executor saw it itself, so it is the dispatch's own effect (measured 01/11: Save
        As opened its dialog, the shortcut stayed "uncertain" and blocked typing the name).
        """
        if before is None:
            return {}
        seen: list[dict[str, Any]] = []

        def changed() -> bool:
            now = self._process_windows()
            if now is None:
                return False
            opened = [{"handle": h, "title": t} for h, t in now.items() if h not in before]
            closed = [{"handle": h, "title": t} for h, t in before.items() if h not in now]
            if opened or closed:
                seen.append({"workflow": "window_change", "verified_by": "process_windows",
                             "opened": opened[:3], "closed": closed[:3]})
                return True
            return False

        self._wait(changed, WINDOW_CHANGE_TIMEOUT_MS)
        return seen[0] if seen else {}

    def _browser(self) -> bool:
        import psutil
        from app.computer.windows_tools import window_pid
        try:
            return psutil.Process(window_pid(int(self._window().handle))).name().casefold() in BROWSER_PROCESSES
        except Exception:  # noqa: BLE001 - unknown process: no browser proof
            return False

    def _calculator_display(self, backend: Any) -> tuple[str, str] | None:
        """Windows Calculator's display and expression, or None in any other window."""
        try:
            root = backend.root(self._window())
            texts: list[str] = []
            for automation_id in ("CalculatorResults", "CalculatorExpression"):
                found = root.FindFirst(backend.uia.tree_scope["descendants"],
                                       backend.client.CreatePropertyCondition(
                                           backend.dll.UIA_AutomationIdPropertyId, automation_id))
                backend.calls += 1
                if not found and not texts:
                    return None  # not Calculator: one search, no expression lookup
                texts.append(str(found.CurrentName or "") if found else "")
        except Exception:  # noqa: BLE001 - no display read, no keypad proof
            return None
        return texts[0], texts[1]

    def _page_url(self, backend: Any) -> str:
        """The URL of the visible page: Chromium exposes it as the RootWebArea's Value."""
        # shortcut: Chromium only (Edge, Chrome, Brave...); Firefox keeps the Enter unproven.
        root = backend.root(self._window())
        condition = backend.client.CreatePropertyCondition(
            backend.dll.UIA_AutomationIdPropertyId, "RootWebArea")
        found = root.FindAll(backend.uia.tree_scope["descendants"], condition)
        backend.calls += 1
        for index in range(min(found.Length, 8)):
            page = found.GetElement(index)
            if page.CurrentIsOffscreen:
                continue
            value = str(backend.pattern(page, "Value").CurrentValue or "")
            if value:
                return value
        return ""

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
        display_before = (self._calculator_display(backend)
                          if operation in {"invoke", "default_action"} and meta["type"] == "Button" else None)
        page_before = (self._page_before(backend)
                       if operation in {"invoke", "default_action"} and display_before is None else None)
        windows_before = (self._process_windows()
                          if operation in {"invoke", "default_action"} and display_before is None else None)
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
        if display_before is not None:
            shown: list[tuple[str, str]] = []

            def changed() -> bool:
                now = self._calculator_display(backend)
                if now is not None and now != display_before:
                    shown.append(now)
                    return True
                return False

            if self._wait(changed, KEYPAD_TIMEOUT_MS):
                # The executor read Calculator's own display: this key's effect, not a label
                # elsewhere. Without it every keypad press stayed unproven (measured 01/11).
                return self._record(operation, identifier, verified=True, pattern=pattern,
                                    before=before_state, workflow="calculator_display",
                                    display=shown[0][0], expression=shown[0][1],
                                    verified_by="calculator_display")
        window_effect = (self._navigation(backend, page_before) or self._window_change(windows_before)
                         if not unchanged else {})
        if window_effect:
            return self._record(operation, identifier, verified=True, pattern=pattern,
                                before=before_state, **window_effect)
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
        windows_before = None if recovery else self._process_windows()
        page_before = None if recovery else self._page_before(backend)
        try:
            backend.atomic(backend.wrap(element, meta), self._window(), "click_input",
                           {"button": button, "double": double})
        except Exception as exc:
            self._record("click_input", identifier, verified=False, executed=False,
                         dispatch_uncertain=True, needs_postcondition=True, pattern="input_fallback",
                         before=before_state, **recovery)
            raise UIAError("mutation_unverified", "Clique falhou durante execução; verifique antes de repetir.") from exc
        changed = self._navigation(backend, page_before) or self._window_change(windows_before)
        if changed:
            return self._record("click_input", identifier, verified=True, pattern="input_fallback",
                                before=before_state, **changed)
        return self._record("click_input", identifier, verified=False, needs_postcondition=True,
                            pattern="input_fallback", before=before_state, **recovery)

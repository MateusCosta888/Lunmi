"""The window an AppAgent reads: selected once, bound, and never invented.

Measured 2026-10-09 through the chat ("abre o spotify e pesquisa por michel jackson"): the
HostAgent selected the window of an application that had just started, the vendor tool's
``set_focus`` raised, nothing was bound — and ASSIGN still created ``AppAgent//Spotify``
with an empty application root. Its data collection then died on the vendor's own
"No window is selected", which is what the user saw as "a pesquisa no Spotify não funciona".
These tests hold the three rules that come out of that run:

- a selection the tool could not bind is bound by the runtime, for the window the desktop
  actually offered (never for a window nobody chose);
- the AppAgent's data collection re-establishes the binding of the window this task chose;
- an ASSIGN with no bound window continues in the HostAgent instead of delegating to an
  agent that has nothing to observe.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
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


@contextlib.contextmanager
def runtime_module() -> Iterator[Any]:
    """Import the real runtime the way the worker does: with UFO's own config as cwd."""
    original = os.getcwd()
    os.chdir(ROOT / "vendor" / "UFO")
    try:
        import app.computer.ufo_runtime as module
        yield module
    finally:
        os.chdir(original)


class FakeWindow:
    """A window as the tools see one: a handle, a title, an owner, and a focus that can fail."""

    def __init__(self, handle: int = 100, title: str = "Spotify Free", root: str = "Spotify.exe",
                 focus_fails: bool = False) -> None:
        self.handle = handle
        self._title = title
        self._root = root
        self.focus_fails = focus_fails
        self.focused = 0
        self.element_info = SimpleNamespace(name=title, control_type="Window")

    def window_text(self) -> str:
        return self._title

    def class_name(self) -> str:
        return "Chrome_WidgetWin_1"

    def process_id(self) -> int:
        return 4242

    def is_minimized(self) -> bool:
        return False

    def is_visible(self) -> bool:
        return True

    def set_focus(self) -> None:
        self.focused += 1
        if self.focus_fails:
            raise RuntimeError("Failed to set focus on window")


def test_the_executor_never_selects_its_callers_chat_as_the_target(monkeypatch: pytest.MonkeyPatch) -> None:
    with runtime_module() as module:
        state, tools, client = FakeState(), FakeTools(), FakeClient()
        state.last_app_windows["2"] = FakeWindow()
        monkeypatch.setattr(module, "UIServerState", lambda: state)
        monkeypatch.setattr(os, "getppid", lambda: 4242)
        computer = computer_with(runtime_with(state, tools, module), module, client)
        with pytest.raises(ValueError, match="próprio assistente"):
            asyncio.run(computer._select_window(selection_tool()))
        assert not client.calls and state.selected_app_window is None


class FakeInspector:
    def __init__(self, root: str = "Spotify.exe") -> None:
        self.root = root
        self.read: list[Any] = []

    def get_application_root_name(self, window: Any) -> str:
        self.read.append(window)
        return self.root if window is not None else ""


class FakeState:
    def __init__(self) -> None:
        self.last_app_windows: dict[str, Any] = {}
        self.selected_app_window: Any = None
        self.control_dict: dict[str, Any] = {}
        self.control_inspector = FakeInspector()
        self.initialized: list[Any] = []

    def initialize_for_window(self, window: Any) -> None:
        # What the vendor tool does after a successful focus: install the binding.
        self.selected_app_window = window
        self.initialized.append(window)


class FakeTools:
    def __init__(self, evidence: list[dict[str, Any]] | None = None) -> None:
        self.selected_window: dict[str, Any] | None = None
        self.evidence = list(evidence or [])
        self.uia = SimpleNamespace(reset=lambda: None, elements=[], last_observation={})
        self.pending_ui = False
        self.cancelled = False


class FakeClient:
    """A stand-in for one MCP server connection: it records calls and can fail."""

    def __init__(self, error: str = "", data: Any = None) -> None:
        self.error = error
        self.data = data if data is not None else {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, params: dict[str, Any], raise_on_error: bool = False) -> Any:
        self.calls.append((name, params))
        return SimpleNamespace(is_error=bool(self.error),
                               content=[SimpleNamespace(text=self.error)] if self.error else [],
                               data=self.data)


def runtime_with(state: FakeState, tools: FakeTools, module: Any) -> Any:
    runtime = module.UFORuntime.__new__(module.UFORuntime)
    runtime.state = state
    runtime.tools = tools
    return runtime


def computer_with(runtime: Any, module: Any, client: FakeClient) -> Any:
    computer = module.PersistentComputer.__new__(module.PersistentComputer)
    computer.runtime = runtime
    computer._meta_tools = {}
    computer.clients = {"HostUIExecutor": client, "UICollector": client}
    computer.stack = None
    return computer


def selection_tool(**parameters: Any) -> Any:
    return SimpleNamespace(tool_name="select_application_window",
                           parameters={"id": "2", "name": "Spotify Free", **parameters},
                           namespace="HostUIExecutor", tool_key="k")


def test_desktop_observation_preserves_labels_but_excludes_the_callers_process(monkeypatch: pytest.MonkeyPatch) -> None:
    with runtime_module() as module:
        state, tools = FakeState(), FakeTools()
        owned, external = FakeWindow(title="Chat"), FakeWindow(title="External")
        external.process_id = lambda: 9999
        state.last_app_windows = {"2": owned, "7": external}
        monkeypatch.setattr(os, "getppid", lambda: 4242)
        client = FakeClient(data=[{"id": "2", "name": "Chat"}, {"id": "7", "name": "External"}])
        computer = computer_with(runtime_with(state, tools, module), module, client)
        tool = SimpleNamespace(tool_name="get_desktop_app_target_info", parameters={}, namespace="UICollector")
        computer._tools_registry = {"k": tool}
        result = asyncio.run(computer._run_action(SimpleNamespace(tool_key="k")))
        assert result.data == [{"id": "7", "name": "External"}]
        assert state.last_app_windows == {"7": external}


def test_launching_another_window_invalidates_the_old_binding() -> None:
    with runtime_module() as module:
        state, tools = FakeState(), FakeTools()
        state.selected_app_window = FakeWindow(handle=100)
        client = FakeClient(data={"verified": True, "handle": 200})
        computer = computer_with(runtime_with(state, tools, module), module, client)
        tool = SimpleNamespace(tool_name="launch_application", parameters={}, namespace="HostUIExecutor")
        computer._tools_registry = {"k": tool}
        result = asyncio.run(computer._run_action(SimpleNamespace(tool_key="k")))
        assert result.data["handle"] == 200
        assert state.selected_app_window is None and tools.selected_window is None


def test_a_selection_the_tool_could_not_bind_is_bound_by_the_runtime(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The measured failure: focus raises, so the selection bound nothing at all."""
    with runtime_module() as module:
        state, tools = FakeState(), FakeTools()
        candidate = FakeWindow(focus_fails=True)
        state.last_app_windows["2"] = candidate
        monkeypatch.setattr(module, "UIServerState", lambda: state)
        monkeypatch.setattr(module, "window_record",
                            lambda handle: {"handle": 100, "pid": 4242, "process": "Spotify.exe",
                                            "title": "Spotify Free", "class": "Chrome_WidgetWin_1"})
        monkeypatch.setattr(module, "window_is_live", lambda handle: True)
        monkeypatch.setattr(module, "uia_window", lambda handle: FakeWindow(handle=handle))
        monkeypatch.setattr(module, "psutil_name", lambda window: "spotify.exe")
        runtime = runtime_with(state, tools, module)
        computer = computer_with(runtime, module, FakeClient(error="Failed to set focus on window"))

        reply = asyncio.run(computer._select_window(selection_tool()))

        assert not reply.is_error
        assert reply.data["root_name"] == "Spotify.exe"
        assert reply.data["bound_by"] == "runtime_binding"
        assert "set focus" in reply.data["selection_failure"]
        # The binding the AppAgent's data collection reads is now the real one.
        assert state.selected_app_window is not None
        assert int(state.selected_app_window.handle) == 100
        assert tools.selected_window == {"handle": 100, "process": "spotify.exe",
                                         "title": "Spotify Free", "label": "2",
                                         "bound_by": "runtime_binding"}
        # The vendor tool was still the first attempt, and it was asked exactly once.
        assert len(computer.clients["HostUIExecutor"].calls) == 1


def test_a_selection_the_tool_bound_normally_is_recorded_and_left_alone(
        monkeypatch: pytest.MonkeyPatch) -> None:
    with runtime_module() as module:
        state, tools = FakeState(), FakeTools()
        window = FakeWindow()
        state.last_app_windows["2"] = window
        state.selected_app_window = window
        monkeypatch.setattr(module, "UIServerState", lambda: state)
        monkeypatch.setattr(module, "uia_window",
                            lambda handle: pytest.fail("a bound selection needs no rebinding"))
        monkeypatch.setattr(module, "psutil_name", lambda window: "spotify.exe")
        runtime = runtime_with(state, tools, module)
        client = FakeClient(data={"root_name": "Spotify.exe"})
        computer = computer_with(runtime, module, client)

        reply = asyncio.run(computer._select_window(selection_tool()))

        assert reply.data["root_name"] == "Spotify.exe"
        assert "bound_by" not in reply.data                # the tool's own answer, untouched
        assert tools.selected_window["handle"] == 100


def test_a_window_whose_owner_died_yields_to_the_application_this_task_launched(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Spotify's window outlived the process that owned it, and the app was still there.

    The task's own verified launch is what identifies the application; the substitution is
    allowed only because exactly one live window of that image exists.
    """
    with runtime_module() as module:
        launched = {"operation": "launch", "verified": True, "target": "exe:Spotify.exe",
                    "process": "spotify.exe", "title": "Spotify"}
        state, tools = FakeState(), FakeTools(evidence=[launched])
        candidate = FakeWindow(handle=100, title="Spotify")
        state.last_app_windows["2"] = candidate
        monkeypatch.setattr(module, "UIServerState", lambda: state)
        monkeypatch.setattr(module, "window_record", lambda handle: None)   # the window is gone
        monkeypatch.setattr(module, "window_is_live", lambda handle: False)
        monkeypatch.setattr(module, "windows",
                            lambda: [{"handle": 200, "pid": 33376, "process": "Spotify.exe",
                                      "title": "Spotify Free", "class": "Chrome_WidgetWin_1"}])
        monkeypatch.setattr(module, "uia_window", lambda handle: FakeWindow(handle=handle))
        monkeypatch.setattr(module, "psutil_name", lambda window: "spotify.exe")
        runtime = runtime_with(state, tools, module)
        computer = computer_with(runtime, module, FakeClient(error="No window is selected"))

        reply = asyncio.run(computer._select_window(
            selection_tool(id="2", name="Spotify")))

        assert reply.data["root_name"] == "Spotify.exe"
        assert reply.data["bound_by"] == "runtime_binding_same_application"
        assert int(state.selected_app_window.handle) == 200
        assert state.selected_app_window.focused == 1


def test_two_windows_of_the_application_are_never_chosen_between(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Guessing which of two windows was meant is worse than reporting the failure."""
    with runtime_module() as module:
        launched = {"operation": "launch", "verified": True, "process": "spotify.exe",
                    "title": "Spotify"}
        state, tools = FakeState(), FakeTools(evidence=[launched])
        state.last_app_windows["2"] = FakeWindow(handle=100, title="Spotify")
        monkeypatch.setattr(module, "UIServerState", lambda: state)
        monkeypatch.setattr(module, "window_record", lambda handle: None)
        monkeypatch.setattr(module, "window_is_live", lambda handle: False)
        monkeypatch.setattr(module, "windows", lambda: [
            {"handle": 200, "pid": 1, "process": "Spotify.exe", "title": "Spotify Free"},
            {"handle": 300, "pid": 2, "process": "Spotify.exe", "title": "Spotify"}])
        monkeypatch.setattr(module, "uia_window", lambda handle: pytest.fail("nothing to bind"))
        runtime = runtime_with(state, tools, module)
        runtime.SELECT_BIND_SECONDS = 0.0
        computer = computer_with(runtime, module, FakeClient(error="No window is selected"))

        with pytest.raises(RuntimeError) as failure:
            asyncio.run(computer._select_window(selection_tool(id="2", name="Spotify")))
        assert "Selecione uma janela disponível" in str(failure.value)
        assert state.selected_app_window is None


def test_appagent_data_collection_rebinds_the_window_this_task_selected(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The delegate asked for the window's info and the binding had been lost."""
    with runtime_module() as module:
        state, tools = FakeState(), FakeTools()
        tools.selected_window = {"handle": 100, "process": "spotify.exe",
                                 "title": "Spotify Free", "label": "2"}
        monkeypatch.setattr(module, "window_record",
                            lambda handle: {"handle": 100, "pid": 4242, "process": "Spotify.exe",
                                            "title": "Spotify Free", "class": "Chrome_WidgetWin_1"})
        monkeypatch.setattr(module, "window_is_live", lambda handle: True)
        monkeypatch.setattr(module, "uia_window", lambda handle: FakeWindow(handle=handle))
        monkeypatch.setattr(module, "psutil_name", lambda window: "spotify.exe")
        runtime = runtime_with(state, tools, module)
        client = FakeClient(data={"control_text": "Spotify Free"})
        computer = computer_with(runtime, module, client)
        tool = SimpleNamespace(tool_name="get_app_window_info", parameters={"field_list": []},
                               namespace="UICollector", tool_key="k")
        computer._tools_registry = {"k": tool}

        reply = asyncio.run(computer._run_action(SimpleNamespace(tool_key="k")))

        assert int(state.selected_app_window.handle) == 100
        assert client.calls == [("get_app_window_info", {"field_list": []})]
        assert reply.data == {"control_text": "Spotify Free"}


def test_appagent_data_collection_without_a_selection_fails_with_a_reason() -> None:
    """Nothing to observe is said out loud, never answered by reading some other window."""
    with runtime_module() as module:
        state, tools = FakeState(), FakeTools()
        runtime = runtime_with(state, tools, module)
        computer = computer_with(runtime, module, FakeClient())
        tool = SimpleNamespace(tool_name="get_app_window_info", parameters={},
                               namespace="UICollector", tool_key="k")
        computer._tools_registry = {"k": tool}

        with pytest.raises(RuntimeError) as failure:
            asyncio.run(computer._run_action(SimpleNamespace(tool_key="k")))
        assert "Nenhuma janela foi selecionada nesta tarefa" in str(failure.value)


class FakeContext:
    """The surface ``_finalize_processing_context`` and the phase metrics touch."""

    def __init__(self, locals: dict[str, Any], runtime: Any, phase: Any = None) -> None:
        self.locals = dict(locals)
        self.phase_results: dict[Any, Any] = {}
        if phase is not None:
            self.phase_results[phase.phase] = phase
        self.global_context = SimpleNamespace(
            command_dispatcher=SimpleNamespace(runtime=runtime),
            get=lambda *args, **kwargs: None,
            set=lambda *args, **kwargs: None)

    def get_local(self, key: str, default: Any = None) -> Any:
        return self.locals.get(key, default)

    def set_local(self, key: str, value: Any) -> None:
        self.locals[key] = value


def processor_with(runtime: Any, module: Any, context: FakeContext) -> Any:
    processor = module.UIAHostProcessor.__new__(module.UIAHostProcessor)
    processor.global_context = context.global_context
    processor.logger = logging.getLogger("test")
    return processor


def phase_with(module: Any, replies: list[Any]) -> Any:
    return SimpleNamespace(phase=module.ProcessingPhase.ACTION_EXECUTION, success=True,
                           execution_time=0.0, data={"execution_result": replies})


def result(module: Any, status: str, error: str = "") -> Any:
    return module.Result(status=getattr(module.ResultStatus, status), error=error)


def test_an_assign_without_a_bound_window_continues_instead_of_delegating() -> None:
    with runtime_module() as module:
        state, tools = FakeState(), FakeTools()
        runtime = runtime_with(state, tools, module)
        context = FakeContext({"function_name": "select_application_window", "status": "ASSIGN"},
                              runtime, phase_with(module, [result(module, "FAILURE",
                                                                  error="No window is selected")]))
        processor_with(runtime, module, context)._finalize_processing_context(context)
        assert context.get_local("status") == "CONTINUE"


def test_an_assign_is_left_alone_when_the_window_is_bound() -> None:
    with runtime_module() as module:
        state, tools = FakeState(), FakeTools()
        state.selected_app_window = FakeWindow()
        runtime = runtime_with(state, tools, module)
        context = FakeContext({"function_name": "select_application_window", "status": "ASSIGN"},
                              runtime, phase_with(module, [result(module, "SUCCESS")]))
        processor_with(runtime, module, context)._finalize_processing_context(context)
        assert context.get_local("status") == "ASSIGN"


def test_a_model_that_selects_with_continue_is_promoted_to_assign() -> None:
    """Unchanged behaviour: a verified selection is what delegation needs."""
    with runtime_module() as module:
        state, tools = FakeState(), FakeTools()
        state.selected_app_window = FakeWindow()
        runtime = runtime_with(state, tools, module)
        context = FakeContext({"function_name": "select_application_window", "status": "CONTINUE",
                               "selected_application_root": "Spotify.exe"},
                              runtime, phase_with(module, [result(module, "SUCCESS")]))
        processor_with(runtime, module, context)._finalize_processing_context(context)
        assert context.get_local("status") == "ASSIGN"


def test_a_third_party_agent_is_assigned_without_a_window() -> None:
    with runtime_module() as module:
        state, tools = FakeState(), FakeTools()
        runtime = runtime_with(state, tools, module)
        context = FakeContext({"status": "ASSIGN", "assigned_third_party_agent": "CustomizedAgent"},
                              runtime)
        processor_with(runtime, module, context)._finalize_processing_context(context)
        assert context.get_local("status") == "ASSIGN"


def backend_with(monkeypatch: pytest.MonkeyPatch, granted_after: int) -> Any:
    """A UIA backend whose foreground request succeeds only on attempt ``granted_after``."""
    import app.computer.windows_tools as windows_tools
    from app.computer.uia import NativeBackend

    # The fake answers both ways a real desktop does: the request, and who owns the
    # foreground afterwards — only a granted request moves it.
    state = {"count": 0, "foreground": 0}

    def foreground_window(handle: int) -> bool:
        state["count"] += 1
        if state["count"] >= granted_after:
            state["foreground"] = int(handle)
        return state["foreground"] == int(handle)

    backend = NativeBackend.__new__(NativeBackend)
    backend.wrap = lambda element, meta: SimpleNamespace(has_keyboard_focus=lambda: True)
    backend.atomic = lambda *args, **kwargs: None
    monkeypatch.setattr(windows_tools, "foreground_window", foreground_window)
    monkeypatch.setattr(windows_tools, "user32",
                        SimpleNamespace(GetForegroundWindow=lambda: state["foreground"]))
    backend.attempts = state
    return backend


def test_a_window_that_needs_a_moment_to_take_the_foreground_is_waited_for(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured in the chat: one refused request lost the Enter that submits the search."""
    backend = backend_with(monkeypatch, granted_after=2)
    backend.focus(object(), SimpleNamespace(handle=42), {"focusable": True, "offscreen": False})
    assert backend.attempts["count"] == 2


def test_a_window_that_never_comes_forward_still_reports_it(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The grace is bounded: a keyboard action that cannot be delivered says so."""
    from app.computer.uia import NativeBackend, UIAError
    backend = backend_with(monkeypatch, granted_after=99)
    with pytest.raises(UIAError) as failure:
        backend.focus(object(), SimpleNamespace(handle=42), {"focusable": True, "offscreen": False})
    assert failure.value.code == "focus_lost"
    assert backend.attempts["count"] == NativeBackend.FOCUS_ATTEMPTS


class FakeForeground:
    """A desktop where the foreground belongs to another process, as measured on 2026-10-09.

    ``SetForegroundWindow`` is refused (Windows refuses it to a process that does not own the
    foreground) and attaching this thread's input changes nothing; the synthetic ALT press is
    what releases the lock. Every route is recorded, so a test can assert which one won and
    that nothing else was tried in between.
    """

    def __init__(self, *, alt_releases: bool = True) -> None:
        self.foreground = 777          # somebody else's window, the normal case
        self.other_thread = 4242
        self.lock_released = False
        self.alt_releases = alt_releases
        self.calls: list[str] = []
        self.alt_presses = 0
        self.alt_releases_seen = 0

    def IsWindow(self, handle: int) -> bool:
        return True

    def IsIconic(self, handle: int) -> bool:
        return False

    def ShowWindow(self, handle: int, command: int) -> None:
        self.calls.append("restore")

    def SetWindowPos(self, *args: object) -> None:
        self.calls.append("raise")

    def GetForegroundWindow(self) -> int:
        return self.foreground

    def GetWindowThreadProcessId(self, handle: int, pointer: object) -> int:
        return self.other_thread

    def AttachThreadInput(self, first: int, second: int, enabled: bool) -> bool:
        self.calls.append(f"attach={enabled}")
        return True

    def SetForegroundWindow(self, handle: int) -> bool:
        self.calls.append("set_foreground")
        if self.lock_released:
            self.foreground = int(handle)
        return self.foreground == int(handle)

    def keybd_event(self, key: int, scan: int, flags: int, extra: int) -> None:
        if flags:
            self.alt_releases_seen += 1
        else:
            self.alt_presses += 1
            self.lock_released = self.alt_releases


def foreground_with(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> FakeForeground:
    import app.computer.windows_tools as windows_tools

    fake = FakeForeground(**kwargs)  # type: ignore[arg-type]
    monkeypatch.setattr(windows_tools, "user32", fake)
    monkeypatch.setattr(windows_tools, "kernel32", SimpleNamespace(GetCurrentThreadId=lambda: 1))
    fake.spotify = 555  # type: ignore[attr-defined]
    return fake


def test_a_foreground_held_by_another_process_is_released_by_the_alt_press(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured 2026-10-09 (`logs/_foreground_routes_compare.py`, live window): with another
    process in front, `SetForegroundWindow` returned false and both thread-input attachments
    left the foreground where it was, while the ALT release won it — and the Enter that
    submits a search was then delivered (`logs/_foreground_free_submit.py`).
    """
    from app.computer.windows_tools import foreground_window
    fake = foreground_with(monkeypatch)

    assert foreground_window(fake.spotify) is True
    assert fake.foreground == fake.spotify
    # The polite routes were tried first, in order, and the release came last.
    assert fake.calls.index("raise") < fake.calls.index("set_foreground")
    assert "attach=True" in fake.calls and "attach=False" in fake.calls
    assert fake.alt_presses == 1 and fake.alt_releases_seen == 1


def test_a_foreground_that_stays_with_another_process_is_reported_as_not_taken(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Even released, the lock is not a guarantee: the answer is who holds the foreground."""
    from app.computer.windows_tools import foreground_window
    fake = foreground_with(monkeypatch, alt_releases=False)

    assert foreground_window(fake.spotify) is False
    assert fake.foreground == 777
    assert fake.alt_presses == 1


def test_a_moment_without_any_foreground_window_is_not_a_crash(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows reports no foreground window while the foreground changes hands.

    Measured 2026-10-09: right after the synthetic ALT press that releases the foreground
    lock, ``GetForegroundWindow`` came back NULL and the focus check died in ``int(None)`` —
    the probe exercising the route (`logs/_focus_route_apps.py`) crashed with TypeError
    instead of reporting why the key could not be delivered. A missing foreground is a
    state: the typed refusal is the answer, never an exception from the conversion.
    """
    import app.computer.windows_tools as windows_tools
    from app.computer.uia import UIAError, NativeBackend

    monkeypatch.setattr(windows_tools, "foreground_window", lambda handle: False)
    monkeypatch.setattr(windows_tools, "user32",
                        SimpleNamespace(GetForegroundWindow=lambda: None))
    assert windows_tools.foreground_handle() == 0
    backend = NativeBackend.__new__(NativeBackend)
    with pytest.raises(UIAError) as failure:
        backend.focus(object(), SimpleNamespace(handle=42),
                      {"focusable": True, "offscreen": False})
    assert failure.value.code == "focus_lost"


def test_the_observation_budget_reaches_past_a_windows_frame() -> None:
    """Measured 2026-10-09: Spotify's search field sat at depth 13, its shell ended at 12.

    At depth 12 the AppAgent's own read of that window produced 197 elements and no search
    field; at depth 16 it produced the application's controls (325 elements, 148 actionable)
    with the search field among them. The budget is what makes an application's content
    reachable at all, so it is guarded here rather than left to drift.
    """
    from app.computer.uia import DEFAULT_DEPTH
    assert DEFAULT_DEPTH > 13


def test_a_python_launcher_between_worker_and_chat_cannot_bypass_self_selection(monkeypatch):
    with runtime_module() as module:
        state, tools, client = FakeState(), FakeTools(), FakeClient()
        state.last_app_windows["2"] = FakeWindow()
        monkeypatch.setattr(module, "UIServerState", lambda: state)
        monkeypatch.setattr(os, "getppid", lambda: 2000)
        monkeypatch.setenv("UFO_CONTROLLER_PID", "4242")
        computer = computer_with(runtime_with(state, tools, module), module, client)
        with pytest.raises(ValueError, match="próprio assistente"):
            asyncio.run(computer._select_window(selection_tool()))
        assert not client.calls and state.selected_app_window is None


def test_desktop_filters_the_actual_controller_pid_through_a_python_launcher(monkeypatch):
    with runtime_module() as module:
        state, tools = FakeState(), FakeTools()
        owned, external = FakeWindow(title="Chat"), FakeWindow(title="Games")
        external.process_id = lambda: 9999
        state.last_app_windows = {"2": owned, "7": external}
        monkeypatch.setattr(os, "getppid", lambda: 2000)
        monkeypatch.setenv("UFO_CONTROLLER_PID", "4242")
        client = FakeClient(data=[{"id": "2", "name": "Chat"}, {"id": "7", "name": "Games"}])
        computer = computer_with(runtime_with(state, tools, module), module, client)
        tool = SimpleNamespace(tool_name="get_desktop_app_target_info", parameters={}, namespace="UICollector")
        computer._tools_registry = {"k": tool}
        result = asyncio.run(computer._run_action(SimpleNamespace(tool_key="k")))
        assert result.data == [{"id": "7", "name": "Games"}]
        assert state.last_app_windows == {"7": external}


def test_host_prompt_receives_window_process_identity_without_changing_the_observed_name(monkeypatch):
    with runtime_module() as module:
        record = {"handle": 100, "pid": 4242, "process": "portable.exe", "class": "GenericWindow", "title": "Games"}
        state = FakeState()
        state.last_app_windows = {"7": FakeWindow(title="Games")}
        monkeypatch.setattr(module, "windows", lambda: [dict(record)])
        async def collect(*args):
            return SimpleNamespace(success=True, data={})
        monkeypatch.setattr(module.DesktopDataCollectionStrategy, "execute", collect)
        prompter = module.CompactHostPrompter.__new__(module.CompactHostPrompter)
        prompter.prompt_template = {"user": "{control_item}"}
        context = SimpleNamespace(global_context=SimpleNamespace(command_dispatcher=SimpleNamespace(runtime=SimpleNamespace(state=state))))
        agent = SimpleNamespace(prompter=prompter)
        asyncio.run(module.NoScreenshotsDesktop().execute(agent, context))
        rows = [{"id": "7", "name": "Games", "kind": "window"}]
        import json
        result = json.loads(prompter.user_prompt_construction(rows, [], [], "Find portable app"))
        assert result[0]["name"] == "Games" and result[0]["window"]["process"] == "portable.exe"
        assert result[0]["window"]["handle"] == 100
        assert rows == [{"id": "7", "name": "Games", "kind": "window"}]
        # A stale registry row cannot receive the identity of a new label occupant.
        rows[0]["name"] = "Old target"
        result = json.loads(prompter.user_prompt_construction(rows, [], [], "Find portable app"))
        assert "window" not in result[0]
        # A vanished window or a failed collection removes the previous context.
        monkeypatch.setattr(module, "windows", lambda: [])
        asyncio.run(module.NoScreenshotsDesktop().execute(agent, context))
        assert prompter.window_metadata == {}
        prompter.window_metadata = {"7": record}
        async def failed(*args):
            return SimpleNamespace(success=False, data={})
        monkeypatch.setattr(module.DesktopDataCollectionStrategy, "execute", failed)
        asyncio.run(module.NoScreenshotsDesktop().execute(agent, context))
        assert prompter.window_metadata == {}


def test_electron_host_pids_are_excluded_while_external_electron_apps_remain_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    """F1: Electron main/renderer PIDs of Lunmi are excluded; external apps (VS Code/Discord) stay selectable."""
    with runtime_module() as module:
        state, tools = FakeState(), FakeTools()
        lunmi_main = FakeWindow(title="Lunmi")
        lunmi_main.process_id = lambda: 5511
        lunmi_renderer = FakeWindow(title="Lunmi Renderer")
        lunmi_renderer.process_id = lambda: 5512
        vscode = FakeWindow(title="Visual Studio Code")
        vscode.process_id = lambda: 8899

        state.last_app_windows = {"1": lunmi_main, "2": lunmi_renderer, "7": vscode}
        monkeypatch.setattr(module, "UIServerState", lambda: state)
        monkeypatch.setenv("UFO_ASSISTANT_PIDS", "5511")
        module.set_assistant_host_pids([5511, 5512])
        try:
            client = FakeClient(data=[
                {"id": "1", "name": "Lunmi"},
                {"id": "2", "name": "Lunmi Renderer"},
                {"id": "7", "name": "Visual Studio Code"},
            ])
            computer = computer_with(runtime_with(state, tools, module), module, client)
            tool = SimpleNamespace(tool_name="get_desktop_app_target_info", parameters={}, namespace="UICollector")
            computer._tools_registry = {"k": tool}
            result = asyncio.run(computer._run_action(SimpleNamespace(tool_key="k")))
            assert result.data == [{"id": "7", "name": "Visual Studio Code"}]
            assert state.last_app_windows == {"7": vscode}

            # If a stale window map still held Lunmi's renderer PID, _select_window blocks it:
            state.last_app_windows["2"] = lunmi_renderer
            with pytest.raises(ValueError, match="próprio assistente"):
                asyncio.run(computer._select_window(selection_tool(id="2", name="Lunmi Renderer")))
        finally:
            module.set_assistant_host_pids([])


def test_dynamic_host_pids_replace_env_bootstrap_after_explicit_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """F1: após atualização explícita, UFO_ASSISTANT_PIDS deixa de compor assistant_process_ids.

    Prova o resultado final de `assistant_process_ids()` (não só o conjunto do engine): um PID
    antigo do bootstrap não sobrevive, e uma lista dinâmica vazia também substitui o ambiente.
    """
    with runtime_module() as module:
        monkeypatch.setattr(module, "_DYNAMIC_HOST_PIDS", set())
        monkeypatch.setattr(module, "_DYNAMIC_HOST_PIDS_SET", False)
        monkeypatch.setenv("UFO_ASSISTANT_PIDS", "77001")

        # Sem atualização explícita, o bootstrap de ambiente continua valendo (primeira partida).
        ids_bootstrap = module.assistant_process_ids()
        assert 77001 in ids_bootstrap

        # Atualização explícita: a lista dinâmica é autoridade e o PID antigo sai do conjunto,
        # mesmo que o ambiente ainda anuncie 77001.
        module.set_assistant_host_pids([77002])
        ids_updated = module.assistant_process_ids()
        assert 77002 in ids_updated
        assert 77001 not in ids_updated

        # Lista dinâmica vazia também substitui o bootstrap: nada do ambiente sobrevive.
        module.set_assistant_host_pids([])
        ids_empty = module.assistant_process_ids()
        assert 77001 not in ids_empty
        assert 77002 not in ids_empty

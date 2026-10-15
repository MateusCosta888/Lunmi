"""Per-run desktop isolation for the live harness (FASE 7).

The harness reused the desktop between runs, so a run measured whatever the previous run
had left behind: T05 "create a folder" can only create it once, T06 renames a folder that
exists only until the first rename, and T01 finds the Calculator already open after the
first run. Runs 2 and 3 of the same task were therefore not samples of the same task, and
a failure in them said nothing about the agent.

`reset_desktop()` puts the desktop back to "nothing the battery provoked is running", and
`prepare_for(task)` then applies that task's own precondition. Applied before every run,
they make the repeats independent samples.

Processes are killed through psutil, not ``taskkill /IM``: the Store Notepad is a single
process hosting several top-level windows and was measured surviving ``taskkill``.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from app.computer.windows_tools import known_folder

# What the battery can leave running: exactly the applications T01-T10 provoke.
BATTERY_APPS = ("Notepad.exe", "CalculatorApp.exe", "SystemSettings.exe")
CALCULATOR_APPS = ("calculatorapp.exe",)
EXPLORER_WINDOW_CLASS = "CabinetWClass"
CALCULATOR_READY_SECONDS = 10.0

# The two folders the filesystem tasks create and rename. Every task that touches them
# starts from the same described state, so no task depends on another having run.
CREATED_FOLDER = "UFO-Test"
RENAMED_FOLDER = "UFO-Test-2"


def desktop_folder() -> Path:
    """The user's Desktop, resolved the same way the product resolves it."""
    return known_folder("desktop")


# --- R6: ownership ----------------------------------------------------------------------
# "Which of these did the battery itself start?" is answered by recording it at the moment
# it starts — never by an image name. Measured incident: an earlier battery ended the
# `cmd.exe` processes of the user because cleanup looked for that name. A recorded pid
# counts as ours only while its creation time still matches the one recorded, so a recycled
# pid is never mistaken for the process we started.
_OWNED_PIDS: dict[int, float] = {}
_OWNED_WINDOWS: set[int] = set()


def own(pid: int) -> None:
    """Record a process this battery started, with the time it started (R6)."""
    import psutil
    if pid <= 0 or pid == os.getpid():
        return
    try:
        created = psutil.Process(pid).create_time()
    except Exception:  # noqa: BLE001 - a process that cannot be described is not owned
        return
    _OWNED_PIDS.setdefault(pid, created)


def own_window(handle: int) -> None:
    """Record a window this battery opened, so only it may be closed later (R6)."""
    if handle:
        _OWNED_WINDOWS.add(int(handle))


def owned_pids() -> set[int]:
    """The recorded processes that are still the ones this battery started."""
    import psutil
    alive: set[int] = set()
    for pid, created in list(_OWNED_PIDS.items()):
        try:
            if abs(psutil.Process(pid).create_time() - created) < 2.0:
                alive.add(pid)
        except Exception:  # noqa: BLE001 - a gone process owns nothing
            continue
    return alive


def owned_windows() -> set[int]:
    return set(_OWNED_WINDOWS)


def own_evidence(evidence: Any) -> None:
    """Record what one task's own evidence proves this battery caused (R6)."""
    for item in evidence or ():
        try:
            own(int(item.get("pid") or 0))
            own_window(int(item.get("handle") or 0))
        except (AttributeError, TypeError, ValueError):
            continue


def kill_apps(images: tuple[str, ...] = BATTERY_APPS) -> list[str]:
    """Kill the battery's own applications and report which ones were still running.

    Only what this battery recorded is killed, and only when its creation time still
    matches: a Notepad the user had open before the battery is left exactly as it is.
    """
    import psutil
    wanted = {image.casefold() for image in images}
    mine = owned_pids()
    killed: list[str] = []
    for process in psutil.process_iter(["pid", "name"]):
        pid = process.info["pid"]
        name = process.info["name"] or ""
        if pid not in mine or name.casefold() not in wanted:
            continue
        try:
            process.kill()
            killed.append(f"{name} ({pid})")
            _OWNED_PIDS.pop(pid, None)
        except Exception:  # noqa: BLE001 - a process that refuses to die is not fatal
            continue
    return killed


def close_explorer_windows() -> list[str]:
    """Close the Explorer windows this battery opened; never touch explorer.exe.

    A folder window the user opened himself is his document, so it is not closed (R6).
    """
    from pywinauto import Desktop
    closed: list[str] = []
    for window in Desktop(backend="uia").windows():
        try:
            handle = int(window.handle)
            if handle not in _OWNED_WINDOWS or window.class_name() != EXPLORER_WINDOW_CLASS:
                continue
            closed.append(f"{window.element_info.name} ({handle})")
            window.close()
        except Exception:  # noqa: BLE001 - a window already gone is not a failure
            continue
    return closed


def reset_desktop() -> dict[str, list[str]]:
    """Desktop back to "nothing the battery provoked is running".

    R6: what the battery did not start is not touched. A leftover the battery itself
    started in an earlier run is recorded on disk (``output/desktop-owned.json``) so it is
    still cleaned, while an application the user already had open is left alone.
    """
    load_owned()
    killed = kill_apps()
    closed = close_explorer_windows()
    save_owned()
    return {"killed": killed, "explorer_windows": closed}


# The registry survives a run, so the next run can still clean what it started.
OWNED_FILE = "output/desktop-owned.json"


def _owned_path() -> Path:
    return Path(__file__).resolve().parents[1] / OWNED_FILE


def save_owned() -> None:
    import json
    try:
        path = _owned_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pids": _OWNED_PIDS, "windows": sorted(_OWNED_WINDOWS)}),
                        encoding="utf-8")
    except OSError:
        return


def load_owned() -> None:
    """Read what an earlier run of this battery started (never what it did not)."""
    import json
    try:
        value = json.loads(_owned_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(value, dict):
        return
    for pid, created in (value.get("pids") or {}).items():
        try:
            _OWNED_PIDS.setdefault(int(pid), float(created))
        except (TypeError, ValueError):
            continue
    for handle in value.get("windows") or ():
        try:
            own_window(int(handle))
        except (TypeError, ValueError):
            continue


def _remove(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
    return not path.exists()


def _make(path: Path) -> bool:
    path.mkdir(parents=True, exist_ok=True)
    return path.is_dir()


def _start_calculator() -> bool:
    """Start the Calculator and wait for its window, the way T10 needs to find it."""
    import psutil
    from app.computer.windows_tools import windows

    os.startfile("calc.exe")
    deadline = time.monotonic() + CALCULATOR_READY_SECONDS
    while time.monotonic() < deadline:
        for window in windows():
            name = str(window.get("process") or "").casefold()
            if name in CALCULATOR_APPS:
                own(int(window["pid"]))
                own_window(int(window["handle"]))
                return True
            if name == "applicationframehost.exe":
                try:
                    if any(psutil.Process(child.process_id()).name().casefold() in CALCULATOR_APPS
                           for child in psutil.Process(window["pid"]).children()):
                        own(int(window["pid"]))
                        own_window(int(window["handle"]))
                        return True
                except Exception:  # noqa: BLE001 - a frame without the app is not the app
                    continue
        time.sleep(0.1)
    return False


def precondition(task_id: int) -> str:
    """One line describing the state `prepare_for(task_id)` has to leave behind."""
    return {
        1: "Calculator closed, so opening it is a real launch",
        2: "Notepad closed, no unsaved document to reuse",
        3: "no Explorer window open",
        4: "Settings closed, so the page is opened from nothing",
        5: f"neither {CREATED_FOLDER} nor {RENAMED_FOLDER} exists, so creating is a creation",
        6: f"{CREATED_FOLDER} exists empty and {RENAMED_FOLDER} does not, so the rename has a source",
        7: f"{RENAMED_FOLDER} exists, independently of whether T05/T06 ran",
        8: "Explorer window open, as left by T07, so minimizing has a real target",
        9: "Notepad and Calculator both closed",
        10: "Calculator open, because the task is to close it",
    }.get(task_id, "nothing beyond the desktop reset")


def verify_task(task_id: int) -> dict[str, Any]:
    """Independent live oracle: product SUCCESS and nonempty evidence are not proof.

    These are acceptance checks for the battery, not instruction-specific app logic.
    """
    from pywinauto import Desktop
    import psutil
    from app.computer.windows_tools import read_text, user32, windows

    snapshot = windows()
    calculator = []
    notepad = [item for item in snapshot if item["process"].casefold() == "notepad.exe"]
    explorer = [item for item in snapshot if item["class"] == EXPLORER_WINDOW_CLASS]
    for item in snapshot:
        process = item["process"].casefold()
        if process == "calculatorapp.exe":
            calculator.append(item)
        elif process == "applicationframehost.exe":
            native = Desktop(backend="uia").window(handle=item["handle"]).wrapper_object()
            if any(psutil.Process(child.process_id()).name().casefold() == "calculatorapp.exe"
                   for child in native.children() if child.process_id() != native.process_id()):
                calculator.append(item)
    observed: dict[str, Any] = {"task": task_id, "verified": False}
    if task_id == 1:
        observed.update(verified=bool(calculator), windows=calculator)
    elif task_id == 2:
        matches = []
        for item in notepad:
            native = Desktop(backend="uia").window(handle=item["handle"]).wrapper_object()
            for kind in ("Document", "Edit"):
                for control in native.descendants(control_type=kind):
                    if read_text(control).replace("\r\n", "\n").rstrip("\n") == "Hello World":
                        matches.append({"handle": item["handle"], "control_type": kind})
        observed.update(verified=bool(matches), editor_matches=matches)
    elif task_id in {3, 7}:
        import pythoncom
        import win32com.client
        expected = known_folder("downloads") if task_id == 3 else desktop_folder() / RENAMED_FOLDER
        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("Shell.Application")
            paths = [str(item.Document.Folder.Self.Path) for item in shell.Windows()]
        finally:
            pythoncom.CoUninitialize()
        observed.update(verified=any(Path(path) == expected for path in paths),
                        expected_path=str(expected), paths=paths)
    elif task_id == 4:
        matches = []
        for item in snapshot:
            if item["process"].casefold() not in {"systemsettings.exe", "applicationframehost.exe"}:
                continue
            native = Desktop(backend="uia").window(handle=item["handle"]).wrapper_object()
            if item["process"].casefold() == "applicationframehost.exe" and not any(
                    psutil.Process(child.process_id()).name().casefold() == "systemsettings.exe"
                    for child in native.children() if child.process_id() != native.process_id()):
                continue
            matches.extend(text for control in native.descendants()
                           if (text := read_text(control)).strip().casefold() in {"som", "sound"})
        observed.update(verified=bool(matches), page_matches=matches[:10])
    elif task_id == 5:
        path = desktop_folder() / CREATED_FOLDER
        observed.update(verified=path.is_dir(), path=str(path))
    elif task_id == 6:
        source, target = desktop_folder() / CREATED_FOLDER, desktop_folder() / RENAMED_FOLDER
        observed.update(verified=not source.exists() and target.is_dir(),
                        source_exists=source.exists(), destination_exists=target.is_dir())
    elif task_id == 8:
        observed.update(verified=bool(explorer) and all(item["minimized"] for item in explorer),
                        windows=explorer)
    elif task_id == 9:
        foreground = int(user32.GetForegroundWindow() or 0)
        observed.update(verified=bool(notepad) and any(item["handle"] == foreground for item in calculator),
                        notepad_handles=[item["handle"] for item in notepad], foreground=foreground,
                        calculator_handles=[item["handle"] for item in calculator])
    elif task_id == 10:
        observed.update(verified=not calculator, remaining_windows=calculator)
    else:
        raise ValueError("Tarefa desconhecida.")
    return observed


def prepare_for(task_id: int) -> dict[str, Any]:
    """Apply the task's precondition on top of a desktop reset. Returns what it changed."""
    reset = reset_desktop()
    desktop = desktop_folder()
    created_folder = desktop / CREATED_FOLDER
    renamed_folder = desktop / RENAMED_FOLDER
    removed: list[str] = []
    created: list[str] = []
    launched: list[str] = []

    if task_id == 5:
        for path in (created_folder, renamed_folder):
            if _remove(path):
                removed.append(path.name)
    elif task_id == 6:
        if _remove(renamed_folder):
            removed.append(renamed_folder.name)
        if created_folder.exists():
            _remove(created_folder)
        if _make(created_folder):
            created.append(created_folder.name)
    elif task_id == 7:
        if _remove(created_folder):
            removed.append(created_folder.name)
        if _make(renamed_folder):
            created.append(renamed_folder.name)
    elif task_id == 8:
        # The original acceptance sequence runs T07 (open folder) before T08
        # (minimize Explorer). Isolation must preserve that precondition, not turn
        # minimizing into an unrequested launch with no window to minimize.
        from app.computer.windows_tools import windows
        folder = known_folder("downloads")
        os.startfile(str(folder), "open")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            opened = next((item for item in windows() if item["class"] == EXPLORER_WINDOW_CLASS),
                          None)
            if opened is not None:
                # Only the window this fixture opened may be closed again (R6).
                own_window(int(opened["handle"]))
                launched.append("Explorer Downloads")
                break
            time.sleep(.05)
        if not launched:
            raise RuntimeError("Pré-condição T08: janela Explorer não abriu.")
    elif task_id == 10:
        if _start_calculator():
            launched.append("CalculatorApp.exe")

    save_owned()
    return {"task": task_id, "precondition": precondition(task_id), "reset": reset,
            "removed": removed, "created": created, "launched": launched,
            "owned": {"pids": len(owned_pids()), "windows": len(owned_windows())}}

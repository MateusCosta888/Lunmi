"""Where the applications on this machine are found (F-AL1 and F-AL6).

Everything here is read-only and Windows-specific: the shell's own application folder,
the Start Menu and Desktop shortcuts, the uninstall registry, the App Paths keys, what
`PATH` exposes, folders the user pointed at, and a **progressive sweep** of the places a
Windows machine keeps programs in — including outside Program Files and AppData, and on
every fixed drive.

Three rules the sweep obeys, because a real machine is not a test fixture:

1. **Nothing heavy runs on the request path.** The sweep is a resumable state machine:
   each call walks a bounded slice of a directory queue within a time budget and records
   where it stopped in the index itself. A cold index therefore means "started", not
   "scanned 1 900 executables before answering the user" (measured: a full recursive scan
   of Program Files ×2 + `LocalAppData\\Programs` is **16 436 ms**).
2. **Identity decides, sources are ranked.** Two sources describing one application
   produce one entry (`appindex.merge_entries`); the better source wins the spelling, and
   a name the user typed for it is never lost by a rescan.
3. **Trust travels with the entry.** What the operating system registered is trusted;
   what was found by sweeping an arbitrary folder or drive is not, and its first launch
   asks for consent.

Measured on this machine (8/out/2026): Apps folder 209 items in 370 ms, Start Menu 116
shortcuts in 7 ms (467 ms to resolve their targets), registry 337 keys in 2 ms, PATH 50
directories. pywin32 may print an "IUnknown" release warning on stderr while shell items
are collected; it is cosmetic.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import time
import winreg
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import psutil

from app.computer.appindex import (
    GENERIC_STEMS, KIND_CLI, KIND_MAIN, KIND_PORTABLE, KIND_UNKNOWN, LAUNCHER_STEMS, USER_SOURCE,
    AppEntry, AppIndex, apply_strategy, classify_stem, clean_name, guess_aliases, identity,
    install_alias, merge_entries, normalize_path, now_ms, pretty_stem, stem,
)
from app.computer.settings_pages import fold

# --- limits ---------------------------------------------------------------------------
# A slice is what one background call may spend; the queue is what makes the rest
# progress on the next call. The ceilings exist so a machine with a huge drive cannot
# turn discovery into a disk benchmark.
SWEEP_BUDGET_MS = 6000.0
SEARCH_BUDGET_MS = 1500.0
SWEEP_MAX_DEPTH = 6
SEARCH_MAX_DEPTH = 3
SWEEP_MAX_ENTRIES = 4000
SWEEP_PENDING_CAP = 1500
# Directories that never hold a user's application, or whose walk is pure cost.
SWEEP_SKIP = frozenset({
    "windows", "winsxs", "servicing", "installer", "softwaredistribution", "$recycle.bin",
    "system volume information", "node_modules", "packages", "assembly", "driverstore",
    "fonts", "crashdumps", "temp", "tmp", "cache", "caches", ".git", ".vs", ".vscode",
    "logs", "log", "appdata", "application data", "cookies", "history", "squirreltemp",
    "microsoft shared", "internet explorer", "windows defender", "defender", "intel",
    "nvidia", "nvidia corporation", "advanced micro devices", "realtek", "boot", "recovery",
    "perflogs", "package cache", "obj", "dist", "build", "site-packages", "__pycache__",
    # Entry-point folders: they hold a runtime's command-line tools, not applications.
    # Measured: 106 entries (`pip`, `pygmentize`, `edge-tts`, `pyside6-*`) came from Python
    # `Scripts` folders alone, and "edge-tts" was a competing candidate for "edge".
    "scripts", "sbin", "libexec", "bin", "cmd", "tools", "modules", "resources",
    # The Store's alias folder: every executable in it is a stub that launches an already
    # indexed package. Measured here: it produced four "Notepad" entries — the package's
    # AppID, its `WindowsApps` executable via App Paths, and two stubs — for one program.
    "windowsapps",
})
# Roots the sweep visits in order: per-user installs first (small and rich), then the
# machine-wide ones, then every fixed drive.
USER_ROOTS = ("LOCALAPPDATA/Programs", "LOCALAPPDATA", "APPDATA", "USERPROFILE/Desktop",
              "USERPROFILE/Downloads", "USERPROFILE/Documents")
MACHINE_ROOTS = ("ProgramFiles", "ProgramFiles(x86)", "ProgramData")
# Roots whose contents are trusted by location alone.
TRUSTED_ROOTS = ("ProgramFiles", "ProgramFiles(x86)", "ProgramData", "LOCALAPPDATA")
# A finished root is only swept again after this long, or when a resolution fails.
REFRESH_DAYS = 7.0
# Where a swept application comes from; ranked below every structured source.
SWEEP_SOURCE = "sweep"
# Folder identifiers Windows itself uses in an App Paths entry. The Apps folder reports
# a classic application as `{7C5A40EF-...}\Steam\Steam.exe`, and storing that literally
# would be a path that does not exist — measured: it produced a second "Steam" whose
# target was never there.
KNOWN_FOLDERS = {
    "{7c5a40ef-a0fb-4bfc-874a-c0f2e0b9fa8e}": "ProgramFiles(x86)",
    "{905e63b6-c1bf-494e-b29c-65b732d3d21a}": "ProgramFiles",
    "{de974d24-d9c6-4d3e-bf91-f4455120b917}": "CommonProgramFiles(x86)",
    "{f1b32785-6fba-4fcf-9d55-7b8e7f157091}": "LOCALAPPDATA",
    "{3eb685db-65f9-4cf6-a03a-e3ef65729f3d}": "APPDATA",
    "{62ab5d82-fdc1-4dc3-a9dd-070d1d495d97}": "ProgramData",
}


def expand_known_folder(value: str) -> str:
    r"""Turn a leading `{FOLDERID}\` prefix into the directory it stands for."""
    text = str(value or "")
    if not text.startswith("{") or "}" not in text[:42]:
        return text
    head, _, tail = text.partition("}")
    identifier = head + "}"
    variable = KNOWN_FOLDERS.get(identifier.casefold())
    if not variable:
        return text
    root = os.environ.get(variable)
    if not root:
        return text
    return str(Path(root) / tail.lstrip("\\/"))


@dataclass(frozen=True)
class SourceCost:
    """What one discovery source found, in how long — the number the probe reports."""

    name: str
    found: int
    ms: float
    # False when the source raised. A source that failed reports nothing, which must never
    # be read as "everything it used to describe is gone".
    ok: bool = True

    def to_wire(self) -> dict[str, Any]:
        return {"source": self.name, "found": self.found, "ms": round(self.ms, 1),
                "ok": self.ok}


def _timed(name: str, function: Callable[[], list[AppEntry]]) -> tuple[list[AppEntry], SourceCost]:
    started = time.perf_counter()
    try:
        found = function()
        ok = True
    except Exception:  # noqa: BLE001 - one dead source must not lose the others
        found = []
        ok = False
    return found, SourceCost(name, len(found), (time.perf_counter() - started) * 1000, ok)


def _with_com(function: Callable[[], Any]) -> Any:
    """Run `function` with COM initialised for *this* thread (discovery runs in one)."""
    import pythoncom
    pythoncom.CoInitialize()
    try:
        return function()
    finally:
        pythoncom.CoUninitialize()


def _arg_image(arguments: Sequence[str]) -> str:
    """The executable an application's own launcher arguments name, if they name one.

    Discord's shortcut runs `Update.exe --processStart Discord.exe`: the process that
    appears is `Discord.exe`, not `Update.exe`, and a verification that expected the
    latter would honestly report a failure for a successful launch.
    """
    for argument in arguments:
        token = argument.strip().strip('"')
        if not token.casefold().endswith(".exe"):
            continue
        name = Path(token).name
        if stem(name).casefold() not in GENERIC_STEMS:
            return name
    return ""


def _shortcut_targets(shortcuts: Sequence[Path]) -> dict[str, tuple[str, tuple[str, ...], str]]:
    """Resolve `.lnk` files to (target, arguments, working directory), one shell session.

    The arguments matter: Discord's own shortcut launches `Update.exe
    --processStart Discord.exe`, so a launcher that ignores them starts nothing. One
    `WScript.Shell` serves every shortcut on purpose: dispatching one per file cost 1.5 s
    for 116 shortcuts (measured), because each `CreateShortCut` paid its own COM object.
    """

    def read() -> dict[str, tuple[str, tuple[str, ...], str]]:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        resolved: dict[str, tuple[str, tuple[str, ...], str]] = {}
        for shortcut in shortcuts:
            try:
                item = shell.CreateShortCut(str(shortcut))
                arguments = str(item.Arguments or "").strip()
                parts = tuple(part for part in arguments.split(" ") if part) if arguments else ()
                resolved[str(shortcut)] = (str(item.TargetPath or ""), parts,
                                           str(item.WorkingDirectory or ""))
            except Exception:  # noqa: BLE001 - a broken shortcut is not an application
                continue
        return resolved

    try:
        return _with_com(read)
    except Exception:  # noqa: BLE001 - a shell that cannot be used resolves nothing
        return {}


def _entry_for_exe(path: str, *, source: str, name: str = "", publisher: str = "",
                   install_dir: str = "", description: str = "", trust: str = "trusted",
                   launch: str = "exe", target: str = "", args: tuple[str, ...] = (),
                   workdir: str = "", extra_aliases: Iterable[str] = (),
                   entry_id: str = "") -> AppEntry | None:
    """Build one entry from an executable path, or nothing when it is not one."""
    text = expand_known_folder(str(path or "").strip().strip('"').split(",")[0].strip().strip('"'))
    if Path(text).suffix.casefold() != ".exe":
        return None
    # A Store application is identified by its package, never by a file inside the package
    # folder or by a stub in the alias folder: measured, `Notepad` was indexed four times
    # that way — the AppID, plus three paths that all launch the same package.
    if any(part.casefold() == "windowsapps" for part in Path(text).parts):
        return None
    image = Path(text).name
    # An updater, launcher, installer or a component's helper is not an application
    # unless a shortcut supplies the arguments that give it a purpose
    # (`Update.exe --processStart Discord.exe`). Measured: `Update.exe`,
    # `updater_launcher.exe`, `inno_updater.exe` and `Uninstall X.exe` were indexed as
    # applications, and the folder they sat in made them answer to the app's own name.
    stem_words = set(stem(image).casefold().replace("-", " ").replace("_", " ").split())
    if stem_words & LAUNCHER_STEMS and not args:
        return None
    raw_name = name.strip() or pretty_stem(text)
    display = clean_name(raw_name)
    if not display:
        return None
    return apply_strategy(AppEntry(
        id=entry_id or identity(launch, target or text, ""),
        name=display,
        # The folder the executable lives in is offered as a name when it is informative
        # ("Microsoft VS Code", "7-Zip"); "bin" or "x64" never are. The executable path
        # is used, never the shortcut's own folder, which says nothing about the program.
        aliases=guess_aliases(display, target or text, image,
                              (*extra_aliases, *install_alias(text, raw_name), raw_name)),
        launch=launch, target=target or text, args=args, workdir=workdir, exe=image,
        source=source, publisher=publisher, install_dir=install_dir or str(Path(text).parent),
        description=description, trust=trust, seen_ms=now_ms(),
        kind=classify_exe(text, source=source, trust=trust),
    ))


# An identifier the shell lists but no program file: a packaged application, or a system
# application like the File Explorer. Its shape is a dotted reverse-domain name, optionally
# with a package qualifier (`Publisher.Name_hash!Id`).
_APPID_OK = re.compile(r"^[A-Za-z0-9][\w.\- ]*(?:![\w.\-]+)?$", re.UNICODE)
# A `shell:AppsFolder` item that is a document, help file or address rather than a program.
_APPID_DOCUMENTS = frozenset({".html", ".htm", ".chm", ".pdf", ".txt", ".url", ".lnk",
                              ".doc", ".docx", ".rtf", ".md", ".png", ".jpg", ".mp4"})


def launchable_appid(app_id: str) -> bool:
    """True when a `shell:AppsFolder` identifier names something Windows can start.

    The folder lists more than applications: web links, help documents and file paths sit
    beside the packaged ones, and indexing one of those would offer the user a "program"
    that is a page. A launchable identifier is a dotted name — `Microsoft.Windows.Explorer`,
    `com.squirrel.Discord.Discord` — with no scheme, no path separator and no document
    extension.
    """
    text = str(app_id or "").strip()
    if not text or not _APPID_OK.match(text):
        return False
    if "://" in text or "\\" in text or "/" in text:
        return False
    if Path(text).suffix.casefold() in _APPID_DOCUMENTS:
        return False
    return "." in text


# --- what an executable says about itself (R2) ---------------------------------------
# Sources whose entries are most likely to be a tool rather than a program: the signals
# that decide a role are read only for these, so the cheap classification of a *name*
# carries the rest and a refresh is not turned into a disk benchmark.
_PROBE_SOURCES = frozenset({SWEEP_SOURCE, USER_SOURCE, "path", "app_paths"})
# PE subsystem 2 is a windowed application, 3 is a console application. Subsystem sits at
# the same offset in the 32- and 64-bit optional headers (measured against both).
_PE_SUBSYSTEM_OFFSET = 68


def pe_subsystem(path: str) -> str:
    """Whether an executable is windowed or a console program, from two tiny reads.

    A console tool has no window of its own, so an entry that expects one would report an
    honest NOT_VERIFIED for a program that opened perfectly — classifying it is how the
    launcher knows not to wait for a window that can never exist. The read is the DOS
    loader offset (4 bytes at 0x3C) and the optional header's subsystem (2 bytes), so it
    costs a file open and nothing else.
    """
    try:
        with open(path, "rb") as stream:
            stream.seek(0x3C)
            offset = int.from_bytes(stream.read(4), "little")
            if offset <= 0 or offset > 0x100000:
                return ""
            stream.seek(offset)
            if stream.read(4) != b"PE\x00\x00":
                return ""
            stream.seek(20 + _PE_SUBSYSTEM_OFFSET, 1)
            subsystem = int.from_bytes(stream.read(2), "little")
    except (OSError, ValueError):
        return ""
    return {2: "gui", 3: "console"}.get(subsystem, "")


def classify_exe(path: str, *, source: str, trust: str = "trusted") -> str:
    """What one executable is, from the cheapest signal that decides it (R2).

    A name the role words already explain is answered without touching the file; a program
    that only a probe can classify is probed only when its source makes that likely. When
    nothing proves a role the answer is `unknown`, never a guess: a wrong classification
    would rank a legitimate program below a part of another one.
    """
    role = classify_stem(Path(path).name)
    if role != KIND_UNKNOWN:
        return role
    if source not in _PROBE_SOURCES:
        return KIND_MAIN
    if pe_subsystem(path) == "console":
        return KIND_CLI
    if source in {SWEEP_SOURCE, USER_SOURCE} and trust == USER_SOURCE:
        # Found by sweeping a folder or a drive that Windows never registered, or pointed
        # at by the user: it runs from where it sits, which is what a portable program is.
        return KIND_PORTABLE
    return KIND_MAIN


# --- the shell's application folder (Win32 *and* packaged applications) --------------
def discover_apps_folder() -> list[AppEntry]:
    """`shell:AppsFolder`: the one place that lists what Windows itself can start.

    Measured: 209 items in 370 ms, covering Store/AppX applications (an AppID carrying a
    package identity) and classic ones (the AppID *is* the executable path). Nothing is
    invented for an item that is neither: a document or a folder in there is skipped.
    """

    def read() -> list[tuple[str, str]]:
        import win32com.client
        shell = win32com.client.Dispatch("Shell.Application")
        folder = shell.NameSpace("shell:AppsFolder")
        return [(str(item.Name), str(item.Path)) for item in folder.Items()]

    entries: list[AppEntry] = []
    for name, path in _with_com(read):
        app_id = str(path or "").strip()
        if not app_id or not name:
            continue
        if app_id.casefold().endswith(".exe"):
            entry = _entry_for_exe(app_id, source="appsfolder", name=name)
        elif "!" in app_id or launchable_appid(app_id):
            # No alias is invented from the identifier: `guess_aliases` of a package
            # identity produced the nonsense name "Microsoft" for the Store Notepad
            # (measured), and the identifier itself is not a way anybody calls a program.
            candidate = apply_strategy(AppEntry(
                id=identity("appid", app_id, app_id), name=name,
                launch="appid", target=app_id, aumid=app_id, source="appsfolder",
                kind=KIND_MAIN, seen_ms=now_ms(),
            ))
            # A *bare* identifier is not a file and not a package: measured (2026-10-08),
            # `explorer.exe shell:AppsFolder\Anysphere.Cursor` opened nothing while
            # `Cursor.exe` opened the program, and indexing the bare id then outranked the
            # entry that worked. So a bare id is kept only when a strategy turns it into
            # something launchable (the File Explorer is opened by folder) — never as a
            # second, non-working description of a program that is already indexed.
            entry = candidate if ("!" in app_id or candidate.launch != "appid") else None
        else:
            continue
        if entry is not None:
            entries.append(entry)
    return entries


# --- Start Menu and Desktop shortcuts ------------------------------------------------
def _shortcut_locations() -> list[tuple[Path, str]]:
    locations = (("APPDATA", "Microsoft/Windows/Start Menu/Programs", "start_menu"),
                 ("ProgramData", "Microsoft/Windows/Start Menu/Programs", "start_menu"),
                 ("USERPROFILE", "Desktop", "desktop"),
                 ("PUBLIC", "Desktop", "desktop"))
    found: list[tuple[Path, str]] = []
    for variable, tail, source in locations:
        root = os.environ.get(variable)
        if not root:
            continue
        directory = Path(root) / tail
        if directory.is_dir():
            found.append((directory, source))
    return found


def discover_start_menu() -> list[AppEntry]:
    """Every shortcut the user can see, resolved to the executable it starts.

    A shortcut *with* arguments keeps the shortcut as the launch target — that is the
    only way the arguments survive; one without them names the executable directly.
    """
    located = [(shortcut, source) for directory, source in _shortcut_locations()
               for shortcut in sorted(directory.rglob("*.lnk"))]
    resolved = _shortcut_targets([shortcut for shortcut, _ in located])
    entries: list[AppEntry] = []
    for shortcut, source in located:
        target, args, workdir = resolved.get(str(shortcut), ("", (), ""))
        if not target.casefold().endswith(".exe"):
            continue
        name = shortcut.stem or pretty_stem(target)
        if not args:
            entry = _entry_for_exe(target, source=source, name=name,
                                   extra_aliases=(shortcut.stem,))
        else:
            # Identity by what the shortcut *does* (its name, its target and its
            # arguments), not by where the file happens to sit: the Start Menu copy and
            # the Desktop copy of one launcher are one application, and renaming a
            # folder must not turn it into two.
            entry = _entry_for_exe(target, source=source, name=name, launch="lnk",
                                   target=str(shortcut), args=args, workdir=workdir,
                                   entry_id=(f"lnk:{fold(name)}|{normalize_path(target)}"
                                             f"|{' '.join(args)}"))
            image = _arg_image(args)
            if entry is not None and image:
                entry = replace(entry, exe=image)
        if entry is not None:
            entries.append(entry)
    return entries


# --- the uninstall registry and App Paths --------------------------------------------
def _stem_words(path: Path) -> list[str]:
    return stem(path.name).casefold().replace("-", " ").replace("_", " ").split()


def _install_location_exe(location: Path, display_name: str) -> str:
    """Which executable a registry key means, when the key did not say."""
    try:
        candidates = [item for item in location.iterdir()
                      if item.is_file() and item.suffix.casefold() == ".exe"]
    except (OSError, ValueError):
        return ""
    if not candidates:
        return ""
    wanted = set(display_name.casefold().replace("-", " ").replace("_", " ").split())
    ranked = sorted(candidates, key=lambda item: (-len(wanted & set(_stem_words(item))),
                                                 item.name.casefold()))
    return str(ranked[0])


def discover_registry() -> list[AppEntry]:
    """What Windows says is installed: uninstall entries plus the App Paths registry.

    Cheap and structured (display name, publisher, install location). Keys that only
    describe a component or an update are skipped.
    """
    entries: list[AppEntry] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            entries.extend(_uninstall_keys(hive, view))
            entries.extend(_app_paths(hive, view))
    return entries


def _subkeys(hive: Any, root: str, view: int) -> list[str]:
    try:
        with winreg.OpenKey(hive, root, 0, winreg.KEY_READ | view) as parent:
            count = winreg.QueryInfoKey(parent)[0]
            names: list[str] = []
            for index in range(count):
                try:
                    names.append(winreg.EnumKey(parent, index))
                except OSError:
                    continue
            return names
    except OSError:
        return []


def _values(key: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    count = winreg.QueryInfoKey(key)[1]
    for index in range(count):
        try:
            name, value, _ = winreg.EnumValue(key, index)
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            result[name] = value.strip()
    return result


def _uninstall_keys(hive: Any, view: int) -> list[AppEntry]:
    root = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    entries: list[AppEntry] = []
    for name in _subkeys(hive, root, view):
        try:
            with winreg.OpenKey(hive, f"{root}\\{name}", 0, winreg.KEY_READ | view) as key:
                values = _values(key)
        except OSError:  # a key that cannot be read is not an application
            continue
        if values.get("SystemComponent") == "1" or values.get("ParentKeyName"):
            continue
        location = values.get("InstallLocation", "")
        executable = values.get("DisplayIcon", "")
        # Measured: plenty of keys point `DisplayIcon` at an `.ico` or a `.dll`
        # ("git-for-windows.ico", "NVI2.dll,0"), so a key that names no executable still
        # has an install folder to look in. Without this, Git had no entry at all.
        if Path(executable.split(",")[0].strip().strip('"')).suffix.casefold() != ".exe" \
                and location and Path(location).is_dir():
            executable = _install_location_exe(Path(location), values.get("DisplayName", ""))
        if not executable:
            continue
        entry = _entry_for_exe(executable, source="registry", name=values.get("DisplayName", ""),
                               publisher=values.get("Publisher", ""), install_dir=location,
                               description=values.get("Comments", ""))
        if entry is not None:
            entries.append(entry)
    return entries


def _app_paths(hive: Any, view: int) -> list[AppEntry]:
    root = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    entries: list[AppEntry] = []
    for name in _subkeys(hive, root, view):
        try:
            with winreg.OpenKey(hive, f"{root}\\{name}", 0, winreg.KEY_READ | view) as key:
                default = str(winreg.QueryValueEx(key, "")[0]).strip().strip('"')
        except OSError:
            continue
        if not default:
            continue
        entry = _entry_for_exe(default, source="app_paths", extra_aliases=(stem(name),))
        if entry is not None:
            entries.append(entry)
    return entries


# --- PATH ----------------------------------------------------------------------------
def _entry_point_folder(directory: str) -> bool:
    """True for a runtime's tool folder, which `PATH` is full of and users never open."""
    try:
        return Path(directory).name.casefold() in SWEEP_SKIP
    except (OSError, ValueError):
        return False


def _reparse(directory: Path) -> bool:
    """True for a junction or symbolic link, which is not a folder of *this* tree.

    Measured on this machine (8/out/2026): `C:\\Users\\<user>\\Configura\u00e7\u00f5es Locais` is a
    junction to `AppData\\Local`, so following it swept an already-skipped tree a second
    time and duplicated every entry under it — the index grew from 519 to 1 205 entries,
    most of them copies. A reparse point is skipped, never descended into.
    """
    try:
        attributes = directory.lstat().st_file_attributes
    except (OSError, ValueError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _system_owned(directory: str) -> bool:
    r"""True for Windows' own directories and the Store alias folder.

    `PATH` normally carries `C:\Windows\System32`, whose hundreds of utilities are the
    operating system's tools, not applications the user installed — the sweep excludes
    the same directories for the same reason. The alias folder (`WindowsApps`) only holds
    stubs; Store applications are already described properly by the Apps folder.
    """
    try:
        path = Path(directory)
        windir = os.environ.get("WINDIR")
        if windir and (path == Path(windir) or Path(windir) in path.parents):
            return True
        return path.name.casefold() == "windowsapps"
    except (OSError, ValueError):
        return False


def discover_path() -> list[AppEntry]:
    """Executables the environment already exposes by name.

    Network shares in `PATH` are skipped on purpose: listing an unreachable
    `\\\\server\\share` blocks, and discovery must never block.
    """
    entries: list[AppEntry] = []
    seen: set[str] = set()
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        directory = folder.strip().strip('"')
        if not directory or directory.startswith("\\\\") or directory in seen \
                or _system_owned(directory) or _entry_point_folder(directory):
            continue
        seen.add(directory)
        try:
            items = list(Path(directory).iterdir())
        except (OSError, ValueError):
            continue
        for item in items:
            if item.suffix.casefold() != ".exe":
                continue
            entry = _entry_for_exe(str(item), source="path")
            if entry is not None:
                entries.append(entry)
        if len(entries) >= SWEEP_MAX_ENTRIES:
            break
    return entries


# --- folders and drives the user pointed at ------------------------------------------
def entry_for_path(value: str) -> AppEntry | None:
    """One entry for an executable the user pointed at, or nothing when it is not one.

    This is the F-AL6 repair: the user knows where the program is even when Windows never
    registered it and the sweep has not reached it. The entry is `user_folder`, so the
    first launch still asks; after that, the path is remembered like any other.
    """
    return _entry_for_exe(str(value), source=USER_SOURCE, trust=USER_SOURCE)


def discover_configured(folders: Iterable[str]) -> list[AppEntry]:
    """Folders the user added: scanned bounded, and never trusted by location alone."""
    entries: list[AppEntry] = []
    started = time.perf_counter()
    for folder in folders:
        root = Path(str(folder)).expanduser()
        if not root.is_dir():
            continue
        remaining = max(50.0, SEARCH_BUDGET_MS - (time.perf_counter() - started) * 1000)
        found, _ = _walk_exes(root, max_depth=3, budget_ms=remaining, trust=USER_SOURCE,
                              source=USER_SOURCE)
        entries.extend(found)
    return entries


def _walk_exes(root: Path, *, max_depth: int, budget_ms: float, trust: str, source: str,
               needle: str = "", limit: int = SWEEP_MAX_ENTRIES) -> tuple[list[AppEntry], int]:
    """Walk `root` for executables, bounded by depth, time and item count.

    Returns the entries and how many directories were read, so a caller can tell how
    much of the tree was actually visited.
    """
    entries: list[AppEntry] = []
    deadline = time.perf_counter() + budget_ms / 1000
    visited = 0
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue and len(entries) < limit:
        if time.perf_counter() >= deadline:
            break
        directory, depth = queue.pop(0)
        visited += 1
        try:
            items = list(directory.iterdir())
        except (OSError, ValueError):
            continue
        for item in items:
            if item.is_dir():
                if depth + 1 <= max_depth and item.name.casefold() not in SWEEP_SKIP \
                        and not _reparse(item):
                    queue.append((item, depth + 1))
                continue
            if item.suffix.casefold() != ".exe":
                continue
            if needle and needle.casefold() not in item.name.casefold():
                continue
            entry = _entry_for_exe(str(item), source=source, trust=trust)
            if entry is not None:
                entries.append(entry)
    return entries, visited


# --- the progressive sweep ------------------------------------------------------------
def _fixed_drives() -> list[Path]:
    """Mounted fixed drives: a portable application may live on any of them."""
    drives: list[Path] = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except Exception:  # noqa: BLE001 - a machine without partition data still has roots
        return drives
    for partition in partitions:
        # psutil reports a fixed volume as opts "rw,fixed"; a removable or network
        # volume is excluded because walking one can block or surprise the user.
        if "fixed" not in str(partition.opts or "").casefold():
            continue
        mount = Path(str(partition.mountpoint))
        if mount.is_dir() and mount not in drives:
            drives.append(mount)
    return drives


def sweep_roots(configured: Sequence[str] = ()) -> list[Path]:
    """Every location worth sweeping: user installs, machine installs, drives, extras."""
    roots: list[Path] = []
    for name in (*USER_ROOTS, *MACHINE_ROOTS):
        value = os.environ.get(name)
        if not value:
            continue
        path = Path(value)
        if path.is_dir() and path not in roots:
            roots.append(path)
    for drive in _fixed_drives():
        if drive not in roots:
            roots.append(drive)
    for folder in configured:
        path = Path(str(folder)).expanduser()
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def trust_for(root: Path) -> str:
    """Trusted by location (the machine's own program folders) or not."""
    for name in TRUSTED_ROOTS:
        value = os.environ.get(name)
        if not value:
            continue
        try:
            if root == Path(value) or Path(value) in root.parents:
                return "trusted"
        except (OSError, ValueError):
            continue
    return USER_SOURCE


def sweep_step(index: AppIndex, *, budget_ms: float = SWEEP_BUDGET_MS,
               configured: Sequence[str] = ()) -> tuple[list[AppEntry], dict[str, Any]]:
    """One bounded slice of the sweep, resumable from what the previous slice left.

    The queue lives in the index (`sweeps`), not in memory: closing the application in
    the middle of a sweep must not throw the progress away, and a directory is visited
    once. A root that finished is only queued again after `REFRESH_DAYS`.
    """
    state: dict[str, Any] = {str(key): dict(value) for key, value in
                             (index.sweeps.get("roots") or {}).items()}
    deadline = time.perf_counter() + budget_ms / 1000
    found: list[AppEntry] = []
    for root in sweep_roots(configured):
        key = str(root).casefold()
        entry = state.get(key) or {"root": str(root), "pending": [str(root)], "depth": {},
                                   "finished_ms": 0.0, "exes": 0}
        finished = float(entry.get("finished_ms") or 0.0)
        if finished and time.time() * 1000 - finished < REFRESH_DAYS * 86_400_000:
            state[key] = entry
            continue
        if finished:
            # Due for a refresh: start the root again from the top.
            entry["pending"] = [str(root)]
            entry["depth"] = {}
            entry["finished_ms"] = 0.0
        pending = list(entry.get("pending") or [])
        depths: dict[str, int] = dict(entry.get("depth") or {})
        while pending and time.perf_counter() < deadline and len(found) < SWEEP_MAX_ENTRIES:
            folder = Path(pending.pop(0))
            depth = int(depths.get(str(folder), 0))
            try:
                items = list(folder.iterdir())
            except (OSError, ValueError):
                continue
            for item in items:
                if item.is_dir():
                    if depth + 1 <= SWEEP_MAX_DEPTH and item.name.casefold() not in SWEEP_SKIP \
                            and len(pending) < SWEEP_PENDING_CAP and not _reparse(item):
                        pending.append(str(item))
                        depths[str(item)] = depth + 1
                    continue
                if item.suffix.casefold() != ".exe" or item.name.casefold() in SWEEP_SKIP:
                    continue
                candidate = _entry_for_exe(str(item), source=SWEEP_SOURCE,
                                           trust=trust_for(item))
                if candidate is not None:
                    found.append(candidate)
                    entry["exes"] = int(entry.get("exes") or 0) + 1
        entry["pending"] = pending
        entry["depth"] = {path: value for path, value in depths.items() if path in pending}
        entry["root"] = str(root)
        if not pending:
            entry["finished_ms"] = now_ms()
        state[key] = entry
    return found, {"roots": state, "last_ms": now_ms()}


def search_for(needle: str, *, budget_ms: float = SEARCH_BUDGET_MS,
               configured: Sequence[str] = (), depth: int = SEARCH_MAX_DEPTH) -> list[AppEntry]:
    """A bounded search for one name, used when the index does not carry it yet.

    Shallow on purpose: the directories a program is normally installed in, plus the top
    of each fixed drive. It is allowed to miss — the sweep keeps making progress in the
    background, and the user can always point at the folder.
    """
    wanted = " ".join(str(needle or "").split())
    if not wanted:
        return []
    deadline = time.perf_counter() + budget_ms / 1000
    roots: list[Path] = []
    for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA", "ProgramData"):
        value = os.environ.get(name)
        if value and Path(value).is_dir():
            roots.append(Path(value))
    roots.extend(_fixed_drives())
    roots.extend(Path(str(folder)).expanduser() for folder in configured)
    found: list[AppEntry] = []
    for root in roots:
        remaining = (deadline - time.perf_counter()) * 1000
        if remaining <= 50:
            break
        entries, _ = _walk_exes(root, max_depth=depth, budget_ms=remaining,
                                trust=trust_for(root), source=SWEEP_SOURCE, needle=wanted,
                                limit=25)
        found.extend(entries)
    return found


# --- putting the sources together -----------------------------------------------------
@dataclass(frozen=True)
class Discovery:
    """One discovery pass: the entries, the cost per source, and the sweep progress."""

    entries: tuple[AppEntry, ...]
    costs: tuple[SourceCost, ...]
    sweeps: dict[str, Any]
    sources: dict[str, int]
    ms: float = 0.0
    failed: frozenset[str] = frozenset()

    def report(self) -> dict[str, Any]:
        return {"total_ms": round(self.ms, 1), "entries": len(self.entries),
                "sources": [cost.to_wire() for cost in self.costs],
                "by_source": dict(self.sources), "failed": sorted(self.failed)}


# Which entries a *complete* read of a source is allowed to say are gone. The sweep is not
# here on purpose: it is progressive, so one slice visits part of the tree and a missing
# entry only means that slice had not reached it yet.
FULL_SOURCE_READER = {"appsfolder": "appsfolder", "registry": "registry",
                      "app_paths": "registry", "start_menu": "start_menu",
                      "desktop": "start_menu", "path": "path"}


def _counts(entries: Sequence[AppEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.source] = counts.get(entry.source, 0) + 1
    return counts


def _registry_signature() -> str:
    """Key count and Windows' own last-write time of both registry roots."""
    parts: list[str] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            for root in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                         r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"):
                try:
                    with winreg.OpenKey(hive, root, 0, winreg.KEY_READ | view) as key:
                        count, _, written = winreg.QueryInfoKey(key)
                        parts.append(f"{root}:{count}:{written}")
                except OSError:
                    parts.append(f"{root}:missing")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def source_signatures(configured: Sequence[str] = ()) -> dict[str, str]:
    """Cheap fingerprints that say whether a source has to be read again.

    Reading these costs milliseconds; it is what keeps a refresh a refresh instead of a
    full rebuild. Paths and modification times, the registry's own last-write stamp, and
    the environment's `PATH` are enough for three of the four sources. The Apps folder
    has no cheap fingerprint — counting its items costs what reading them costs (measured
    370 ms) — so it is refreshed by age instead (see `AGE_ONLY_SOURCES`).
    """
    shortcuts: list[str] = []
    for directory, _ in _shortcut_locations():
        for item in sorted(directory.rglob("*.lnk")):
            try:
                shortcuts.append(f"{item}|{int(item.stat().st_mtime)}")
            except OSError:
                shortcuts.append(str(item))
    return {
        "start_menu": hashlib.sha256("\n".join(shortcuts).encode()).hexdigest()[:16],
        "registry": _registry_signature(),
        "path": hashlib.sha256(os.environ.get("PATH", "").encode()).hexdigest()[:16],
        "configured": hashlib.sha256("|".join(sorted(configured)).encode()).hexdigest()[:16],
    }


# Sources whose fingerprint would cost a full read: refreshed by age alone.
AGE_ONLY_SOURCES = frozenset({"appsfolder"})
# How long a structured source may go unread before it is read again regardless.
SOURCE_MAX_AGE_MS = 6 * 3_600_000.0


def _due(name: str, index: AppIndex, signatures: dict[str, str], age_ms: float) -> bool:
    """Whether one source must be read again in this pass."""
    if age_ms > SOURCE_MAX_AGE_MS:
        return True
    known = index.signatures.get(name)
    if known is None:
        return True
    if name in AGE_ONLY_SOURCES:
        return False
    return known != signatures.get(name, "")


def _sources(configured: Sequence[str]) -> list[tuple[str, str, Callable[[], list[AppEntry]]]]:
    """Name, signature key and reader of every structured source."""
    sources: list[tuple[str, str, Callable[[], list[AppEntry]]]] = [
        ("appsfolder", "appsfolder", discover_apps_folder),
        ("registry", "registry", discover_registry),
        ("start_menu", "start_menu", discover_start_menu),
        ("path", "path", discover_path),
    ]
    if configured:
        sources.append(("configured", "configured", lambda: discover_configured(configured)))
    return sources


def discover(*, include_sweep: bool = True, budget_ms: float = SWEEP_BUDGET_MS,
             configured: Sequence[str] = (), previous: AppIndex | None = None,
             names: Sequence[str] | None = None) -> Discovery:
    """Read the requested structured sources, then spend one sweep slice.

    The structured sources are what make an application findable immediately; the sweep
    is what finds one that is registered nowhere. A source that fails is dropped, not
    fatal: a machine with an unreadable registry key still has the other four.
    """
    started = time.perf_counter()
    collected: list[AppEntry] = []
    costs: list[SourceCost] = []
    for name, _, function in _sources(configured):
        if names is not None and name not in names:
            continue
        entries, cost = _timed(name, function)
        collected.extend(entries)
        costs.append(cost)
    # Only the sweeps of the roots still in progress matter here; the ones not run keep
    # their state, which is what makes the next slice continue instead of restart.
    sweeps: dict[str, Any] = dict((previous.sweeps if previous is not None else {}))
    if include_sweep:
        sweep_started = time.perf_counter()
        entries, sweeps = sweep_step(previous or AppIndex(), budget_ms=budget_ms,
                                     configured=configured)
        collected.extend(entries)
        costs.append(SourceCost("sweep", len(entries),
                                (time.perf_counter() - sweep_started) * 1000))
    merged = merge_entries(collected)
    return Discovery(entries=merged, costs=tuple(costs), sweeps=sweeps,
                     sources=_counts(merged), ms=(time.perf_counter() - started) * 1000,
                     failed=frozenset(cost.name for cost in costs if not cost.ok))


def target_exists(entry: AppEntry) -> bool:
    """Whether the thing this entry would launch is still there.

    A packaged application has no path to check (the package identity is the target) and a
    file-manager strategy names a known folder, not a file — both are trusted here and
    validated by the launch itself.
    """
    if entry.launch in {"appid", "folder"}:
        return True
    try:
        return Path(entry.target).exists()
    except (OSError, ValueError):
        return False


def stale_ids(entries: Sequence[AppEntry]) -> list[str]:
    """Entries whose target is gone: an uninstalled application, or one that moved."""
    return [entry.id for entry in entries if not target_exists(entry)]


def merge_pass(index: AppIndex, discovery: Discovery, *,
               validated: Iterable[str] = ()) -> AppIndex:
    """Fold one discovery pass into an index, keeping learned names, picks and progress.

    An entry the pass did not report is kept while its target still exists (the sweep may
    not have reached its directory yet, and a source that did not run reported nothing)
    and dropped when it does not — an uninstalled or moved application leaves the index
    instead of failing on every request.
    """
    previous = {entry.id: entry for entry in index.entries}
    entries: list[AppEntry] = []
    reported: set[str] = set()
    for entry in discovery.entries:
        reported.add(entry.id)
        old = previous.get(entry.id)
        if old is None:
            entries.append(entry)
            continue
        aliases = list(entry.aliases)
        known = {fold(alias) for alias in aliases} | {fold(entry.name)}
        # A name the user typed for this application is never thrown away by a rescan.
        for extra in (old.name, *old.aliases):
            if extra and fold(extra) not in known:
                known.add(fold(extra))
                aliases.append(extra)
        entries.append(replace(entry, aliases=tuple(aliases), picks=old.picks))
    checked = set(validated)
    for entry in index.entries:
        if entry.id in reported:
            continue
        # A source that was read *completely* in this pass and did not report the entry is
        # the only thing that may say it is gone (F-AL8, R2): measured, the AppID of a
        # program whose shell identifier Windows no longer publishes stayed in the index
        # forever, because a package identity has no file to become missing. A source that
        # failed says nothing, and the progressive sweep is not asked at all.
        reader = FULL_SOURCE_READER.get(entry.source)
        if reader is not None and reader in checked and reader not in discovery.failed:
            continue
        # Everything else is left as it is when its source was not read (checking the disk
        # for those cost ~0.5 ms each, measured 937 ms for one pass) or when its file is
        # still there.
        if entry.source not in checked or target_exists(entry):
            entries.append(entry)
    return AppIndex(entries=merge_entries(entries), built_ms=discovery.ms,
                    sources=_counts(entries), signatures=dict(index.signatures),
                    sweeps=discovery.sweeps, updated_ms=now_ms())


def refresh_index(index: AppIndex, *, configured: Sequence[str] = (), force: bool = False,
                  include_sweep: bool = True, budget_ms: float = SWEEP_BUDGET_MS) -> AppIndex:
    """Update the index incrementally: only the sources that changed are read again.

    This is the F-AL6 refresh. A warm start reads nothing: the stored signatures still
    match, so the pass costs milliseconds instead of the ~2.4 s the four structured
    sources take together. `force` (the first build, or an explicit rebuild) reads all of
    them, which is what the probe measures.
    """
    signatures = source_signatures(configured)
    age_ms = max(0.0, now_ms() - (index.updated_ms or 0.0))
    due = [name for name, _, _ in _sources(configured)
           if force or _due(name, index, signatures, age_ms)]
    discovery = discover(include_sweep=include_sweep, budget_ms=budget_ms,
                         configured=configured, previous=index, names=due)
    checked = set(due) | ({"sweep", SWEEP_SOURCE} if include_sweep else set())
    updated = merge_pass(index, discovery, validated=checked)
    stored = dict(index.signatures)
    for name in due:
        stored[name] = signatures.get(name) or f"ts:{int(now_ms())}"
    return replace(updated, signatures=stored)


def build_index(index: AppIndex, *, configured: Sequence[str] = (), include_sweep: bool = True,
                budget_ms: float = SWEEP_BUDGET_MS) -> AppIndex:
    """Read every source once (first build, or an explicit rebuild)."""
    return refresh_index(index, configured=configured, force=True,
                         include_sweep=include_sweep, budget_ms=budget_ms)

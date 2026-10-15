"""The application index: the data about installed applications, and the ranking.

The FASE 8 table (`fastpath.APPS`) named the six applications its author knew; this
module holds what a machine can fill in for itself, and the rules that turn a name a
user typed into one of those entries. Like `settings_pages.py` it is **pure**: no
Windows, no COM, no registry, no I/O beyond the JSON file it is told to read or write —
so the ranking and the de-duplication are tested without a desktop.

Two properties matter more than the rest:

* **Identity is explicit.** A Store application is identified by its Application User
  Model ID, a classic Win32 application by its executable path. De-duplication uses
  that identity, so "Discord" stays one entry even when the Start Menu shortcut, the
  Apps folder and the uninstall registry key all describe it.
* **A name that does not clearly match is not a match.** ``MINIMUM_MATCH`` is a floor,
  not a hint, and a second candidate within ``AMBIGUITY_MARGIN`` of the first makes the
  answer *ambiguous* instead of a guess. Guessing is how the wrong application gets
  opened.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from app.computer.settings_pages import fold

# Bumped to 2 for F-AL8/R1: an entry now carries its classification and its window
# strategy (`kind`, `processes`, `classes`, `frame_child`), and an index written before
# that has neither. Reading it would answer with entries that cannot be verified, so it
# is discarded and rebuilt — which is what the background discovery does anyway.
INDEX_VERSION = 2
INDEX_NAME = "apps.json"

# Sources the operating system itself maintains: the user is not asked to authorise
# what Windows already registered.
TRUSTED_SOURCES = ("appsfolder", "start_menu", "desktop", "registry", "app_paths", "path",
                   "package")
# A folder or drive the user pointed at: the path is theirs, the executables are
# unknown, so the first launch of one of them asks for consent.
USER_SOURCE = "user_folder"
# Ordered best-source-first; also the de-duplication preference between two entries
# describing the same application.
SOURCE_RANK = {name: index for index, name in enumerate(TRUSTED_SOURCES)}
SOURCE_RANK[USER_SOURCE] = len(TRUSTED_SOURCES)

# Executables that exist only to start another process. A shortcut to `Update.exe
# --processStart Discord.exe` must not make "update" a name for Discord, and one of these
# is not an application on its own: measured, `Discord\Update.exe`, `updater_launcher.exe`
# and `inno_updater.exe` were all indexed as applications whose *name* was a verb, and one
# of them answered to "discord" because of the folder it sat in.
LAUNCHER_STEMS = frozenset({
    "update", "updater", "launcher", "launch", "start", "starter", "setup", "install",
    "installer", "unins000", "uninstall", "uninstaller", "bootstrap", "loader", "gate",
    "crashpad_handler", "crashreportclient", "squirrel", "squirrelsetup",
    "notification_helper", "chrome_proxy", "helper", "maintenanceservice",
})
# Stems that are not identifying, even when the executable is a real application: they
# never become a name, and they never prove an identity.
GENERIC_STEMS = LAUNCHER_STEMS | frozenset({
    "run", "app", "application", "index", "main", "browser", "bin", "service", "server",
    "client", "current", "default", "python", "pythonw", "java", "node", "electron",
    # Measured (2026-10-08): the desktop held four shortcuts whose *target* is
    # `explorer.exe` (folder links), so every one of them acquired "explorer" as a name
    # and the file manager could no longer be resolved unambiguously. The shell is not an
    # application name, so it never becomes one.
    "explorer",
})

# Ranking calibration. Both numbers are floors that a measured case had to clear: an
# exact name (1.00) beats an exact alias (0.97), a typed name with one wrong letter
# lands around 0.83, and a name only *contained* in a longer one (0.75-0.80) is never
# enough on its own to open something.
MINIMUM_MATCH = 0.80
AMBIGUITY_MARGIN = 0.10
CANDIDATE_LIMIT = 5
# A name typed with one wrong letter is still the name. Measured ratios: a single
# transposition ("discrod") 0.857, a missing or doubled letter ("steemm") 0.727, an
# extra letter ("spotfy") 0.923. The scale maps the floor to the match floor and a
# perfect ratio to just below the exact forms, so a typo can decide an answer but can
# never outrank a name that was written correctly.
FUZZY_FLOOR = 0.70
FUZZY_OFFSET = 0.80
FUZZY_SCALE = (0.90 - 0.80) / (1.0 - 0.70)
FUZZY_CEILING = 0.90

LAUNCH_KINDS = ("exe", "lnk", "appid", "folder")

# What an entry *is*, so the ranking can prefer a program over a part of one. The values
# are a closed set on purpose: they are the classes a person would recognise, and every
# one of them is decided from signals discovery already reads (PE metadata, the publisher,
# a shortcut's arguments, the registry's own component flags) — never from a name.
KIND_MAIN = "main"                 # the program a person means to open
KIND_PORTABLE = "portable"         # a program that runs from its own folder
KIND_LAUNCHER = "launcher"         # starts another program (Update.exe, a stub)
KIND_AUXILIARY = "auxiliary"       # a helper of a program (a crash handler, a proxy)
KIND_CLI = "cli"                   # a command-line tool, no window of its own
KIND_INSTALLER = "installer"       # installs a program
KIND_UNINSTALLER = "uninstaller"    # removes a program
KIND_COMPONENT = "component"       # an internal file of a program
KIND_UNKNOWN = "unknown"           # nothing said what it is
APP_KINDS = (KIND_MAIN, KIND_PORTABLE, KIND_LAUNCHER, KIND_AUXILIARY, KIND_CLI, KIND_INSTALLER,
             KIND_UNINSTALLER, KIND_COMPONENT, KIND_UNKNOWN)
# Kinds that describe a program a person opens, as opposed to a part of one. The ranking
# prefers them and discovery never loses them: a legitimate portable program is found by a
# sweep and must stay findable.
OPENABLE_KINDS = frozenset({KIND_MAIN, KIND_PORTABLE})
# Executable stems whose role is unambiguous, so an entry carrying one is classified
# without reading the file: the same words `LAUNCHER_STEMS` already refuses as a name.
_ROLE_STEMS: dict[str, str] = {}
for _stem in ("unins000", "uninstall", "uninstaller", "unins", "unwise"):
    _ROLE_STEMS[_stem] = KIND_UNINSTALLER
for _stem in ("install", "installer", "setup", "installshield", "msiexec"):
    _ROLE_STEMS[_stem] = KIND_INSTALLER
for _stem in ("update", "updater", "launch", "launcher", "start", "starter", "bootstrap",
              "loader", "squirrel", "squirrelsetup", "notification_helper", "chrome_proxy",
              "crashpad_handler", "crashreportclient", "maintenanceservice"):
    _ROLE_STEMS[_stem] = KIND_LAUNCHER
for _stem in ("plugin", "plugins", "addin", "addins", "addon", "addons", "extension",
              "extensions", "component", "components", "broker", "renderer", "daemon",
              "telemetry", "watchdog", "handler", "provider", "previewhandler",
              "thumbnailprovider"):
    _ROLE_STEMS[_stem] = KIND_AUXILIARY


def classify_stem(image: str) -> str:
    """The role an executable *name* alone proves, or ``KIND_UNKNOWN``.

    Only the names whose meaning is not a matter of opinion: an updater does not become
    the program, and nothing here is guessed from a folder.
    """
    words = stem(image).casefold().replace("-", " ").replace("_", " ").split()
    for word in words:
        role = _ROLE_STEMS.get(word)
        if role is not None:
            return role
    return KIND_UNKNOWN


def normalize_path(value: str) -> str:
    """One spelling per file path, for identity and for de-duplication."""
    return os.path.normcase(os.path.normpath(str(value or "").strip()))


def words(text: str) -> tuple[str, ...]:
    """The folded words of a name: "7-Zip File Manager" -> ("7", "zip", "file", "manager")."""
    return tuple(fold(text).replace("-", " ").replace("_", " ").split())


def stem(value: str) -> str:
    """A file name without its extension ("Code.exe" -> "Code")."""
    return Path(str(value or "")).stem


def pretty_stem(value: str) -> str:
    """An executable stem as a display name: separators become spaces, case is kept."""
    text = stem(value).replace("_", " ").replace("-", " ").strip()
    return " ".join(text.split())


# Folder names that describe a layout, not an application: the directory an executable
# sits in only becomes a name when it says something ("Microsoft VS Code", "7-Zip").
STRUCTURAL_DIRS = frozenset({
    "bin", "bin64", "app", "apps", "application", "applications", "client", "current",
    "latest", "release", "release64", "dist", "build", "x64", "x86", "x32", "32", "64",
    "32bit", "64bit", "programs", "program files", "program files (x86)", "local", "roaming",
    "appdata", "desktop", "documents", "downloads", "system32", "syswow64", "windowsapps",
    "data", "core", "lib", "libs", "usr", "opt", "tools", "temp", "tmp", "version",
    "versions", "resources", "support", "framework", "runtime", "windows", "contents",
    "macos", "linux", "debug",
})


# A version an installer wrote into the display name: "Lightshot-5.5.0.7", "Windhawk
# v1.7.3", "AudioRelay version 0.27.5". Two entries of the same program then look like
# two different programs, and the user is asked to choose between them. The version is
# never deleted, only moved to the aliases, where it still matches if it is typed.
_VERSION_TAIL = re.compile(
    r"(?:[\s\-\u2013_(]+(?:version\s+)?v?\d[\d.]*(?:\s*[+\-]\s*\S+)?)+$", re.IGNORECASE)


def clean_name(name: str) -> str:
    """A display name without a trailing version, or the name unchanged."""
    text = " ".join(str(name or "").split())
    stripped = _VERSION_TAIL.sub("", text).strip(" -_(\u2013")
    return stripped if len(stripped) >= 3 else text


def install_alias(path: str, name: str) -> tuple[str, ...]:
    """The install folder as a name, when the folder itself is informative."""
    leaf = Path(str(path or "")).parent.name.strip()
    if not leaf or leaf.casefold() in STRUCTURAL_DIRS or leaf.isdigit():
        return ()
    if fold(leaf) == fold(name):
        return ()
    return (leaf,)


def guess_aliases(name: str, target: str, exe: str, extra: Iterable[str] = ()) -> tuple[str, ...]:
    """Every other spelling that should find this entry, longest first, no duplicates.

    Contains the executable stem only when it is an identifying word: a shortcut that
    launches `Update.exe` must not teach the index that Discord is called "update".
    """
    candidates: list[str] = [*extra]
    for path in (target, exe):
        stem_name = stem(path)
        # A launcher/updater/component name is never a way to call the application.
        if not stem_name or stem_name.casefold() in GENERIC_STEMS:
            continue
        candidates.extend((pretty_stem(path), stem_name))
    seen = {fold(name)}
    aliases: list[str] = []
    for candidate in sorted((item.strip() for item in candidates if item and item.strip()),
                            key=lambda item: (-len(item), item.casefold())):
        key = fold(candidate)
        if not key or key in seen:
            continue
        seen.add(key)
        aliases.append(candidate)
    return tuple(aliases)


def identity(launch: str, target: str, aumid: str) -> str:
    """The stable identity of an application, used to de-duplicate discovery."""
    if aumid:
        return f"aumid:{aumid.casefold()}"
    if launch in {"exe", "lnk"} and str(target).casefold().endswith(".exe"):
        return f"exe:{normalize_path(target)}"
    return f"{launch}:{normalize_path(target).casefold()}"


@dataclass(frozen=True)
class AppEntry:
    """One application: how it is named, how it starts, and what proves it started."""

    id: str
    name: str
    aliases: tuple[str, ...] = ()
    launch: str = "exe"          # exe | lnk | appid | folder
    target: str = ""             # executable path, shortcut path, or AppID
    args: tuple[str, ...] = ()   # kept from the shortcut, never invented
    workdir: str = ""
    exe: str = ""                # process image expected to appear
    aumid: str = ""              # packaged-app identity ("" for classic Win32)
    source: str = ""
    publisher: str = ""
    install_dir: str = ""
    description: str = ""
    category: str = ""
    kind: str = KIND_UNKNOWN     # what the entry is, from `APP_KINDS`
    trust: str = "trusted"
    seen_ms: float = 0.0
    picks: int = 0               # times the user chose this entry among candidates
    # What proves the application opened, when the executable alone is not enough. These
    # are strategies, not names: the file manager's windows are `CabinetWClass`, and a
    # Store application's window is an ApplicationFrameWindow hosting its own process.
    processes: tuple[str, ...] = ()   # images that own the top-level windows
    classes: tuple[str, ...] = ()     # window-class filter, when the image is too broad
    frame_child: str = ""             # process hosted by ApplicationFrameHost, if any

    @property
    def labels(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    @property
    def launch_target(self) -> str:
        """What `WindowsTools.launch` is given, prefix included."""
        return f"{self.launch}:{self.target}"

    def to_wire(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "aliases": list(self.aliases),
                "launch": self.launch, "target": self.target, "args": list(self.args),
                "workdir": self.workdir, "exe": self.exe, "aumid": self.aumid,
                "source": self.source, "publisher": self.publisher,
                "install_dir": self.install_dir, "description": self.description,
                "category": self.category, "kind": self.kind, "trust": self.trust,
                "seen_ms": self.seen_ms, "picks": self.picks,
                "processes": list(self.processes), "classes": list(self.classes),
                "frame_child": self.frame_child}

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> AppEntry:
        """Rebuild one entry, refusing anything that could not be launched safely."""
        launch = str(value.get("launch") or "")
        target = str(value.get("target") or "")
        if launch not in LAUNCH_KINDS or not target:
            raise ValueError("entrada de aplicativo sem destino")
        name = str(value.get("name") or "")
        if not name:
            raise ValueError("entrada de aplicativo sem nome")
        return cls(
            id=str(value.get("id") or identity(launch, target, str(value.get("aumid") or ""))),
            name=name,
            aliases=tuple(str(item) for item in value.get("aliases") or ()),
            launch=launch, target=target,
            args=tuple(str(item) for item in value.get("args") or ()),
            workdir=str(value.get("workdir") or ""),
            exe=str(value.get("exe") or ""),
            aumid=str(value.get("aumid") or ""),
            source=str(value.get("source") or ""),
            publisher=str(value.get("publisher") or value.get("Publisher") or ""),
            install_dir=str(value.get("install_dir") or value.get("InstallLocation") or ""),
            description=str(value.get("description") or ""),
            category=str(value.get("category") or ""),
            kind=str(value.get("kind") or KIND_UNKNOWN),
            trust=str(value.get("trust") or "trusted"),
            seen_ms=float(value.get("seen_ms") or 0.0),
            picks=int(value.get("picks") or 0),
            processes=tuple(str(item) for item in value.get("processes") or ()),
            classes=tuple(str(item) for item in value.get("classes") or ()),
            frame_child=str(value.get("frame_child") or ""),
        )


@dataclass(frozen=True)
class KnownApp:
    """Metadata Windows does not carry for one well-known application (F-AL8, R1).

    The match is an *identity*, never a name: an Application User Model ID prefix (the
    package Windows itself assigned) or an executable image. A name is what this record
    *adds*, for the cases where the shell reports the application in a language the user
    does not write — and a window strategy, for the cases where an executable alone does
    not identify what opened. Size is a name table; this is strategy data.
    """

    aliases: tuple[str, ...] = ()
    aumid_prefix: str = ""
    exe_stem: str = ""
    processes: tuple[str, ...] = ()
    classes: tuple[str, ...] = ()
    frame_child: str = ""
    launch: str = ""
    target: str = ""
    kind: str = ""


# The measured quirks of the applications the old fixed table carried, expressed as
# reusable strategy data instead of a name lookup: the identity is Windows', the names
# and the window behaviour are what Windows does not say. Nothing here decides *which*
# application a user named — the index does that; this only completes an entry.
WELL_KNOWN: tuple[KnownApp, ...] = (
    # Measured (2026-10-08): `shell:AppsFolder` reports the Store Calculator as
    # "Calculator" and the Store Notepad as "Notepad" on a machine whose owner writes
    # Portuguese. The package identity is stable, the display language is not.
    KnownApp(("Calculadora", "Maquina de calcular", "Calculadora do Windows"),
             aumid_prefix="microsoft.windowscalculator_",
             processes=("CalculatorApp.exe",), frame_child="CalculatorApp.exe",
             kind=KIND_MAIN),
    KnownApp(("Calculadora", "Maquina de calcular", "Calculadora do Windows"),
             exe_stem="calc.exe", processes=("CalculatorApp.exe",),
             frame_child="CalculatorApp.exe", kind=KIND_MAIN),
    KnownApp(("Bloco de Notas", "Bloco de anotacoes", "Editor de texto", "Notas"),
             aumid_prefix="microsoft.windowsnotepad_",
             processes=("Notepad.exe",), kind=KIND_MAIN),
    KnownApp(("Bloco de Notas", "Bloco de anotacoes", "Editor de texto", "Notas"),
             exe_stem="notepad.exe", processes=("Notepad.exe",), kind=KIND_MAIN),
    # The File Explorer is an AppID the shell lists (`Microsoft.Windows.Explorer`), and
    # its measured quirk is the launch itself: `explorer.exe` started as a child process
    # while the shell already runs exits with code 1 and opens no window, so the file
    # manager is opened by folder — and its windows are told apart by class.
    KnownApp(("Explorer", "Explorador de arquivos", "Explorador", "Gerenciador de arquivos",
              "Windows Explorer", "Explorador do Windows"),
             aumid_prefix="microsoft.windows.explorer",
             processes=("explorer.exe",), classes=("CabinetWClass",),
             launch="folder", target="Desktop", kind=KIND_MAIN),
    KnownApp(("Configuracoes", "Configuracao do Windows", "Settings"),
             aumid_prefix="windows.immersivecontrolpanel_",
             processes=("SystemSettings.exe",), kind=KIND_MAIN),
    KnownApp(("Edge", "Navegador", "Navegador Edge"), exe_stem="msedge.exe",
             processes=("msedge.exe",), kind=KIND_MAIN),
    KnownApp(("Paint",), aumid_prefix="microsoft.paint_", processes=("mspaint.exe",),
             kind=KIND_MAIN),
    KnownApp(("Paint",), exe_stem="mspaint.exe", processes=("mspaint.exe",), kind=KIND_MAIN),
)


def _known_for(entry: AppEntry) -> KnownApp | None:
    """The strategy record an entry's identity selects, or ``None``."""
    for known in WELL_KNOWN:
        if known.aumid_prefix and entry.aumid.casefold().startswith(known.aumid_prefix):
            return known
        if known.exe_stem and entry.exe and entry.exe.casefold() == known.exe_stem:
            return known
    return None


def apply_strategy(entry: AppEntry) -> AppEntry:
    """Complete one entry with the metadata Windows does not carry (F-AL8, R1).

    Pure and idempotent: one identity always receives the same names and the same window
    strategy, so this is safe on every load, every merge and every discovery pass. An
    entry whose identity matches nothing is returned unchanged, and a value discovery
    already found is never overwritten (the record only fills gaps).
    """
    known = _known_for(entry)
    if known is None:
        return entry
    aliases = list(entry.aliases)
    known_names = {fold(alias) for alias in aliases} | {fold(entry.name)}
    for alias in known.aliases:
        if alias and fold(alias) not in known_names:
            known_names.add(fold(alias))
            aliases.append(alias)
    return replace(
        entry,
        aliases=tuple(aliases),
        kind=known.kind or entry.kind,
        processes=known.processes or entry.processes,
        classes=known.classes or entry.classes,
        frame_child=known.frame_child or entry.frame_child,
        launch=known.launch or entry.launch,
        target=known.target or entry.target,
    )


@dataclass(frozen=True)
class Match:
    """One candidate entry for a query, with the score and why it scored that."""

    entry: AppEntry
    score: float
    reason: str

    def to_wire(self) -> dict[str, Any]:
        # Everything needed to start the candidate travels with it: two installs of the
        # same application have the same name, and the path is what tells them apart in a
        # choice — and the worker is handed a launchable description, never a name.
        return {"app_id": self.entry.id, "name": self.entry.name, "score": round(self.score, 4),
                "reason": self.reason, "source": self.entry.source, "launch": self.entry.launch,
                "target": self.entry.target, "args": list(self.entry.args),
                "workdir": self.entry.workdir, "exe": self.entry.exe, "aumid": self.entry.aumid,
                "install_dir": self.entry.install_dir, "kind": self.entry.kind,
                "processes": list(self.entry.processes), "classes": list(self.entry.classes),
                "frame_child": self.entry.frame_child,
                "publisher": self.entry.publisher, "trust": self.entry.trust}


@dataclass(frozen=True)
class _Prepared:
    """Lookup tables over the entries, built once per index instead of per query.

    Measured on this machine with 2 372 entries: scoring by brute force (one
    ``SequenceMatcher`` per label per entry) took **130-200 ms per name** — far from the
    milliseconds a resolution is supposed to cost. Every exact spelling is therefore a
    dictionary hit, word containment is an intersection of precomputed sets, and the
    approximate pass only runs when no solid match was found, over candidates whose
    first character and length are close to the query.
    """

    entries: tuple[AppEntry, ...]
    exact: Mapping[str, tuple[int, ...]]
    packed: Mapping[str, tuple[int, ...]]
    stems: Mapping[str, tuple[int, ...]]
    by_word: Mapping[str, frozenset[int]]
    by_initial: Mapping[str, frozenset[int]]
    by_first: Mapping[str, tuple[int, ...]]
    labels: tuple[tuple[str, ...], ...]
    words: tuple[tuple[frozenset[str], ...], ...]
    ordered: tuple[tuple[tuple[str, ...], ...], ...]


FUZZY_CANDIDATE_CAP = 300
FUZZY_LENGTH_BAND = 2


@lru_cache(maxsize=8)
def prepare(entries: tuple[AppEntry, ...]) -> _Prepared:
    """Index an immutable entry tuple once; the cache is keyed by its identity."""
    exact: dict[str, list[int]] = {}
    packed: dict[str, list[int]] = {}
    stems: dict[str, list[int]] = {}
    by_word: dict[str, set[int]] = {}
    by_initial: dict[str, set[int]] = {}
    by_first: dict[str, list[int]] = {}
    labels: list[tuple[str, ...]] = []
    word_sets: list[tuple[frozenset[str], ...]] = []
    sequences: list[tuple[tuple[str, ...], ...]] = []
    for position, entry in enumerate(entries):
        folded = sorted({fold(label) for label in entry.labels if label})
        labels.append(tuple(folded))
        per_label: list[frozenset[str]] = []
        per_ordered: list[tuple[str, ...]] = []
        for label in folded:
            spelled = label.replace("-", " ").replace("_", " ")
            exact.setdefault(label, []).append(position)
            packed.setdefault(label.replace(" ", "").replace("-", "").replace("_", ""),
                              []).append(position)
            by_first.setdefault(label[:1], []).append(position)
            words_here = frozenset(spelled.split())
            per_label.append(words_here)
            per_ordered.append(tuple(spelled.split()))
            for word in words_here:
                by_word.setdefault(word, set()).add(position)
                by_initial.setdefault(word[:1], set()).add(position)
        word_sets.append(tuple(per_label))
        sequences.append(tuple(per_ordered))
        for candidate in {stem(entry.exe), stem(entry.target)}:
            key = fold(candidate)
            if key and candidate.casefold() not in GENERIC_STEMS:
                stems.setdefault(key, []).append(position)
    return _Prepared(
        entries=entries,
        exact={key: tuple(value) for key, value in exact.items()},
        packed={key: tuple(value) for key, value in packed.items()},
        stems={key: tuple(value) for key, value in stems.items()},
        by_word={key: frozenset(value) for key, value in by_word.items()},
        by_initial={key: frozenset(value) for key, value in by_initial.items()},
        by_first={key: tuple(value) for key, value in by_first.items()},
        labels=tuple(labels),
        words=tuple(word_sets),
        ordered=tuple(sequences),
    )


def _offer(best: dict[int, Match], prepared: _Prepared, position: int, score: float,
           reason: str) -> None:
    """Keep the best explanation of one entry, not the last one seen."""
    current = best.get(position)
    if current is None or score > current.score:
        best[position] = Match(prepared.entries[position], score, reason)


def _prefixes_cover(wanted: Sequence[str], labels: Sequence[frozenset[str]]) -> bool:
    """True when every query word starts a different word of one label.

    This is the abbreviation a person actually types: "vs code" for "Visual Studio Code"
    and "7 zip" for "7-Zip File Manager". Distinct words are required, so "code code"
    cannot match a label that has the word once.
    """
    for label_words in labels:
        remaining = set(label_words)
        matched = True
        for word in wanted:
            hit = next((candidate for candidate in remaining if candidate.startswith(word)), None)
            if hit is None:
                matched = False
                break
            remaining.discard(hit)
        if matched:
            return True
    return False


def _acronym(query: str, spelled: Sequence[str]) -> bool:
    """Whether `query` is exactly the concatenation of a non-empty prefix of each word.

    "vscode" is visual+studio+code; "vs" is visual+studio. Both must consume the query
    completely, and a one-word name has no initials at all — otherwise "sets" would be
    the initials of "setspn" and would open a program nobody named.
    """
    if len(spelled) < 2 or not query:
        return False
    position = 0
    for word in spelled:
        take = 0
        while take < len(word) and position + take < len(query) \
                and word[take] == query[position + take]:
            take += 1
        if take == 0:
            return False
        position += take
    return position == len(query)


def query_fold(query: str) -> str:
    """The folded form of a name, with an extension removed: "Code.exe" is "code"."""
    text = " ".join(str(query or "").split())
    if text.casefold().endswith(".exe"):
        text = text[:-4]
    return fold(text)


def _names_query(prepared: _Prepared, query: str) -> dict[int, Match]:
    """Every entry the query names, by exact spelling, containment or approximation."""
    best: dict[int, Match] = {}
    packed_query = query.replace(" ", "").replace("-", "").replace("_", "")
    for position in prepared.exact.get(query, ()):
        exact = fold(prepared.entries[position].name) == query
        _offer(best, prepared, position, 1.0 if exact else 0.97, "exata" if exact else "alias")
    for position in prepared.packed.get(packed_query, ()):
        _offer(best, prepared, position, 0.95, "normalizada")
    for position in prepared.stems.get(query, ()):
        _offer(best, prepared, position, 0.92, "executável")
    wanted = query.replace("-", " ").replace("_", " ").split()
    shared: frozenset[int] | None = None
    for word in wanted:
        hits = prepared.by_word.get(word)
        if hits is None:
            shared = None
            break
        shared = hits if shared is None else (shared & hits)
    for position in shared or ():
        coverage = len(wanted) / max(1, len(prepared.labels[position][0].split()))
        _offer(best, prepared, position, min(0.79, 0.75 + 0.04 * coverage), "palavras contidas")
    # One word of a longer name is how people shorten it: "Edge" for Microsoft Edge,
    # "OBS" for OBS Studio. It scores above the floor, but below every exact form, so an
    # entry that carries the whole name always wins.
    for position in shared or ():
        for label_words in prepared.words[position]:
            if any(word in label_words for word in wanted):
                _offer(best, prepared, position, 0.82, "palavra do nome")
    if best and max(match.score for match in best.values()) >= 0.90:
        # A solid match exists; the weaker rules would only add noise (and cost).
        return best
    # Initials are how a long name is written short: "vsc" and "vscode" for Visual Studio
    # Code. Every word of the name must contribute at least one letter, in order, and the
    # query must be consumed *exactly* — so "vs" is Visual Studio and "vscode" is Visual
    # Studio Code, never each other. Without that, the shorter name would win every time
    # and the abbreviation would open the wrong program.
    for position in prepared.by_first.get(packed_query[:1], ()):
        if any(_acronym(packed_query, spelled) for spelled in prepared.ordered[position]):
            _offer(best, prepared, position, 0.86, "iniciais")
    # Words may be abbreviated too, but only for a query specific enough to mean
    # something: a bare "st" names nothing, and a four-letter word that merely starts
    # another one ("sets" -> setspn) must not open a program nobody asked for.
    if len(wanted) <= 4 and (len(wanted) > 1 or len(wanted[0]) >= 5):
        initials_set: frozenset[int] | None = None
        for word in wanted:
            hits = prepared.by_initial.get(word[:1])
            if hits is None:
                initials_set = None
                break
            initials_set = hits if initials_set is None else (initials_set & hits)
        for position in initials_set or ():
            if _prefixes_cover(wanted, prepared.words[position]):
                _offer(best, prepared, position, 0.84, "abreviada")
    if best and max(match.score for match in best.values()) >= 0.90:
        return best
    candidates = [position for position in prepared.by_first.get(query[:1], ())
                  if any(abs(len(label) - len(query)) <= FUZZY_LENGTH_BAND
                         for label in prepared.labels[position])]
    if len(candidates) > FUZZY_CANDIDATE_CAP:
        candidates.sort(key=lambda position: min(
            abs(len(label) - len(query)) for label in prepared.labels[position]))
        candidates = candidates[:FUZZY_CANDIDATE_CAP]
    for position in candidates:
        for label in prepared.labels[position]:
            if not label:
                continue
            # A name that merely *starts* with the query is an abbreviation, judged by
            # the rules above; as a typo it would look extremely similar ("sets" and
            # "setspn" score 0.83) and would open something nobody named.
            if label.startswith(query) or query.startswith(label):
                continue
            ratio = SequenceMatcher(None, query, label).ratio()
            if ratio < FUZZY_FLOOR:
                continue
            score = min(FUZZY_CEILING, FUZZY_OFFSET + (ratio - FUZZY_FLOOR) * FUZZY_SCALE)
            _offer(best, prepared, position, score, "aproximada")
    return best


def score_entry(query: str, entry: AppEntry) -> Match | None:
    """How well one query names one entry, or ``None`` when it does not name it at all.

    Kept as the single-entry form of the ranking, so a caller (or a test) can ask about
    one candidate without building an index.
    """
    prepared = prepare((entry,))
    for match in _names_query(prepared, query_fold(query)).values():
        if entry.picks and match.score <= 0.95:
            return replace(match, score=min(0.95, match.score + min(0.03, 0.01 * entry.picks)),
                           reason=f"{match.reason}+escolha")
        return match
    return None


def rank(query: str, entries: Sequence[AppEntry]) -> list[Match]:
    """Every entry the query names, best first."""
    text = query_fold(query)
    if not text:
        return []
    prepared = prepare(tuple(entries))
    matches = []
    for match in _names_query(prepared, text).values():
        if match.entry.picks and match.score <= 0.95:
            boost = min(0.03, 0.01 * match.entry.picks)
            match = replace(match, score=min(0.95, match.score + boost),
                            reason=f"{match.reason}+escolha")
        matches.append(match)
    return sorted(matches, key=lambda match: (-match.score, kind_rank(match.entry),
                                              _source_rank(match.entry),
                                              len(match.entry.name), match.entry.id))


# Words that describe a *request* rather than an application: they are what a vague
# sentence has instead of a name, and matching them would make every entry a candidate.
REQUEST_WORDS = frozenset({
    "programa", "programas", "aplicativo", "aplicativos", "app", "apps", "instalado",
    "instalada", "instalados", "tenho", "meu", "minha", "quero", "abre", "abra", "abrir",
    "abro", "de", "do", "da", "dos", "das", "e", "o", "a", "os", "as", "em", "no",
    "na", "nos", "nas", "para", "por", "com", "que", "um", "uma", "favor", "edicao",
})


def search(query: str, entries: Sequence[AppEntry], limit: int = 5) -> list[Match]:
    """Which applications a *description* points at, best first (F-AL5).

    ``rank`` answers "which application is this name"; this answers "which applications
    has this person described", which is the vague request the HostAgent handles. A word
    in the name or an alias counts fully; a word in the publisher, the description or the
    category counts partly, so "editor de vídeo" can find something whose *name* says
    nothing but whose publisher does.
    """
    wanted = {word for word in query_fold(query).replace("-", " ").split()
              if len(word) > 2 and word not in REQUEST_WORDS}
    if not wanted:
        return []
    found: list[Match] = []
    for entry in entries:
        named = {word for label in entry.labels for word in words(label)}
        described = {word for label in (entry.publisher, entry.description, entry.category)
                     for word in words(label)}
        strong = wanted & named
        weak = (wanted & described) - strong
        if not strong and not weak:
            continue
        score = min(0.99, len(strong) / len(wanted) + 0.4 * len(weak) / len(wanted))
        found.append(Match(entry, score, "palavras do nome" if strong else "descrição"))
    return sorted(found, key=lambda match: (-match.score, kind_rank(match.entry),
                                            _source_rank(match.entry),
                                            len(match.entry.name), match.entry.id))[:limit]


@dataclass(frozen=True)
class AppResolution:
    """What the index answered for a name: one application, several, or none."""

    kind: str                                   # single | ambiguous | none
    matches: tuple[Match, ...] = ()

    @property
    def best(self) -> Match | None:
        return self.matches[0] if self.matches else None

    def to_wire(self) -> dict[str, Any]:
        return {"kind": self.kind, "candidates": [match.to_wire() for match in self.matches]}


def image_key(entry: AppEntry) -> str:
    """The identifying executable image of an entry, or "" when it has none.

    A packaged application has no image of its own (its identity is the package), and an
    AppID is not a file name — reading `stem()` of it once produced the nonsense key
    "microsoft" for the Store Notepad.
    """
    if entry.exe:
        name = stem(entry.exe)
    elif entry.launch in {"exe", "lnk"}:
        name = stem(entry.target)
    else:
        name = ""
    if not name or name.casefold() in GENERIC_STEMS:
        return ""
    return fold(name)


def collapse_same_application(matches: Sequence[Match]) -> tuple[Match, ...]:
    r"""Drop a copy of an application that lives inside another copy of it.

    Measured: Discord was both the registry's `AppData\Local\Discord\Discord.exe` and
    the sweep's copy inside `...\Discord\app-1.0.9260\Discord.exe`; Discord Canary and
    Lightshot had the same shape. They are one application described twice, and the copy
    in the versioned folder is exactly the path that changes at the next update — so the
    outer description wins. Two *separate* installations (neither inside the other) stay
    two candidates, because only the user knows which one he meant.
    """
    kept: list[Match] = []
    for position, match in enumerate(matches):
        other = matches[:position]
        nested = any(_inside(match.entry.target, item.entry) for item in other)
        if not nested:
            kept.append(match)
    return tuple(_collapse_shell_twins(kept))


def _collapse_shell_twins(matches: Sequence[Match]) -> tuple[Match, ...]:
    """One application whose *name* is described twice: by the shell and by a file.

    Measured (2026-10-08): `shell:AppsFolder` lists `com.squirrel.Discord.Discord` beside
    the swept `Discord\\Discord.exe`, and both are called "Discord" — one program, two
    identities, and a question the user should never be asked. Only a shell identifier is
    collapsed this way; two *file* entries with one name stay a genuine question, because
    two installations of one program are two installations.
    """
    kept: list[Match] = []
    for position, match in enumerate(matches):
        twin = any(_shell_twin(match.entry, item.entry) for item in matches[:position])
        if not twin:
            kept.append(match)
    return tuple(kept)


def _shell_twin(one: AppEntry, other: AppEntry) -> bool:
    """One entry is the shell's identifier and the other a file, under one name."""
    if one.launch != "appid" and other.launch != "appid":
        return False
    if one.launch == other.launch:
        return False
    return fold(one.name) == fold(other.name) and fold(one.name) != ""


def _inside(target: str, container: AppEntry) -> bool:
    """Whether `target` is a file inside `container`'s install folder."""
    folder = container.install_dir or (str(container.target).rsplit(os.sep, 1)[0]
                                       if os.sep in str(container.target) else "")
    if not folder or not target:
        return False
    try:
        return Path(normalize_path(folder)) in Path(normalize_path(target)).parents
    except (OSError, ValueError):
        return False


def _source_rank(entry: AppEntry) -> int:
    return SOURCE_RANK.get(entry.source, len(SOURCE_RANK))


# How much an entry's role deserves to be the answer, when two entries tie on the name.
# A program the user means beats a part of one, a launcher or an installer — but only as a
# tie-breaker: a lower score is never reordered, so an application is never lost because
# discovery could not say what it was (R2).
KIND_ORDER = {KIND_MAIN: 0, KIND_PORTABLE: 0, KIND_UNKNOWN: 1, KIND_AUXILIARY: 2, KIND_CLI: 3,
              KIND_LAUNCHER: 4, KIND_COMPONENT: 5, KIND_INSTALLER: 6, KIND_UNINSTALLER: 7}


def kind_rank(entry: AppEntry) -> int:
    """The tie-breaking order of an entry's role: lower is preferred (R2)."""
    return KIND_ORDER.get(entry.kind, 1)


def drop_variants(matches: Sequence[Match], query: str = "") -> tuple[Match, ...]:
    """Remove an entry the query did not name: a longer name, or a component file.

    "Microsoft Edge WebView2 Runtime" is a longer name that starts with "Microsoft
    Edge", and "Discord PTB" starts with "Discord": when the shorter name scored at
    least as well, the query named the shorter one. The same reasoning applies to a file
    that lives *inside* another candidate's install folder — measured: "PowerToys"
    matched `RestartAgent.exe` and `FancyZonesCLI.exe` through the folder they sit in,
    and "Antigravity" matched its own uninstaller. Those are parts of an application,
    not competing applications.

    The third form is the same idea across two folders: an entry that matched *only*
    because it carries a better candidate's name as an alias. Measured on this machine:
    "steam" matched `Common Files\\Steam\\drivers.exe` and `...\\steamservice.exe`, whose
    install folder lent them the name "Steam" — they are Steam's components, not Steam.
    """
    kept: list[Match] = []
    for position, match in enumerate(matches):
        name = fold(match.entry.name)
        # Only an entry that already ranked better may explain this one away, so two
        # equal candidates in one folder can never eliminate each other.
        better = matches[:position]
        extended = any(other.score >= match.score - 1e-9
                       and name.startswith(fold(other.entry.name) + " ")
                       for other in better)
        inside = any(other.score >= match.score - 1e-9
                     and other.entry.install_dir and match.entry.install_dir
                     and fold(other.entry.install_dir) == fold(match.entry.install_dir)
                     for other in better)
        borrowed = any(other.score > match.score
                       and any(fold(alias) == fold(other.entry.name)
                               for alias in match.entry.aliases)
                       for other in better)
        # The same borrowing, one step weaker: the query word is in a better entry's *name*
        # while this entry only carries it somewhere else (an alias from its folder).
        # Measured: "obs" matched `inject-helper32.exe` through its `ow-obs` alias while
        # OBS Studio was right there. A candidate that carries the word in its own name is
        # never explained away this way, so two "\u2026 Studio" applications stay a question.
        wanted = set(query_fold(query).replace("-", " ").split()) if query else set()
        borrowed_word = bool(wanted) and any(
            other.score >= match.score - 1e-9
            and match.reason in {"palavra do nome", "palavras contidas"}
            and not (wanted & set(words(match.entry.name)))
            and bool(wanted & set(words(other.entry.name)))
            for other in better)
        if not (extended or inside or borrowed or borrowed_word):
            kept.append(match)
    return tuple(kept)


def resolve_name(name: str, entries: Sequence[AppEntry]) -> AppResolution:
    """The one entry a name means, the candidates to choose from, or nothing.

    Ambiguity is a result, not a failure: two plausible applications within
    ``AMBIGUITY_MARGIN`` of each other are handed to the user instead of being guessed.
    Descriptions of the *same* application are collapsed first, so the question is only
    ever asked between genuinely different programs.
    """
    plausible = drop_variants(collapse_same_application(
        [match for match in rank(name, entries) if match.score >= MINIMUM_MATCH]), name)
    if not plausible:
        return AppResolution("none")
    best = plausible[0]
    # A choice the user already made for this name decides it: that is what memorising
    # the answer is for, and it is the only thing that may break a real tie.
    remembered = next((match for match in plausible
                       if match.entry.picks and best.score - match.score < AMBIGUITY_MARGIN), None)
    if remembered is not None:
        return AppResolution("single", (remembered, *[m for m in plausible if m is not remembered][:CANDIDATE_LIMIT]))
    if len(plausible) == 1 or best.score - plausible[1].score >= AMBIGUITY_MARGIN:
        # A single candidate is still handed on, so the caller can decide whether the
        # confidence is enough to act without asking.
        return AppResolution("single", (best, *plausible[1:CANDIDATE_LIMIT]))
    if best.reason == "exata" and all(other.reason != "exata" for other in plausible[1:]):
        # The user wrote an application's exact name. An alias of a *different*
        # application is not a reason to ask which one he meant; two entries with that
        # very name still are.
        return AppResolution("single", (best, *plausible[1:CANDIDATE_LIMIT]))
    return AppResolution("ambiguous", tuple(plausible[:CANDIDATE_LIMIT]))


def _preference(entry: AppEntry) -> tuple[int, int, int, float, int]:
    """How much this entry deserves to be the surviving spelling of an application.

    A *lower* source rank is better (the Apps folder describes what Windows starts, a
    swept folder is the last resort), so it enters negated: the comparison is "greater
    is better" like the rest of the tuple.
    """
    return (-_source_rank(entry), -kind_rank(entry), 1 if entry.args else 0, entry.seen_ms,
            entry.picks)


def merge_entries(entries: Sequence[AppEntry]) -> tuple[AppEntry, ...]:
    """De-duplicate by identity, keeping every name that was learned along the way."""
    merged: dict[str, AppEntry] = {}
    order: list[str] = []
    for entry in entries:
        current = merged.get(entry.id)
        if current is None:
            merged[entry.id] = entry
            order.append(entry.id)
            continue
        winner, loser = (entry, current) if _preference(entry) > _preference(current) else (current, entry)
        aliases = list(winner.aliases)
        known = {fold(alias) for alias in aliases} | {fold(winner.name)}
        for alias in (*loser.aliases, loser.name):
            if alias and fold(alias) not in known:
                known.add(fold(alias))
                aliases.append(alias)
        merged[entry.id] = replace(
            winner, aliases=tuple(aliases), picks=max(entry.picks, current.picks),
            seen_ms=max(entry.seen_ms, current.seen_ms),
            install_dir=winner.install_dir or loser.install_dir,
            publisher=winner.publisher or loser.publisher,
            aumid=winner.aumid or loser.aumid,
            exe=winner.exe or loser.exe,
            description=winner.description or loser.description,
            category=winner.category or loser.category,
            kind=winner.kind if winner.kind != KIND_UNKNOWN else loser.kind,
            processes=winner.processes or loser.processes,
            classes=winner.classes or loser.classes,
            frame_child=winner.frame_child or loser.frame_child,
            args=winner.args or loser.args,
            workdir=winner.workdir or loser.workdir)
    return tuple(merged[identity_] for identity_ in order)


def with_learned(query: str, entry_id: str, entries: Sequence[AppEntry]) -> tuple[AppEntry, ...]:
    """Record that a user chose an entry for a name, so the same name never asks again."""
    learned = " ".join(str(query).split())
    wanted = fold(learned)
    if not wanted:
        return tuple(entries)
    result: list[AppEntry] = []
    for entry in entries:
        if entry.id != entry_id:
            result.append(entry)
            continue
        aliases = list(entry.aliases)
        if wanted not in {fold(alias) for alias in (*aliases, entry.name)}:
            aliases.append(learned)
        result.append(replace(entry, aliases=tuple(aliases), picks=entry.picks + 1))
    return tuple(result)


def drop_entries(entries: Sequence[AppEntry], stale: Iterable[str]) -> tuple[AppEntry, ...]:
    """Remove entries whose target is gone, so a moved application is rediscovered."""
    gone = set(stale)
    return tuple(entry for entry in entries if entry.id not in gone)


@dataclass(frozen=True)
class AppIndex:
    """The persistent index: entries, where they came from, and how fresh each source is."""

    entries: tuple[AppEntry, ...] = ()
    built_ms: float = 0.0
    sources: dict[str, int] = field(default_factory=dict)
    signatures: dict[str, str] = field(default_factory=dict)
    sweeps: dict[str, Any] = field(default_factory=dict)
    updated_ms: float = 0.0

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[AppEntry]:
        return iter(self.entries)

    def resolve(self, name: str) -> AppResolution:
        return resolve_name(name, self.entries)

    def by_id(self, entry_id: str) -> AppEntry | None:
        return next((entry for entry in self.entries if entry.id == entry_id), None)

    def by_exe(self, exe: str) -> AppEntry | None:
        """The entry that runs this executable image, or ``None``.

        Used to answer "how is that application's window identified" for a caller that only
        knows the executable name — the HostAgent's `launch_application` does. The window
        strategy discovery measured (`processes`, `classes`, `frame_child`) travels with the
        entry, so a packaged application hosted in a frame and an application whose interface
        lives in a helper process are both recognised without a per-application table here.
        """
        wanted = str(exe or "").casefold()
        if not wanted:
            return None
        return next((entry for entry in self.entries if entry.exe.casefold() == wanted), None)

    def with_entries(self, entries: Sequence[AppEntry]) -> AppIndex:
        return replace(self, entries=merge_entries(entries))

    def learned(self, query: str, entry_id: str) -> AppIndex:
        return replace(self, entries=with_learned(query, entry_id, self.entries))

    def without(self, stale: Iterable[str]) -> AppIndex:
        return replace(self, entries=drop_entries(self.entries, stale))

    def trusted(self, entry_id: str) -> AppIndex:
        """Remember that the user allowed launching this entry (F-AL6).

        A swept folder or drive holds executables Windows never registered: the first
        launch of one of them is the user's decision, and this is how that decision
        survives.
        """
        return replace(self, entries=tuple(
            replace(entry, trust="trusted") if entry.id == entry_id else entry
            for entry in self.entries))

    def forget(self, entry_id: str) -> AppIndex:
        """Drop one entry whose target is gone, so the sweep can find it again."""
        return self.without([entry_id])

    def find(self, query: str, limit: int = 5) -> list[Match]:
        """The applications a description points at, best first (F-AL5)."""
        answered = list(rank(query, self.entries)[:limit])
        known = {match.entry.id for match in answered}
        answered.extend(match for match in search(query, self.entries, limit)
                        if match.entry.id not in known)
        return answered[:limit]

    def to_wire(self) -> dict[str, Any]:
        return {"version": INDEX_VERSION, "updated_ms": self.updated_ms,
                "built_ms": self.built_ms, "sources": dict(self.sources),
                "signatures": dict(self.signatures), "sweeps": dict(self.sweeps),
                "apps": [entry.to_wire() for entry in self.entries]}

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> AppIndex:
        """Read an index, keeping the entries that survive validation.

        A corrupt or older file must never break resolution: bad entries are dropped and
        the caller rebuilds what is missing.
        """
        if int(value.get("version") or 0) != INDEX_VERSION:
            return cls()
        entries: list[AppEntry] = []
        for item in value.get("apps") or ():
            if not isinstance(item, Mapping):
                continue
            try:
                entries.append(AppEntry.from_wire(item))
            except (ValueError, TypeError):
                continue
        return cls(entries=merge_entries(entries),
                   built_ms=float(value.get("built_ms") or 0.0),
                   sources={str(k): int(v) for k, v in (value.get("sources") or {}).items()},
                   signatures={str(k): str(v) for k, v in (value.get("signatures") or {}).items()},
                   sweeps=dict(value.get("sweeps") or {}),
                   updated_ms=float(value.get("updated_ms") or 0.0))

    def save(self, path: Path) -> None:
        """Write atomically, exactly like the brain selection does."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.to_wire(), ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path) -> AppIndex:
        """Read the index, or return an empty one instead of failing the task."""
        if not path.exists():
            return cls()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(value, Mapping):
            return cls()
        return cls.from_wire(value)


def default_index_path(root: Path) -> Path:
    return Path(root) / ".local" / INDEX_NAME


def now_ms() -> float:
    return time.time() * 1000

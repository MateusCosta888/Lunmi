"""Deterministic fast path: the desktop commands whose result the tools can prove (FASE 8).

Measured on 2026-10-07 (``logs/generic-live-fase7-before.jsonl``, Qoder/Qwen3.8-Flash,
"Feche a Calculadora."): **8 981 ms** of wall time, of which 4 304 ms was the UFO worker
boot and 4 366 ms was ONE HostAgent round over a 14 786-character prompt. The action
itself — closing a window — was measured at **82 ms**. The task was not slow because the
desktop is slow; it was slow because a model was asked to decide something a table
already knows.

This module is that table. It is pure, vendor-free and side-effect free: it maps an
instruction to a plan of deterministic tool calls (and what each one proves), or it
declines. Declining is a first-class answer — anything the grammar cannot resolve with
certainty falls through to the HostAgent exactly as before, so the fast path can only
ever remove work, never invent it.

The safety property is *full consumption*: every token of the instruction must be
accounted for by the shape that matched. "Abra as Configurações." resolves; "Abra as
Configurações e mude o papel de parede" does not, because ``mude`` is not in the
vocabulary the shape is allowed to explain. A shape that cannot explain the whole
sentence is a shape that has not understood it.
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

from app.computer.appindex import AppIndex, AppResolution
from app.computer.settings_pages import SECTION_WORDS, SettingsPageError, resolve_page

# A command that names several things, or a question, is not a deterministic command:
# it is a request to plan, and planning is what the HostAgent is for.
REFUSED_WORDS = frozenset({"todos", "todas", "ambos", "ambas", "exceto", "menos", "senao",
                           "cada", "algum", "alguma", "qualquer"})
_SENTENCES = re.compile(r"[.;!?\n]+")
# Words that only connect a verb to its subject: never part of a name.
_CONNECTORS = frozenset({"a", "o", "as", "os", "ao", "aos", "para", "pro", "pra", "em", "na",
                         "no", "nas", "nos", "de", "do", "da", "dos", "das", "meu", "minha",
                         "janela", "janelas", "app", "aplicativo", "programa"})
# A folder name must not be built out of the words that describe the instruction.
_NAME_STOP_WORDS = frozenset({"a", "o", "as", "os", "um", "uma", "de", "do", "da", "dos", "das",
                              "na", "no", "em", "para", "com", "e", "que", "por", "favor", "pasta",
                              "folder", "area", "trabalho", "desktop", "meu", "minha"})


@dataclass(frozen=True)
class Token:
    """One word of the instruction, with both spellings that matter."""

    folded: str   # accent- and case-insensitive form the shapes match against
    text: str     # the original spelling, so a name keeps its capital letters


def tokenize(text: str) -> list[Token]:
    """Split an instruction into tokens that carry their own folded form.

    Character by character, so "Área" folds to ``area`` and "UFO-Test-2" stays one
    token with its original spelling: a plan has to launch the folder the user named,
    not a fold of it.
    """
    tokens: list[Token] = []
    folded: list[str] = []
    original: list[str] = []
    for char in str(text):
        decomposed = unicodedata.normalize("NFKD", char)
        plain = "".join(item for item in decomposed if not unicodedata.combining(item))
        if plain and all(item.isalnum() or item in "-_" for item in plain):
            folded.append(plain)
            original.append(char)
        else:
            if folded:
                tokens.append(Token("".join(folded).casefold(), "".join(original)))
                folded, original = [], []
    if folded:
        tokens.append(Token("".join(folded).casefold(), "".join(original)))
    return tokens


@dataclass(frozen=True)
class AppTarget:
    """One application the fast path knows how to name, launch and find."""

    name: str
    exe: str                                    # what launch_application receives
    aliases: tuple[str, ...] = ()
    processes: tuple[str, ...] = ()             # images that own the top-level windows
    classes: tuple[str, ...] = ()               # extra window-class filter (Explorer)
    frame_child: str = ""                       # Store app hosted in ApplicationFrameHost


# Measured (2026-10-07): the Calculator's window is an ApplicationFrameWindow owned by
# applicationframehost.exe, and its hosted child process is CalculatorApp.exe — a
# calculatorapp.exe window does not exist. Closing that frame closed the Calculator in
# 82 ms, so the frame is the target, not a process to kill.
APPS: tuple[AppTarget, ...] = (
    AppTarget("Calculadora", "calc.exe",
              ("calculadora", "calculator", "calc", "maquina de calcular", "calculadora do windows"),
              ("calculatorapp.exe",), (), "calculatorapp.exe"),
    AppTarget("Bloco de Notas", "notepad.exe",
              ("bloco de notas", "bloco de anotacoes", "notepad", "editor de texto", "notas"),
              ("notepad.exe",)),
    # Measured (2026-10-07): `explorer.exe` started as a child process while the shell
    # is already running exits with code 1 and opens no window, so a plan that has to
    # start the file manager names a folder instead — which `launch` verifies by path.
    AppTarget("Explorer", "Desktop",
              ("explorer", "explorador de arquivos", "explorador", "gerenciador de arquivos",
               "windows explorer", "explorador do windows"),
              ("explorer.exe",), ("CabinetWClass",)),
    AppTarget("Configurações", "ms-settings:",
              ("configuracoes", "settings", "configuracao do windows"), ("systemsettings.exe",)),
    AppTarget("Paint", "mspaint.exe", ("paint",), ("mspaint.exe",)),
    AppTarget("Microsoft Edge", "msedge.exe",
              ("edge", "microsoft edge", "navegador", "navegador edge"), ("msedge.exe",)),
)

OPEN_VERBS = frozenset({"abra", "abre", "abrir", "abro", "abram", "open", "inicia", "inicie",
                        "iniciar", "executa", "execute", "executar", "roda", "rode", "rodar",
                        "liga", "ligue", "ligar", "levanta", "levante", "mostra", "mostre",
                        "mostrar", "start", "launch"})
CLOSE_VERBS = frozenset({"fecha", "feche", "fechar", "fecho", "encerra", "encerre", "encerrar",
                         "finaliza", "finalize", "finalizar", "sai", "saia", "sair", "quit",
                         "close", "exit"})
MINIMIZE_VERBS = frozenset({"minimiza", "minimize", "minimizar", "reduz", "reduza", "reduzir",
                            "minimize", "minimise"})
MAXIMIZE_VERBS = frozenset({"maximiza", "maximize", "maximizar", "maximize", "maximise"})
RESTORE_VERBS = frozenset({"restaura", "restaure", "restaurar", "desminimiza", "desminimize",
                           "desminimizar", "restore"})
# FASE 5.2: "foque no <app>" is the generic ensure-for-window verb: with the application
# open, the runtime proves the foreground; closed, it launches the same target the
# opening shape would and then focuses it, exactly as the agent does for minimize/restore.
FOCUS_VERBS = frozenset({"foca", "foque", "focar", "foco", "focaliza", "focalize", "focalizar",
                         "focus", "ativa", "ative", "ativar"})
MKDIR_VERBS = frozenset({"crie", "cria", "criar", "faca", "faz", "create", "novo", "nova"})
RENAME_VERBS = frozenset({"renomeie", "renomeia", "renomear", "rename", "mude", "muda", "mudar"})
_FOLDER_WORDS = frozenset({"pasta", "folder", "diretorio", "directory"})
_MKDIR_QUALIFIERS = frozenset({"chamada", "chamado", "nomeada", "nomeado", "com", "o", "nome", "de",
                               "called", "named"})
_RENAME_QUALIFIERS = frozenset({"o", "a", "os", "as", "nome", "de", "do", "da"})
_RENAME_SEPARATORS = frozenset({"para", "a", "em", "to"})
_SETTINGS_WORDS = frozenset({"configuracao", "configuracoes", "config", "settings"})
_SETTINGS_ALLOWED = frozenset({"windows", "pagina", "aba", "opcao", "opcoes", "configuracoes",
                               "configuracao", "config", "settings", "painel"})
# Destinations a folder can be created in, and the tool alias that resolves them.
_DESTINATIONS: tuple[tuple[str, str], ...] = (
    ("na area de trabalho", "Desktop"), ("no desktop", "Desktop"),
    ("no ambiente de trabalho", "Desktop"), ("em desktop", "Desktop"),
    ("na mesa", "Desktop"),
    ("em documentos", "Documents"), ("na pasta documentos", "Documents"),
    ("em downloads", "Downloads"), ("na pasta downloads", "Downloads"),
)
_KNOWN_FOLDERS: tuple[tuple[str, str], ...] = (
    ("area de trabalho", "Desktop"), ("ambiente de trabalho", "Desktop"), ("desktop", "Desktop"),
    ("documentos", "Documents"), ("documents", "Documents"), ("meus documentos", "Documents"),
    ("downloads", "Downloads"),
)
# The Settings home, for a request that names no page at all.
SETTINGS_HOME = "ms-settings:"
_MOVE_WORDS = frozenset({"e", "depois", "va", "vai", "ir", "navegue", "navegar", "acesse",
                         "acessar", "entre", "entrar", "abra", "abre", "abrir", "mostre",
                         "mostrar", "abro", "go", "to", "open"})


@dataclass(frozen=True)
class FastPlan:
    """One deterministic task: what to run, and how each step proves itself."""

    kind: str                      # launch | window | settings | folder | filesystem | ambiguous
    label: str                     # what the plan is about, for the record and the UI
    operation: str = ""            # window operation, or the filesystem operation
    target: str = ""               # what launch() receives
    page: str = ""                 # Settings page name for open_settings_page
    paths: tuple[str, ...] = ()    # candidate paths, first existing one wins
    destination: str = ""
    processes: tuple[str, ...] = ()
    classes: tuple[str, ...] = ()
    frame_child: str = ""
    # An application the index answered for: how to start it, and what proves it started.
    args: tuple[str, ...] = ()     # kept from the shortcut, never invented
    workdir: str = ""
    expected: str = ""             # process image expected to appear
    aumid: str = ""                # packaged identity, for the AppID activation
    app_id: str = ""               # entry identity, for the record and for learning
    app_source: str = ""           # which discovery source the entry came from
    trust: str = ""                # trusted | user_folder (the first launch asks)
    candidates: tuple[dict[str, Any], ...] = ()   # the choices, when ambiguous
    resolve_ms: float = 0.0        # how long the index took (a subset of the task's time)
    index_hit: bool = False        # answered by the index, not by the curated table
    query: str = ""                # literal search text, never keyboard syntax

    def to_wire(self) -> dict[str, Any]:
        """The plain-data form the worker executes; the worker knows no app aliases."""
        return {"kind": self.kind, "label": self.label, "operation": self.operation,
                "target": self.target, "page": self.page, "paths": list(self.paths),
                "destination": self.destination, "processes": list(self.processes),
                "classes": list(self.classes), "frame_child": self.frame_child,
                "args": list(self.args), "workdir": self.workdir, "expected": self.expected,
                "aumid": self.aumid, "app_id": self.app_id, "app_source": self.app_source,
                "trust": self.trust, "candidates": [dict(item) for item in self.candidates],
                "query": self.query}

    def with_choice(self, position: int) -> FastPlan | None:
        """The plan for the candidate a user picked out of an ambiguous answer."""
        if position < 0 or position >= len(self.candidates):
            return None
        return _plan_for(dict(self.candidates[position]), label=self.label,
                         resolve_ms=self.resolve_ms)


def _aliases(target: AppTarget) -> list[list[str]]:
    """Every way to name one application, longest alias first.

    Longest first matters: "bloco de notas" must win over "notas", and "calculadora do
    windows" over "calculadora", or the shape would leave tokens unexplained.
    """
    names = [target.name, *target.aliases]
    return sorted((name.casefold().split() for name in names), key=len, reverse=True)


def _skip(tokens: list[Token], index: int, words: frozenset[str]) -> int:
    while index < len(tokens) and tokens[index].folded in words:
        index += 1
    return index


def _named_app(tokens: list[Token], index: int) -> AppTarget | None:
    """The application a shape names from ``index`` on, consuming *every* remaining token."""
    index = _skip(tokens, index, _CONNECTORS)
    for target in APPS:
        for alias in _aliases(target):
            if index + len(alias) == len(tokens) and [token.folded for token in tokens[index:]] == alias:
                return target
    return None


def _starts_with(tokens: list[Token], verbs: frozenset[str]) -> bool:
    return bool(tokens) and tokens[0].folded in verbs


def _page_words(page: Any) -> set[str]:
    """Every word the catalogue itself uses to name one page."""
    words: set[str] = set()
    for spelling in (page.name, *page.aliases):
        words.update(token for token in str(spelling).casefold().replace("-", " ").split())
        words.add(str(spelling).casefold())
    return words


def _settings_shape(tokens: list[Token], instruction: str,
                    apps: AppIndex | None = None) -> FastPlan | None:
    """A Settings request, accepted only when the catalogue can explain every word."""
    if not _starts_with(tokens, OPEN_VERBS):
        return None
    if not any(token.folded in _SETTINGS_WORDS for token in tokens):
        return None
    try:
        page = resolve_page(instruction)
    except SettingsPageError:
        # The catalogue refused the page (unknown, or ambiguous between two): a
        # deterministic tool that cannot verify the page must not claim it.
        page = None
    if page is None:
        # Bare "Abra as Configurações.": accepted only if nothing else is said.
        allowed = set(SECTION_WORDS) | _SETTINGS_ALLOWED | {tokens[0].folded}
        if any(token.folded not in allowed for token in tokens):
            return None
        return FastPlan("settings", "Configurações", page=SETTINGS_HOME)
    allowed = set(SECTION_WORDS) | _SETTINGS_ALLOWED | {tokens[0].folded} | _page_words(page)
    if any(token.folded not in allowed for token in tokens):
        return None
    return FastPlan("settings", page.name, page=page.name)


def _window_subject(tokens: list[Token], apps: AppIndex | None,
                    start: int) -> FastPlan | None:
    """The window plan for the application a window instruction names (F-AL8, R1).

    The index answers first: an entry carries the window strategy discovery found
    (`processes`, `classes`, `frame_child`), which is exactly what closing or minimizing
    an application needs. The bootstrap table answers only when there is no index at all.
    """
    if apps is not None and len(apps):
        answer, _ = _index_answer(tokens, apps, start)
        if answer is None or answer.best is None or answer.kind != "single":
            return None
        entry = answer.best.entry
        opening = (entry.launch_target if entry.launch in {"exe", "lnk", "appid"}
                   else entry.target)
        return FastPlan("window", entry.name, target=opening, app_id=entry.id,
                        app_source=entry.source, aumid=entry.aumid,
                        processes=entry.processes or ((entry.exe,) if entry.exe else ()),
                        classes=entry.classes, frame_child=entry.frame_child)
    curated = _named_app(tokens, start)
    if curated is None:
        return None
    return FastPlan("window", curated.name, target=curated.exe,
                    processes=curated.processes,
                    classes=curated.classes, frame_child=curated.frame_child)


def _window_shape(tokens: list[Token], instruction: str,
                  apps: AppIndex | None = None) -> FastPlan | None:
    for operation, verbs in (("close", CLOSE_VERBS), ("minimize", MINIMIZE_VERBS),
                             ("restore", RESTORE_VERBS), ("maximize", MAXIMIZE_VERBS),
                             ("focus", FOCUS_VERBS)):
        if not _starts_with(tokens, verbs):
            continue
        # `target` travels with every window plan, but only minimize/restore/focus may use
        # it: with nothing open, opening the application to minimize or focus it is what
        # the agent itself does, while opening it to close it is absurd (the runner decides).
        plan = _window_subject(tokens, apps, 1)
        if plan is None:
            continue
        return FastPlan("window", plan.label, operation=operation, target=plan.target,
                        processes=plan.processes, classes=plan.classes,
                        frame_child=plan.frame_child, aumid=plan.aumid,
                        app_id=plan.app_id, app_source=plan.app_source)
    return None


def _launch_shape(tokens: list[Token], instruction: str, apps: AppIndex | None = None) -> FastPlan | None:
    if not _starts_with(tokens, OPEN_VERBS):
        return None
    if apps is not None and len(apps):
        # The index already had its say inside `_app_shape`; the bootstrap table must not
        # answer behind its back, or the fixed table would still be the source.
        return None
    target = _named_app(tokens, 1)
    if target is None:
        return None
    return FastPlan("launch", target.name, target=target.exe, processes=target.processes,
                    classes=target.classes, frame_child=target.frame_child)


def _plan_for(candidate: dict[str, Any], *, label: str, resolve_ms: float = 0.0) -> FastPlan:
    """The launch plan for one index entry.

    The launch kind travels in the target itself (`exe:`, `lnk:`, `aumid:`) so the worker
    needs no application knowledge at all: it is handed what to start and what to expect.
    """
    launch = str(candidate.get("launch") or "exe")
    target = str(candidate.get("target") or "")
    exe = str(candidate.get("exe") or "")
    return FastPlan(
        "launch", str(candidate.get("name") or label),
        target=f"{launch}:{target}" if launch in {"exe", "lnk", "appid"} else target,
        args=tuple(str(item) for item in candidate.get("args") or ()),
        workdir=str(candidate.get("workdir") or ""),
        expected=exe,
        aumid=str(candidate.get("aumid") or ""),
        app_id=str(candidate.get("app_id") or ""),
        app_source=str(candidate.get("source") or ""),
        trust=str(candidate.get("trust") or ""),
        # The window strategy the entry carries, so a Store application is found by the
        # process that really owns its window and the file manager by its window class.
        processes=tuple(str(item) for item in candidate.get("processes") or ()) or ((exe,) if exe else ()),
        classes=tuple(str(item) for item in candidate.get("classes") or ()),
        frame_child=str(candidate.get("frame_child") or ""),
        index_hit=True, resolve_ms=resolve_ms,
    )


def _subject_name(tokens: list[Token], start: int) -> str:
    """The original spelling of the name an instruction ends with."""
    index = _skip(tokens, start, _CONNECTORS)
    if index >= len(tokens):
        return ""
    name = " ".join(token.text for token in tokens[index:]).strip()
    return name if name and len(name) <= 80 else ""


# The only match reasons that may carry a descriptive word: an exact label, because a name
# may legitimately contain one ("Ghost App", "App Builder").
_NAMED_REASONS = frozenset({"exata", "alias"})


def _describes(tokens: list[Token], start: int) -> bool:
    """True when the words after `start` describe a program instead of naming one."""
    index = _skip(tokens, start, _CONNECTORS)
    return index < len(tokens) and any(token.folded in _TASK_WORDS
                                       for token in tokens[index:])


def _index_answer(tokens: list[Token], apps: AppIndex,
                  start: int) -> tuple[AppResolution | None, float]:
    """The index's answer for the name the instruction ends with, and how long it took.

    A *description* is the Brain's job, never the index's (F-AL8, R3). Measured: "Abre meu
    editor de c\u00f3digo." reached the Bloco de Notas through the approximate pass, because
    "editor de codigo" is one letter-family away from that entry's "Editor de texto" alias
    and scores above the match floor — and a vague request answered by typo distance never
    reaches the Brain that should handle it. A word that only describes a request is
    therefore allowed to match by exact label (a name may contain it) and nothing weaker.
    """
    name = _subject_name(tokens, start)
    if not name:
        return None, 0.0
    started = time.perf_counter()
    answer = apps.resolve(name)
    resolve_ms = (time.perf_counter() - started) * 1000
    if _describes(tokens, start) and (answer.best is None
                                      or answer.best.reason not in _NAMED_REASONS):
        return None, 0.0
    return answer, resolve_ms


def _app_shape(tokens: list[Token], instruction: str, apps: AppIndex | None = None) -> FastPlan | None:
    """Open *any* application: the index is the source of identification (F-AL8, R1).

    When the machine has an index, it is the only thing consulted — the six applications
    the old fixed table named are answered by their own index entries, which carry the
    names and the window strategy Windows does not report (`appindex.WELL_KNOWN`). The
    fixed table survives only as the bootstrap for a machine whose index has never been
    built, so a cold start is not a mute one. Either way the shape consumes the whole
    instruction: "Abra o VS Code e escreva nele" still declines, and a name the index
    does not clearly resolve is not opened.
    """
    if not _starts_with(tokens, OPEN_VERBS):
        return None
    if apps is None or len(apps) == 0:
        curated = _named_app(tokens, 1)
        if curated is None:
            return None
        return FastPlan("launch", curated.name, target=curated.exe,
                        processes=curated.processes, classes=curated.classes,
                        frame_child=curated.frame_child)
    answer, resolve_ms = _index_answer(tokens, apps, 1)
    if answer is None:
        return None
    if answer.kind == "none":
        return None
    if answer.kind == "ambiguous":
        return FastPlan("ambiguous", _subject_name(tokens, 1),
                        candidates=tuple(match.to_wire() for match in answer.matches),
                        resolve_ms=resolve_ms, index_hit=True)
    best = answer.best
    if best is None:
        return None
    return _plan_for(best.to_wire(), label=best.entry.name, resolve_ms=resolve_ms)


def _explorer_folder_shape(tokens: list[Token], instruction: str,
                           apps: AppIndex | None = None) -> FastPlan | None:
    """Open the file manager *at* a known folder — the one compound shape that is exact."""
    if not _starts_with(tokens, OPEN_VERBS) or not any(token.folded == "explorer" for token in tokens):
        return None
    for phrase, alias in sorted(_KNOWN_FOLDERS, key=lambda pair: -len(pair[0].split())):
        words = phrase.split()
        if [token.folded for token in tokens[-len(words):]] != words:
            continue
        head = tokens[:-len(words)]
        allowed = (set(_CONNECTORS) | _MOVE_WORDS
                   | {"explorer", "explorador", "arquivos", "area", "ambiente", "de", "trabalho",
                      "meus", "do", "da"})
        if any(token.folded not in allowed for token in head):
            continue
        return FastPlan("folder", f"pasta {alias}", paths=(alias,))
    return None


def _name(tokens: list[Token], start: int, end: int | None = None) -> str:
    """The original spelling of a name, or "" when it is not a name at all."""
    chosen = tokens[start:end]
    if not chosen or len(chosen) > 4:
        return ""
    if any(token.folded in _NAME_STOP_WORDS for token in chosen):
        return ""
    return " ".join(token.text for token in chosen).strip()


def _folder_shape(tokens: list[Token], instruction: str,
                  apps: AppIndex | None = None) -> FastPlan | None:
    """"Abra a pasta X": open an existing folder, wherever the user keeps it."""
    if not _starts_with(tokens, OPEN_VERBS):
        return None
    index = _skip(tokens, 1, _CONNECTORS)
    if index >= len(tokens) or tokens[index].folded not in _FOLDER_WORDS:
        return None
    index = _skip(tokens, index + 1, _CONNECTORS)
    name = _name(tokens, index)
    if not name:
        return None
    return FastPlan("folder", f"pasta {name}",
                    paths=tuple(f"{alias}/{name}" for alias in ("Desktop", "Documents", "Downloads")))


def _mkdir_shape(tokens: list[Token], instruction: str,
                 apps: AppIndex | None = None) -> FastPlan | None:
    """"Crie uma pasta X na Área de Trabalho": one exact target, one exact place."""
    if not _starts_with(tokens, MKDIR_VERBS):
        return None
    index = _skip(tokens, 1, frozenset({"uma", "um", "a", "o"}))
    if index >= len(tokens) or tokens[index].folded not in _FOLDER_WORDS:
        return None
    index = _skip(tokens, index + 1, _MKDIR_QUALIFIERS)
    for phrase, alias in sorted(_DESTINATIONS, key=lambda pair: -len(pair[0].split())):
        words = phrase.split()
        if [token.folded for token in tokens[-len(words):]] != words:
            continue
        name = _name(tokens, index, len(tokens) - len(words))
        if not name:
            return None
        return FastPlan("filesystem", f"pasta {name}", operation="mkdir",
                        paths=(f"{alias}/{name}",))
    return None


def _rename_shape(tokens: list[Token], instruction: str,
                  apps: AppIndex | None = None) -> FastPlan | None:
    """"Renomeie X para Y": a rename inside one known folder, never across the disk."""
    if not _starts_with(tokens, RENAME_VERBS):
        return None
    index = _skip(tokens, 1, _RENAME_QUALIFIERS)
    for position in range(index + 1, len(tokens)):
        if tokens[position].folded not in _RENAME_SEPARATORS:
            continue
        source = _name(tokens, index, position)
        destination = _name(tokens, position + 1)
        if not source or not destination:
            continue
        return FastPlan("filesystem", f"pasta {source}", operation="rename",
                        paths=(f"Desktop/{source}",), destination=f"Desktop/{destination}")
    return None


@dataclass(frozen=True)
class KnownApp:
    """An application whose deterministic action a *table*, not a model, can run.

    ``label`` is what a plan and a record show; ``aliases`` is every way the name may be
    written after a preposition (``no Spotify``) or as the object of an opening verb
    (``abra o Spotify``); ``processes`` is the image that owns the application's window;
    and ``actions`` names the registered workflows the worker executes and proves.

    Today the only registered workflow is ``search`` (literal query + rendered-result
    proof, implemented by ``app.computer.search``). Opening, focusing, closing and the
    Settings/Explorer shapes are already table entries of their own — they are the
    ``FastPlan.kind`` values the worker dispatches on. A new *searchable* application is a
    new entry here: no parser, HostAgent or AppAgent change is needed to name it.
    """

    label: str                        # canonical name, for the plan label and the record
    aliases: tuple[str, ...]          # lowercase spellings that may name it
    processes: tuple[str, ...]        # window owner image(s), for target_windows()
    actions: tuple[str, ...] = ("search",)


# Registered applications with a table-driven action. Only entries whose workflow the
# worker actually implements belong here, so a name is never accepted for an action that
# would then have to be invented.
KNOWN_APPS: tuple[KnownApp, ...] = (
    KnownApp("Spotify", ("spotify",), ("spotify.exe",), ("search",)),
)


def known_app(name: str, action: str = "search") -> KnownApp | None:
    """The registered application this name means, when it carries ``action``.

    Matching is exact (case- and whitespace-insensitive) on purpose: an approximate match
    would turn a name the user did not say into a search inside an application they did
    not mean.
    """
    text = " ".join(str(name or "").casefold().split())
    if not text:
        return None
    for app in KNOWN_APPS:
        if action in app.actions and text in app.aliases:
            return app
    return None


def known_actions(label: str) -> tuple[str, ...]:
    """Every action registered for one application label (empty when it is unknown)."""
    for app in KNOWN_APPS:
        if app.label == label:
            return app.actions
    return ()


_SEARCH_VERB = (r"(?:busque|busca|buscar|procure|procura|procurar|pesquise|pesquisa|pesquisar"
                r"|search)")
_MEDIA = r"(?:por\s+)?(?:a\s+(?:musica|música)\s+)?"
_ARTICLE = r"(?:(?:o|a|os|as|ao|aos)\s+)?"
# The opening verbs as one alternation, longest first so "iniciar" wins over "inicia".
_OPEN_VERB = "|".join(sorted(OPEN_VERBS, key=len, reverse=True))
# "busque <query> no <app>": the application is named at the end.
_SEARCH_TRAILING = re.compile(
    rf"^(?:{_SEARCH_VERB})\s+{_MEDIA}(?P<query>.+?)\s+(?:no|na|on)\s+(?P<app>.+?)[.!]?$",
    re.IGNORECASE)
# "no <app> busque <query>": the application is named first.
_SEARCH_LEADING = re.compile(
    rf"^(?:no|na|on)\s+(?P<app>.+?)\s+(?:{_SEARCH_VERB})\s+{_MEDIA}(?P<query>.+?)[.!]?$",
    re.IGNORECASE)
# "abra <app> e busque <query>": start (or focus) the application, then search inside it.
_OPEN_SEARCH = re.compile(
    rf"^(?:{_OPEN_VERB})\s+{_ARTICLE}(?P<app>.+?)\s+e\s+(?:{_SEARCH_VERB})\s+{_MEDIA}"
    rf"(?P<query>.+?)[.!]?$",
    re.IGNORECASE)


def _clean_query(raw: str) -> str | None:
    """A literal search query, or ``None`` when the text asks for something else.

    Quotes delimit a literal query exactly, including punctuation and the word "e".
    Unquoted extra operations (a second verb, a conditional) decline rather than
    pretending to understand them, exactly as the single-application search always did.
    """
    query = str(raw or "").strip()
    if len(query) >= 2 and query[0] in {'"', "'"} and query[-1] == query[0]:
        query = query[1:-1]
    elif re.search(r"\b(?:e|depois|entao|então|se|quando|envie|toque|toca|reproduza|apague)\b"
                   r"|[;!?\n]", query, re.IGNORECASE):
        return None
    if not query.strip() or len(query) > 200 or any(ord(c) < 32 for c in query):
        return None
    return query


def _parse_search(text: str) -> tuple[KnownApp, str] | None:
    """The application and literal query a search instruction names, or ``None``.

    Three shapes are recognised, and each consumes the *whole* instruction: a shape that
    cannot explain every word has not understood it. The application is looked up in the
    registry, so a name the table does not carry declines to the agent instead of being
    searched somewhere close enough.
    """
    for pattern in (_SEARCH_TRAILING, _SEARCH_LEADING, _OPEN_SEARCH):
        match = pattern.fullmatch(text)
        if not match:
            continue
        app = known_app(match.group("app"))
        if app is None:
            continue
        query = _clean_query(match.group("query"))
        if query is None:
            return None
        return app, query
    return None


def search_plan(instruction: str, apps: AppIndex | None = None) -> FastPlan | None:
    """A fully consumed literal search, resolved through the registry (FASE 1.1).

    Recognises ``busque <query> no <app>``, ``no <app> busque <query>`` and the compound
    ``abra <app> e busque <query>``. The application comes from ``KNOWN_APPS``, not from a
    hardcoded name, and its launch metadata comes from the machine's index whenever the
    index carries the name — so a **closed** application is started by the same verified
    launcher the plain "abra X" uses, before the literal query is typed and the rendered
    results are proved. No playback is implied and nothing is invented for the agent.
    """
    parsed = _parse_search(str(instruction or "").strip())
    if parsed is None:
        return None
    app, query = parsed
    launch = _app_shape(tokenize(f"abra {app.label}"), f"abra {app.label}", apps)
    if launch is not None and launch.kind == "launch":
        from dataclasses import replace
        return replace(launch, kind="search", label=f"busca no {app.label}", query=query)
    return FastPlan("search", f"busca no {app.label}", processes=app.processes, query=query)


# Ordered by how specific the shape is: Settings and folders before the plain launch,
# because "configuracoes" and "explorer" are also application names. `_app_shape` is the
# index's entry point; `_launch_shape` is the bootstrap that runs only without an index.
SHAPES: tuple[Callable[[list[Token], str, AppIndex | None], FastPlan | None], ...] = (
    _settings_shape, _explorer_folder_shape, _window_shape, _folder_shape,
    _mkdir_shape, _rename_shape, _app_shape, _launch_shape,
)


def resolve(instruction: str, apps: AppIndex | None = None) -> FastPlan | None:
    """The deterministic plan for an instruction, or ``None`` to let the agent plan it.

    ``apps`` is the machine's application index. Without it the behaviour is exactly the
    FASE 8 one (the curated table only), which is what the unit tests rely on: a name
    that only *this* machine happens to have installed must not change a resolution test.
    """
    text = str(instruction or "").strip()
    search = search_plan(text, apps)
    if search is not None:
        return search
    if not text or "?" in text:
        return None
    if len([part for part in (piece.strip() for piece in _SENTENCES.split(text)) if part]) != 1:
        return None
    tokens = tokenize(text)
    if not tokens or any(token.folded in REFUSED_WORDS for token in tokens):
        return None
    for shape in SHAPES:
        plan = shape(tokens, text, apps)
        if plan is not None:
            return plan
    return None


# Words that mean the sentence asks for more than a name. They are a guard, not a
# grammar: they decide whether the user may be *interrupted* ("não encontrei X; indique a
# pasta?"), and a question that stops the chat must never be asked about a sentence that
# also asks for something else.
_TASK_WORDS = frozenset({
    "escreva", "escrever", "digite", "digitar", "coloque", "colocar", "mude", "mudar",
    "faca", "fazer", "salve", "salvar", "apague", "apagar", "delete", "deletar", "instale",
    "instalar", "baixe", "baixar", "envie", "enviar", "copie", "copiar", "mova", "mover",
    "renomeie", "renomear", "procure", "procurar", "busque", "buscar", "verifique",
    "verificar", "fecha", "feche", "fechar", "minimize", "restaure", "maximum", "maximize",
    "programa", "programas", "aplicativo", "aplicativos", "app", "apps", "instalado",
    "instalada", "instalados", "tenho", "que", "quero", "queria", "meu", "minha", "para",
    "com", "por", "favor", "algum", "alguma", "qualquer", "como", "onde", "qual",
    "edicao", "editor", "versao",
})


def open_query(instruction: str) -> str:
    """The name a plain opening command carries, when the sentence is *only* that name.

    Stricter than the shape on purpose: this is what decides whether the index is searched
    for the name and whether the user is asked where the program is. A compound sentence
    ("Abra o VS Code e escreva nele") is a plan, a description ("o programa de edição de
    vídeo") is a question for the agent, and neither may turn into a search of the disk.
    """
    text = str(instruction or "").strip()
    if not text or "?" in text:
        return ""
    if len([part for part in (piece.strip() for piece in _SENTENCES.split(text)) if part]) != 1:
        return ""
    tokens = tokenize(text)
    if not tokens or not _starts_with(tokens, OPEN_VERBS):
        return ""
    if _named_app(tokens, 1) is not None:
        return ""
    if any(token.folded in REFUSED_WORDS for token in tokens):
        return ""
    if any(token.folded in _SETTINGS_WORDS or token.folded in _FOLDER_WORDS for token in tokens):
        return ""
    index = _skip(tokens, 1, _CONNECTORS)
    if index >= len(tokens):
        return ""
    name = tokens[index:]
    # A program's name is one to three words, each worth writing; a connector inside it
    # ("o editor **de** vídeo") means the rest is a description, not a name.
    if not 1 <= len(name) <= 3 or any(len(token.text) < 2 for token in name):
        return ""
    if any(token.folded in _TASK_WORDS or token.folded in _CONNECTORS for token in name):
        return ""
    return " ".join(token.text for token in name).strip()

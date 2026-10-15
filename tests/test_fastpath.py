"""FASE 8: the deterministic path, and the point at which it hands the task back.

Two things are being tested and they are different: the *resolution* (which needs no
Windows at all, because it is a pure table) and the *hand-off* (a plan that does not
apply must reach the agent unchanged, and a plan that ran must never be run again).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.computer import appindex, fastpath
from app.computer.appcatalog import AppCatalog
from app.computer.appindex import AppEntry, AppIndex
from app.computer.windows_tools import WindowsTools
from app.core.controller import TaskController
from app.settings import Settings


@pytest.mark.parametrize(("instruction", "kind", "label"), [
    ("Abra a Calculadora.", "launch", "Calculadora"),
    ("Abra o Calculator", "launch", "Calculadora"),
    ("Feche a Calculadora.", "window", "Calculadora"),
    ("Feche o Bloco de Notas.", "window", "Bloco de Notas"),
    ("Restaure o bloco de notas", "window", "Bloco de Notas"),
    ("Minimize o Explorer.", "window", "Explorer"),
    ("Foque no Bloco de Notas.", "window", "Bloco de Notas"),
    ("Ative a Calculadora.", "window", "Calculadora"),
    ("Crie uma pasta chamada UFO-Test na Área de Trabalho.", "filesystem", "pasta UFO-Test"),
    ("crie uma pasta chamada Relatórios em Documentos", "filesystem", "pasta Relatórios"),
    ("Renomeie UFO-Test para UFO-Test-2.", "filesystem", "pasta UFO-Test"),
    ("Abra a pasta UFO-Test-2.", "folder", "pasta UFO-Test-2"),
    ("Abra o Explorer e vá para Downloads.", "folder", "pasta Downloads"),
    ("Abra as Configurações.", "settings", "Configurações"),
    ("Abra as Configurações e entre em Sistema > Som.", "settings", "Som"),
    ("Feche a janela do explorer", "window", "Explorer"),
])
def test_resolve_maps_the_provable_commands(instruction: str, kind: str, label: str) -> None:
    plan = fastpath.resolve(instruction)
    assert plan is not None, instruction
    assert (plan.kind, plan.label) == (kind, label)


@pytest.mark.parametrize("instruction", [
    "", "   ", "?",
    "Abra o Bloco de Notas e escreva Hello World.",          # T02: writing is not a table entry
    "Abra o Notepad e depois troque para a Calculadora.",     # T09: two applications, one order
    "Abra a Calculadora e escreva 2+2",
    "Minimize todas as janelas.",
    "Como fechar a calculadora?",
    "Feche a calculadora se estiver aberta.",
    "Abra o Notepad. Depois escreva ola.",
    "Abra as Configurações e mude o papel de parede",         # changing a setting is not opening it
    "apague a pasta UFO-Test",                                # destructive: the agent must plan it
    "Feche o aplicativo que estou usando",
    "Abra o Visual Studio Code",
    "Restaurare o bloco de notas",
])
def test_resolve_declines_what_it_cannot_prove(instruction: str) -> None:
    assert fastpath.resolve(instruction) is None, instruction


def test_a_plan_only_claims_what_the_tool_layer_can_do() -> None:
    """The wire form is data: the worker needs no application aliases of its own."""
    plan = fastpath.resolve("Feche a Calculadora.")
    assert plan is not None
    wire = plan.to_wire()
    assert json.loads(json.dumps(wire)) == wire                      # JSON-safe by construction
    assert wire["operation"] == "close"
    assert wire["processes"] == ["calculatorapp.exe"]
    # FASE 5.2: "foque" carries the launch target too — an open application is brought to
    # the front and proven; a closed one is started by the same target "abra" would use.
    focused = fastpath.resolve("Foque no Bloco de Notas.")
    assert focused is not None and focused.operation == "focus"
    assert focused.to_wire()["target"] == "notepad.exe"
    # The Calculator has no window of its own: the frame hosting it is the target.
    assert wire["frame_child"] == "calculatorapp.exe"
    # The launch target travels with the plan, but a close never uses it: with nothing
    # open, opening the application in order to close it would be the opposite request.
    assert wire["target"] == "calc.exe"
    # `explorer.exe` started as a child process while the shell is already running exits
    # with code 1 and opens no window (measured), so the file manager is opened by folder.
    assert fastpath.resolve("Minimize o Explorer.").to_wire()["target"] == "Desktop"


def test_names_keep_the_spelling_the_user_typed() -> None:
    plan = fastpath.resolve("Renomeie UFO-Test para UFO-Test-2.")
    assert plan is not None
    assert plan.paths == ("Desktop/UFO-Test",)
    assert plan.destination == "Desktop/UFO-Test-2"


def test_tokenize_keeps_accents_and_hyphens_apart() -> None:
    tokens = fastpath.tokenize("Área de trabalho: UFO-Test-2")
    assert [token.text for token in tokens] == ["Área", "de", "trabalho", "UFO-Test-2"]
    assert [token.folded for token in tokens] == ["area", "de", "trabalho", "ufo-test-2"]


class FakeEngine:
    """The engine surface the deterministic path uses, and nothing else."""

    process = None
    worker_ready = False

    def __init__(self, outcome: str = "success", evidence: list[dict] | None = None) -> None:
        self.outcome = outcome
        self.evidence = evidence if evidence is not None else [{"operation": "close", "verified": True}]
        self.fast_calls: list[dict] = []
        self.run_task_calls = 0
        self.started = 0

    async def start(self) -> None:
        self.started += 1

    async def fast_path(self, plan: dict, confirm=None) -> dict:
        self.fast_calls.append(plan)
        return {"outcome": self.outcome, "message": "Fechado e verificado.",
                "kind": plan.get("kind"), "label": plan.get("label"),
                "evidence": self.evidence if self.outcome == "success" else [],
                "actions": 1 if self.outcome == "success" else 0, "steps": 1,
                "execution_time_ms": 82.0, "observation_time_ms": 0.0,
                "verification_time_ms": 0.0, "waits_ms": 0.0,
                "failure_kind": "" if self.outcome == "success" else "ACTION_FAILURE"}

    async def run_task(self, instruction: str, completion, confirm, *,
                     approval_mode: str = "manual") -> dict:
        self.run_task_calls += 1
        return {"status": "SUCCESS", "message": "agente", "actions": 1, "steps": 1,
                "evidence": [{"operation": "agent", "verified": True}]}

    async def stop(self) -> None:
        pass


class FakeBrain:
    async def complete(self, messages: list[dict]) -> object:  # pragma: no cover - unused
        raise AssertionError("the deterministic path must not infer")

    async def close(self) -> None:
        pass


def run_with(outcome: str, instruction: str = "Feche a Calculadora.",
             settings: Settings | None = None) -> tuple[object, FakeEngine]:
    async def scenario() -> tuple[object, FakeEngine]:
        controller = TaskController(settings or Settings(provider="codex", model="account-model",
                                                          collect_metrics=False))
        engine = FakeEngine(outcome)
        controller.engine = engine  # type: ignore[assignment]
        controller.brain = FakeBrain()  # type: ignore[assignment]
        result = await controller.run(instruction)
        await controller.close()
        return result, engine

    return asyncio.run(scenario())


def test_a_provable_command_runs_without_any_inference() -> None:
    result, engine = run_with("success")
    assert result.status == "SUCCESS"
    assert result.plan_source == "deterministic"
    assert result.llm_calls == 0 and result.llm_time_ms == 0
    # R1: the index names the Store Calculator the way the shell does ("Calculator"); the
    # Portuguese name is an alias, so the label depends on whether this machine's index
    # was built with the strategy seed.
    assert result.fast_path_label.casefold() in {"calculadora", "calculator"}
    assert [call["kind"] for call in engine.fast_calls] == ["window"]
    assert engine.run_task_calls == 0
    assert result.false_success is False
    assert result.actions == 1 and result.execution_time_ms == 82.0


def test_the_deterministic_path_needs_no_model_configured() -> None:
    """A command a table can prove must not require choosing a brain first."""
    result, engine = run_with("success", settings=Settings(provider="openai", model="",
                                                           collect_metrics=False))
    assert result.status == "SUCCESS"
    assert result.llm_calls == 0 and engine.run_task_calls == 0


def test_a_declined_plan_reaches_the_agent_unchanged() -> None:
    result, engine = run_with("declined")
    assert engine.run_task_calls == 1
    assert result.plan_source == "deterministic+llm"
    assert result.status == "SUCCESS"


def test_a_failed_plan_is_not_run_twice() -> None:
    """The action already ran: repeating it through the agent could act on the desktop twice."""
    result, engine = run_with("failed")
    assert result.status == "FAILED"
    assert result.failure_kind == "ACTION_FAILURE"
    assert engine.run_task_calls == 0
    assert result.plan_source == "deterministic"


def test_a_refused_confirmation_cancels_without_a_second_attempt() -> None:
    result, engine = run_with("cancelled")
    assert result.status == "CANCELLED"
    assert result.failure_kind == "CANCELLED"
    assert engine.run_task_calls == 0


def test_fast_path_can_be_turned_off() -> None:
    result, engine = run_with("success", settings=Settings(provider="codex", model="m",
                                                           fast_path=False, collect_metrics=False))
    assert engine.fast_calls == []
    assert engine.run_task_calls == 1
    assert result.plan_source == "llm"


def window(handle: int, process: str, title: str = "t", cls: str = "CabinetWClass") -> dict:
    return {"handle": handle, "pid": handle, "process": process, "title": title,
            "class": cls, "visible": True, "minimized": False, "active": False}


def app_target(name: str) -> fastpath.AppTarget:
    return next(target for target in fastpath.APPS if target.name == name)


def test_target_windows_filters_by_process_and_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """The desktop shell is explorer.exe too; only a CabinetWClass window is a folder."""
    import app.computer.windows_tools as module

    listing = [window(1, "explorer.exe", "Program Manager", "Progman"),
               window(2, "explorer.exe", "Downloads", "CabinetWClass"),
               window(3, "notepad.exe", "Bloco de notas", "Notepad")]
    monkeypatch.setattr(module, "windows", lambda: listing)
    tools = WindowsTools(state=object(), confirm=_never)
    explorer = app_target("Explorer")
    assert [item["handle"] for item in tools.target_windows(explorer.processes,
                                                           explorer.classes)] == [2]
    notepad = app_target("Bloco de Notas")
    assert [item["handle"] for item in tools.target_windows(notepad.processes)] == [3]


def test_existing_folder_picks_the_first_real_candidate(tmp_path: Path) -> None:
    tools = WindowsTools(state=object(), confirm=_never)
    (tmp_path / "UFO-Test").mkdir()
    candidates = [f"{tmp_path}/UFO-Test", "Desktop/nao-existe-mesmo"]
    # The candidate is returned as it was written, so the caller launches the path
    # spelling the instruction produced, not a normalised one.
    assert tools.existing_folder(candidates) == candidates[0]
    assert tools.existing_folder(["Desktop/nao-existe-mesmo"]) == ""
    assert tools.existing_folder([]) == ""


async def _never(_: str) -> bool:
    return False

# --- F-AL2/F-AL4/F-AL6: the index is what identifies the application ------------------
def installed(name: str, *, exe: str = "", target: str = "", launch: str = "exe",
              source: str = "start_menu", aliases: tuple[str, ...] = (),
              install_dir: str = "", aumid: str = "", trust: str = "trusted",
              picks: int = 0) -> AppEntry:
    """One index entry, built the way discovery builds it."""
    exe = exe or f"{name}.exe"
    target = target or f"C:/Program Files/{name}/{exe}"
    return AppEntry(
        id=appindex.identity(launch, target, aumid), name=name,
        aliases=aliases, launch=launch, target=target, exe=exe, source=source,
        install_dir=install_dir or str(target).rsplit("/", 1)[0], aumid=aumid,
        trust=trust, picks=picks,
    )


INSTALLED = (installed("Discord"), installed("Spotify"),
             installed("7-Zip File Manager", exe="7zFM.exe"))


def catalog_at(tmp_path: Path, entries: tuple[AppEntry, ...]) -> AppCatalog:
    """A catalog whose index is exactly what the test says this machine has."""
    AppIndex(entries=entries).save(appindex.default_index_path(tmp_path))
    catalog = AppCatalog(appindex.default_index_path(tmp_path))
    catalog.load()
    return catalog


@pytest.mark.parametrize(("instruction", "name", "expected"), [
    ("Abre o Discord.", "Discord", "Discord.exe"),
    ("Abra o Spotify.", "Spotify", "Spotify.exe"),
    ("abre o 7-zip", "7-Zip File Manager", "7zFM.exe"),
    ("Abre o Code.", "Visual Studio Code", "Code.exe"),
])
def test_the_index_answers_which_program_a_name_means(instruction: str, name: str,
                                                      expected: str) -> None:
    """F-AL2: the machine's own index is what identifies the application."""
    index = AppIndex(entries=(*INSTALLED,
                              installed("Visual Studio Code", exe="Code.exe",
                                        aliases=("Code", "vscode"))))
    plan = fastpath.resolve(instruction, apps=index)
    assert plan is not None and plan.kind == "launch", instruction
    assert plan.label == name
    assert plan.index_hit is True
    assert plan.app_source == "start_menu"
    assert plan.expected == expected
    assert plan.target == f"exe:C:/Program Files/{name}/{expected}"
    assert plan.trust == "trusted"
    assert json.loads(json.dumps(plan.to_wire())) == plan.to_wire()


def migrated(name: str, *, launch: str, target: str, exe: str = "", aumid: str = "",
             source: str = "appsfolder") -> AppEntry:
    """One entry built the way discovery builds it, strategy included (R1)."""
    return appindex.apply_strategy(AppEntry(
        id=appindex.identity(launch, target, aumid), name=name, launch=launch, target=target,
        exe=exe, aumid=aumid, source=source))


# The six applications the fixed table named, as the index holds them (R1).
MIGRATED = (
    migrated("Calculator", launch="appid", target="Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
             aumid="Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
    migrated("Notepad", launch="appid", target="Microsoft.WindowsNotepad_8wekyb3d8bbwe!App",
             aumid="Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"),
    migrated("Explorador de Arquivos", launch="appid", target="Microsoft.Windows.Explorer",
             aumid="Microsoft.Windows.Explorer"),
    migrated("Microsoft Edge", launch="exe", target="C:/e/msedge.exe", exe="msedge.exe",
             source="start_menu"),
    migrated("Paint", launch="appid", target="Microsoft.Paint_8wekyb3d8bbwe!App",
             aumid="Microsoft.Paint_8wekyb3d8bbwe!App"),
)


@pytest.mark.parametrize(("instruction", "label"), [
    ("Abra a Calculadora.", "Calculator"),
    ("Abra o Calculator", "Calculator"),
    ("Abra o Bloco de Notas.", "Notepad"),
    ("Abra o bloco de anotacoes", "Notepad"),
    ("Abra o Explorer.", "Explorador de Arquivos"),
    ("Abra o explorador de arquivos.", "Explorador de Arquivos"),
    ("abre o edge", "Microsoft Edge"),
    ("Abra o Paint.", "Paint"),
])
def test_the_six_migrated_applications_are_answered_by_the_index(instruction: str,
                                                                 label: str) -> None:
    """R1: the fixed table is gone from the answer; the machine's own entries answer."""
    plan = fastpath.resolve(instruction, apps=AppIndex(entries=MIGRATED))
    assert plan is not None and plan.kind == "launch", instruction
    assert plan.index_hit is True and plan.label == label


def test_the_index_carries_the_launch_strategy_the_old_table_held() -> None:
    """R1: the measured quirks are entry metadata now, not per-name conditionals."""
    index = AppIndex(entries=MIGRATED)
    calc = fastpath.resolve("Abra a Calculadora.", apps=index)
    assert calc is not None
    assert calc.target == "appid:Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
    assert calc.aumid == "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
    assert calc.processes == ("CalculatorApp.exe",)
    assert calc.frame_child == "CalculatorApp.exe"
    # The file manager cannot be started as a child process: the strategy opens a folder.
    explorer = fastpath.resolve("Abra o Explorer.", apps=index)
    assert explorer is not None and explorer.target == "Desktop"
    assert explorer.classes == ("CabinetWClass",)


def test_a_window_operation_uses_the_strategy_the_entry_carries() -> None:
    """R1: closing or minimizing an index entry needs the entry's window strategy."""
    index = AppIndex(entries=MIGRATED)
    closed = fastpath.resolve("Feche a Calculadora.", apps=index)
    assert closed is not None and closed.kind == "window" and closed.operation == "close"
    assert closed.processes == ("CalculatorApp.exe",) and closed.frame_child == "CalculatorApp.exe"
    minimized = fastpath.resolve("Minimize o Explorer.", apps=index)
    assert minimized is not None and minimized.operation == "minimize"
    assert minimized.processes == ("explorer.exe",) and minimized.classes == ("CabinetWClass",)
    focused = fastpath.resolve("Foque no Explorer.", apps=index)
    assert focused is not None and focused.operation == "focus"
    assert focused.processes == ("explorer.exe",) and focused.classes == ("CabinetWClass",)


def test_without_an_index_the_bootstrap_table_still_answers() -> None:
    """A machine whose discovery never ran is not a mute one: the table is a cold start."""
    cold = fastpath.resolve("Abra a Calculadora.")
    assert cold is not None and cold.target == "calc.exe"
    assert cold.frame_child == "calculatorapp.exe"
    assert fastpath.resolve("Abra o Paint.") is not None


def test_the_bootstrap_table_never_answers_behind_the_index() -> None:
    """R1: with an index, a name it does not carry is not resolved by the fixed table."""
    index = AppIndex(entries=INSTALLED)
    assert fastpath.resolve("Abra a Calculadora.", apps=index) is None
    assert fastpath.resolve("Abra o Paint.", apps=index) is None
    assert fastpath.resolve("Minimize o Explorer.", apps=index) is None


def test_the_index_never_opens_a_name_it_does_not_clearly_match() -> None:
    index = AppIndex(entries=INSTALLED)
    assert fastpath.resolve("Abra o Telegram.", apps=index) is None
    assert fastpath.resolve("Abra o Blender.", apps=index) is None
    assert fastpath.resolve("Abra a Calculadora.", apps=index) is None


def test_a_compound_instruction_is_still_declined_with_an_index() -> None:
    """The FASE 8 safety property survives: every token has to be explained."""
    index = AppIndex(entries=INSTALLED)
    assert fastpath.resolve("Abra o Discord e escreva nele.", apps=index) is None
    assert fastpath.resolve("Abra o Spotify e o Discord.", apps=index) is None
    assert fastpath.resolve("Como abrir o Discord?", apps=index) is None


def test_a_swept_executable_is_marked_as_needing_consent() -> None:
    entries = (installed("Portable Tool", source="user_folder", trust="user_folder",
                         target="D:/portable/tool.exe"),)
    plan = fastpath.resolve("Abre o Portable Tool.", apps=AppIndex(entries=entries))
    assert plan is not None
    assert plan.trust == "user_folder"
    assert plan.target == "exe:D:/portable/tool.exe"
    assert plan.to_wire()["trust"] == "user_folder"


def test_a_packaged_application_travels_by_its_identity() -> None:
    entries = (installed("Photos", launch="appid", source="appsfolder",
                         target="Microsoft.Windows.Photos_8wekyb3d8bbwe!App",
                         aumid="Microsoft.Windows.Photos_8wekyb3d8bbwe!App",
                         exe="Photos.exe"),)
    plan = fastpath.resolve("Abre o Photos.", apps=AppIndex(entries=entries))
    assert plan is not None
    assert plan.target == "appid:Microsoft.Windows.Photos_8wekyb3d8bbwe!App"
    assert plan.aumid == "Microsoft.Windows.Photos_8wekyb3d8bbwe!App"
    assert plan.app_id.startswith("aumid:")


def test_two_plausible_applications_become_a_question_not_a_guess() -> None:
    entries = (installed("Discord", install_dir="C:/one"),
               installed("Discord", install_dir="C:/two", source="user_folder",
                         trust="user_folder", target="D:/two/Discord.exe"))
    plan = fastpath.resolve("Abre o Discord.", apps=AppIndex(entries=entries))
    assert plan is not None and plan.kind == "ambiguous"
    assert len(plan.candidates) == 2
    assert plan.index_hit is True
    chosen = plan.with_choice(1)
    assert chosen is not None
    assert chosen.kind == "launch"
    assert chosen.target == "exe:D:/two/Discord.exe"
    assert chosen.trust == "user_folder"
    assert plan.with_choice(9) is None


@pytest.mark.parametrize(("instruction", "query"), [
    ("Abre o Discord.", "Discord"),
    ("abre o VS Code", "VS Code"),
    ("Abra o programa X.", ""),                 # a placeholder is not a name
    ("Abra o editor de video.", ""),            # a description is the agent's job
    ("Abra o Discord e escreva nele.", ""),     # a plan, not a name
    ("Abra o Bloco de Notas.", ""),             # the curated table already knows it
    ("Feche o Discord.", ""),
    ("Como abrir o Discord?", ""),
])
def test_only_a_plain_program_name_may_be_searched_or_asked_about(instruction: str,
                                                                  query: str) -> None:
    assert fastpath.open_query(instruction) == query, instruction


def run_application(instruction: str, *, catalog: AppCatalog, choose=None,
                    outcome: str = "success",
                    evidence: list[dict] | None = None) -> tuple[object, FakeEngine, AppCatalog]:
    """Run one instruction with a prepared index, and report what the worker was asked to do."""
    async def scenario() -> tuple[object, FakeEngine, AppCatalog]:
        controller = TaskController(Settings(provider="codex", model="account-model",
                                             collect_metrics=False), choose=choose)
        controller.catalog = catalog
        engine = FakeEngine(outcome, evidence=evidence)
        controller.engine = engine  # type: ignore[assignment]
        controller.brain = FakeBrain()  # type: ignore[assignment]
        result = await controller.run(instruction)
        await controller.close()
        return result, engine, catalog

    return asyncio.run(scenario())


LAUNCHED = [{"operation": "launch", "kind": "exe", "verified": True, "trust": "trusted"}]


def test_an_indexed_application_opens_with_zero_inference(tmp_path: Path) -> None:
    result, engine, _ = run_application("Abre o Discord.",
                                        catalog=catalog_at(tmp_path, INSTALLED),
                                        evidence=LAUNCHED)
    assert result.status == "SUCCESS"
    assert result.plan_source == "deterministic"
    assert result.llm_calls == 0 and engine.run_task_calls == 0
    assert result.app_index_hit is True
    assert result.app_source == "start_menu"
    assert result.app_launch_kind == "exe"
    assert [call["kind"] for call in engine.fast_calls] == ["launch"]
    assert engine.fast_calls[0]["target"].startswith("exe:")
    assert engine.fast_calls[0]["expected"] == "Discord.exe"


def test_a_choice_is_asked_once_and_remembered(tmp_path: Path) -> None:
    """F-AL4: two plausible installs, one question, and the answer is in the index."""
    entries = (installed("Discord", install_dir="C:/one"),
               installed("Discord", install_dir="C:/two", target="D:/two/Discord.exe"))
    catalog = catalog_at(tmp_path, entries)
    asked: list[tuple[str, int]] = []

    async def choose(label: str, candidates: list[dict]) -> int | None:
        asked.append((label, len(candidates)))
        return 1

    result, engine, _ = run_application("Abre o Discord.", catalog=catalog, choose=choose,
                                        evidence=LAUNCHED)
    assert asked == [("Discord", 2)]
    assert result.status == "SUCCESS"
    assert result.app_launch_kind == "exe"
    assert result.app_candidates == 2
    assert engine.fast_calls[0]["target"] == "exe:D:/two/Discord.exe"
    assert engine.run_task_calls == 0
    # The choice is this user's word for this name now, and it survived the run.
    reloaded = AppIndex.load(appindex.default_index_path(tmp_path))
    chosen = reloaded.by_id(engine.fast_calls[0]["app_id"])
    assert chosen is not None and chosen.picks == 1
    assert reloaded.resolve("Discord").kind == "single"


def test_refusing_the_choice_opens_nothing(tmp_path: Path) -> None:
    entries = (installed("Discord", install_dir="C:/one"),
               installed("Discord", install_dir="C:/two", target="D:/two/Discord.exe"))

    async def refuse(label: str, candidates: list[dict]) -> int | None:
        return None

    result, engine, _ = run_application("Abre o Discord.",
                                        catalog=catalog_at(tmp_path, entries), choose=refuse)
    assert result.status == "CANCELLED"
    assert result.failure_kind == "CANCELLED"
    assert engine.fast_calls == [] and engine.run_task_calls == 0


def test_without_a_way_to_ask_the_agent_decides(tmp_path: Path) -> None:
    """No chooser means the ambiguity is the agent's: the same hand-off as FASE 8."""
    entries = (installed("Discord", install_dir="C:/one"),
               installed("Discord", install_dir="C:/two", target="D:/two/Discord.exe"))
    result, engine, _ = run_application("Abre o Discord.", catalog=catalog_at(tmp_path, entries))
    assert engine.run_task_calls == 1
    assert result.plan_source == "deterministic+llm"


def test_a_name_the_index_does_not_carry_is_searched_before_the_agent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F-AL6: one bounded search on the request path, then the answer is stored."""
    import app.computer.appdiscovery as discovery

    catalog = catalog_at(tmp_path, INSTALLED)
    searched: list[str] = []
    found = [installed("Telegram", source="sweep", target="D:/apps/telegram/Telegram.exe")]

    def search_for(name: str, *, budget_ms: float = 0.0, configured=()) -> list[AppEntry]:
        searched.append(name)
        return found

    monkeypatch.setattr(discovery, "search_for", search_for)
    result, engine, _ = run_application("Abre o Telegram.", catalog=catalog, evidence=LAUNCHED)
    assert searched == ["Telegram"]
    assert result.status == "SUCCESS"
    assert result.app_search_ms > 0
    assert engine.fast_calls[0]["expected"] == "Telegram.exe"
    stored = AppIndex.load(appindex.default_index_path(tmp_path))
    assert stored.resolve("Telegram").kind == "single"


def test_the_user_can_point_at_where_a_program_is(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """F-AL6: nothing found, the user indicates a folder, and it opens — and is remembered."""
    import app.computer.appdiscovery as discovery

    catalog = catalog_at(tmp_path, INSTALLED)
    pointed = [installed("My Portable", source="user_folder", trust="user_folder",
                         target=str(tmp_path / "apps" / "mine.exe"))]
    (tmp_path / "apps").mkdir()
    monkeypatch.setattr(discovery, "discover_configured", lambda folders: list(pointed))
    answers: list[str] = []

    async def indicate(query: str) -> str:
        answers.append(query)
        return str(tmp_path / "apps")

    async def scenario() -> tuple[object, FakeEngine]:
        controller = TaskController(Settings(provider="codex", model="account-model",
                                             collect_metrics=False), indicate=indicate)
        controller.catalog = catalog
        engine = FakeEngine("success", evidence=[{"operation": "launch", "kind": "exe",
                                                 "verified": True, "trust": "user_folder"}])
        controller.engine = engine  # type: ignore[assignment]
        controller.brain = FakeBrain()  # type: ignore[assignment]
        result = await controller.run("Abre o My Portable.")
        await controller.close()
        return result, engine

    result, engine = asyncio.run(scenario())
    assert answers == ["My Portable"]
    assert result.status == "SUCCESS"
    assert engine.run_task_calls == 0
    assert engine.fast_calls[0]["trust"] == "user_folder"
    # The folder the user pointed at is remembered for the next sessions, next to the
    # index it feeds.
    assert (tmp_path / ".local" / "app-folders.json").is_file()


def test_a_target_that_disappeared_leaves_the_index() -> None:
    """F-AL6: an uninstalled or moved application must not fail forever on a ghost path."""
    index = AppIndex(entries=INSTALLED)
    assert len(index.without(["exe:nonexistent"])) == len(index)
    assert len(index.forget(INSTALLED[0].id)) == len(index) - 1


def test_a_launch_whose_target_vanished_ends_with_the_reason(tmp_path: Path) -> None:
    """F-AL6: the entry leaves the index, and no inference is spent on a ghost.

    Measured live (2026-10-08): an instruction naming an entry whose target is gone used
    to reach the agent, which would be asked to open a program that no longer exists.
    """
    ghost = installed("Ghost App", source="user_folder", trust="user_folder",
                      target=str(tmp_path / "ghost" / "ghostapp.exe"), exe="ghostapp.exe")
    catalog = catalog_at(tmp_path, (*INSTALLED, ghost))
    result, engine, _ = run_application("Abre o Ghost App.", catalog=catalog,
                                       outcome="declined")
    assert result.status == "FAILED"
    assert result.failure_kind == "ACTION_FAILURE"
    assert result.plan_source == "deterministic"
    assert engine.run_task_calls == 0 and result.llm_calls == 0
    assert "Indique o executável" in result.message
    assert catalog.entry(ghost.id) is None


def test_the_run_reports_the_index_metrics_the_record_needs(tmp_path: Path) -> None:
    result, _, _ = run_application("Abre o Spotify.", catalog=catalog_at(tmp_path, INSTALLED),
                                   evidence=LAUNCHED)
    assert result.app_index_hit is True
    assert result.app_source == "start_menu"
    assert result.app_launch_kind == "exe"
    assert result.app_resolve_ms >= 0
    assert result.app_intent_ms >= 0
    assert result.app_candidates == 0
    assert result.app_index_cold is False

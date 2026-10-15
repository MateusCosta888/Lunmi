"""F-AL1: the application index, and the rules that decide which application a name means.

Everything here runs without a desktop: the index and its ranking are pure data, exactly
like the Settings catalogue. What is being tested is *recall without guessing* — the
name a user types has to reach the right entry, and a name that does not clearly reach
one entry must not be turned into a decision.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.computer import appindex
from app.computer.appindex import AppEntry, AppIndex


def entry(name: str, *, exe: str = "", target: str = "", launch: str = "exe",
          source: str = "registry", aliases: tuple[str, ...] = (), install_dir: str = "",
          aumid: str = "", picks: int = 0, processes: tuple[str, ...] = (),
          frame_child: str = "") -> AppEntry:
    target = target or f"C:/Program Files/{name}/{exe or name + '.exe'}"
    return AppEntry(
        id=appindex.identity(launch, target, aumid), name=name,
        aliases=aliases or appindex.guess_aliases(name, target, exe or f"{name}.exe"),
        launch=launch, target=target, exe=exe or f"{name}.exe", source=source,
        install_dir=install_dir or str(target).rsplit("/", 1)[0], aumid=aumid, picks=picks,
        processes=processes, frame_child=frame_child,
    )


def test_a_windows_style_folder_identifier_becomes_a_real_path() -> None:
    """What Windows itself writes in an App Paths entry is not a path until expanded."""
    from app.computer import appdiscovery
    expanded = appdiscovery.expand_known_folder(
        r"{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}\Steam\Steam.exe")
    assert expanded.casefold().startswith(str(appdiscovery.os.environ["ProgramFiles(x86)"]).casefold())
    assert expanded.casefold().endswith(r"\steam\steam.exe")
    untouched = "C:/games/Game.exe"
    assert appdiscovery.expand_known_folder(untouched) == untouched


def test_the_version_an_installer_wrote_is_a_name_and_an_alias() -> None:
    assert appindex.clean_name("Lightshot-5.5.0.7") == "Lightshot"
    assert appindex.clean_name("AudioRelay version 0.27.5") == "AudioRelay"
    assert appindex.clean_name("Windhawk v1.7.3") == "Windhawk"
    assert appindex.clean_name("PowerToys (Preview)") == "PowerToys (Preview)"
    assert appindex.clean_name("7-Zip File Manager") == "7-Zip File Manager"


def test_aliases_never_make_a_launcher_into_a_name() -> None:
    """`Update.exe` must not teach the index that Discord is called "update"."""
    aliases = [appindex.fold(alias) for alias in
               appindex.guess_aliases("Discord", r"C:\x\Update.exe", "Update.exe")]
    assert "update" not in aliases


def test_the_install_folder_becomes_a_name_only_when_it_says_something() -> None:
    assert appindex.install_alias(r"C:\x\Microsoft VS Code\Code.exe", "Code") == ("Microsoft VS Code",)
    assert appindex.install_alias(r"C:\x\7-Zip\7zFM.exe", "7zFM") == ("7-Zip",)
    assert appindex.install_alias(r"C:\x\bin\tool.exe", "tool") == ()
    assert appindex.install_alias(r"C:\x\x64\app.exe", "app") == ()
    assert appindex.install_alias(r"C:\x\Code\Code.exe", "Code") == ()


def test_identity_is_the_package_or_the_executable_path() -> None:
    assert appindex.identity("appid", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
                             "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App").startswith("aumid:")
    assert appindex.identity("exe", r"C:\A\App.exe", "") == appindex.identity(
        "exe", "c:/a/APP.EXE", "")
    assert appindex.identity("lnk", r"C:\A\App.lnk", "") != appindex.identity(
        "exe", r"C:\A\App.exe", "")


# --- ranking ---------------------------------------------------------------------------
INSTALLED = (
    entry("Discord", exe="Discord.exe"),
    entry("Steam", exe="Steam.exe", launch="appid", aumid="Valve.Steam_1!App",
          target="Valve.Steam_1!App"),
    entry("Steam Link", exe="streaming_client.exe"),
    entry("Spotify", exe="Spotify.exe"),
    entry("Visual Studio Code", exe="Code.exe", aliases=("Code", "Microsoft VS Code", "vscode")),
    entry("Notepad", exe="notepad.exe"),
    entry("Registry Editor", exe="regedit.exe", aliases=("regedit",)),
)

APP_INDEX = AppIndex(entries=INSTALLED)


@pytest.mark.parametrize(("query", "expected"), [
    ("discord", "Discord"),
    ("Discord", "Discord"),
    ("DISCORD", "Discord"),
    ("steam", "Steam"),
    ("code", "Visual Studio Code"),
    ("Code.exe", "Visual Studio Code"),
    ("notepad", "Notepad"),
    ("regedit", "Registry Editor"),
    ("Visual Studio Code", "Visual Studio Code"),
    ("vscode", "Visual Studio Code"),
    ("vs code", "Visual Studio Code"),
])
def test_a_clear_name_reaches_exactly_one_application(query: str, expected: str) -> None:
    answer = APP_INDEX.resolve(query)
    assert answer.kind == "single", (query, answer.kind)
    assert answer.best is not None and answer.best.entry.name == expected


@pytest.mark.parametrize("query", ["spotfy", "discrod", "notepd", "steemm"])
def test_one_wrong_letter_still_finds_the_application(query: str) -> None:
    assert APP_INDEX.resolve(query).kind == "single", query


@pytest.mark.parametrize("query", [
    "minecraft", "photoshop", "final cut pro", "abre o programa de edicao de video",
    "st", "set", "sets",
])
def test_a_name_that_matches_nothing_is_not_turned_into_a_guess(query: str) -> None:
    assert APP_INDEX.resolve(query).kind == "none", query


def test_a_short_form_of_a_real_name_is_enough() -> None:
    """"Edge" for Microsoft Edge, "OBS" for OBS Studio: a word of the name."""
    entries = (entry("Microsoft Edge", exe="msedge.exe"),
               entry("Microsoft Edge WebView2 Runtime", exe="msedgewebview2.exe"),
               entry("OBS Studio", exe="obs64.exe"),
               entry("Obsidian", exe="Obsidian.exe"))
    index = AppIndex(entries=entries)
    assert index.resolve("edge").best is not None
    assert index.resolve("edge").best.entry.name == "Microsoft Edge"  # type: ignore[union-attr]
    assert index.resolve("obs").best is not None
    assert index.resolve("obs").best.entry.name == "OBS Studio"  # type: ignore[union-attr]


def test_an_abbreviation_only_helps_when_nothing_else_matched() -> None:
    """"vsc" is the initials of the name; "sets" only starts another word."""
    entries = (entry("Visual Studio Code", exe="Code.exe"),)
    index = AppIndex(entries=entries)
    assert index.resolve("vsc").best is not None
    assert index.resolve("vsc").best.entry.name == "Visual Studio Code"  # type: ignore[union-attr]
    assert index.resolve("sets").kind == "none"
    assert AppIndex(entries=(entry("setspn", exe="setspn.exe"),)).resolve("sets").kind == "none"
    # The initials only match once nothing solid did: the full alias always wins.
    both = AppIndex(entries=(entry("Visual Studio Code", exe="Code.exe"),
                             entry("Vsc Remote", exe="vsc-remote.exe")))
    assert both.resolve("vsc").best is not None


def test_two_separate_installs_are_a_question_not_a_guess() -> None:
    """Neither installation is inside the other: only the user knows which one he meant."""
    entries = (entry("Discord", exe="Discord.exe", install_dir="C:/one"),
               entry("Discord", exe="Discord.exe", install_dir="C:/two", source="user_folder"))
    answer = AppIndex(entries=entries).resolve("discord")
    assert answer.kind == "ambiguous"
    assert len(answer.matches) == 2


def test_a_copy_of_an_application_inside_itself_is_one_answer() -> None:
    r"""A versioned copy (Discord\app-1.0.9260\Discord.exe) is not a second Discord."""
    entries = (entry("Discord", exe="Discord.exe", install_dir="C:/d", source="registry"),
               entry("Discord", exe="Discord.exe", install_dir="C:/d/app-1.0.9260",
                     target="C:/d/app-1.0.9260/Discord.exe", source="sweep"))
    assert AppIndex(entries=entries).resolve("discord").kind == "single"


def test_a_component_of_an_application_does_not_compete_with_it() -> None:
    """"PowerToys" matched its own RestartAgent through the folder both live in."""
    entries = (entry("PowerToys", exe="PowerToys.exe", install_dir="C:/pt", source="registry"),
               entry("RestartAgent", exe="RestartAgent.exe", install_dir="C:/pt",
                     source="sweep", aliases=("PowerToys",)))
    index = AppIndex(entries=entries)
    answer = index.resolve("powertoys")
    assert answer.kind == "single"
    assert answer.best is not None and answer.best.entry.name == "PowerToys"


def test_a_name_the_user_chose_before_wins_the_tie() -> None:
    one = entry("Discord", exe="Discord.exe", target="C:/one/Discord.exe", install_dir="C:/one")
    two = entry("Discord", exe="Discord.exe", target="C:/two/Discord.exe", install_dir="C:/two",
                source="user_folder")
    assert AppIndex(entries=(one, two)).resolve("discord").kind == "ambiguous"
    learned = AppIndex(entries=(one, two)).learned("discord", two.id)
    answer = learned.resolve("discord")
    assert answer.kind == "single"
    assert answer.best is not None and answer.best.entry.id == two.id


# --- the index as a file ---------------------------------------------------------------
def test_the_index_round_trips_and_keeps_every_name() -> None:
    original = AppIndex(entries=INSTALLED, built_ms=12.5, sources={"registry": 2},
                        signatures={"registry": "abc"}, sweeps={"roots": {"c:/": {"pending": []}}},
                        updated_ms=99.0)
    restored = AppIndex.from_wire(json.loads(json.dumps(original.to_wire())))
    assert restored.entries == original.entries
    assert restored.signatures == original.signatures
    assert restored.sweeps == original.sweeps
    assert len(restored) == len(original)


def test_a_corrupt_or_older_file_does_not_break_resolution(tmp_path) -> None:
    path = tmp_path / "apps.json"
    path.write_text("{ not json", encoding="utf-8")
    assert len(AppIndex.load(path)) == 0
    path.write_text(json.dumps({"version": 0, "apps": [{"name": "x"}]}), encoding="utf-8")
    assert len(AppIndex.load(path)) == 0
    # An entry that could not be launched safely is dropped, the rest survives.
    good = entry("Discord").to_wire()
    payload = {"version": appindex.INDEX_VERSION,
               "apps": [good, {"name": "broken", "launch": "shell", "target": "x"},
                        {"name": "no target"}]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = AppIndex.load(path)
    assert [item.name for item in loaded.entries] == ["Discord"]


def test_a_saved_index_is_written_atomically_and_reread(tmp_path) -> None:
    path = tmp_path / ".local" / "apps.json"
    AppIndex(entries=INSTALLED).save(path)
    assert AppIndex.load(path).entries == INSTALLED
    assert not list(path.parent.glob("*.tmp"))


def test_a_target_that_is_gone_leaves_the_index() -> None:
    from app.computer import appdiscovery
    present = entry("Here", exe="Here.exe", target="C:/definitely/not/here/../missing.exe")
    index = AppIndex(entries=(present,))
    assert appdiscovery.stale_ids(index.entries) == [present.id]
    assert len(index.without([present.id])) == 0


def test_a_junction_is_never_descended_into(tmp_path) -> None:
    """A junction to a tree the sweep already skips must not be walked a second time.

    Measured (2026-10-08): `C:\\Users\\<user>\\Configura\u00e7\u00f5es Locais` is a junction to
    `AppData\\Local`, and following it duplicated every entry under it — the index grew
    from 519 to 1 205 entries, most of them copies of entries it already had.
    """
    import subprocess

    from app.computer import appdiscovery

    real = tmp_path / "real"
    real.mkdir()
    (real / "nested.exe").write_bytes(b"MZ")
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "direct.exe").write_bytes(b"MZ")
    link = parent / "junction"
    created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)],
                             capture_output=True)
    if created.returncode != 0:
        pytest.skip("a junction could not be created here")
    assert appdiscovery._reparse(link) is True
    assert appdiscovery._reparse(real) is False
    walked, _ = appdiscovery._walk_exes(parent, max_depth=4, budget_ms=2000.0,
                                        trust="trusted", source="sweep")
    names = {appdiscovery.Path(item.target).name for item in walked}
    assert "direct.exe" in names
    assert "nested.exe" not in names


def test_a_packaged_application_is_never_stale() -> None:
    """Its target is a package identity, not a file, so the disk cannot answer."""
    from app.computer import appdiscovery
    packaged = entry("Calculator", launch="appid", target="Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
                     aumid="Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")
    assert appdiscovery.stale_ids((packaged,)) == []


# --- R1/F-AL8: the strategies the fixed table used to hold -------------------------------
def test_the_names_a_shell_reports_in_another_language_come_from_the_identity() -> None:
    """Measured: `shell:AppsFolder` reports the Store Calculator as "Calculator"."""
    raw = entry("Calculator", launch="appid",
                target="Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
                aumid="Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")
    enriched = appindex.apply_strategy(raw)
    assert enriched.id == raw.id                                  # identity never changes
    assert enriched.processes == ("CalculatorApp.exe",)
    assert enriched.frame_child == "CalculatorApp.exe"
    assert enriched.kind == appindex.KIND_MAIN
    assert appindex.apply_strategy(enriched) == enriched          # idempotent
    index = AppIndex(entries=(enriched,))
    for query in ("calculadora", "Calculadora", "calculator", "maquina de calcular"):
        assert index.resolve(query).kind == "single", query


def test_the_file_manager_is_opened_by_folder_and_found_by_class() -> None:
    """Measured: `explorer.exe` started while the shell runs exits 1 and opens no window."""
    raw = entry("Explorador de Arquivos", launch="appid", target="Microsoft.Windows.Explorer",
                aumid="Microsoft.Windows.Explorer", source="appsfolder")
    enriched = appindex.apply_strategy(raw)
    assert enriched.launch == "folder" and enriched.target == "Desktop"
    assert enriched.classes == ("CabinetWClass",) and enriched.processes == ("explorer.exe",)
    index = AppIndex(entries=(enriched,))
    for query in ("explorer", "Explorador", "explorador de arquivos"):
        assert index.resolve(query).kind == "single", query
    # The folder strategy names a known folder, not a file, so it is never stale.
    from app.computer import appdiscovery
    assert appdiscovery.target_exists(enriched) is True
    assert appdiscovery.stale_ids((enriched,)) == []


def test_an_entry_no_strategy_knows_is_returned_unchanged() -> None:
    raw = entry("Discord", exe="Discord.exe")
    assert appindex.apply_strategy(raw) == raw


def test_a_shell_identifier_and_its_file_are_one_application() -> None:
    """Measured: Discord was the shell's `com.squirrel.Discord.Discord` and a swept file."""
    shell = entry("Discord", launch="appid", target="com.squirrel.Discord.Discord",
                  aumid="com.squirrel.Discord.Discord", source="appsfolder")
    disk = entry("Discord", exe="Discord.exe", install_dir="C:/d")
    assert AppIndex(entries=(shell, disk)).resolve("discord").kind == "single"
    # But two *file* installs of one program stay a genuine question.
    one = entry("Discord", exe="Discord.exe", install_dir="C:/one")
    two = entry("Discord", exe="Discord.exe", install_dir="C:/two", target="C:/two/Discord.exe")
    assert AppIndex(entries=(one, two)).resolve("discord").kind == "ambiguous"


@pytest.mark.parametrize(("image", "kind"), [
    ("unins000.exe", appindex.KIND_UNINSTALLER),
    ("Uninstall Tool.exe", appindex.KIND_UNINSTALLER),
    ("setup.exe", appindex.KIND_INSTALLER),
    ("Update.exe", appindex.KIND_LAUNCHER),
    ("Discord.exe", appindex.KIND_UNKNOWN),
    ("Code.exe", appindex.KIND_UNKNOWN),
])
def test_an_executable_name_that_proves_a_role_is_classified(image: str, kind: str) -> None:
    from app.computer.appindex import classify_stem
    assert classify_stem(image) == kind


@pytest.mark.parametrize("image", ["plugin.dll", "My Plugin.exe", "extension.exe",
                                   "Renderer.exe", "telemetry.exe"])
def test_a_component_name_is_classified_as_a_part_of_a_program(image: str) -> None:
    from app.computer.appindex import classify_stem
    assert classify_stem(image) == appindex.KIND_AUXILIARY


def test_a_program_outranks_a_part_of_a_program_with_the_same_name() -> None:
    """R2: the role is a tie-breaker — it never reorders two different scores."""
    main = appindex.replace(entry("Widget", exe="Widget.exe"), kind=appindex.KIND_MAIN)
    part = entry("Widget", exe="Widget.exe", install_dir="C:/other")
    part = appindex.replace(part, kind=appindex.KIND_AUXILIARY)
    assert appindex.kind_rank(main) < appindex.kind_rank(part)
    ranked = appindex.rank("widget", (part, main))
    assert ranked[0].entry.kind == appindex.KIND_MAIN
    # A lower-scoring candidate is never promoted by its role.
    exact = appindex.rank("widget", (part, main))[0]
    assert exact.score >= ranked[1].score


def test_an_executable_is_windowed_or_a_console_program(tmp_path) -> None:
    """R2: the PE subsystem is what tells a tool from a windowed application."""
    from app.computer import appdiscovery

    gui = Path(appdiscovery.os.environ.get("WINDIR", "")) / "explorer.exe"
    console = Path(appdiscovery.os.environ.get("WINDIR", "")) / "System32" / "cmd.exe"
    if not gui.is_file() or not console.is_file():
        pytest.skip("a conhecida janela/console do Windows nao esta disponivel")
    assert appdiscovery.pe_subsystem(str(gui)) == "gui"
    assert appdiscovery.pe_subsystem(str(console)) == "console"
    # Nothing invented for a file that is not an executable.
    fake = tmp_path / "not-a-pe.exe"
    fake.write_bytes(b"MZ" + b"\x00" * 64)
    assert appdiscovery.pe_subsystem(str(fake)) == ""
    assert appdiscovery.classify_exe(str(console), source="sweep", trust="user_folder") \
        == appindex.KIND_CLI
    assert appdiscovery.classify_exe(str(gui), source="sweep", trust="user_folder") \
        == appindex.KIND_PORTABLE


def test_a_registered_application_is_not_probed() -> None:
    """R2: the probe is spent only where a tool is likely; a shortcut is a program."""
    from app.computer import appdiscovery
    assert appdiscovery.classify_exe("C:/nowhere/missing-helper-name.exe",
                                     source="start_menu") == appindex.KIND_MAIN


def test_only_a_shell_identifier_that_can_be_started_is_kept() -> None:
    """R2: `shell:AppsFolder` lists addresses and help pages beside the applications."""
    from app.computer.appdiscovery import launchable_appid
    assert launchable_appid("Microsoft.Windows.Explorer")
    assert launchable_appid("com.squirrel.Discord.Discord")
    assert launchable_appid("Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")
    assert not launchable_appid("https://gitforwindows.org/faq")
    assert not launchable_appid("googleplaygames://launch/?id=x")
    assert not launchable_appid(r"C:\Users\x\python\Doc\html\index.html")
    assert not launchable_appid(r"C:\Users\x\Desktop\7Zip\7-Zip\7-zip.chm")
    assert not launchable_appid("")


def test_what_a_completely_read_source_does_not_report_is_gone() -> None:
    """R2: a shell identifier has no file to become missing, so only the source can say."""
    from app.computer.appdiscovery import Discovery, merge_pass
    gone = entry("Stale Store App", launch="appid", target="Vendor.Stale_1!App",
                 aumid="Vendor.Stale_1!App", source="appsfolder")
    index = AppIndex(entries=(gone,))
    pass_ = Discovery(entries=(), costs=(), sweeps={}, sources={})
    assert len(merge_pass(index, pass_, validated={"appsfolder"})) == 0
    # A source that failed says nothing, so its entries stay.
    failed = Discovery(entries=(), costs=(), sweeps={}, sources={},
                       failed=frozenset({"appsfolder"}))
    assert len(merge_pass(index, failed, validated={"appsfolder"})) == 1
    # A source that was not read says nothing either.
    assert len(merge_pass(index, pass_, validated=set())) == 1


def test_the_progressive_sweep_is_never_treated_as_complete(tmp_path) -> None:
    """R2: one slice visits part of the tree, so a missing entry is not a removal."""
    from app.computer.appdiscovery import Discovery, merge_pass
    real = tmp_path / "Widget.exe"
    real.write_bytes(b"MZ")
    swept = appindex.replace(entry("Widget", exe="Widget.exe", source="sweep"),
                             target=str(real), id="exe:widget")
    index = AppIndex(entries=(swept,))
    pass_ = Discovery(entries=(), costs=(), sweeps={}, sources={})
    assert len(merge_pass(index, pass_, validated={"sweep"})) == 1
    # An entry whose file is gone does leave, whichever source described it.
    real.unlink()
    assert len(merge_pass(index, pass_, validated={"sweep"})) == 0


def test_a_classification_never_makes_an_entry_unlaunchable() -> None:
    """R2: the index still carries what it cannot classify, it only ranks it lower."""
    raw = entry("unins000", exe="unins000.exe")
    assert raw.kind == appindex.KIND_UNKNOWN          # discovery classifies, not `entry()`
    assert raw.launch in appindex.LAUNCH_KINDS


def test_the_index_is_iterable_and_sized() -> None:
    assert len(APP_INDEX) == len(INSTALLED)
    assert [item.name for item in APP_INDEX][0] == "Discord"


def test_merging_keeps_the_better_source_and_every_name() -> None:
    first = entry("Msedge", exe="msedge.exe", source="app_paths", target="C:/e/msedge.exe",
                  aliases=())
    second = entry("Microsoft Edge", exe="msedge.exe", source="registry", target="C:/e/msedge.exe",
                   aliases=("Edge",))
    merged = appindex.merge_entries((first, second))
    assert len(merged) == 1
    assert merged[0].source == "registry"
    assert {appindex.fold(alias) for alias in merged[0].aliases} >= {"edge", "msedge"}


def test_an_executable_finds_its_entry_so_the_launcher_knows_how_to_identify_it() -> None:
    """`by_exe` is what lets the HostAgent name only the executable.

    The window strategy discovery measured (`processes`, `frame_child`, `aumid`) travels
    with the entry, so the launcher learns it from the machine's own index instead of a
    per-application table (measured 2026-10-09: Steam's window belongs to a helper process,
    Discord's to a process the launcher does not know).
    """
    index = AppIndex(entries=(entry("Steam", exe="Steam.exe", processes=("Steam.exe",)),
                              entry("Calculator", exe="calc.exe", aumid="Calc!App",
                                    frame_child="CalculatorApp.exe")))
    assert index.by_exe("steam.exe") is not None
    assert index.by_exe("STEAM.EXE").frame_child == ""
    assert index.by_exe("calc.exe").aumid == "Calc!App"
    assert index.by_exe("nothing.exe") is None
    assert index.by_exe("") is None


def test_the_resolution_is_milliseconds_even_on_a_large_index() -> None:
    """2 400 entries are a real machine; a resolution must not walk them all.

    The first resolution pays for ``prepare``, the tables built once per entry tuple
    (measured 2026-10-09: ~30 ms for 2 400 entries), which is why the warm-up is there and
    what the bound below is not allowed to hide. What follows is the steady state.
    """
    import time

    def cost(entries: int) -> float:
        index = AppIndex(entries=tuple(entry(f"Application {number}", exe=f"app{number}.exe")
                                       for number in range(entries)))
        index.resolve("Application 7")      # builds the tables, once for this tuple
        started = time.perf_counter()
        for _ in range(50):
            assert index.resolve(f"Application {entries - 1}").kind == "single"
        return (time.perf_counter() - started) * 1000 / 50

    # The fastest of a few batches: this machine is shared, and load can only ever *add*
    # time, never remove it, so the minimum is the honest cost of one resolution while the
    # mean is a measurement of whatever else was running (seen: 0.57 ms idle against
    # 1.05 ms with the whole suite running — the same code).
    per_resolution_ms = min(cost(2400) for _ in range(5))
    # The bound is calibrated above that contention and stays far below anything a real
    # regression would cost: rebuilding the tables is ~30 ms and a per-call rescan of a
    # 2 400-entry index is milliseconds, so the paths this test exists to catch (a resolution
    # that re-prepares or rescans) still fail it by an order of magnitude.
    assert per_resolution_ms < 5.0, f"{per_resolution_ms:.3f} ms por resolucao"

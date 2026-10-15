"""F-AL2/F-AL4/F-AL6: the parent-side catalog — memory, persistence and progressive discovery.

The Windows calls (the structured sources, the sweep) are replaced here, because what is
being tested is not what a disk contains but what the catalog *does* with it: that a name
resolves from memory, that a choice and an authorisation survive the session, that a search
is stored instead of repeated, and that the background loop stops when it is told to.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.computer import appindex
from app.computer.appcatalog import AppCatalog
from app.computer.appindex import AppEntry, AppIndex


def entry(name: str, *, exe: str = "", target: str = "", launch: str = "exe",
          source: str = "start_menu", trust: str = "trusted",
          install_dir: str = "", aumid: str = "") -> AppEntry:
    exe = exe or f"{name}.exe"
    target = target or f"C:/Program Files/{name}/{exe}"
    return AppEntry(id=appindex.identity(launch, target, aumid), name=name, launch=launch,
                    target=target, exe=exe, source=source, install_dir=install_dir or
                    str(target).rsplit("/", 1)[0], aumid=aumid, trust=trust)


def catalog(tmp_path: Path, entries: tuple[AppEntry, ...] = ()) -> AppCatalog:
    path = appindex.default_index_path(tmp_path)
    if entries:
        AppIndex(entries=entries).save(path)
    built = AppCatalog(path)
    built.load()
    return built


def test_an_index_that_is_not_there_yet_is_cold_and_empty(tmp_path: Path) -> None:
    built = catalog(tmp_path)
    assert len(built.index) == 0
    assert built.cold is True


def test_a_saved_index_is_loaded_warm_so_the_second_session_is_instant(tmp_path: Path) -> None:
    first = catalog(tmp_path, (entry("Discord"),))
    first.save()
    started = time.perf_counter()
    second = catalog(tmp_path)
    assert (time.perf_counter() - started) * 1000 < 50
    assert second.cold is False
    assert second.resolve("Discord").kind == "single"


def test_a_choice_the_user_made_survives_the_session(tmp_path: Path) -> None:
    """F-AL4: what the user answered is the index's memory, not the chat's."""
    built = catalog(tmp_path, (entry("Discord", install_dir="C:/one",
                                     target="C:/one/Discord.exe"),
                               entry("Discord", install_dir="C:/two",
                                     target="C:/two/Discord.exe")))
    second = built.index.resolve("Discord").matches[1]
    built.learn("Discord", second.entry.id)
    reloaded = catalog(tmp_path)
    answer = reloaded.resolve("Discord")
    assert answer.kind == "single"
    assert answer.best is not None and answer.best.entry.id == second.entry.id
    assert reloaded.entry(second.entry.id).picks == 1  # type: ignore[union-attr]


def test_an_authorised_executable_is_not_asked_about_again(tmp_path: Path) -> None:
    """F-AL6: the consent a launch granted belongs to the entry, not to the process."""
    portable = entry("Portable Tool", source="user_folder", trust="user_folder",
                     target="D:/portable/tool.exe")
    built = catalog(tmp_path, (portable,))
    assert built.entry(portable.id).trust == "user_folder"  # type: ignore[union-attr]
    built.trust(portable.id)
    assert catalog(tmp_path).entry(portable.id).trust == "trusted"  # type: ignore[union-attr]


def test_forgetting_an_entry_removes_it_for_good(tmp_path: Path) -> None:
    gone = entry("Moved Away", target="D:/old/Moved Away.exe")
    built = catalog(tmp_path, (gone, entry("Kept")))
    built.forget(gone.id)
    reloaded = catalog(tmp_path)
    assert reloaded.entry(gone.id) is None
    assert reloaded.resolve("Kept").kind == "single"


def test_an_unreadable_index_file_is_not_a_failure(tmp_path: Path) -> None:
    """A corrupt file must never break a task: the catalog comes back empty."""
    path = appindex.default_index_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    built = AppCatalog(path)
    assert len(built.load()) == 0


def test_a_search_is_stored_so_the_next_request_is_a_lookup(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.computer.appdiscovery as discovery

    found = [entry("Telegram", source="sweep", target="D:/apps/telegram/Telegram.exe")]
    monkeypatch.setattr(discovery, "search_for", lambda name, **kwargs: list(found))
    built = catalog(tmp_path, (entry("Discord"),))
    assert built.resolve("Telegram").kind == "none"
    elapsed = built.search("Telegram")
    assert elapsed >= 0
    assert built.resolve("Telegram").kind == "single"
    assert catalog(tmp_path).resolve("Telegram").kind == "single"


def test_a_search_that_finds_nothing_does_not_touch_the_index(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.computer.appdiscovery as discovery

    monkeypatch.setattr(discovery, "search_for", lambda name, **kwargs: [])
    built = catalog(tmp_path)
    assert built.search("Nothing At All") >= 0
    assert len(built.index) == 0


def test_a_folder_the_user_points_at_is_scanned_and_remembered(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.computer.appdiscovery as discovery

    folder = tmp_path / "portable"
    folder.mkdir()
    pointed = [entry("My Tool", source="user_folder", trust="user_folder",
                     target=str(folder / "my.exe"))]
    monkeypatch.setattr(discovery, "discover_configured", lambda folders: list(pointed))
    built = catalog(tmp_path)
    adopted = built.adopt(str(folder))
    assert [item.name for item in adopted] == ["My Tool"]
    assert built.resolve("My Tool").kind == "single"
    assert str(folder) in built.configured
    reloaded = catalog(tmp_path)
    assert str(folder) in reloaded.configured


def test_an_executable_the_user_points_at_becomes_one_entry(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.computer.appdiscovery as discovery

    program = tmp_path / "mine.exe"
    program.write_bytes(b"MZ")
    made = entry("Mine", source="user_folder", trust="user_folder", target=str(program))
    monkeypatch.setattr(discovery, "entry_for_path", lambda value: made)
    built = catalog(tmp_path)
    assert [item.id for item in built.adopt(str(program))] == [made.id]
    assert built.resolve("Mine").kind == "single"


def test_a_directory_that_says_nothing_is_not_adopted(tmp_path: Path) -> None:
    built = catalog(tmp_path)
    assert built.adopt(str(tmp_path / "nao-existe")) == []
    assert built.adopt("") == []


def test_a_sweep_slice_merges_what_it_found_and_reports_that_it_is_done(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F-AL6: the sweep is resumable state, and its progress lives in the index."""
    import app.computer.appdiscovery as discovery

    found = [entry("Found By Sweep", source="sweep", target="D:/sweep/found.exe")]
    state = {"roots": {"c:": {"pending": [], "finished_ms": 1.0}}}
    monkeypatch.setattr(discovery, "sweep_step",
                        lambda index, **kwargs: (list(found), state))
    built = catalog(tmp_path)
    assert built.sweep_slice() is False
    assert len(built.index) == 1
    assert built.index.sweeps["roots"]["c:"]["pending"] == []
    reloaded = catalog(tmp_path)
    assert reloaded.resolve("Found By Sweep").kind == "single"
    # A root that is still walking says so, which is what keeps the loop running.
    monkeypatch.setattr(discovery, "sweep_step",
                        lambda index, **kwargs: ([], {"roots": {"c:": {"pending": ["d:"]}}}))
    assert built.sweep_slice() is True


def test_the_background_loop_starts_once_and_stops(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    import app.computer.appdiscovery as discovery

    calls: list[bool] = []

    def refresh_index(index, *, configured=(), force=False, include_sweep=True,
                      budget_ms=0.0):
        calls.append(force)
        return index

    monkeypatch.setattr(discovery, "refresh_index", refresh_index)
    monkeypatch.setattr(discovery, "sweep_step",
                        lambda index, **kwargs: ([], {"roots": {}}))
    built = catalog(tmp_path)
    built.start()
    thread = built._thread
    built.start()          # a second start must not spawn a second thread
    assert built._thread is thread
    deadline = time.perf_counter() + 5
    while not calls and time.perf_counter() < deadline:
        time.sleep(0.02)
    built.stop()
    assert calls and calls[0] is True     # the structured sources are read first
    assert not thread.is_alive()

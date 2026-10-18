"""The launcher's tool surface, as the agent receives it (R3).

`find_application` and `launch_application` are one contract: the descriptions tell the
HostAgent to hand the chosen result to the launcher. These tests hold that contract — the
target must be launchable as returned, and the identity of the application must be accepted
so an application that is already running is recognised instead of started twice.

Measured before the fix (2026-10-08): the search returned a bare AUMID, and the launcher
refused it with "Use nome/caminho de executável .exe, pasta existente ou ms-settings:".
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor" / "UFO"))

import psutil  # noqa: E402
from fastmcp import Client  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from app.computer import windows_tools  # noqa: E402
from app.computer.appindex import AppEntry, AppIndex, default_index_path  # noqa: E402
from app.computer.windows_tools import WindowsTools  # noqa: E402


def installed(name: str, *, launch: str, target: str, exe: str = "", aumid: str = "",
              source: str = "appsfolder") -> AppEntry:
    return AppEntry(id=f"{launch}:{target.casefold()}", name=name, aliases=(),
                    launch=launch, target=target, exe=exe, aumid=aumid, source=source,
                    processes=((exe,) if exe else ()))


ENTRIES = (
    installed("Notepad", launch="appid", target="Microsoft.WindowsNotepad_8wekyb3d8bbwe!App",
              aumid="Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"),
    installed("Spotify", launch="exe", target=r"C:\Users\TestUser\AppData\Roaming\Spotify\Spotify.exe",
              exe="spotify.exe", source="sweep"),
    installed("Meus Documentos", launch="folder", target=r"C:\Users\TestUser\Documents",
              source="configured"),
)


def tools_at(tmp_path: Path, monkeypatch) -> WindowsTools:
    """A tool layer whose index is this test's own file, not the machine's."""
    path = default_index_path(tmp_path)
    AppIndex(entries=ENTRIES).save(path)
    monkeypatch.setattr(windows_tools, "default_index_path", lambda _root: path)

    async def confirm(_: str) -> bool:
        return True

    return WindowsTools(object(), confirm)


def test_the_search_returns_a_target_the_launcher_accepts(tmp_path: Path,
                                                          monkeypatch) -> None:
    tools = tools_at(tmp_path, monkeypatch)
    results = {item["name"]: item for item in tools.find_application("notepad", 5)["results"]}
    assert results["Notepad"]["target"] == "appid:Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"

    results = {item["name"]: item for item in tools.find_application("Spotify", 5)["results"]}
    assert results["Spotify"]["target"].startswith("exe:")
    assert results["Spotify"]["aumid"] == ""

    results = {item["name"]: item for item in tools.find_application("documentos", 5)["results"]}
    # An existing folder is given to the launcher bare: it is accepted as a path, and a
    # prefix the launcher does not implement would be read as part of the path.
    assert results["Meus Documentos"]["target"] == r"C:\Users\TestUser\Documents"


def test_the_worker_searches_the_index_of_the_state_root_its_parent_names(
        tmp_path: Path, monkeypatch) -> None:
    """A sandboxed session's worker reads the sandbox index, never the machine's own."""
    AppIndex(entries=ENTRIES).save(default_index_path(tmp_path))
    monkeypatch.setenv("LUNMI_STATE_ROOT", str(tmp_path))

    async def confirm(_: str) -> bool:
        return True

    tools = WindowsTools(object(), confirm)
    names = {item["name"] for item in tools.find_application("Spotify", 5)["results"]}
    assert "Spotify" in names


def test_a_window_belonging_to_a_child_process_proves_the_application_opened(monkeypatch) -> None:
    """Steam's window is owned by `steamwebhelper.exe`, a child of the process started.

    The launcher accepts it as the same application being up; a process outside that tree
    is never accepted, however it appeared on screen.
    """
    tree = {10: 1, 11: 10, 12: 11}  # 1 -> launched, 10/11/12 -> its descendants

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def ppid(self) -> int:
            if self.pid not in tree:
                raise psutil.NoSuchProcess(self.pid)
            return tree[self.pid]

    monkeypatch.setattr(windows_tools.psutil, "Process", FakeProcess)
    assert windows_tools.descends_from(1, 1)
    assert windows_tools.descends_from(10, 1)
    assert windows_tools.descends_from(11, 1)
    assert windows_tools.descends_from(12, 1)
    assert not windows_tools.descends_from(1, 10)
    assert not windows_tools.descends_from(99, 1)  # a process that ended: no claim either way
    assert not windows_tools.descends_from(0, 1)
    # The walk is bounded: a chain longer than the depth limit is not followed forever.
    assert not windows_tools.descends_from(12, 1, depth=1)


def test_a_window_owned_by_a_process_the_application_started_is_its_window(monkeypatch) -> None:
    """Measured (2026-10-09): Steam's window belongs to `steamwebhelper.exe`, a child of the
    `steam.exe` that owns the application, so no window carries the image's own name.

    The family is walked only when nothing matched by name, so an application that owns its
    window directly keeps the fast, exact match — and an unrelated process never becomes one.
    """
    listing = [
        {"handle": 2, "pid": 200, "process": "steamwebhelper.exe", "class": "SDL_app",
         "title": "Iniciar a sessão no Steam", "visible": True, "minimized": False,
         "active": True},
        {"handle": 3, "pid": 300, "process": "vivaldi.exe", "class": "Chrome_WidgetWin_1",
         "title": "Freebuff", "visible": True, "minimized": False, "active": False},
    ]
    tree = {200: 100}  # steamwebhelper 200 is a child of the steam.exe 100 that owns Steam

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def ppid(self) -> int:
            if self.pid not in tree:
                raise psutil.NoSuchProcess(self.pid)
            return tree[self.pid]

    walked: list[set[str]] = []
    running = {"steam.exe": 100, "vivaldi.exe": 300}

    def roots(images: object) -> list[int]:
        walked.append(set(images))  # asks the live process list for those image names
        return [pid for name, pid in running.items() if name in set(images)]

    monkeypatch.setattr(windows_tools, "windows", lambda: listing)
    monkeypatch.setattr(windows_tools.psutil, "Process", FakeProcess)
    monkeypatch.setattr(windows_tools, "process_roots", roots)

    async def confirm(_: str) -> bool:
        return True

    tools = WindowsTools(object(), confirm)
    assert [window["handle"] for window in tools.target_windows(["steam.exe"])] == [2]
    assert tools.target_windows(["outro.exe"]) == []
    # A direct match still wins and never walks the process tree: the walk is the expensive
    # answer, and an image name already answered the question (measured 2026-10-09: walking
    # cost ~20 s for one window list against the 11 `code.exe` of a VS Code session).
    walked.clear()
    assert [window["handle"] for window in tools.target_windows(["vivaldi.exe"])] == [3]
    assert walked == []


def test_a_transition_is_never_decided_by_a_settled_unsatisfied_state() -> None:
    """A window *transition* is a change, so two identical polls decide nothing.

    Measured (2026-10-09): "fecha o discord" dispatched the close, the window handle was
    still alive 435 ms later, and the wait declared "Estado final da janela não confirmado"
    while the close was still in flight — a false failure on a request that was already
    happening. A window *state* (foreground, iconic) keeps the settled-state rule.
    """
    from app.computer.windows_tools import VERIFY_BUDGET_CEILING, WindowsTools

    async def confirm(_: str) -> bool:
        return True

    tools = WindowsTools(object(), confirm)

    def stuck() -> tuple[bool, object]:
        return False, ("alive", False)

    decided, polls, _, _ = asyncio.run(tools._await_condition("window", stuck, floor_ms=0.0))
    assert decided is False and polls >= 2      # a state to hold: the repeat decides it

    seen = {"polls": 0}

    def closing() -> tuple[bool, object]:
        seen["polls"] += 1
        return seen["polls"] >= 3, seen["polls"]

    closed, polls, _, budget_ms = asyncio.run(
        tools._await_condition("transition", closing, settle_samples=0))
    assert closed and polls == 3
    assert budget_ms == VERIFY_BUDGET_CEILING * 1000


def test_a_window_of_an_instance_that_was_already_running_proves_the_launch(monkeypatch) -> None:
    """Measured (2026-10-09): `steam.exe` started while Steam sat in the background handed
    the request to the instance already running (exit 0, 205 ms), and the window that then
    appeared belonged to the pre-existing `steamwebhelper.exe` — not to a descendant of the
    process this call had just started.

    The launcher must accept that window as the application being up. Before this it polled
    its whole budget and reported a timeout on a request that had already succeeded.
    """
    import app.computer.windows_tools as windows_tools
    from app.computer.windows_tools import WindowsTools

    listing: list[dict[str, object]] = []
    tree = {200: 100}  # the helper 200 is a child of the steam.exe 100 already running

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def ppid(self) -> int:
            if self.pid not in tree:
                raise psutil.NoSuchProcess(self.pid)
            return tree[self.pid]

    class Handoff:
        pid = 300        # the instance this call started: it exits 0 and owns no window

        def poll(self) -> int:
            return 0

    def start(command: list[str], **kwargs: object) -> Handoff:
        # The running instance answers by raising its helper's window.
        listing.append({"handle": 7, "pid": 200, "process": "steamwebhelper.exe",
                        "class": "SDL_app", "title": "Steam", "visible": True,
                        "minimized": False, "active": False})
        return Handoff()

    class Native:
        element_info = SimpleNamespace(name="Steam")

    class Desktop:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def window(self, handle: int = 0) -> "Desktop":
            assert handle == 7
            return self

        def wrapper_object(self) -> Native:
            return Native()

    monkeypatch.setattr(windows_tools, "windows", lambda: list(listing))
    monkeypatch.setattr(windows_tools.psutil, "Process", FakeProcess)
    monkeypatch.setattr(windows_tools, "process_roots",
                        lambda images: [100] if "steam.exe" in set(images) else [])
    monkeypatch.setattr(windows_tools.shutil, "which", lambda name: r"C:\Steam\steam.exe")
    monkeypatch.setattr(windows_tools.subprocess, "Popen", start)
    monkeypatch.setattr("pywinauto.Desktop", Desktop)

    async def confirm(_: str) -> bool:
        return True

    tools = WindowsTools(object(), confirm)
    monkeypatch.setattr(WindowsTools, "installed_apps", lambda self: AppIndex(entries=()))
    evidence = asyncio.run(tools.launch("steam.exe"))
    assert evidence["verified"] and evidence["pid"] == 200
    assert evidence["process"].casefold() == "steamwebhelper.exe"


def _stub_close(monkeypatch, window: dict[str, object], live: dict[str, bool], execute) -> None:
    """The real tool against a stubbed vendor mutation engine and a stubbed desktop.

    Only the two seams the tool does not own are replaced: the vendor's `AtomicCommand`
    (which is not on the test path) and the Win32 window state. Everything between them —
    the identity rule, the dispatch, and the verification the close is judged by — is the
    code that runs on the user's machine.
    """
    import sys
    import types

    import app.computer.windows_tools as windows_tools

    class Receiver:
        def __init__(self, *args: object) -> None:
            pass

    class Command:
        def __init__(self, *args: object) -> None:
            pass

        def execute(self) -> None:
            execute()

    class Native:
        def window_text(self) -> str:
            return str(window.get("title") or "")

    class Desktop:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def window(self, handle: int = 0) -> "Desktop":
            return self

        def wrapper_object(self) -> Native:
            return Native()

    stub = types.ModuleType("ufo.automator.ui_control.controller")
    stub.ControlReceiver = Receiver  # type: ignore[attr-defined]
    stub.AtomicCommand = Command  # type: ignore[attr-defined]
    for name in ("ufo", "ufo.automator", "ufo.automator.ui_control",
                 "ufo.automator.ui_control.controller"):
        monkeypatch.setitem(sys.modules, name, stub if name.endswith(".controller")
                            else types.ModuleType(name))
    monkeypatch.setattr(windows_tools, "windows", lambda: [window])
    monkeypatch.setattr(windows_tools.user32, "IsWindow", lambda handle: live["alive"])
    monkeypatch.setattr(windows_tools.user32, "IsWindowVisible", lambda handle: live["visible"])
    monkeypatch.setattr(windows_tools.user32, "IsIconic", lambda handle: False)
    monkeypatch.setattr("pywinauto.Desktop", Desktop)


def test_a_close_that_hides_the_window_is_a_close(monkeypatch) -> None:
    """Measured (2026-10-09): Discord answers the close by hiding its window and keeping the
    application in the notification area, so the handle stays alive — `IsWindow` is True
    forever and a close verified only by the handle being gone can *never* be confirmed.

    That is exactly the failure the user reported as "fecha o discord → FAILED: Estado final
    da janela não confirmado": the close had happened, the window was hidden, and the
    verification kept demanding a destroyed handle for its whole budget.
    """
    from app.computer.windows_tools import WindowsTools

    window = {"handle": 5, "pid": 400, "process": "Discord.exe",
              "class": "Chrome_WidgetWin_1", "title": "@Chapisco - Discord",
              "visible": True, "minimized": False, "active": True}
    live = {"alive": True, "visible": True}

    def hide() -> None:
        live["visible"] = False      # the window leaves the desktop; the handle does not go

    _stub_close(monkeypatch, window, live, hide)

    async def approve(_: str) -> bool:
        return True

    tools = WindowsTools(object(), approve)
    evidence = asyncio.run(tools.window_operation("close", processes=["Discord.exe"],
                                                 label="Discord"))
    assert evidence["verified"]
    closed = evidence["windows"][0]
    assert closed["state"] == "hidden" and closed["still_running"] is True
    assert closed["readiness_ms"] < 1000, "a hidden close is confirmed at once, not on the budget"


def test_a_close_the_window_refuses_fails_with_the_state_it_is_still_in(monkeypatch) -> None:
    """A close that leaves the window *visible* is a failure, and it says why.

    The application did not accept it — an unsaved document is the common case — and that is
    a fact about the window, reported as one. The record survives the error so the failure is
    traceable instead of arriving as an unconfirmed "final state".
    """
    import app.computer.windows_tools as windows_tools
    from app.computer.windows_tools import WindowsTools

    window = {"handle": 6, "pid": 401, "process": "Notepad.exe", "class": "Notepad",
              "title": "*sem titulo - Bloco de notas", "visible": True, "minimized": False,
              "active": True}
    live = {"alive": True, "visible": True}
    _stub_close(monkeypatch, window, live, lambda: None)
    # The transition budget is the wait this failure must spend; shortened here to the
    # smallest honest one so the suite does not sleep for it.
    monkeypatch.setattr(windows_tools, "VERIFY_BUDGET_DEFAULT",
                        {**windows_tools.VERIFY_BUDGET_DEFAULT, "transition": 0.05})

    async def approve(_: str) -> bool:
        return True

    tools = WindowsTools(object(), approve)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(tools.window_operation("close", processes=["Notepad.exe"], label="Notepad"))
    assert "continua visível" in str(failure.value)
    assert "processo vivo true" in str(failure.value)
    recorded = tools.evidence[-1]
    assert recorded["operation"] == "close" and recorded["verified"] is False
    assert recorded["windows"][0]["visible"] is True


def test_focus_brings_an_open_window_forward_and_proves_the_foreground(monkeypatch) -> None:
    """FASE 5.2: "foque no <app>" é um pedido explícito de primeiro plano — e é ele que prova."""
    import app.computer.windows_tools as windows_tools
    from app.computer.windows_tools import WindowsTools

    window = {"handle": 9, "pid": 500, "process": "Notepad.exe", "class": "Notepad",
              "title": "Untitled - Notepad", "visible": True, "minimized": False, "active": False}
    monkeypatch.setattr(windows_tools, "windows", lambda: [window])
    monkeypatch.setattr(windows_tools, "foreground_window", lambda handle: True)

    async def approve(_: str) -> bool:
        return True

    tools = WindowsTools(object(), approve)
    evidence = asyncio.run(tools.window_operation("focus", processes=["Notepad.exe"],
                                                 label="Bloco de Notas"))
    assert evidence["operation"] == "focus" and evidence["verified"] and evidence["focused"]
    assert evidence["handle"] == 9 and evidence["title"] == "Untitled - Notepad"
    assert tools.pending_ui is False


def test_focus_refused_by_windows_fails_saying_the_window_is_still_open(monkeypatch) -> None:
    import app.computer.windows_tools as windows_tools
    from app.computer.windows_tools import WindowsTools

    window = {"handle": 10, "pid": 501, "process": "Notepad.exe", "class": "Notepad",
              "title": "Untitled - Notepad", "visible": True, "minimized": False, "active": False}
    monkeypatch.setattr(windows_tools, "windows", lambda: [window])
    monkeypatch.setattr(windows_tools, "foreground_window", lambda handle: False)

    async def approve(_: str) -> bool:
        return True

    tools = WindowsTools(object(), approve)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(tools.window_operation("focus", processes=["Notepad.exe"], label="Bloco de Notas"))
    assert "primeiro plano" in str(failure.value)
    assert tools.evidence[-1]["operation"] == "focus" and tools.evidence[-1]["verified"] is False


def test_the_process_family_of_an_image_is_the_running_processes_of_that_image() -> None:
    """The family lookup names real running processes, and it is the fast enumeration.

    Measured on this machine (2026-10-09): psutil's per-process name lookup costs ~1 000 ms
    for its ~350 processes against ~8 ms for `EnumProcesses` + `QueryFullProcessImageName`,
    and this runs on the path of every launch whose window is not the application's own
    image. The check is against the live machine on purpose — the image name and the pid
    this very test runs under are the two facts an implementation may get wrong without
    any of it noticing.
    """
    import os
    import time

    started = time.perf_counter()
    roots = windows_tools.process_roots({"python.exe", "PYTHON.EXE"})
    elapsed_ms = (time.perf_counter() - started) * 1000
    images = dict(windows_tools.running_images())
    assert os.getpid() in roots
    assert all(images.get(pid) == "python.exe" for pid in roots)
    assert elapsed_ms < 500, f"a process sweep took {elapsed_ms:.0f} ms"
    # An image nobody runs is nobody: an empty answer is a real answer, not a failure.
    assert windows_tools.process_roots({"nao-existe-este-processo.exe"}) == []


def test_the_agent_is_offered_the_identity_of_the_application() -> None:
    """The tool the HostAgent calls must accept what its own description tells it to pass."""
    async def scenario() -> list[str]:
        server = WindowsTools(object(), confirm).server()
        async with Client(server) as client:
            listing = {tool.name: tool.inputSchema for tool in await client.list_tools()}
        return sorted(listing["launch_application"].get("properties", {}))

    async def confirm(_: str) -> bool:
        return True

    names = asyncio.run(scenario())
    assert "expected" in names and "aumid" in names


# --------------------------------------------------- T5: leitura de arquivo próprio --


def test_read_returns_the_text_with_hash_and_size_from_the_same_bytes(tmp_path: Path) -> None:
    """A leitura é verificável: hash, tamanho e mtime vêm dos bytes que produziram o texto."""
    import hashlib

    from app.computer.windows_tools import read_text_file

    target = tmp_path / "nota.txt"
    content = "LUNMI-F2 leitura própria\nacentuação 🌙\n"
    target.write_bytes(content.encode("utf-8"))

    result = read_text_file(str(target))
    assert result["text"] == content
    assert result["bytes"] == len(content.encode("utf-8"))
    assert result["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert result["modified_ns"] == target.stat().st_mtime_ns
    assert result["encoding"] == "utf-8" and result["truncated"] is False
    assert result["verified"] is True


def test_read_refuses_binary_oversize_and_special_paths_without_touching_ui(tmp_path: Path) -> None:
    """Recusa com motivo, sem UI e sem truncar: nada aqui pode virar um quase-certo."""
    import asyncio

    from app.computer.windows_tools import MAX_READ_BYTES, FileReadError, WindowsTools, read_text_file

    binary = tmp_path / "bin.dat"
    binary.write_bytes(b"\xff\xfe\xfd")            # UTF-8 inválido, sem NUL
    nul = tmp_path / "nul.dat"
    nul.write_bytes(b"a\x00b")                      # NUL: sinal forte de binário
    big = tmp_path / "grande.txt"
    big.write_bytes(b"a" * (MAX_READ_BYTES + 1))

    with pytest.raises(FileReadError) as failure:
        read_text_file(str(binary))
    assert "UTF-8" in str(failure.value)
    with pytest.raises(FileReadError) as failure:
        read_text_file(str(nul))
    assert "binário" in str(failure.value)
    with pytest.raises(FileReadError) as failure:
        read_text_file(str(big))
    assert str(MAX_READ_BYTES) in str(failure.value)
    with pytest.raises(FileReadError) as failure:
        read_text_file(str(tmp_path))
    assert "arquivo regular" in str(failure.value)
    with pytest.raises(FileReadError) as failure:
        read_text_file(str(tmp_path / "ausente.txt"))
    assert "não existe" in str(failure.value)

    # Pela ferramenta: leitura boa não pede consentimento nem deixa UI pendente; leitura recusada
    # também não (a recusa é da leitura, não um pedido de permissão).
    asked: list[str] = []

    async def approve(message: str) -> bool:
        asked.append(message)
        return True

    ok_file = tmp_path / "ok.txt"
    ok_file.write_bytes("LUNMI-F2 leitura ok\n".encode("utf-8"))
    tools = WindowsTools(object(), approve)
    read_ok = asyncio.run(tools.filesystem("read", str(ok_file)))
    assert read_ok["text"] == "LUNMI-F2 leitura ok\n"
    with pytest.raises(FileReadError):
        asyncio.run(tools.filesystem("read", str(big)))
    with pytest.raises(FileReadError):
        asyncio.run(tools.filesystem("read", str(tmp_path / "ausente.txt")))
    assert asked == []
    assert tools.pending_ui is False


def test_read_evidence_keeps_metadata_only_never_the_file_text(tmp_path: Path) -> None:
    """`logs/tasks.jsonl` recebe `asdict(TaskResult)`: o texto do usuário não pode ir junto."""
    import asyncio

    from app.computer.windows_tools import WindowsTools

    target = tmp_path / "nota.txt"
    secret = "LUNMI-F2-DADO-DO-USUARIO-NAO-PODE-IR-PARA-LOG"
    target.write_bytes(f"{secret}\n".encode("utf-8"))

    async def approve(_: str) -> bool:
        return True

    tools = WindowsTools(object(), approve)
    result = asyncio.run(tools.filesystem("read", str(target)))
    assert result["text"] == f"{secret}\n"          # quem pediu recebe o conteúdo
    assert len(tools.evidence) == 1
    evidence = tools.evidence[-1]
    assert "text" not in evidence
    assert secret not in str(evidence)
    assert evidence["sha256"] == result["sha256"] and evidence["verified"] is True


def test_read_fails_when_the_file_changes_during_the_read(tmp_path: Path, monkeypatch) -> None:
    """Mudança durante a leitura nunca é sucesso: o texto deixaria de ser destes bytes."""
    import asyncio
    import os
    from types import SimpleNamespace

    from app.computer.windows_tools import WindowsTools

    target = tmp_path / "nota.txt"
    target.write_bytes(b"conteudo inicial\n")
    real_fstat = os.fstat
    calls = {"count": 0}

    def fake_fstat(descriptor: int):
        calls["count"] += 1
        real = real_fstat(descriptor)
        if calls["count"] == 1:
            return real
        # Segundo `fstat` do mesmo handle: tamanho/mtime diferentes = o arquivo mudou.
        return SimpleNamespace(st_mode=real.st_mode, st_size=real.st_size + 1,
                               st_mtime_ns=real.st_mtime_ns + 1)

    monkeypatch.setattr(windows_tools.os, "fstat", fake_fstat)

    async def approve(_: str) -> bool:
        return True

    tools = WindowsTools(object(), approve)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(tools.filesystem("read", str(target)))
    assert "mudou durante a leitura" in str(failure.value)


def test_read_refuses_a_different_file_swapped_in_during_open(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "target.txt"
    other = tmp_path / "other.txt"
    target.write_bytes(b"expected")
    other.write_bytes(b"unexpected")
    real_open = windows_tools.os.open

    def swapped_open(path, flags):
        return real_open(other, flags)

    monkeypatch.setattr(windows_tools.os, "open", swapped_open)
    with pytest.raises(RuntimeError, match="identidade"):
        windows_tools.read_text_file(str(target))


def test_read_refuses_a_link_to_another_file(tmp_path: Path) -> None:
    import os

    from app.computer.windows_tools import FileReadError, read_text_file

    real = tmp_path / "real.txt"
    real.write_bytes(b"conteudo\n")
    link = tmp_path / "link.txt"
    try:
        os.symlink(str(real), str(link))
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("este Windows não permite criar link simbólico sem privilégio")

    with pytest.raises(FileReadError) as failure:
        read_text_file(str(link))
    assert "link" in str(failure.value)

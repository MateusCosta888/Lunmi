"""Execute the production NSIS hooks without installing or changing registry keys.

Directory/visibility: the setup destination for manual setups and updates, and that an update
started with /S shows its progress window. Components: the real `lunmiStage`/`customInstall`
macros extract tiny Python/voice archives beside a scratch installation, skip unchanged ones,
remove stale versions and stop with code 2 on a broken archive without leaving a partial tree.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from components import SEVEN_ZIP_ARGS, seven_zip  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def _tools() -> tuple[Path, Path, Path]:
    cache = Path(os.environ.get('ELECTRON_BUILDER_CACHE',
                               str(Path(os.environ['LOCALAPPDATA']) / 'electron-builder/Cache')))
    compiler = next(p for p in cache.glob('nsis-*/**/makensis.exe') if (p.parent / 'Include').is_dir())
    plugins = next(cache.glob('nsis-resources-*/**/x86-unicode/StdUtils.dll')).parent
    return compiler, plugins, REPO / 'desktop/node_modules/app-builder-lib/templates/nsis'


def _header(work: Path, out: Path, plugins: Path, templates: Path, updated: bool) -> str:
    return f'''
!define LUNMI_SETUP_LOG "{work / 'setup.log'}"
Unicode true
Name "Lunmi installer verification"
OutFile "{out}"
RequestExecutionLevel user
SilentInstall silent
!addincludedir "{templates}"
!addincludedir "{templates / 'include'}"
!addplugindir /x86-unicode "{plugins}"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "StdUtils.nsh"
!include "{templates / 'multiUser.nsh'}"
!define APP_FILENAME "Lunmi"
!define BUILD_RESOURCES_DIR "{work / 'build'}"
!define isUpdated '{'1 = 1' if updated else '0 = 1'}'
!define isForceRun '0 = 1'
Var launchLink
Var appExe
Var newDesktopLink
Var newStartMenuLink
Var visible
Var lunmiRuntime
Var lunmiTarget
Var lunmiValue
Var lunmiOut
Var lunmiHandle
!include "{REPO / 'desktop/build/installer.nsh'}"
'''


def _compile(compiler: Path, source: Path, text: str) -> None:
    source.write_text(text, encoding='utf-8')
    built = subprocess.run([str(compiler), '-V1', str(source)], capture_output=True)
    if built.returncode:
        raise RuntimeError((built.stdout + built.stderr).decode('utf-8', 'replace'))


def _components(work: Path, python_files: dict[str, str], corrupt: str | None = None) -> None:
    """Tiny archives with the production 7z options and the include `components.py` writes.

    `corrupt` stores the Python archive uncompressed and flips one byte of that text, so the
    sentinel extracts fine and a later file fails its CRC."""
    tool = seven_zip()
    out = work / 'build/components'
    out.mkdir(parents=True, exist_ok=True)
    defines = [f'!define LUNMI_SEVEN_ZIP "{tool}"']
    for name, files in (('python', python_files), ('voice', {'runtime/python.exe': 'voice'})):
        source = work / f'src-{name}'
        for relative, text in files.items():
            (source / relative).parent.mkdir(parents=True, exist_ok=True)
            (source / relative).write_text(text, encoding='utf-8')
        archive = out / f'{name}.7z'
        archive.unlink(missing_ok=True)
        options = [*SEVEN_ZIP_ARGS, '-mx=0'] if corrupt and name == 'python' else SEVEN_ZIP_ARGS
        subprocess.run([tool, *options, str(archive), '.'], cwd=source, check=True, stdout=subprocess.DEVNULL)
        if corrupt and name == 'python':
            data = archive.read_bytes()
            at = data.index(corrupt.encode())
            archive.write_bytes(data[:at] + bytes([data[at] ^ 1]) + data[at + 1:])
        key = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]
        defines += [f'!define LUNMI_{name.upper()}_KEY "{key}"',
                    f'!define LUNMI_{name.upper()}_ARCHIVE "{archive}"', f'!define LUNMI_{name.upper()}_MB 1',
                    f'!define LUNMI_{name.upper()}_ARCHIVE_MB 1']
        for relative in files:
            (source / relative).unlink()
    (out / 'lunmi-components.nsh').write_text('\n'.join(defines) + '\n', encoding='utf-8')


def _keys(work: Path) -> dict[str, str]:
    text = (work / 'build/components/lunmi-components.nsh').read_text(encoding='utf-8')
    return {line.split()[1]: line.split('"')[1] for line in text.splitlines() if '_KEY ' in line}


def directory_checks(compiler: Path, plugins: Path, templates: Path, work: Path) -> list[dict[str, object]]:
    expected = str(Path(os.environ['LOCALAPPDATA']) / 'Programs/Lunmi')
    cases: list[dict[str, object]] = []
    for updated in (False, True):
        _compile(compiler, work / 'probe.nsi', _header(work, work / 'probe.exe', plugins, templates, updated) + r'''
Function .onInit
  StrCpy $INSTDIR "C:\Legacy Alpha QA\Lunmi"
  !insertmacro GetDParameter $0
  ${If} $0 != ""
    StrCpy $INSTDIR $0
  ${EndIf}
  !insertmacro customInit
  StrCpy $visible "visible"
  ${If} ${Silent}
    StrCpy $visible "silent"
  ${EndIf}
  SetSilent silent ; the probe has no pages; never wait for a click
FunctionEnd
Section
  FileOpen $0 "$EXEDIR\destination.txt" w
  FileWriteUTF16LE $0 "$INSTDIR|$visible"
  FileClose $0
  ; A successful uninstall makes the replacement check a no-op.
  ClearErrors
  StrCpy $R0 0
  !insertmacro customUnInstallCheck
  ; The builder sets the error flag when it cannot launch the old uninstaller at all.
  ${If} $INSTDIR == "C:\Uninstaller Missing"
    SetErrors
    !insertmacro customUnInstallCheck
  ${EndIf}
SectionEnd
''')
        for explicit in (None, str(work / 'custom directory' / 'Lunmi')):
            args = [str(work / 'probe.exe')]
            if explicit:
                args.append('/D=' + explicit)
            subprocess.run(args, check=True, timeout=20)
            observed, visibility = (work / 'destination.txt').read_text(encoding='utf-16-le').lstrip('\ufeff').split('|')
            wanted = explicit or (r'C:\Legacy Alpha QA\Lunmi' if updated else expected)
            assert observed == wanted, (updated, explicit, observed, wanted)
            # The probe runs with /S semantics: only an app update may turn the window back on.
            assert visibility == ('visible' if updated else 'silent'), (updated, explicit, visibility)
            cases.append(dict(updated=updated, explicit_directory=bool(explicit), shows_progress=updated, passed=True))
        # The Lunmi setup window (--lunmi-ui) already shows progress: NSIS must stay hidden.
        subprocess.run([str(work / 'probe.exe'), '--lunmi-ui'], check=True, timeout=20)
        visibility = (work / 'destination.txt').read_text(encoding='utf-16-le').lstrip('﻿').split('|')[1]
        assert visibility == 'silent', (updated, 'lunmi-ui', visibility)
        cases.append(dict(updated=updated, lunmi_ui=True, shows_progress=False, passed=True))
        missing = subprocess.run([str(work / 'probe.exe'), r'/D=C:\Uninstaller Missing'], timeout=20).returncode
        assert missing == 2, (updated, missing)
        cases.append(dict(updated=updated, uninstaller_launch_failure_stops=True, passed=True))
    return cases


def component_checks(compiler: Path, plugins: Path, templates: Path, work: Path) -> list[str]:
    install = work / 'scratch install'
    runtime = work / 'scratch install-runtime'
    stage = r'''
Function .onInit
  StrCpy $INSTDIR "''' + str(install) + r'''"
FunctionEnd
Section
  !insertmacro lunmiCheckPath
  StrCpy $lunmiRuntime "$INSTDIR-runtime"
  InitPluginsDir
  CreateDirectory $lunmiRuntime
  File "/oname=$PLUGINSDIR\7za.exe" "${LUNMI_SEVEN_ZIP}"
  !insertmacro lunmiCheckSpace
  !insertmacro lunmiStage python ${LUNMI_PYTHON_KEY} "${LUNMI_PYTHON_ARCHIVE}" "python.exe"
  !insertmacro lunmiStage voice ${LUNMI_VOICE_KEY} "${LUNMI_VOICE_ARCHIVE}" "runtime\python.exe"
  !insertmacro customInstall
SectionEnd
'''

    def build_and_run(python_files: dict[str, str], corrupt: str | None = None) -> tuple[int, dict[str, str]]:
        _components(work, python_files, corrupt)
        _compile(compiler, work / 'stage.nsi', _header(work, work / 'stage.exe', plugins, templates, False) + stage)
        return subprocess.run([str(work / 'stage.exe')], timeout=60).returncode, _keys(work)

    checks: list[str] = []
    code, keys = build_and_run({'python.exe': 'one', 'Lib/a.py': 'a'})
    py, voice = runtime / f'python-{keys["LUNMI_PYTHON_KEY"]}', runtime / f'voice-{keys["LUNMI_VOICE_KEY"]}'
    assert code == 0 and (py / 'Lib/a.py').read_text() == 'a' and (voice / 'runtime/python.exe').is_file(), code
    assert (py / '.lunmi-complete').read_text() == keys['LUNMI_PYTHON_KEY']
    checks.append('first install extracts both components and writes the completion markers')

    for directory in (py, voice):
        (directory / 'kept.txt').write_text('unchanged component is not extracted again')
    (runtime / 'python-stale').mkdir()
    (runtime / f'voice-{keys["LUNMI_VOICE_KEY"]}.partial').mkdir()
    code, new = build_and_run({'python.exe': 'two', 'Lib/a.py': 'b'})
    new_py = runtime / f'python-{new["LUNMI_PYTHON_KEY"]}'
    assert code == 0 and new["LUNMI_PYTHON_KEY"] != keys["LUNMI_PYTHON_KEY"]
    assert (new_py / 'Lib/a.py').read_text() == 'b' and (voice / 'kept.txt').is_file()
    assert sorted(p.name for p in runtime.iterdir()) == sorted([new_py.name, voice.name]), list(runtime.iterdir())
    checks.append('changed Python extracted; unchanged voice skipped; old key, stale and partial trees removed')

    code, broken = build_and_run({'Lib/a.py': 'no interpreter'})
    assert code == 2, code
    assert not (runtime / f'python-{broken["LUNMI_PYTHON_KEY"]}').exists()
    assert not (runtime / f'python-{broken["LUNMI_PYTHON_KEY"]}.partial').exists()
    assert (new_py / 'Lib/a.py').read_text() == 'b'
    checks.append('archive without its interpreter stops with code 2, no partial tree, installed version intact')

    code, damaged = build_and_run({'python.exe': 'three', 'Lib/z.py': 'DAMAGED-AFTER-SENTINEL'}, 'DAMAGED-AFTER-SENTINEL')
    assert code == 2, code
    assert not (runtime / f'python-{damaged["LUNMI_PYTHON_KEY"]}').exists()
    assert not (runtime / f'python-{damaged["LUNMI_PYTHON_KEY"]}.partial').exists()
    assert (new_py / 'Lib/a.py').read_text() == 'b'
    assert 'nsis: python not prepared: 7za exit 2' in (work / 'setup.log').read_text(), (work / 'setup.log').read_text()
    checks.append('CRC failure after the sentinel stops with code 2, never marked complete, installed version intact')

    # Windows Search or an antivirus holding a fresh file open blocks the rename for a moment.
    stop = threading.Event()

    def hold() -> None:
        while not stop.is_set():
            partial = next(runtime.glob('python-*.partial'), None)
            sentinel = partial / 'python.exe' if partial else None
            if sentinel and sentinel.is_file():
                try:
                    with open(sentinel, 'rb'):
                        time.sleep(3)
                    return
                except OSError:
                    pass
            time.sleep(0.01)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        code, held = build_and_run({'python.exe': 'four', 'Lib/a.py': 'held'})
    finally:
        stop.set()
        holder.join()
    held_py = runtime / f'python-{held["LUNMI_PYTHON_KEY"]}'
    assert code == 0 and (held_py / 'Lib/a.py').read_text() == 'held', code
    checks.append('a rename blocked by a file held open for a few seconds is retried until it succeeds')

    deep = work / ('d' * max(1, 101 - len(str(work)) - 1))
    assert len(str(deep)) == 101, len(str(deep))
    _compile(compiler, work / 'deep.nsi', _header(work, work / 'deep.exe', plugins, templates, False)
             + stage.replace(str(install), str(deep)))
    code = subprocess.run([str(work / 'deep.exe')], timeout=60).returncode
    assert code == 2 and not Path(str(deep) + '-runtime').exists(), code
    checks.append('a folder past 100 characters stops with code 2 before anything is created')
    checks.extend(protected_folder_checks(compiler, plugins, templates, work))
    return checks


def protected_folder_checks(compiler: Path, plugins: Path, templates: Path, work: Path) -> list[str]:
    """Controlled folder access refuses the setup's writes; a deny-write ACL behaves the same."""
    locked, standard, result = work / 'Documents' / 'Lunmi', work / 'Programs' / 'Lunmi', work / 'result.txt'
    locked.mkdir(parents=True)
    defines = f'!define LUNMI_STANDARD_DIR "{standard}"\n!define APP_EXECUTABLE_FILENAME "Lunmi.exe"\n'
    probe = r'''
Function .onInit
  StrCpy $INSTDIR "''' + str(locked) + r'''"
FunctionEnd
Section
  StrCpy $lunmiRuntime "$INSTDIR-runtime"
  !insertmacro lunmiCheckWritable
  FileOpen $0 "''' + str(result) + r'''" w
  FileWrite $0 "$INSTDIR|$lunmiMoved"
  FileClose $0
SectionEnd
'''
    deny = subprocess.run(['icacls', str(locked.parent), '/deny', f'{os.environ["USERNAME"]}:(OI)(CI)(W,D)'],
                          capture_output=True)
    assert deny.returncode == 0, deny.stdout
    try:
        codes = {}
        for updated in (True, False):
            result.unlink(missing_ok=True)
            out = work / f'protected-{updated}.exe'
            _compile(compiler, work / 'protected.nsi', defines + _header(work, out, plugins, templates, updated) + probe)
            codes[updated] = subprocess.run([str(out)], timeout=60).returncode
            if updated:
                assert codes[True] == 0 and result.read_text() == f'{standard}|{locked}', (codes, result.read_text())
                moved = (Path(str(standard) + '-runtime') / 'previous-runtime.txt').read_text(encoding='utf-16')
                assert moved == f'{locked}-runtime', moved
        assert codes[False] == 2 and not result.exists(), codes
    finally:
        subprocess.run(['icacls', str(locked.parent), '/reset', '/T', '/Q'], capture_output=True)
    return ['an update in a folder that refuses writes moves to the standard folder and names the old runtime',
            'a fresh install into a folder that refuses writes stops with code 2 and explains why']


def main() -> None:
    compiler, plugins, templates = _tools()
    with tempfile.TemporaryDirectory(prefix='lunmi-installer-check-') as directory:
        work = Path(directory)
        _components(work, {'python.exe': 'probe'})
        report = {'nsis_directory_checks': directory_checks(compiler, plugins, templates, work),
                  'nsis_component_checks': component_checks(compiler, plugins, templates, work)}
    print(json.dumps(report))


if __name__ == '__main__':
    main()

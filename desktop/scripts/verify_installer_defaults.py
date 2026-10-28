"""Execute the production NSIS directory hook without installing or changing registry keys."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[2]


def main() -> None:
    cache = Path(os.environ.get('ELECTRON_BUILDER_CACHE',
                               str(Path(os.environ['LOCALAPPDATA']) / 'electron-builder/Cache')))
    compiler = next(p for p in cache.glob('nsis-*/**/makensis.exe') if (p.parent / 'Include').is_dir())
    plugin = next(cache.glob('nsis-resources-*/**/x86-unicode/StdUtils.dll'))
    templates = REPO / 'desktop/node_modules/app-builder-lib/templates/nsis'
    expected = str(Path(os.environ['LOCALAPPDATA']) / 'Programs/Lunmi')
    cases = []
    with tempfile.TemporaryDirectory(prefix='lunmi-directory-check-') as directory:
        work = Path(directory)
        for updated in (False, True):
            source = work / 'probe.nsi'
            source.write_text(f'''
Unicode true
Name "Lunmi directory verification"
OutFile "{work / 'probe.exe'}"
RequestExecutionLevel user
SilentInstall silent
!addincludedir "{templates}"
!addincludedir "{templates / 'include'}"
!addplugindir /x86-unicode "{plugin.parent}"
!include "LogicLib.nsh"
!include "StdUtils.nsh"
!include "{templates / 'multiUser.nsh'}"
!define APP_FILENAME "Lunmi"
!define isUpdated '{'1 = 1' if updated else '0 = 1'}'
!include "{REPO / 'desktop/build/installer.nsh'}"
Function .onInit
  StrCpy $INSTDIR "C:\\Legacy Alpha QA\\Lunmi"
  !insertmacro GetDParameter $0
  ${{If}} $0 != ""
    StrCpy $INSTDIR $0
  ${{EndIf}}
  !insertmacro customInit
FunctionEnd
Section
  FileOpen $0 "$EXEDIR\\destination.txt" w
  FileWriteUTF16LE $0 "$INSTDIR"
  FileClose $0
SectionEnd
''', encoding='utf-8')
            built = subprocess.run([str(compiler), '-V1', str(source)], capture_output=True)
            if built.returncode:
                raise RuntimeError((built.stdout + built.stderr).decode('utf-8', 'replace'))
            for explicit in (None, str(work / 'custom directory' / 'Lunmi')):
                args = [str(work / 'probe.exe')]
                if explicit:
                    args.append('/D=' + explicit)
                subprocess.run(args, check=True, timeout=20)
                observed = (work / 'destination.txt').read_text(encoding='utf-16-le').lstrip('\ufeff')
                wanted = explicit or (r'C:\Legacy Alpha QA\Lunmi' if updated else expected)
                assert observed == wanted, (updated, explicit, observed, wanted)
                cases.append(dict(updated=updated, explicit_directory=bool(explicit), passed=True))
    print(json.dumps({'nsis_directory_checks': cases}))


if __name__ == '__main__':
    main()

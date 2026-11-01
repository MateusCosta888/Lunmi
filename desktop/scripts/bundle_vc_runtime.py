"""Deploy the licensed Visual Studio x64 CRT beside the private Python executables."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from verify_installer_runtime import imported_dlls


def missing_vc_runtime(root: Path) -> dict[str, list[str]]:
    """Do not let the CI runner's system-wide C++ runtime mask absent payload DLLs."""
    files = [path for path in root.rglob('*') if path.suffix.lower() in {'.exe', '.dll', '.pyd'}]
    bundled = {path.name.lower() for path in files}
    missing = {}
    for path in files:
        absent = [name for name in imported_dlls(path)
                  if re.match(r'^(?:(?:vcruntime|msvcp|concrt|vccorlib|vcomp)\d|libiomp)', name)
                  and not ((root / name).is_file() or (path.parent / name).is_file()
                           or ('-' in name and name in bundled))]
        if absent:
            missing[path.relative_to(root).as_posix()] = absent
    return missing


def copy_vc_runtime(destination: Path, studio: Path) -> None:
    candidates = list((studio / 'VC/Redist/MSVC').glob('*/x64/Microsoft.VC14*.CRT'))
    candidates = [path for path in candidates if (path / 'msvcp140.dll').is_file()
                  and re.fullmatch(r'\d+(\.\d+)+', path.parents[1].name)]
    if not candidates:
        raise RuntimeError('Visual Studio x64 redistributable files are missing')
    source = max(candidates, key=lambda path: tuple(int(v) for v in path.parents[1].name.split('.')))
    license_file = studio / 'Licenses/1033/Redist.txt'
    if not license_file.is_file():
        raise RuntimeError('Visual Studio redistribution notice is missing')
    hashes = {}
    for file in sorted(source.glob('*.dll')):
        target = destination / file.name
        shutil.copyfile(file, target)
        hashes[file.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    shutil.copyfile(license_file, destination / 'VC-REDIST.txt')
    (destination / 'VC-RUNTIME.json').write_text(json.dumps({
        'source': 'Microsoft Visual Studio x64 redistributable CRT',
        'version': source.parents[1].name, 'sha256': hashes,
    }, indent=2) + '\n', encoding='utf-8')


def install_vc_runtime(destination: Path) -> None:
    vswhere = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / (
        'Microsoft Visual Studio/Installer/vswhere.exe')
    result = subprocess.run([str(vswhere), '-latest', '-products', '*', '-requires',
                             'Microsoft.VisualStudio.Component.VC.Tools.x86.x64',
                             '-property', 'installationPath'],
                            capture_output=True, text=True, check=True)
    studio = Path(result.stdout.strip())
    if not studio.is_absolute():
        raise RuntimeError('A licensed Visual Studio C++ build installation is required')
    copy_vc_runtime(destination, studio)
    missing = missing_vc_runtime(destination)
    if missing:
        raise RuntimeError(f'Payload has unresolved C++ imports: {json.dumps(missing)}')

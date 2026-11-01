"""Ship pinned HTTP search dependencies, isolated from the backend's MCP version."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from bundle_vc_runtime import missing_vc_runtime
from fetch_runtime import RESOURCES


def prune_web(packages: Path) -> None:
    # HTTP-only policy forbids launching processes; no browser or Node driver is shipped.
    for directory in (packages / 'playwright/driver', packages / 'bin', packages / 'Scripts'):
        if directory.exists():
            shutil.rmtree(directory)
    for directory in [*packages.rglob('__pycache__'), *packages.rglob('.agents')]:
        if not directory.resolve().is_relative_to(packages.resolve()):
            raise RuntimeError('Web package cleanup escaped its build directory')
        if directory.exists():
            shutil.rmtree(directory)


def fetch_web(python: Path, packages: Path) -> None:
    if packages.exists():
        raise RuntimeError('Web destination exists; use a fresh release checkout')
    subprocess.run([str(python), '-I', '-m', 'pip', '--isolated', 'install', '--target', str(packages),
                    '--require-hashes', '--no-build-isolation', '--no-compile',
                    '--disable-pip-version-check', '--no-input', '--no-warn-script-location',
                    '-r', str(RESOURCES / 'requirements-web.lock')], check=True)
    prune_web(packages)
    if missing_vc_runtime(python.parent):
        raise RuntimeError('Bundled web dependencies require absent C++ DLLs')


if __name__ == '__main__':
    fetch_web(RESOURCES / 'python/python.exe', RESOURCES / 'python/web-packages')

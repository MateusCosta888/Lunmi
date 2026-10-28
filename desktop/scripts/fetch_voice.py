"""Build the offline CPU voice bundle from public, pinned artifacts only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import urllib.request

from fetch_runtime import REPO, RESOURCES, fetch_python


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def prune_voice_runtime(runtime: Path) -> None:
    # Keep the complete offline setup below NSIS/GitHub's 2 GiB limit; retain runtime APIs and notices.
    directories = [runtime / name for name in ('Scripts', 'Lib/venv/scripts')]
    site = runtime / 'Lib/site-packages'
    directories += [site / name for name in ('pip', 'setuptools', '_distutils_hack', 'pkg_resources')]
    for pattern in ('pip-*.dist-info', 'setuptools-*.dist-info'):
        directories += list(site.glob(pattern))
    for package in ('numpy', 'scipy'):
        directories += list((site / package).rglob('tests'))
    directories += list(runtime.rglob('__pycache__'))
    for directory in directories:
        if directory.exists():
            if not directory.resolve().is_relative_to(runtime.resolve()) or directory.is_symlink():
                raise ValueError('Voice runtime path escapes its build directory.')
            shutil.rmtree(directory)
    (site / 'distutils-precedence.pth').unlink(missing_ok=True)
    for path in runtime.rglob('*'):
        if path.is_file() and path.suffix in {'.pyc', '.pyo', '.pdb'}:
            path.unlink()


def main() -> None:
    pin = json.loads((RESOURCES / 'VOICE-SOURCE.json').read_text(encoding='utf-8'))
    destination = RESOURCES / 'voice'
    if destination.exists():
        raise SystemExit('Voice destination exists; use a fresh release checkout.')
    cache = REPO / 'desktop/.voice-cache'
    cache.mkdir(parents=True, exist_ok=True)
    for item in pin['files']:
        relative = PurePosixPath(item['path'])
        if relative.is_absolute() or '..' in relative.parts or '\\' in item['path']:
            raise SystemExit('Invalid voice asset path.')
        cached = cache / item['sha256']
        if not cached.is_file() or cached.stat().st_size != item['bytes'] or digest(cached) != item['sha256']:
            partial = cached.with_suffix('.download')
            try:
                with urllib.request.urlopen(item['url'], timeout=120) as response, partial.open('wb') as output:
                    shutil.copyfileobj(response, output)
                if partial.stat().st_size != item['bytes'] or digest(partial) != item['sha256']:
                    raise SystemExit('Voice asset checksum mismatch.')
                partial.replace(cached)
            finally:
                partial.unlink(missing_ok=True)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cached, target)
    fetch_python(destination / 'runtime')
    python = destination / 'runtime/python.exe'
    subprocess.run([str(python), '-I', '-m', 'pip', 'install', '--require-hashes', '--only-binary=:all:',
                    '--no-warn-script-location', '--disable-pip-version-check', '--no-input',
                    '-r', str(RESOURCES / 'requirements-voice.lock')], check=True)
    subprocess.run([str(python), '-I', '-m', 'pip', 'check'], check=True)
    subprocess.run([str(python), '-I', '-c',
                    'import supertonic, faster_whisper, sounddevice, onnxruntime, scipy; '
                    'import importlib.util; assert importlib.util.find_spec("nvidia") is None'], check=True)
    prune_voice_runtime(destination / 'runtime')
    subprocess.run([str(python), '-I', '-B', '-c',
                    'import supertonic, faster_whisper, sounddevice, onnxruntime, scipy.signal'], check=True)
    (destination / 'VOICE-SOURCE.json').write_text(json.dumps(pin, indent=2) + '\n', encoding='utf-8')
    notices = ['Lunmi voice resources\n\nSupertonic 3 model weights: BigScience Open RAIL-M. '
               'The use restrictions below apply to the model.\n'
               'Supertonic Python code, Whisper and Silero VAD: MIT. '
               'These terms apply to their respective components, not the entire Lunmi application.\n']
    for name in ('models/supertonic-3/LICENSE', 'licenses/supertonic-code-MIT.txt',
                 'licenses/whisper-MIT.txt', 'licenses/silero-MIT.txt'):
        notices.append((destination / name).read_text(encoding='utf-8'))
    (destination / 'NOTICE.txt').write_text('\n\n'.join(notices), encoding='utf-8')
    print('Verified offline voice bundle: CPU, Supertonic 3, Whisper large-v3-turbo, Silero VAD.')


if __name__ == '__main__':
    main()

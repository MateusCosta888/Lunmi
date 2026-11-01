"""Package acceptance must not depend on the developer's installed Visual C++ DLLs."""
import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / 'desktop/scripts'


@pytest.fixture
def bundler(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location('bundle_vc_runtime', SCRIPTS / 'bundle_vc_runtime.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_cpp_runtime_is_detected_even_on_a_machine_with_visual_studio(tmp_path, bundler, monkeypatch):
    extension = tmp_path / 'Lib/site-packages/onnxruntime/capi/engine.pyd'
    extension.parent.mkdir(parents=True)
    extension.touch()
    monkeypatch.setattr(bundler, 'imported_dlls',
                        lambda _: ['kernel32.dll', 'msvcp_win.dll', 'msvcp140.dll', 'msvcp140_1.dll'])
    assert bundler.missing_vc_runtime(tmp_path) == {
        'Lib/site-packages/onnxruntime/capi/engine.pyd': ['msvcp140.dll', 'msvcp140_1.dll']}
    unrelated = tmp_path / 'unrelated'
    unrelated.mkdir()
    (unrelated / 'msvcp140.dll').touch()
    assert bundler.missing_vc_runtime(tmp_path), 'An unrelated folder is not a loader search directory'
    monkeypatch.setattr(bundler, 'imported_dlls',
                        lambda path: ['msvcp140.dll', 'msvcp140_1.dll'] if path == extension else [])
    (tmp_path / 'msvcp140.dll').touch()
    (tmp_path / 'msvcp140_1.dll').touch()
    assert bundler.missing_vc_runtime(tmp_path) == {}


def test_copy_uses_redistributable_files_and_keeps_provenance_without_machine_paths(tmp_path, bundler):
    studio = tmp_path / 'Visual Studio'
    source = studio / 'VC/Redist/MSVC/14.44.35112/x64/Microsoft.VC143.CRT'
    source.mkdir(parents=True)
    alias = studio / 'VC/Redist/MSVC/v143/x64/Microsoft.VC143.CRT'
    alias.mkdir(parents=True)
    (alias / 'msvcp140.dll').write_bytes(b'not a numeric version')
    for name in ('msvcp140.dll', 'msvcp140_1.dll', 'vcruntime140.dll'):
        (source / name).write_bytes(name.encode())
    license_file = studio / 'Licenses/1033/Redist.txt'
    license_file.parent.mkdir(parents=True)
    license_file.write_text('Microsoft redistribution notice', encoding='utf-8')
    destination = tmp_path / 'runtime'
    destination.mkdir()
    bundler.copy_vc_runtime(destination, studio)
    assert (destination / 'msvcp140.dll').read_bytes() == b'msvcp140.dll'
    assert (destination / 'VC-REDIST.txt').read_text() == license_file.read_text()
    assert '14.44.35112' in (destination / 'VC-RUNTIME.json').read_text()
    assert str(studio) not in (destination / 'VC-RUNTIME.json').read_text()


def test_absent_build_runtime_fails_closed(tmp_path, bundler):
    with pytest.raises(RuntimeError, match='redistributable'):
        bundler.copy_vc_runtime(tmp_path, tmp_path / 'missing')

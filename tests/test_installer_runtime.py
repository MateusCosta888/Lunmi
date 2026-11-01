"""The setup must start on Windows before any bundled runtime is extracted."""
import importlib.util
from pathlib import Path
import struct

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'desktop/scripts/verify_installer_runtime.py'
spec = importlib.util.spec_from_file_location('verify_installer_runtime', SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def executable(tmp_path, dll='kernel32.dll', *, delay=False, x86=False):
    data = bytearray(0x600)
    data[:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    optional = 0x98
    size = 224 if x86 else 240
    struct.pack_into('<H', data, 0x86, 1)
    struct.pack_into('<H', data, 0x94, size)
    struct.pack_into('<H', data, optional, 0x10b if x86 else 0x20b)
    struct.pack_into('<I' if x86 else '<Q', data, optional + (28 if x86 else 24), 0x400000)
    struct.pack_into('<I', data, optional + 60, 0x200)
    directories = optional + (96 if x86 else 112)
    struct.pack_into('<I', data, directories - 4, 16)
    struct.pack_into('<II', data, directories + 8 * (13 if delay else 1), 0x1000, 64 if delay else 40)
    struct.pack_into('<IIII', data, optional + size + 8, 0x400, 0x1000, 0x400, 0x200)
    struct.pack_into('<' + 'I' * (8 if delay else 5), data, 0x200,
                     *([1, 0x1100, 0, 0, 0, 0, 0, 0] if delay else [0, 0, 0, 0x1100, 0]))
    name = dll.encode('ascii') + b'\0'
    data[0x300:0x300 + len(name)] = name
    p = tmp_path / 'setup.exe'
    p.write_bytes(data)
    return p


@pytest.mark.parametrize('dll', ['VCRUNTIME140.dll', 'vcruntime140_1.dll', 'MSVCP140.dll',
                                 'MSVCR120.dll', 'CONCRT140.dll', 'WebView2Loader.dll'])
@pytest.mark.parametrize('delay', [False, True])
def test_external_bootstrap_runtime_is_rejected(tmp_path, dll, delay):
    path = executable(tmp_path, dll, delay=delay)
    assert gate.runtime_dependencies(gate.imported_dlls(path)) == [dll.lower()]


@pytest.mark.parametrize('x86', [False, True])
def test_windows_system_dll_is_allowed_and_overlay_ignored(tmp_path, x86):
    path = executable(tmp_path, x86=x86)
    with path.open('ab') as stream:
        stream.write(b'NSIS overlay with VCRUNTIME140.dll as data, not an import')
    assert gate.imported_dlls(path) == ['kernel32.dll']
    assert gate.runtime_dependencies(gate.imported_dlls(path)) == []


@pytest.mark.parametrize('payload', [b'', b'MZ', b'not a PE'])
def test_invalid_executable_fails_closed(tmp_path, payload, monkeypatch):
    p = tmp_path / 'bad.exe'
    p.write_bytes(payload)
    monkeypatch.setattr(gate.sys, 'argv', ['gate', str(p)])
    assert gate.main() == 1


def test_unmapped_import_name_is_rejected(tmp_path):
    path = executable(tmp_path)
    data = bytearray(path.read_bytes())
    struct.pack_into('<I', data, 0x20c, 0x900000)
    path.write_bytes(data)
    with pytest.raises(ValueError, match='Unmapped'):
        gate.imported_dlls(path)


def test_legacy_delay_import_virtual_address_is_checked(tmp_path):
    path = executable(tmp_path, 'vcruntime140.dll', delay=True, x86=True)
    data = bytearray(path.read_bytes())
    struct.pack_into('<II', data, 0x200, 0, 0x401100)
    path.write_bytes(data)
    assert gate.runtime_dependencies(gate.imported_dlls(path)) == ['vcruntime140.dll']


def test_truncated_optional_header_is_rejected(tmp_path):
    path = executable(tmp_path)
    data = bytearray(path.read_bytes())
    struct.pack_into('<H', data, 0x94, 12)
    path.write_bytes(data)
    with pytest.raises(ValueError, match='Truncated'):
        gate.imported_dlls(path)


def test_cli_checks_loader_dependencies(tmp_path, monkeypatch):
    path = executable(tmp_path, 'vcruntime140.dll')
    monkeypatch.setattr(gate.sys, 'argv', ['gate', str(path)])
    assert gate.main() == 1
    path = executable(tmp_path, 'kernel32.dll')
    assert gate.main() == 0

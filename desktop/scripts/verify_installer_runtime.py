"""Reject installer bootstrap imports requiring a separate Visual C++ installation.

Read PE import tables without loading or executing the executable. The appended NSIS
payload is ignored: its contents cannot supply DLLs needed before the bootstrap starts.
"""
from __future__ import annotations

import mmap
from pathlib import Path
import struct
import sys


def imported_dlls(path: Path) -> list[str]:
    with path.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        def unpack(fmt: str, offset: int) -> tuple:
            if offset < 0 or offset + struct.calcsize(fmt) > len(data):
                raise ValueError('Truncated PE image')
            return struct.unpack_from(fmt, data, offset)

        if data[:2] != b'MZ':
            raise ValueError('Missing DOS header')
        pe = unpack('<I', 0x3c)[0]
        if data[pe:pe + 4] != b'PE\0\0':
            raise ValueError('Missing PE header')
        count = unpack('<H', pe + 6)[0]
        optional_size = unpack('<H', pe + 20)[0]
        optional = pe + 24
        magic = unpack('<H', optional)[0]
        if magic not in (0x10b, 0x20b):
            raise ValueError('Unknown PE optional header')
        directory_offset = 112 if magic == 0x20b else 96
        if optional_size < directory_offset or optional + optional_size + count * 40 > len(data):
            raise ValueError('Truncated optional header or sections')
        number = unpack('<I', optional + directory_offset - 4)[0]
        image_base = unpack('<Q' if magic == 0x20b else '<I', optional + (24 if magic == 0x20b else 28))[0]
        headers_size = unpack('<I', optional + 60)[0]
        sections = [unpack('<IIII', optional + optional_size + 40 * i + 8) for i in range(count)]

        def file_offset(rva: int) -> int:
            if 0 <= rva < headers_size and rva < len(data):
                return rva
            for _virtual_size, address, raw_size, raw_offset in sections:
                if address <= rva < address + raw_size:
                    offset = raw_offset + rva - address
                    if offset < len(data):
                        return offset
            raise ValueError('Unmapped import RVA')

        def name(rva: int) -> str:
            start = file_offset(rva)
            end = data.find(b'\0', start, min(start + 512, len(data)))
            if end < 0:
                raise ValueError('Unterminated DLL name')
            return data[start:end].decode('ascii').lower()

        result: set[str] = set()
        for index, width in ((1, 20), (13, 32)):
            if index >= number:
                continue
            if directory_offset + 8 * (index + 1) > optional_size:
                raise ValueError('Truncated data directories')
            rva, size = unpack('<II', optional + directory_offset + 8 * index)
            if not rva:
                continue
            start = file_offset(rva)
            for i in range(min(size // width, 4096)):
                record = unpack('<' + 'I' * (width // 4), start + i * width)
                if not any(record):
                    break
                dll_rva = record[3] if index == 1 else record[1]
                if index == 13 and not record[0] & 1:
                    dll_rva -= image_base
                result.add(name(dll_rva))
            else:
                raise ValueError('Import table lacks a terminator')
        return sorted(result)


def runtime_dependencies(dlls: list[str]) -> list[str]:
    return [name for name in dlls if name.lower().startswith(('vcruntime', 'msvcp', 'msvcr', 'concrt'))
            or name.lower() == 'webview2loader.dll']


def main() -> int:
    if len(sys.argv) != 2:
        print('Usage: python verify_installer_runtime.py <setup.exe>', file=sys.stderr)
        return 2
    try:
        dlls = imported_dlls(Path(sys.argv[1]))
        missing = runtime_dependencies(dlls)
    except (OSError, ValueError, struct.error) as exc:
        print(f'[installer-runtime] invalid executable: {exc}', file=sys.stderr)
        return 1
    print('[installer-runtime] imports: ' + ', '.join(dlls))
    if missing:
        print('[installer-runtime] FAIL: external prerequisite: ' + ', '.join(missing), file=sys.stderr)
        return 1
    print('[installer-runtime] PASS: no load-time VC++/WebView2Loader dependency')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

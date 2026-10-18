"""Regressão do leitor de `app.asar` do auditor de payload.

O defeito real: a base dos offsets era calculada como `16 + tamanho_do_JSON_sem_padding`. No
`app.asar` da alpha.2 o header JSON tem 1.180.930 bytes — não é múltiplo de 4 — enquanto o bloco
de header, com padding, tem 1.180.940. A base correta é `8 + 1.180.940 = 1.180.948`, dois bytes
depois da usada; todo chunk lido saía deslocado e o `package.json` extraído nem passava por
`json.loads`. O enquadramento correto é o do `@electron/asar/lib/disk.js`: 4 bytes com o tamanho
do Pickle externo, 4 com o tamanho do bloco e o header JSON com o seu próprio prefixo.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
AUDITOR = REPO / "desktop" / "scripts" / "audit_payload.py"
REAL_ASAR = REPO / "desktop" / "release" / "win-unpacked" / "resources" / "app.asar"
NODE = shutil.which("node")
ASAR_MODULE = REPO / "desktop" / "node_modules" / "@electron" / "asar"


def _load_auditor():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("audit_payload", AUDITOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pack(tree: dict[str, dict], payload: bytes) -> bytes:
    """Serializa header+payload no mesmo enquadramento de Pickle aninhado do `@electron/asar`."""
    raw = json.dumps({"files": tree}).encode("utf-8")
    padded = raw + b"\x00" * (-len(raw) % 4)
    string_entry = struct.pack("<I", len(raw)) + padded
    block = struct.pack("<I", len(string_entry)) + string_entry
    return struct.pack("<I", 4) + struct.pack("<I", len(block)) + block + payload


def _asar_blob(entries: dict[str, bytes], unpacked: tuple[str, ...] = ()) -> bytes:
    tree: dict[str, dict] = {}
    payload = bytearray()
    for name, data in entries.items():
        tree[name] = {"size": len(data), "offset": str(len(payload))}
        payload += data
    for name in unpacked:
        tree[name] = {"size": 0, "unpacked": True}
    return _pack(tree, bytes(payload))


def _non_padded_fixture() -> tuple[bytes, dict[str, bytes]]:
    """Fixture cujo header JSON **não** é múltiplo de 4 (a condição que expunha o defeito)."""
    for filler in range(4):
        entries = {"package.json": b'{"name":"lunmi","version":"9.9.9"}',
                   "filler.bin": b"x" * filler}
        blob = _asar_blob(entries)
        if struct.unpack_from("<I", blob, 12)[0] % 4:
            return blob, entries
    raise AssertionError("não foi possível montar header com comprimento não múltiplo de 4")


def test_header_with_padding_is_located_by_the_block_size() -> None:
    audit = _load_auditor()
    blob, entries = _non_padded_fixture()
    json_len = struct.unpack_from("<I", blob, 12)[0]
    string_entry = struct.unpack_from("<I", blob, 8)[0]
    block = struct.unpack_from("<I", blob, 4)[0]
    assert block == string_entry + 4, "o bloco é a entrada de string com o seu próprio prefixo"
    assert block > json_len, "o bloco tem padding além do JSON"

    header, base = audit._asar_header(blob)
    assert header["files"].keys() == entries.keys()
    assert base == 8 + block, "base = 8 + tamanho do bloco, como no @electron/asar/lib/disk.js"
    assert base != 16 + json_len, "a base antiga (JSON sem padding) estava errada"

    read = {name: (offset, size) for name, offset, size in audit._asar_entries(blob)}
    for name, data in entries.items():
        offset, size = read[name]
        assert offset is not None
        assert blob[offset:offset + size] == data
    packed_offset, packed_size = read["package.json"]
    assert json.loads(blob[packed_offset:packed_offset + packed_size])["version"] == "9.9.9"


def test_unpacked_entries_have_no_offset_and_out_of_bounds_entries_fail() -> None:
    audit = _load_auditor()
    blob = _asar_blob({"dist/index.js": b"x=1;"}, unpacked=("node.dll",))
    entries = {name: (offset, size) for name, offset, size in audit._asar_entries(blob)}
    assert entries["node.dll"][0] is None, "entrada `unpacked` vive fora do asar"
    assert entries["dist/index.js"][0] is not None

    # Entrada que aponta além do arquivo não pode passar calada: era o modo de falha silencioso.
    broken = _pack({"a.txt": {"size": 10_000, "offset": "0"}}, b"curto")
    with pytest.raises(ValueError):
        audit._asar_entries(broken)


@pytest.mark.skipif(not REAL_ASAR.is_file(), reason="payload do instalador ainda não montado")
def test_real_asar_package_json_is_parseable_and_matches_the_source_version() -> None:
    audit = _load_auditor()
    blob = REAL_ASAR.read_bytes()
    entries = {name: (offset, size) for name, offset, size in audit._asar_entries(blob)}
    offset, size = entries["package.json"]
    assert offset is not None
    packed = json.loads(blob[offset:offset + size].decode("utf-8"))
    source = json.loads((REPO / "desktop" / "package.json").read_text(encoding="utf-8"))
    assert packed["name"] == source["name"]
    assert packed["version"] == source["version"]


@pytest.mark.skipif(
    not REAL_ASAR.is_file() or NODE is None or not ASAR_MODULE.is_dir(),
    reason="node e/ou @electron/asar ausentes para a comparação oficial",
)
def test_extraction_matches_the_official_asar_module() -> None:
    audit = _load_auditor()
    blob = REAL_ASAR.read_bytes()
    entries = {name: (offset, size) for name, offset, size in audit._asar_entries(blob)}
    offset, size = entries["package.json"]
    assert offset is not None
    mine = blob[offset:offset + size]
    script = (
        "const asar=require('@electron/asar');"
        f"process.stdout.write(asar.extractFile({json.dumps(str(REAL_ASAR))}, 'package.json'));"
    )
    done = subprocess.run([NODE, "-e", script], cwd=str(REPO / "desktop"),
                          capture_output=True, check=True)
    assert done.stdout == mine, "extração própria divergiu da do @electron/asar"

"""Audita a árvore extraída do pacote: nenhum segredo, dado pessoal ou dependência de caminho.

Roda sobre `desktop/release/win-unpacked` (ou outra raiz de payload) e falha quando encontra:

* estado pessoal/gravável que não pode viajar (`.env`, `.local`, `.git`, `.agents`, DPAPI,
  SQLite com os seus sidecars, diretórios `logs/` e `output/` sob `resources/`);
* caminho da máquina de desenvolvimento (diretório pessoal ou checkout de quem executa);
* material que parece credencial real (chaves `sk-`, PEM, `api_key=` com valor não vazio).

Também **abre os arquivos `app.asar`** e inspeciona as suas entradas: `rglob` enxerga o asar
como um único arquivo de dezenas de MB, então o conteúdo empacotado do renderer/main ficaria
sem auditoria se o script o pulasse por tamanho.

O que **não** é achado: bundles de CA de terceiros (`certifi/cacert.pem`); diretórios de dados
que um pacote de terceiro embarca em `site-packages`; e heurística de credencial em
código-fonte de bibliotecas — o caminho pessoal continua sendo varrido em todo lugar.

Nunca imprime valores sensíveis: mostra o caminho e o tipo de achado.

Uso:  python desktop/scripts/audit_payload.py [raiz]
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import components  # noqa: E402

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "release" / "win-unpacked"

FORBIDDEN_NAMES = {".env", ".local", ".git", ".agents", ".claude", ".cursor", ".qoder",
                   ".freebuff", "secret.bin", "install.json", "state.json"}
FORBIDDEN_DIR_NAMES = {"logs", "output"}
FORBIDDEN_SUFFIXES = {".dpapi", ".pfx", ".p12", ".key"}
FORBIDDEN_NAME_SUFFIXES = ("-wal", "-shm", "-journal", ".sqlite3", ".sqlite3-wal",
                           ".sqlite3-shm", ".sqlite3-journal", ".db", ".env.local")
ALLOWED_NAMES = {"cacert.pem"}

PERSONAL_PATHS = tuple(value.encode("utf-8")
                       for base in (str(Path.home()), str(Path(__file__).resolve().parents[2]),
                                    r"C:\Users\runneradmin")
                       for value in {base, base.replace("\\", "/"), base.replace("\\", "\\\\")})
# Exact public wheel bytes only: upstream CI also uses the username "runneradmin".
# PyPI cp311-win_amd64 wheels: numpy 2.4.6 (1e254a00...1500147), pandas 2.2.3 (3fc6873a...5ab2c5).
PUBLIC_BUILD_METADATA_SHA256 = {
    "resources/python/Lib/site-packages/numpy/__config__.py":
        "7ff3893d0f50f52c748988bbc88bfe970a052fd81e4dfa65bc536d61d20ae18d",
    "resources/python/Lib/site-packages/numpy-2.4.6.dist-info/DELVEWHEEL":
        "0399822db132f754a7e3ba5eac8e564e4049b30df55d53bfeba6d38b812de58d",
    "resources/python/Lib/site-packages/pandas-2.2.3.dist-info/DELVEWHEEL":
        "dde7fd56615a1f7cf30d97ff48643e3c1a980e7a43ab7846fe64365231b89891",
    "resources/voice/runtime/Lib/site-packages/numpy/__config__.py":
        "7ff3893d0f50f52c748988bbc88bfe970a052fd81e4dfa65bc536d61d20ae18d",
    "resources/voice/runtime/Lib/site-packages/numpy-2.4.6.dist-info/DELVEWHEEL":
        "0399822db132f754a7e3ba5eac8e564e4049b30df55d53bfeba6d38b812de58d",
    "resources/voice/runtime/Lib/site-packages/av-18.1.0.dist-info/DELVEWHEEL":
        "6d5497e1c1f3801d70a6b64498f582486a72ada94fc133fe547aa49462a78433",
    "resources/voice/runtime/Lib/site-packages/scipy/__config__.py":
        "ee1de88617d5488d192044e28dd47ceb4c31632c30e4c98d317a7458dbb25645",
    "resources/voice/runtime/Lib/site-packages/scipy-1.17.1.dist-info/DELVEWHEEL":
        "bfde4e6a348d78f3cfb5ac14c6af2d312ba63bbcb29f1cd16103dbb0a65d20a1",
}
SECRET_PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9]{20,}"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"api[_-]?key\s*=\s*[\"'][A-Za-z0-9\-_]{24,}"),
    # Bundle privado do Spotify (config injetada): a chave de pareamento tem 32 bytes
    # em hex e nunca pode viajar num payload. O JS público só tem a validação da chave.
    re.compile(rb"secretHex\"?\s*[:=]\s*[\"'][0-9a-fA-F]{64}[\"']"),
)

BINARY_SUFFIXES = {".exe", ".dll", ".pyd", ".so", ".bin", ".ico", ".png", ".jpg", ".ttf",
                   ".7z", ".zip", ".dat", ".pyc", ".node", ".moz", ".pak", ".pem", ".onnx"}


def _inside_site_packages(relative: Path) -> bool:
    return "site-packages" in relative.parts


def _name_finding(relative: Path) -> str | None:
    name = relative.name
    lowered = name.lower()
    if lowered in ALLOWED_NAMES:
        return None
    if name in FORBIDDEN_NAMES or lowered in FORBIDDEN_NAMES:
        # Um pacote de terceiro pode trazer os seus próprios dados de agente em `site-packages`.
        if _inside_site_packages(relative):
            return None
        return f"nome proibido: {relative}"
    if relative.suffix.lower() in FORBIDDEN_SUFFIXES or any(
        lowered.endswith(suffix) for suffix in FORBIDDEN_NAME_SUFFIXES
    ):
        return f"estado/segredo por sufixo: {relative}"
    return None


def _scan_bytes(relative: Path, blob: bytes) -> str | None:
    public_metadata = PUBLIC_BUILD_METADATA_SHA256.get(relative.as_posix()) == hashlib.sha256(blob).hexdigest()
    if any(token in blob for token in PERSONAL_PATHS) and not public_metadata:
        return f"caminho pessoal: {relative}"
    if _inside_site_packages(relative) and relative.suffix == ".py":
        return None
    if any(pattern.search(blob) for pattern in SECRET_PATTERNS):
        return f"aparência de credencial: {relative}"
    return None


def audit_tree(root: Path, prefix: Path = Path()) -> list[str]:
    """`prefix` dá a um componente extraído fora da raiz o caminho lógico `resources/<nome>`."""
    findings: list[str] = []
    for path in root.rglob("*"):
        relative = prefix / path.relative_to(root)
        if path.is_file() and path.name.lower().endswith(".asar"):
            findings += _audit_asar(path, relative)
            continue
        name_finding = _name_finding(relative)
        if name_finding:
            findings.append(name_finding)
            continue
        if path.is_dir():
            if relative.name.lower() in FORBIDDEN_DIR_NAMES and "resources" in relative.parts:
                findings.append(f"diretório de estado: {relative}")
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        try:
            if path.stat().st_size > 32 * 1024 * 1024:
                continue
            blob = path.read_bytes()
        except OSError as exc:
            findings.append(f"ilegível ({type(exc).__name__}): {relative}")
            continue
        scanned = _scan_bytes(relative, blob)
        if scanned:
            findings.append(scanned)
    return findings


def _asar_header(blob: bytes) -> tuple[dict, int]:
    """Lê o header do asar e devolve (árvore, base dos offsets).

    O header é um Pickle aninhado, como em `@electron/asar/lib/disk.js`: 4 bytes com o tamanho do
    Pickle externo, 4 bytes com o tamanho do **bloco** de header, e dentro do bloco a entrada de
    string (4 bytes com o tamanho da entrada, 4 com o comprimento do JSON, e só então os bytes do
    JSON, com padding até múltiplo de 4). Os offsets das entradas são relativos a
    `8 + tamanho_do_bloco`.

    O comprimento do JSON **sem padding** não serve de base: o header real do `app.asar` tem
    1.180.930 bytes de JSON (não múltiplo de 4) e o bloco 1.180.940. Usar o valor sem padding
    colocava a base 2 bytes antes do payload e todo chunk lido saía deslocado — o
    `package.json` extraído nem passava por `json.loads`.
    """
    if len(blob) < 16:
        raise ValueError("asar truncado")
    size_payload = struct.unpack_from("<I", blob, 0)[0]
    header_block = struct.unpack_from("<I", blob, 4)[0]
    if size_payload != 4 or header_block < 16:
        raise ValueError(f"prefixo de header inesperado ({size_payload}, {header_block})")
    if 8 + header_block > len(blob):
        raise ValueError("bloco de header maior que o arquivo")
    block = blob[8:8 + header_block]
    string_entry_len = struct.unpack_from("<I", block, 0)[0]
    json_len = struct.unpack_from("<I", block, 4)[0]
    if string_entry_len + 4 > header_block or 8 + json_len > header_block or json_len <= 0:
        raise ValueError("tamanho do header JSON fora do bloco")
    return json.loads(block[8:8 + json_len].decode("utf-8")), 8 + header_block


def _asar_entries(blob: bytes, prefix: str = "") -> list[tuple[str, int | None, int]]:
    """Percorre a árvore de arquivos do header de um asar: (caminho, offset, tamanho)."""
    header, base = _asar_header(blob)
    entries: list[tuple[str, int | None, int]] = []

    def walk(node: dict, current: str) -> None:
        for name, item in (node.get("files") or {}).items():
            full = f"{current}/{name}" if current else name
            if not isinstance(item, dict):
                continue
            if "files" in item:
                walk(item, full)
                continue
            offset = item.get("offset")
            size = int(item.get("size") or 0)
            if item.get("unpacked"):
                # Fora do asar: fica ao lado, em `app.asar.unpacked/`, e não é lido aqui.
                entries.append((full, None, size))
                continue
            absolute = base + int(offset or 0)
            if int(offset or 0) < 0 or size < 0 or absolute + size > len(blob):
                raise ValueError(f"entrada fora dos limites do arquivo: {full}")
            entries.append((full, absolute, size))

    walk(header, prefix)
    return entries


def _audit_asar(path: Path, relative: Path) -> list[str]:
    findings: list[str] = []
    try:
        blob = path.read_bytes()
        entries = _asar_entries(blob)
    except (OSError, ValueError, struct.error, json.JSONDecodeError) as exc:
        return [f"asar ilegível ({type(exc).__name__}): {relative}"]
    for name, offset, size in entries:
        entry = Path(f"{relative}/{name}")
        name_finding = _name_finding(entry)
        if name_finding:
            findings.append(name_finding)
            continue
        if offset is None or size == 0 or size > 32 * 1024 * 1024:
            continue
        chunk = blob[offset:offset + size]
        scanned = _scan_bytes(entry, chunk)
        if scanned:
            findings.append(scanned)
    return findings


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_ROOT
    if not root.is_dir():
        print(f"[audit] raiz inexistente: {root}", file=sys.stderr)
        return 1
    findings = audit_tree(root)
    entries = sum(1 for _ in root.rglob("*"))
    # Python e voz viajam como arquivos de componentes; audita-se o conteúdo extraído e conferido.
    if (root / "resources" / components.MANIFEST).is_file():
        for name, directory in components.ensure_materialized(root).items():
            findings += audit_tree(directory, Path("resources") / name)
            entries += sum(1 for _ in directory.rglob("*"))
    if findings:
        print(f"[audit] {len(findings)} achado(s) em {root}:")
        for item in findings[:40]:
            print("  -", item)
        return 1
    print(f"[audit] limpo: {entries} entradas sob {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

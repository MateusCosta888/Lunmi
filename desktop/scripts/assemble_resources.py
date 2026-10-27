"""Monta `desktop/resources/app`: o runtime Python do pacote Lunmi, por allowlist.

Por que existe: o instalador não pode levar o checkout. Este script copia só o que o
backend e o worker UFO realmente precisam ler em execução — e recusa qualquer coisa que
pareça segredo, dado pessoal ou estado gravável (`.env`, `.local`, DPAPI, SQLite e os seus
sidecars `-wal`/`-shm`/`-journal`, logs, caches, agentes e diretórios Git).

Duas regras de inclusão:

* `app/` — o código do produto, inteiro, menos os caches e o estado acima.
* `vendor/UFO` — a lista exata de `ufo.lock.json` (501 arquivos) **mais** os recursos de
  runtime comprovados (`config/**`, `ufo/**` e o `LICENSE`). O worker confere byte a byte
  todos os arquivos do pin antes de importar, então recortar a árvore pinada faria o worker
  real recusar a inicialização; copiar a árvore inteira traria `tests/`, `documents/`,
  `.github/` e o antigo frontend `galaxy/` sem uso em execução.

O filtro é aplicado a **todos** os componentes do caminho relativo, e `os.walk` poda os
diretórios proibidos: um `continue` só sobre o diretório não impediria os descendentes de
entrar (defeito real: 29 arquivos de `vendor/UFO/.git` foram parar no payload).

Uso:  python desktop/scripts/assemble_resources.py
Saída: desktop/resources/app/  (consumida por `extraResources` do electron-builder)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEST = REPO / "desktop" / "resources" / "app"
UFO_SRC = REPO / "vendor" / "UFO"
UFO_LOCK = REPO / "ufo.lock.json"

SKIP_DIR_NAMES = {
    "__pycache__", ".git", ".github", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv",
    "node_modules", ".local", "logs", "output", "dist", "dist-electron", ".agents", ".claude",
    ".cursor", ".freebuff", ".qoder", "coverage",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".pdb", ".log", ".sqlite", ".sqlite3", ".db", ".dpapi",
                 ".tmp", ".bak", ".p12", ".pfx", ".pem", ".key", ".zip", ".7z"}
# Sidecars de SQLite não são cobertos por `suffix` (`.sqlite3-wal` termina em `-wal`).
SKIP_NAME_SUFFIXES = ("-wal", "-shm", "-journal", ".sqlite3-wal", ".sqlite3-shm",
                      ".sqlite3-journal", ".db-wal", ".db-shm")
SKIP_NAMES = {".env", "Thumbs.db", "desktop.ini", ".gitignore", ".gitattributes",
              "secret.bin", "install.json", "state.json"}

# Diretórios de `vendor/UFO` copiados inteiros como recursos de runtime comprovados: o
# carregador de configuração (yaml/template) e os prompts/recursos do pacote `ufo`.
UFO_RUNTIME_DIRS = ("config", "ufo")
# Arquivos soltos de `vendor/UFO` que precisam viajar (licença do upstream).
UFO_RUNTIME_FILES = ("LICENSE",)


def _forbidden_component(name: str) -> bool:
    lowered = name.lower()
    if name in SKIP_NAMES or name in SKIP_DIR_NAMES:
        return True
    if lowered.startswith(".env"):
        return True
    if Path(lowered).suffix in SKIP_SUFFIXES:
        return True
    return any(lowered.endswith(suffix) for suffix in SKIP_NAME_SUFFIXES)


def _relative_is_clean(relative: Path) -> bool:
    """Todos os componentes do caminho relativo passam, não só o último."""
    return not any(_forbidden_component(part) for part in relative.parts)


# Bundle privado gerado no setup: contém a chave de pareamento (32 bytes em hex).
# O JS público valida `secretHex`, mas nunca o contém como literal. Um ficheiro que
# o contenha não pode entrar no payload, mesmo que o nome pareça público.
_PRIVATE_PAIRING_SECRET = re.compile(
    rb'secretHex"?\s*[:=]\s*["\'][0-9a-fA-F]{64}["\']'
)


def _contains_pairing_secret(path: Path) -> bool:
    if path.suffix.lower() not in {".js", ".mjs", ".cjs", ".json"}:
        return False
    try:
        return _PRIVATE_PAIRING_SECRET.search(path.read_bytes()) is not None
    except OSError:
        return True


def _copy_tree(src: Path, dst: Path, written: list[Path], predicate=None) -> None:
    for root, dirs, files in os.walk(src):
        # Poda de verdade: um diretório proibido não é percorrido, então nada abaixo dele entra.
        dirs[:] = sorted(d for d in dirs if not _forbidden_component(d))
        for name in sorted(files):
            if _forbidden_component(name):
                continue
            source = Path(root) / name
            relative = source.relative_to(src)
            if not _relative_is_clean(relative):
                continue
            if predicate is not None and not predicate(relative):
                continue
            if _contains_pairing_secret(source):
                continue
            target = dst / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            written.append(target)


def _rmtree(path: Path) -> None:
    """Remove a árvore anterior mesmo com arquivos marcados somente-leitura pelo Git."""
    def _on_error(func, target, _exc):  # type: ignore[no-untyped-def]
        os.chmod(target, 0o700)
        func(target)

    shutil.rmtree(path, onerror=_on_error)


def _ufo_predicate(locked: set[str]):
    def predicate(relative: Path) -> bool:
        posix = relative.as_posix()
        if posix in locked:
            return True
        head = relative.parts[0] if relative.parts else ""
        return head in UFO_RUNTIME_DIRS or posix in UFO_RUNTIME_FILES

    return predicate


def main() -> int:
    manifest_raw = json.loads(UFO_LOCK.read_text(encoding="utf-8"))
    locked: set[str] = set(manifest_raw["sha256"])
    if not locked:
        print("[assemble] ufo.lock.json sem entradas", file=sys.stderr)
        return 1

    if DEST.exists():
        _rmtree(DEST)
    DEST.mkdir(parents=True)
    written: list[Path] = []

    _copy_tree(REPO / "app", DEST / "app", written)
    _copy_tree(UFO_SRC, DEST / "vendor" / "UFO", written, predicate=_ufo_predicate(locked))

    for name in ("ufo.lock.json", "THIRD_PARTY_NOTICES.md"):
        source = REPO / name
        if not source.is_file():
            print(f"[assemble] ausente: {name}", file=sys.stderr)
            return 1
        target = DEST / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        written.append(target)

    # Pacotes de terceiros cujos avisos precisam viajar com o binário, mais a proveniência do
    # runtime para um rebuild verificável (URL + sha256 do Python e lock das transitivas).
    for name in ("desktop/licenses", "desktop/THIRD-PARTY-MOTION.md"):
        source = REPO / name
        if source.is_dir():
            _copy_tree(source, DEST / name, written)
        elif source.is_file():
            target = DEST / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            written.append(target)
    for name in ("RUNTIME-SOURCE.json", "requirements-runtime.lock"):
        source = REPO / "desktop" / "resources" / name
        if source.is_file():
            target = DEST / name
            shutil.copy2(source, target)
            written.append(target)

    # O worker confere cada arquivo do pin: um único ausente já recusa a inicialização, então
    # o defeito tem de aparecer aqui, na montagem, e não na máquina do testador.
    copied = {str(p.relative_to(DEST / "vendor" / "UFO")).replace("\\", "/")
              for p in written if (DEST / "vendor" / "UFO") in p.parents}
    missing = sorted(locked - copied)
    if missing:
        print(f"[assemble] {len(missing)} arquivo(s) do pin UFO ausente(s); ex.: {missing[:5]}",
              file=sys.stderr)
        return 1

    forbidden = [str(p.relative_to(DEST)) for p in written
                 if not _relative_is_clean(p.relative_to(DEST))]
    if forbidden:
        print(f"[assemble] caminho proibido no payload: {forbidden[:5]}", file=sys.stderr)
        return 1

    manifest = {
        "files": len(written),
        "bytes": sum(p.stat().st_size for p in written),
        "ufo_locked_files_verified": len(locked),
        "sha256": {
            str(p.relative_to(DEST)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(written)
        },
    }
    (DEST / "PAYLOAD-MANIFEST.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[assemble] {manifest['files']} arquivos, {manifest['bytes'] / 1_048_576:.1f} MiB em {DEST}")
    print(f"[assemble] pin UFO conferido: {len(locked)} arquivos presentes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

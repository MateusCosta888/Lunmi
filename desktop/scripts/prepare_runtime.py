"""Limpa e completa o runtime Python privado antes de empacotar (`desktop/resources/python`).

Três problemas reais depois do `pip install`:

* `Scripts/*` são shims de console com shebang absoluto do caminho de build
  (`#!C:\\Users\\<usuario>\\...\\resources\\python\\python.exe`). O produto nunca os usa — chama
  `python.exe -m <módulo>` — e eles vazam o caminho da máquina de desenvolvimento para dentro
  do pacote, que é exatamente o que a higiene proíbe.* Sobram símbolos de depuração e cópias de `python.exe` em `Lib/venv/scripts` que só engordam
  o instalador.
* Falta `comtypes/gen/__init__.py`, que o comtypes **escreve** na primeira importação — no pacote,
  isso é uma escrita dentro dos recursos somente-leitura. `ensure_comtypes_gen_package` deixa o
  arquivo pronto (conteúdo idêntico ao do comtypes) e descarta wrappers de typelib já gerados.

Uso:  python desktop/scripts/prepare_runtime.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "desktop" / "resources" / "python"

# Diretórios removidos por não terem uso em execução.
# `Scripts/` são os shims com shebang absoluto; `Lib/venv/scripts` é uma cópia do intérprete
# para ambientes virtuais que o produto não cria. `pip` permanece: diagnóstico local não é
# motivo para remover ferramenta, e o vazamento de caminho não está nele.
DROP_DIRS = ("Scripts", "Lib/venv/scripts")
# Padrões de arquivo removidos por sufixo.
DROP_SUFFIXES = (".pdb", ".pyc", ".pyo")

# `comtypes` cria `comtypes/gen/__init__.py` na **primeira** importação de `comtypes.client`
# (ver `comtypes/client/_code_cache.py`), e no pacote isso é uma escrita dentro de `resources/`.
# O arquivo é metadado de pacote, não código gerado: ele precisa viajar pronto, com o mesmo
# conteúdo que o comtypes escreveria. Wrappers de typelib (`stdole.py`, `UIAutomationClient.py`)
# continuam **fora** — eles são gerados em memória (`app/computer/com_cache.py`).
COMTYPES_GEN_PACKAGE = "comtypes/gen/__init__.py"
COMTYPES_GEN_CONTENT = "# comtypes.gen package, directory for generated files.\n"
DEFAULT_COMTYPES_GEN = "Lib/site-packages/comtypes/gen"


def _rmtree(path: Path) -> None:
    def _on_error(func, target, _exc):  # type: ignore[no-untyped-def]
        import os

        os.chmod(target, 0o700)
        func(target)

    shutil.rmtree(path, onerror=_on_error)


def ensure_comtypes_gen_package(runtime: Path) -> bool:
    """Deixa `comtypes/gen/__init__.py` pronto e remove wrappers gerados; devolve mudou_algo.

    Sem o `__init__.py` pronto, a primeira importação de `comtypes.client` cria o arquivo dentro
    de `site-packages` — no pacote, dentro dos recursos. Com ele pronto, o comtypes encontra o
    pacote e nada é escrito.
    """
    package = runtime / DEFAULT_COMTYPES_GEN.rsplit("/", 1)[0]
    if not package.is_dir():
        return False  # comtypes não está no runtime: nada a preparar
    gen_dir = runtime / DEFAULT_COMTYPES_GEN
    changed = False
    if not gen_dir.is_dir():
        # Estado de um `pip install` fresco: o comtypes só cria isto na primeira importação.
        gen_dir.mkdir(parents=True)
        changed = True
    for path in sorted(gen_dir.iterdir()):
        if path.is_file() and path.suffix == ".py" and path.name != "__init__.py":
            path.unlink()
            changed = True
    init = gen_dir / "__init__.py"
    if not init.is_file() or init.read_text(encoding="utf-8") != COMTYPES_GEN_CONTENT:
        init.write_text(COMTYPES_GEN_CONTENT, encoding="utf-8")
        changed = True
    return changed


def main() -> int:
    if not (RUNTIME / "python.exe").is_file():
        print(f"[runtime] intérprete ausente em {RUNTIME}", file=sys.stderr)
        return 1
    removed: list[str] = []
    # Bytecode contains build filenames; workers compile in RAM without writing installed resources.
    for cached in list(RUNTIME.rglob('__pycache__')):
        if cached.exists():
            if not cached.resolve().is_relative_to(RUNTIME.resolve()) or cached.is_symlink():
                raise ValueError('Runtime cache escapes its build directory.')
            _rmtree(cached)
            removed.append(str(cached.relative_to(RUNTIME)))
    for relative in DROP_DIRS:
        target = RUNTIME / relative
        if target.exists():
            _rmtree(target)
            removed.append(relative)
    for path in RUNTIME.rglob("*"):
        if path.is_file() and path.suffix.lower() in DROP_SUFFIXES:
            path.unlink()
            removed.append(str(path.relative_to(RUNTIME)))
    ensured = ensure_comtypes_gen_package(RUNTIME)
    print(f"[runtime] removidos {len(removed)} item(ns): {removed[:6]}")
    print(f"[runtime] comtypes/gen/__init__.py pronto: {ensured or 'já estava'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

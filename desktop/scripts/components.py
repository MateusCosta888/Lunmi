"""Componentes pesados do pacote (Python e voz) fora da pasta da aplicação.

Por que existe: o setup completo leva ~2,6 GB de runtime Python e voz em ~23 400 ficheiros.
Dentro de `resources/`, cada atualização removia e voltava a extrair tudo, minutos sem
progresso. Fora dela, em `<pasta de instalação>-runtime/<nome>-<chave>`, o instalador só
extrai um componente quando o conteúdo muda (a chave é o hash do conteúdo) e antes de
remover a versão anterior.

`pack` cria, a partir de `desktop/resources/{python,voice}`:
  * `desktop/build/components/<nome>.7z` — 7z não sólido, dicionário de 1 MB e sem datas, as
    opções diferenciais do electron-builder, para que arquivos iguais rendam blocos iguais;
  * `desktop/build/components/lunmi-components.nsh` — chaves, arquivos e tamanhos para o NSIS;
  * `desktop/resources/lunmi-components.json` — as chaves que a aplicação instalada procura.

`materialize` reproduz a instalação ao lado de um `win-unpacked` para a verificação do pacote.

Uso:  python desktop/scripts/components.py pack
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RESOURCES = REPO / "desktop" / "resources"
OUT = REPO / "desktop" / "build" / "components"
NAMES = ("python", "voice")
# Ficheiro que tem de existir depois de extrair; prova mínima de que o arquivo é o certo.
SENTINELS = {"python": "python.exe", "voice": "runtime/python.exe"}
MANIFEST = "lunmi-components.json"
COMPLETE = ".lunmi-complete"
# O NSIS não gera instaladores com mais de 2 GiB; a margem cobre a app e o próprio NSIS.
MAX_ARCHIVES_BYTES = 1_990_000_000
KEY = re.compile(r"[0-9a-f]{16}")
SEVEN_ZIP_ARGS = ("a", "-t7z", "-mx=9", "-md=1m", "-ms=off", "-mtc=off", "-mtm=off", "-mta=off", "-bd")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_key(root: Path) -> str:
    """Hash estável do conteúdo: caminho relativo e SHA-256 de cada ficheiro, por ordem."""
    digest = hashlib.sha256()
    for relative, path in sorted((p.relative_to(root).as_posix(), p) for p in root.rglob("*") if p.is_file()):
        if relative != COMPLETE:
            digest.update(f"{relative}\0{_sha256(path)}\n".encode("utf-8"))
    return digest.hexdigest()[:16]


def seven_zip() -> str:
    found = subprocess.run(
        ["node", "-e", "require('app-builder-lib/out/toolsets/7zip').getPath7za().then(p=>console.log(p))"],
        cwd=REPO / "desktop", capture_output=True, text=True, check=True)
    return found.stdout.strip()


def _rmtree(path: Path) -> None:
    def _on_error(func, target, _exc):  # type: ignore[no-untyped-def]
        os.chmod(target, 0o700)
        func(target)

    if path.exists():
        shutil.rmtree(path, onerror=_on_error)


def _nsis_string(value: str) -> str:
    if '"' in value or "$" in value:
        raise ValueError(f"caminho impróprio para NSIS: {value}")
    return value


def pack(resources: Path = RESOURCES, out: Path = OUT) -> dict[str, dict[str, object]]:
    tool = seven_zip()
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, dict[str, object]] = {}
    for name in NAMES:
        source = resources / name
        if not (source / SENTINELS[name]).is_file():
            raise SystemExit(f"[components] {name} ausente ou incompleto em {source}")
        files = [p for p in source.rglob("*") if p.is_file()]
        archive = out / f"{name}.7z"
        partial = archive.with_suffix(".7z.partial")
        partial.unlink(missing_ok=True)
        subprocess.run([tool, *SEVEN_ZIP_ARGS, str(partial), "."], cwd=source, check=True,
                       stdout=subprocess.DEVNULL)
        os.replace(partial, archive)
        report[name] = {
            "key": content_key(source),
            "archive": str(archive),
            "archive_bytes": archive.stat().st_size,
            "archive_sha256": _sha256(archive),
            "files": len(files),
            "bytes": sum(p.stat().st_size for p in files),
        }
    # O instalador extrai com o 7za (código de saída fiável; o Nsis7z ignora erros de CRC).
    shutil.copyfile(tool, out / "7za.exe")
    total = sum(int(item["archive_bytes"]) for item in report.values())
    if total > MAX_ARCHIVES_BYTES:
        raise SystemExit(f"[components] {total} bytes excedem o limite de 2 GiB do NSIS")
    defines = [f'!define LUNMI_SEVEN_ZIP "{_nsis_string(str(out / "7za.exe"))}"']
    for name, item in report.items():
        upper = name.upper()
        # Espaço extraído arredondado para cima, em MB, para a verificação de disco do NSIS.
        megabytes = -(-int(item["bytes"]) // 1_048_576)
        defines += [f'!define LUNMI_{upper}_KEY "{item["key"]}"',
                    f'!define LUNMI_{upper}_ARCHIVE "{_nsis_string(str(item["archive"]))}"',
                    f'!define LUNMI_{upper}_MB {megabytes}',
                    f'!define LUNMI_{upper}_ARCHIVE_MB {-(-int(item["archive_bytes"]) // 1_048_576)}']
    (out / "lunmi-components.nsh").write_text("\n".join(defines) + "\n", encoding="utf-8")
    (out / "components.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (resources / MANIFEST).write_text(
        json.dumps({name: item["key"] for name, item in report.items()}, indent=2), encoding="utf-8")
    return report


def runtime_root(unpacked: Path) -> Path:
    """O mesmo irmão `<instalação>-runtime` que o NSIS e `backend.ts` usam."""
    return unpacked.parent / f"{unpacked.name}-runtime"


def manifest_keys(unpacked: Path) -> dict[str, str]:
    """Chaves do manifesto, validadas: delas sai um caminho que depois é apagado."""
    keys = json.loads((unpacked / "resources" / MANIFEST).read_text(encoding="utf-8"))
    for name in NAMES:
        if not isinstance(keys.get(name), str) or not KEY.fullmatch(keys[name]):
            raise SystemExit(f"[components] chave inválida para {name} em {MANIFEST}: {keys.get(name)!r}")
    return {name: keys[name] for name in NAMES}


def materialize(unpacked: Path, out: Path = OUT) -> dict[str, Path]:
    """Extrai os arquivos ao lado de `unpacked`, como o instalador, e confere cada chave."""
    keys = manifest_keys(unpacked)
    tool = seven_zip()
    result: dict[str, Path] = {}
    for name in NAMES:
        target = runtime_root(unpacked) / f"{name}-{keys[name]}"
        _rmtree(target)
        target.mkdir(parents=True)
        subprocess.run([tool, "x", "-y", "-bd", str(out / f"{name}.7z"), f"-o{target}"], check=True,
                       stdout=subprocess.DEVNULL)
        observed = content_key(target)
        if observed != keys[name]:
            raise SystemExit(f"[components] {name}: chave {observed} difere do manifesto {keys[name]}")
        (target / COMPLETE).write_text(keys[name], encoding="utf-8")
        result[name] = target
    return result


def ensure_materialized(unpacked: Path, out: Path = OUT) -> dict[str, Path]:
    """Reaproveita uma extração já conferida (marcador com a chave certa) ou extrai de novo."""
    keys = manifest_keys(unpacked)
    targets = {name: runtime_root(unpacked) / f"{name}-{keys[name]}" for name in NAMES}
    if all((t / COMPLETE).is_file() and (t / COMPLETE).read_text(encoding="utf-8") == keys[n]
           for n, t in targets.items()):
        return targets
    return materialize(unpacked, out)


def main() -> int:
    if sys.argv[1:] != ["pack"]:
        print("uso: python desktop/scripts/components.py pack", file=sys.stderr)
        return 2
    for name, item in pack().items():
        print(f"[components] {name}-{item['key']}: {item['files']} ficheiros, "
              f"{int(item['archive_bytes']) / 1_048_576:.1f} MiB comprimidos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

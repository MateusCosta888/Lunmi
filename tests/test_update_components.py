"""Componentes do instalador (`desktop/scripts/components.py`).

Python e voz saem de `resources/` para `<instalação>-runtime/<nome>-<chave>`: a chave decide se
o instalador volta a extrair um componente, e o manifesto decide o caminho que a aplicação usa.
Uma chave instável reextrairia 2,6 GB em cada atualização; uma chave cega a mudanças deixaria
a versão nova correr com o runtime antigo.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _components():
    path = REPO / "desktop" / "scripts" / "components.py"
    spec = importlib.util.spec_from_file_location("components", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(root: Path, files: dict[str, bytes]) -> Path:
    for relative, data in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_bytes(data)
    return root


def test_content_key_is_stable_and_tracks_names_and_bytes(tmp_path: Path) -> None:
    components = _components()
    files = {"python.exe": b"interp", "Lib/a.py": b"a = 1", "Lib/b/c.py": b"c"}
    first = components.content_key(_tree(tmp_path / "one", files))
    # Ordem de criação e o marcador de extração não mudam a chave.
    reordered = _tree(tmp_path / "two", dict(reversed(list(files.items()))))
    (reordered / components.COMPLETE).write_text("marker", encoding="utf-8")
    assert components.content_key(reordered) == first
    assert len(first) == 16 and int(first, 16) >= 0

    renamed = _tree(tmp_path / "three", {**files, "Lib/a2.py": files["Lib/a.py"]})
    (renamed / "Lib/a.py").unlink()
    changed = _tree(tmp_path / "four", {**files, "Lib/a.py": b"a = 2"})
    assert components.content_key(renamed) != first
    assert components.content_key(changed) != first


def test_nsis_defines_refuse_paths_that_would_break_the_script() -> None:
    components = _components()
    assert components._nsis_string(r"C:\build\components\voice.7z") == r"C:\build\components\voice.7z"
    for bad in ('C:\\a"b', "C:\\$PLUGINSDIR"):
        with pytest.raises(ValueError):
            components._nsis_string(bad)


@pytest.mark.skipif(not (REPO / "desktop/node_modules/app-builder-lib").is_dir() or shutil.which("node") is None,
                    reason="7za do electron-builder indisponível antes de npm ci")
def test_pack_and_materialize_reproduce_the_installed_layout(tmp_path: Path) -> None:
    components = _components()
    resources = tmp_path / "resources"
    _tree(resources / "python", {"python.exe": b"py", "Lib/site.py": b"site"})
    _tree(resources / "voice", {"runtime/python.exe": b"voice", "models/m.bin": bytes(range(256)) * 64})
    out = tmp_path / "build" / "components"
    report = components.pack(resources, out)

    keys = json.loads((resources / components.MANIFEST).read_text(encoding="utf-8"))
    assert keys == {name: report[name]["key"] for name in components.NAMES}
    nsh = (out / "lunmi-components.nsh").read_text(encoding="utf-8")
    assert f'!define LUNMI_VOICE_KEY "{keys["voice"]}"' in nsh and "!define LUNMI_PYTHON_MB 1" in nsh

    unpacked = tmp_path / "win-unpacked"
    (unpacked / "resources").mkdir(parents=True)
    shutil.copy(resources / components.MANIFEST, unpacked / "resources" / components.MANIFEST)
    dirs = components.materialize(unpacked, out)
    assert dirs["python"] == tmp_path / "win-unpacked-runtime" / f"python-{keys['python']}"
    assert (dirs["voice"] / "models/m.bin").read_bytes() == bytes(range(256)) * 64
    assert components.ensure_materialized(unpacked, out) == dirs

    # Um arquivo que não corresponde ao manifesto nunca é aceite como componente desta versão.
    (unpacked / "resources" / components.MANIFEST).write_text(
        json.dumps({**keys, "python": "0" * 16}), encoding="utf-8")
    with pytest.raises(SystemExit):
        components.materialize(unpacked, out)


@pytest.mark.parametrize("key", ["../../outside", "x/../../outside", "ABCDEF0123456789", "abc", None])
def test_manifest_key_that_is_not_a_content_hash_is_refused_before_any_removal(tmp_path: Path, key: object) -> None:
    components = _components()
    unpacked = tmp_path / "win-unpacked"
    (unpacked / "resources").mkdir(parents=True)
    (unpacked / "resources" / "lunmi-components.json").write_text(
        json.dumps({"python": key, "voice": "0123456789abcdef"}), encoding="utf-8")
    outside = _tree(tmp_path / "outside", {"keep.txt": b"must survive"})
    with pytest.raises(SystemExit, match="chave inválida"):
        components.ensure_materialized(unpacked, tmp_path / "no-archives")
    assert (outside / "keep.txt").read_bytes() == b"must survive"

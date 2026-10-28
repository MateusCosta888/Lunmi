"""Higiene do payload do instalador alpha (`desktop/scripts/assemble_resources.py`).

Regressão do defeito real: o montador usava `rglob` e só descartava o diretório proibido, sem
podar os descendentes — 29 arquivos de `vendor/UFO/.git` entraram no payload, junto com
`config/index/hooks/logs`. Estes testes cobrem o filtro por *todos* os componentes do caminho,
a poda de verdade na cópia e os sidecars de SQLite sem sufixo próprio.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ASSEMBLER_PATH = REPO / "desktop" / "scripts" / "assemble_resources.py"


def _load_assembler():
    spec = importlib.util.spec_from_file_location("assemble_resources", ASSEMBLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_script(name: str):
    path = REPO / "desktop" / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_payload_auditor_rejects_local_paths_without_hardcoded_usernames() -> None:
    audit = _load_script("audit_payload.py")
    for base in (Path.home(), REPO):
        for value in (str(base), str(base).replace("\\", "/"), str(base).replace("\\", "\\\\")):
            assert audit._scan_bytes(Path("resources/app/sample.py"), value.encode("utf-8"))
    assert audit._scan_bytes(Path("resources/app/sample.py"), b"print('hello')") is None


def test_public_build_metadata_exception_requires_exact_path_and_bytes(monkeypatch) -> None:
    audit = _load_script("audit_payload.py")
    relative = Path("resources/python/Lib/site-packages/numpy/__config__.py")
    content = b"public wheel build path: fixture-build-home"
    monkeypatch.setattr(audit, "PERSONAL_PATHS", (b"fixture-build-home",))
    monkeypatch.setitem(audit.PUBLIC_BUILD_METADATA_SHA256, relative.as_posix(),
                        hashlib.sha256(content).hexdigest())
    assert audit._scan_bytes(relative, content) is None
    assert audit._scan_bytes(relative, content + b" changed")
    assert audit._scan_bytes(Path("resources/app/sample.py"), content)


def test_public_build_metadata_exception_does_not_skip_secret_checks(monkeypatch) -> None:
    audit = _load_script("audit_payload.py")
    relative = Path("resources/python/Lib/site-packages/numpy-2.4.6.dist-info/DELVEWHEEL")
    content = b"fixture-build-home sk-" + b"x" * 30
    monkeypatch.setattr(audit, "PERSONAL_PATHS", (b"fixture-build-home",))
    monkeypatch.setitem(audit.PUBLIC_BUILD_METADATA_SHA256, relative.as_posix(),
                        hashlib.sha256(content).hexdigest())
    assert "credencial" in audit._scan_bytes(relative, content)


def test_a_forbidden_component_anywhere_in_the_path_is_rejected() -> None:
    assemble = _load_assembler()
    # O defeito: só o último componente era olhado, então o que estava *dentro* de um
    # diretório proibido passava.
    assert assemble._relative_is_clean(Path("vendor/UFO/.git/config")) is False
    assert assemble._relative_is_clean(Path("app/logs/tasks.jsonl")) is False
    assert assemble._relative_is_clean(Path("app/.local/memory.sqlite3")) is False
    assert assemble._relative_is_clean(Path("app/__pycache__/mod.cpython-311.pyc")) is False
    assert assemble._relative_is_clean(Path("x/.env")) is False
    assert assemble._relative_is_clean(Path("x/.env.local")) is False
    assert assemble._relative_is_clean(Path("vendor/UFO/ufo/__init__.py")) is True


def test_sqlite_sidecars_are_rejected_even_without_their_own_suffix() -> None:
    assemble = _load_assembler()
    for name in ("memory.sqlite3-wal", "memory.sqlite3-shm", "provider.dpapi-wal",
                 "store.db-journal", "cache.sqlite3-journal"):
        assert assemble._forbidden_component(name) is True, name
    assert assemble._forbidden_component("worker.py") is False


def test_copy_prunes_forbidden_directories_and_keeps_content_inside(tmp_path: Path) -> None:
    assemble = _load_assembler()
    source = tmp_path / "src"
    (source / ".git" / "objects").mkdir(parents=True)
    (source / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    (source / "logs").mkdir()
    (source / "logs" / "tasks.jsonl").write_text("{}\n", encoding="utf-8")
    (source / "app").mkdir()
    (source / "app" / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    (source / "app" / ".local").mkdir()
    (source / "app" / ".local" / "memory.sqlite3-wal").write_bytes(b"dados pessoais")
    (source / "app" / "__pycache__").mkdir()
    (source / "app" / "__pycache__" / "ok.pyc").write_bytes(b"\x00\x01")

    destination = tmp_path / "dest"
    written: list[Path] = []
    assemble._copy_tree(source, destination, written)

    relative = sorted(str(p.relative_to(destination)).replace("\\", "/") for p in written)
    assert relative == ["app/ok.py"]


def test_pin_ufo_allowlist_partitions_the_vendored_tree() -> None:
    assemble = _load_assembler()
    locked = set(json.loads((REPO / "ufo.lock.json").read_text(encoding="utf-8"))["sha256"])
    assert len(locked) == 501
    predicate = assemble._ufo_predicate(locked)
    # O pin manda; os recursos comprovados (config/ufo e prompts) acompanham.
    assert predicate(Path("ufo/agents/agent/host_agent.py")) is True
    # O pin inclui `tests/` do UFO: `check_source` confere os 501 arquivos antes de importar,
    # então recortá-los faria o worker real recusar a inicialização no pacote.
    sample_test = next(item for item in locked if item.startswith("tests/"))
    assert predicate(Path(sample_test)) is True
    assert predicate(Path("ufo/prompts/share/base/host_agent.yaml")) is True
    assert predicate(Path("config/ufo/system.yaml")) is True
    assert predicate(Path("LICENSE")) is True
    # Ruído de repositório (docs do GitHub, frontend de avaliação) fica fora.
    assert predicate(Path(".github/workflows/ci.yml")) is False
    assert predicate(Path("galaxy/src/App.tsx")) is False
    assert predicate(Path("assets/screenshot.png")) is False


# --- Recursos somente-leitura: nada de código gerado em execução dentro do pacote -----------


def test_com_cache_configures_generation_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """`comtypes` gravava wrappers de typelib dentro de `resources/python/.../comtypes/gen`."""
    import sys
    import types

    fake_client = types.SimpleNamespace(gen_dir=str(REPO / "comtypes" / "gen"))
    monkeypatch.setitem(sys.modules, "comtypes", types.SimpleNamespace(client=fake_client))
    monkeypatch.setitem(sys.modules, "comtypes.client", fake_client)

    from app.computer.com_cache import use_in_memory_com_modules

    assert use_in_memory_com_modules() is True
    assert fake_client.gen_dir is None


def test_bridge_entry_disables_on_disk_com_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """O processo da ponte também pode importar UIA: precisa configurar antes da cadeia."""
    comtypes_client = pytest.importorskip("comtypes.client")
    import app.bridge.server as server_module

    monkeypatch.setattr(comtypes_client, "gen_dir", str(REPO / "comtypes" / "gen"))
    importlib.reload(server_module)
    assert comtypes_client.gen_dir is None


def test_worker_entry_disables_on_disk_com_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    comtypes_client = pytest.importorskip("comtypes.client")
    import app.computer.worker as worker_module

    monkeypatch.setattr(comtypes_client, "gen_dir", str(REPO / "comtypes" / "gen"))
    importlib.reload(worker_module)
    assert comtypes_client.gen_dir is None


def test_worker_env_disables_bytecode_writes_and_keeps_user_paths() -> None:
    """O worker importa `vendor/UFO` e `site-packages`: sem a flag, o cache ia para `resources/`."""
    from app.computer.ufo_engine import build_worker_env
    from app.settings import Settings

    settings = Settings()
    env = build_worker_env(
        settings, host_pids={42, 7},
        environ={"PATH": "C:/bin", "OPENAI_API_KEY": "fixture-nao-real"},
    )
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["LUNMI_STATE_ROOT"] == str(settings.state_root)
    assert env["OUTPUT_DIRECTORY"] == str(settings.output_path)
    assert env["UFO_PATH"] == str(settings.ufo_path)
    assert env["UFO_ASSISTANT_PIDS"] == "7,42"
    assert "OPENAI_API_KEY" not in env, "credencial do cérebro não vai para o executor"
    assert env["PATH"] == "C:/bin"


def test_resource_drift_counts_creations_changes_and_deletions() -> None:
    """O comparador do aceite precisa enxergar remoções: snapshot só antes do worker não cobria."""
    verify = _load_script("verify_package.py")
    drift = verify._resource_drift(
        {"a.py": 1, "b.py": 1, "c.py": 1},
        {"a.py": 2, "c.py": 1, "d.py": 1},
    )
    assert drift["created"] == ["d.py"]
    assert drift["changed"] == ["a.py"]
    assert drift["deleted"] == ["b.py"]
    assert all(not paths for paths in verify._resource_drift({}, {}).values())


def test_pre_generated_wrappers_flags_only_what_the_product_generates(tmp_path: Path) -> None:
    verify = _load_script("verify_package.py")
    resources = tmp_path / "resources"
    (resources / "app" / "__pycache__").mkdir(parents=True)
    (resources / "app" / "__pycache__" / "worker.cpython-311.pyc").write_bytes(b"\x00")
    (resources / "app" / "worker.py").write_text("x = 1\n", encoding="utf-8")
    (resources / "python" / "Lib" / "site-packages" / "openai").mkdir(parents=True)
    (resources / "python" / "Lib" / "site-packages" / "openai" / "mod.py").write_text(
        "x = 1\n", encoding="utf-8")
    (resources / "python" / "Lib" / "site-packages" / "openai" / "__pycache__").mkdir()
    (resources / "python" / "Lib" / "site-packages" / "openai" / "__pycache__" / "mod.pyc").write_bytes(b"\x00")
    gen = resources / "python" / "Lib" / "site-packages" / "comtypes" / "gen"
    gen.mkdir(parents=True)
    (gen / "stdole.py").write_text("x = 1\n", encoding="utf-8")
    # Metadado do pacote: precisa viajar pronto e **não** é wrapper pré-gerado.
    (gen / "__init__.py").write_text(
        "# comtypes.gen package, directory for generated files.\n", encoding="utf-8")
    (gen / "__pycache__").mkdir()
    (gen / "__pycache__" / "__init__.cpython-311.pyc").write_bytes(b"\x00")

    found = sorted(Path(p).name for p in verify._pre_generated_wrappers(resources))
    assert found == ["stdole.py", "worker.cpython-311.pyc"]
    # `__pycache__` de site-packages vem do `pip install` e viaja na lista instalada: não é
    # wrapper pré-gerado pelo produto.
    assert not any("openai" in p for p in verify._pre_generated_wrappers(resources))


def test_prepare_runtime_readies_the_comtypes_gen_package(tmp_path: Path) -> None:
    """Sem o `__init__.py` pronto, a primeira importação de comtypes escreve em `site-packages`."""
    prepare = _load_script("prepare_runtime.py")
    runtime = tmp_path / "runtime"
    gen = runtime / "Lib" / "site-packages" / "comtypes" / "gen"
    gen.mkdir(parents=True)
    (gen / "stdole.py").write_text("# gerado em execução\n", encoding="utf-8")

    assert prepare.ensure_comtypes_gen_package(runtime) is True
    assert (gen / "__init__.py").read_text(encoding="utf-8") == prepare.COMTYPES_GEN_CONTENT
    assert not (gen / "stdole.py").exists(), "wrapper gerado não pode viajar no payload"
    assert prepare.ensure_comtypes_gen_package(runtime) is False

    # Estado de um `pip install` fresco: a pasta `gen/` nem existe ainda.
    fresh = tmp_path / "fresh"
    (fresh / "Lib" / "site-packages" / "comtypes").mkdir(parents=True)
    assert prepare.ensure_comtypes_gen_package(fresh) is True
    assert (fresh / prepare.DEFAULT_COMTYPES_GEN / "__init__.py").is_file()

    assert prepare.ensure_comtypes_gen_package(tmp_path / "sem-comtypes") is False, "no-op"


@pytest.mark.skipif(
    not (REPO / "desktop" / "resources" / "app").is_dir(),
    reason="payload do instalador ainda não montado nesta árvore",
)
def test_the_mounted_payload_has_no_forbidden_paths() -> None:
    assemble = _load_assembler()
    payload = REPO / "desktop" / "resources" / "app"
    offenders = [
        str(path.relative_to(payload)).replace("\\", "/")
        for path in payload.rglob("*")
        if not assemble._relative_is_clean(path.relative_to(payload))
    ]
    assert offenders == []

"""Aceite do PACOTE: exercita o artefato extraído, sem o checkout, .venv, Node ou Python do dev.

O que este script prova, contra `desktop/release/win-unpacked` (ou outra raiz):

1. o backend empacotado sobe por stdio e completa `session.hello -> session.ready`;
2. `memory.list` cria o banco **na raiz de dados do utilizador** (`LUNMI_USER_DATA`), nunca
   dentro dos recursos instalados, e a base nova passa `integrity_check`, `foreign_key_check`,
   FTS5 e as migrações do esquema;
3. nada é escrito **nem apagado** sob `resources/` (recursos tratados como somente-leitura) — a
   comparação é feita **depois do worker**, que é justamente o caminho que importa UIA/comtypes;
   o payload também precisa chegar sem wrappers gerados (`comtypes/gen`, `__pycache__` em `app/`),
   senão o "primeiro uso" não estaria sendo exercitado;
4. o worker UFO **real** do pacote inicializa (`{"ready": true}`), responde a uma operação de
   observação sem inferência e encerra limpo;
5. o intérprete executado é o Python do pacote, não o da máquina. Com `lunmi-components.json`,
   Python e voz são extraídos dos arquivos de componentes para `<raiz>-runtime/`, como o
   instalador faz, e a chave de conteúdo de cada um é conferida; nenhum pode continuar dentro
   de `resources/`.

Não usa GUI, não faz inferência, não abre provedor e não toca aplicativos pessoais.

Uso:  python desktop/scripts/verify_package.py [raiz_unpacked] [pasta_de_evidencia]
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import components  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = REPO / "desktop" / "release" / "win-unpacked"
WORKER_STARTUP_SECONDS = 300.0


def _snapshot(root: Path) -> dict[str, int]:
    return {str(p): p.stat().st_mtime_ns for p in root.rglob("*") if p.is_file()}


def _voice_assets_valid(bundle: Path) -> bool:
    try:
        pin = json.loads((REPO / 'desktop/resources/VOICE-SOURCE.json').read_text(encoding='utf-8'))
        if json.loads((bundle / 'VOICE-SOURCE.json').read_text(encoding='utf-8')) != pin:
            return False
        if (bundle / 'runtime/Lib/site-packages/nvidia').exists():
            return False
        for item in pin['files']:
            file = bundle / item['path']
            if not file.resolve().is_relative_to(bundle.resolve()) or file.stat().st_size != item['bytes']:
                return False
            with file.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != item['sha256']:
                    return False
        return True
    except (OSError, ValueError, KeyError):
        return False


def _resource_drift(before: dict[str, int], after: dict[str, int]) -> dict[str, list[str]]:
    """Drift dos recursos: criados, alterados e apagados. O aceite exige os três vazios."""
    created = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    changed = sorted(p for p in set(after) & set(before) if after[p] != before[p])
    return {"created": created, "changed": changed, "deleted": deleted}


def _pre_generated_wrappers(resources: Path) -> list[str]:
    """Wrappers que já viajariam prontos e mascarariam o teste de "primeiro uso".

    Só o que **este produto** pode gerar em execução: módulos de typelib em `comtypes/gen/` e
    `__pycache__` dentro de `app/`. Ficam de fora, de propósito:

    * `comtypes/gen/__init__.py` — metadado do pacote que o comtypes escreveria na primeira
      importação (o montador deixa pronto; ver `prepare_runtime.py`);
    * `comtypes/gen/__pycache__/**` — bytecode do metadado, criado pelo `pip install`;
    * `__pycache__` de `site-packages` — também do `pip install`, viaja na lista instalada.
    """
    found: list[str] = []
    app_tree = resources / "app"
    for path in resources.rglob("*"):
        if not path.is_file():
            continue
        parts = path.parts
        if "comtypes" in parts and "gen" in parts:
            relative = path.parts[parts.index("gen") + 1:]
            if relative and relative[0] != "__pycache__" and relative != ("__init__.py",):
                found.append(str(path))
        elif path.suffix == ".pyc" and "__pycache__" in parts and app_tree in path.parents:
            found.append(str(path))
    return sorted(found)


def _run_bridge(app_root: Path, python: Path, voice: Path, user_data: Path) -> dict[str, object]:
    env = dict(os.environ)
    env.update({
        "LUNMI_USER_DATA": str(user_data),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    })
    env.pop("LLM_PROVIDER", None)
    env.pop("LLM_MODEL", None)
    env.pop('LUNMI_VOICE_HOME', None)
    env['LUNMI_BUNDLED_VOICE_HOME'] = str(voice)
    proc = subprocess.Popen(
        [str(python), "-u", "-m", "app.bridge.server"],
        cwd=str(app_root), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    script = "\n".join([
        json.dumps({"v": 1, "id": "hello", "type": "session.hello", "payload": {"client": "verify"}}),
        json.dumps({"v": 1, "id": "mem", "type": "memory.list", "payload": {"limit": 10}}),
        json.dumps({'v': 1, 'id': 'voice_out', 'type': 'voice.get', 'payload': {}}),
        json.dumps({'v': 1, 'id': 'voice_in', 'type': 'voice.input.get', 'payload': {}}),
        json.dumps({"v": 1, "id": "shut", "type": "app.shutdown", "payload": {}}),
        "",
    ]).encode("utf-8")
    try:
        out, err = proc.communicate(input=script, timeout=180)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return {"rc": None, "timeout": True, "stdout": out.decode("utf-8", "replace"),
                "stderr": err.decode("utf-8", "replace"), "messages": []}
    messages = [json.loads(line) for line in out.decode("utf-8", "replace").splitlines() if line.strip()]
    return {"rc": proc.returncode, "timeout": False, "messages": messages,
            "stdout": out.decode("utf-8", "replace"), "stderr": err.decode("utf-8", "replace")}


def _run_worker(app_root: Path, python: Path, user_data: Path) -> dict[str, object]:
    env = dict(os.environ)
    env.update({
        "UFO_PATH": str(app_root / "vendor" / "UFO"),
        "OUTPUT_DIRECTORY": str(user_data / "output"),
        "LUNMI_STATE_ROOT": str(user_data),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    (user_data / "output").mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(python), "-u", str(app_root / "app" / "computer" / "worker.py")],
        cwd=str(app_root), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    ready = False
    observed: dict[str, object] | None = None
    started = time.perf_counter()
    deadline = started + WORKER_STARTUP_SECONDS
    while time.perf_counter() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("ready") is True:
            ready = True
            proc.stdin.write((json.dumps({"operation": "verify_notepad", "host_pids": []}) + "\n").encode())
            proc.stdin.flush()
            continue
        if "ok" in message:
            observed = message
            break
    if ready and observed is None:
        observed = {"ok": False, "error": "sem resposta a verify_notepad"}
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        rc = proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = proc.wait()
    stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    return {"ready": ready, "observed": observed, "rc": rc,
            "startup_ms": round((time.perf_counter() - started) * 1000),
            "stderr_tail": stderr[-1200:]}


def _inspect_database(path: Path) -> dict[str, object]:
    result: dict[str, object] = {"exists": path.is_file(), "path": str(path)}
    if not path.is_file():
        return result
    con = sqlite3.connect(str(path))
    try:
        result["integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
        result["foreign_keys"] = con.execute("PRAGMA foreign_key_check").fetchall()
        result["user_version"] = con.execute("PRAGMA user_version").fetchone()[0]
        tables = sorted(row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
        result["tables"] = tables
        result["fts5"] = bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE name LIKE '%_fts%' LIMIT 1").fetchone())
        try:
            con.execute("CREATE VIRTUAL TABLE temp.__fts5_probe USING fts5(x)")
            result["fts5_available"] = True
            con.execute("DROP TABLE temp.__fts5_probe")
        except sqlite3.Error as exc:
            result["fts5_available"] = f"erro: {exc}"
    finally:
        con.close()
    return result


def main() -> int:
    root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_ROOT
    evidence = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else REPO / "output" / "alpha-acceptance"
    app_root = root / "resources" / "app"
    # Python e voz chegam como componentes: extraídos ao lado da raiz, como faz o instalador.
    if (root / "resources" / components.MANIFEST).is_file():
        component_dirs = components.materialize(root)
    else:
        component_dirs = {name: root / "resources" / name for name in components.NAMES}
    python = component_dirs["python"] / "python.exe"
    if not python.is_file():
        print(f"[verify] runtime ausente: {python}", file=sys.stderr)
        return 1
    # Com manifesto, uma cópia dentro de resources/ voltaria a pesar em cada atualização.
    legacy_inside = sorted(name for name in components.NAMES if (root / "resources" / name).exists()
                           and component_dirs[name] != root / "resources" / name)
    trees = [root / "resources", *component_dirs.values()]
    evidence.mkdir(parents=True, exist_ok=True)
    # Diretório próprio, com espaços e acentos, como pede o aceite.
    user_data = evidence / "dados do testador çã"
    if user_data.exists():
        shutil.rmtree(user_data)
    user_data.mkdir(parents=True)

    before = {k: v for tree in trees for k, v in _snapshot(tree).items()}
    pre_generated = [p for tree in trees for p in _pre_generated_wrappers(tree)]
    report: dict[str, object] = {"unpacked_root": str(root), "python": str(python),
                                 "components": {k: str(v) for k, v in component_dirs.items()},
                                 "components_inside_resources": legacy_inside,
                                 "user_data": str(user_data),
                                 "pre_generated_wrappers": pre_generated[:20],
                                 "pre_generated_wrappers_total": len(pre_generated)}

    bridge = _run_bridge(app_root, python, component_dirs["voice"], user_data)
    report["bridge"] = {k: v for k, v in bridge.items() if k not in {"stdout"}}
    report["bridge_messages"] = [m.get("type") for m in bridge["messages"]]
    report["bridge_ready"] = any(m.get("type") == "session.ready" for m in bridge["messages"])
    report["bridge_memory_result"] = next(
        (m.get("payload") for m in bridge["messages"] if m.get("id") == "mem"), None)

    report["database"] = _inspect_database(user_data / ".local" / "memory.sqlite3")
    report['voice'] = {name: next((m.get('payload', {}) for m in bridge['messages'] if m.get('id') == name), {})
                       for name in ('voice_out', 'voice_in')}
    report['voice_assets_valid'] = _voice_assets_valid(component_dirs['voice'])

    # O worker fica por último de propósito: é o caminho que importa `vendor/UFO` e a pilha de
    # UIA, onde os wrappers COM eram gravados. Com o snapshot antigo (antes do worker) o PASS de
    # "nada escrito" não cobria justamente quem escrevia.
    worker = _run_worker(app_root, python, user_data)
    report["worker"] = worker

    drift = _resource_drift(before, {k: v for tree in trees for k, v in _snapshot(tree).items()})
    report["resources_drift"] = {name: paths[:20] for name, paths in drift.items()}
    report["resources_drift_totals"] = {name: len(paths) for name, paths in drift.items()}

    (evidence / "package-verification.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({
        "bridge_ready": report["bridge_ready"],
        "bridge_rc": bridge["rc"],
        "memory": report["bridge_memory_result"],
        "resources_drift_totals": report["resources_drift_totals"],
        "pre_generated_wrappers": len(pre_generated),
        "database": report["database"],
        "worker_ready": worker["ready"],
        "worker_observed_ok": (worker["observed"] or {}).get("ok"),
        "worker_rc": worker["rc"],
        'voice_available': {name: value.get('available') for name, value in report['voice'].items()},
        'voice_assets_valid': report['voice_assets_valid'],
        'components_inside_resources': legacy_inside,
    }, indent=2, ensure_ascii=False))

    ok = (
        not legacy_inside
        and report["bridge_ready"] is True
        and bridge["rc"] == 0
        and not pre_generated
        and all(not paths for paths in drift.values())
        and report["database"].get("integrity") == "ok"
        and report["database"].get("foreign_keys") == []
        and report["database"].get("fts5") is True
        and worker["ready"] is True
        and (worker["observed"] or {}).get("ok") is True
        and worker["rc"] == 0
        and all(value.get('available') is True for value in report['voice'].values())
        and report['voice_assets_valid'] is True
    )
    print(f"[verify] evidência: {evidence / 'package-verification.json'}")
    print(f"[verify] RESULTADO: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

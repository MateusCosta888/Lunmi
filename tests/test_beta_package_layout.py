"""Beta package acceptance helpers: synthetic trees only, no shipped executables run."""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def verify(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "desktop/scripts"))
    import verify_package
    return verify_package


@pytest.fixture
def mounted(tmp_path, verify):
    root = tmp_path / "beta-root/versions/1.0.0-beta.1"
    (root / "resources/app").mkdir(parents=True)
    (root / "Lunmi.exe").write_bytes(b"fixture launcher, never executed")
    (root / "Lunmi-app.exe").write_bytes(b"fixture, never executed")
    (root / "resources/app.asar").write_bytes(b"fixture")
    keys, directories = {}, {}
    for name, sentinel in verify.COMPONENTS.items():
        source = tmp_path / name
        target = source / sentinel
        target.parent.mkdir(parents=True)
        target.write_bytes(b"fixture, never executed")
        key = verify._pack_content_key(source)
        directory = root.parent.parent / "store" / f"{name}-{key}"
        directory.parent.mkdir(exist_ok=True)
        source.rename(directory)
        (directory / ".complete").write_text(key, encoding="utf-8")
        keys[name], directories[name] = key, directory
    (root / "resources/lunmi-components.json").write_text(json.dumps(keys), encoding="utf-8")
    return root, directories


def test_pack_key_matches_rust_contract_and_ignores_only_root_marker(tmp_path, verify):
    (tmp_path / "z").write_bytes(b"z")
    (tmp_path / "á").mkdir()
    (tmp_path / "á/.complete").write_bytes(b"payload")
    records = b"".join(
        f"{name}\0{len(blob)}\0{hashlib.sha256(blob).hexdigest()}\n".encode("utf-8")
        for name, blob in [("z", b"z"), ("á/.complete", b"payload")]
    )
    expected = hashlib.sha256(records).hexdigest()[:16]
    assert verify._pack_content_key(tmp_path) == expected
    (tmp_path / ".complete").write_text("not payload")
    assert verify._pack_content_key(tmp_path) == expected
    (tmp_path / "z").write_bytes(b"x")
    assert verify._pack_content_key(tmp_path) != expected


def test_beta_layout_resolves_content_addressed_store(mounted, verify):
    root, directories = mounted
    assert verify.component_directories(root) == directories


@pytest.mark.parametrize("damage", ["key", "shape", "marker", "hash", "sentinel", "duplicate", "version"])
def test_beta_layout_rejects_invalid_or_incomplete_payload(mounted, verify, damage):
    root, directories = mounted
    manifest = root / "resources/lunmi-components.json"
    if damage == "key":
        manifest.write_text(json.dumps({"python": "../outside", "voice": "f" * 16}))
    elif damage == "shape":
        manifest.write_text("[]")
    elif damage == "marker":
        (directories["python"] / ".complete").write_text("wrong")
    elif damage == "hash":
        (directories["python"] / "python.exe").write_bytes(b"corrupt")
    elif damage == "sentinel":
        (directories["voice"] / "runtime/python.exe").unlink()
    elif damage == "duplicate":
        (root / "resources/python").mkdir()
    else:
        root = root.rename(root.with_name("0.1.0-alpha.8"))
    with pytest.raises((OSError, ValueError)):
        verify.component_directories(root)


def test_beta_layout_rejects_directory_junction_without_reading_its_contents(mounted, verify, tmp_path):
    root, directories = mounted
    outside = tmp_path / "outside"
    outside.mkdir()
    link = directories["python"] / "linked"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    try:
        with pytest.raises(ValueError, match="ligação"):
            verify.component_directories(root)
    finally:
        link.rmdir()


def test_beta_path_budget_checks_actual_and_fixed_install_paths(mounted, verify, monkeypatch, tmp_path):
    root, directories = mounted
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert verify._beta_path_budget_failure(root, directories) == ""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / ("u" * 170)))
    assert "caminho com" in verify._beta_path_budget_failure(root, directories)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(verify, "MAX_PATH", len(str(directories["python"] / "python.exe")) - 1)
    assert "caminho com" in verify._beta_path_budget_failure(root, directories)


def test_invalid_layout_fails_before_runtime_or_evidence_cleanup(mounted, verify, monkeypatch, tmp_path):
    root, directories = mounted
    evidence = tmp_path / "evidence"
    personal = evidence / "dados do testador çã/keep.txt"
    personal.parent.mkdir(parents=True)
    personal.write_text("preserve")
    (directories["python"] / ".complete").unlink()
    monkeypatch.setattr(verify.sys, "argv", ["verify_package.py", str(root), str(evidence)])
    monkeypatch.setattr(verify.subprocess, "Popen", lambda *a, **k: pytest.fail("runtime executed"))
    assert verify.main() == 1
    assert personal.read_text() == "preserve"


def test_evidence_cannot_be_under_installed_tree(mounted, verify, monkeypatch):
    root, _ = mounted
    monkeypatch.setattr(verify.sys, "argv", ["verify_package.py", str(root), str(root / "evidence")])
    assert verify.main() == 1
    assert not (root / "evidence").exists()


@pytest.mark.parametrize("missing", ["Lunmi.exe", "Lunmi-app.exe"])
def test_both_launcher_and_electron_executables_are_required(mounted, verify, monkeypatch, tmp_path, missing):
    root, _ = mounted
    evidence = tmp_path / "evidence"
    (root / missing).unlink()
    monkeypatch.setattr(verify.sys, "argv", ["verify_package.py", str(root), str(evidence)])
    monkeypatch.setattr(verify.subprocess, "Popen", lambda *a, **k: pytest.fail("runtime executed"))
    assert verify.main() == 1
    assert not evidence.exists()


def test_acceptance_allocates_new_data_without_deleting_existing_junction(mounted, verify, monkeypatch, tmp_path):
    root, _ = mounted
    evidence, outside = tmp_path / "evidence", tmp_path / "outside"
    evidence.mkdir()
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("preserve")
    link = evidence / "dados do testador çã"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    monkeypatch.setattr(verify.sys, "argv", ["verify_package.py", str(root), str(evidence)])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))

    def stop_before_runtime(data):
        assert data.parent == evidence and data != link
        assert data.name.startswith("dados do testador çã-") and data.is_dir()
        raise RuntimeError("stop before runtime")

    monkeypatch.setattr(verify, "_stage_downloaded_models", stop_before_runtime)
    monkeypatch.setattr(verify.subprocess, "Popen", lambda *a, **k: pytest.fail("runtime executed"))
    try:
        with pytest.raises(RuntimeError, match="stop before runtime"):
            verify.main()
        assert link.exists() and marker.read_text() == "preserve"
    finally:
        link.rmdir()


def test_bridge_and_worker_use_minimal_environment(verify, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret")
    monkeypatch.setenv("PYTHONPATH", "personal-path")
    monkeypatch.setenv("LUNMI_VOICE_HOME", "personal-voice")
    captured = []

    class Process:
        returncode = 0
        def __init__(self, *args, **kwargs):
            captured.append(kwargs["env"])
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b'{"ready":true}\n{"ok":true}\n')
            self.stderr = io.BytesIO()
        def communicate(self, **kwargs):
            return b'{"type":"session.ready"}\n', b""
        def wait(self, **kwargs):
            return 0

    monkeypatch.setattr(verify.subprocess, "Popen", Process)
    python, data = tmp_path / "python/python.exe", tmp_path / "data"
    assert verify._run_bridge(tmp_path, python, tmp_path / "voice", data)["rc"] == 0
    assert verify._run_worker(tmp_path, python, data)["ready"] is True
    for env in captured:
        assert not {"OPENAI_API_KEY", "PYTHONPATH", "LUNMI_VOICE_HOME"} & env.keys()
        assert env["LOCALAPPDATA"] == str(data)
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        assert env.get("LUNMI_USER_DATA", env.get("LUNMI_STATE_ROOT")) == str(data)


@pytest.mark.parametrize("failure", [None, "worker", "integrity", "engine", "models", "native", "drift"])
def test_beta_acceptance_preserves_runtime_gates_without_running_binaries(
    mounted, verify, monkeypatch, tmp_path, failure,
):
    root, _ = mounted
    evidence = tmp_path / "evidence"
    messages = [{"type": "session.ready"}, {"id": "mem", "payload": []}]
    messages += [{"id": name, "payload": {"available": True}} for name in ("voice_out", "voice_in")]
    monkeypatch.setattr(verify.sys, "argv", ["verify_package.py", str(root), str(evidence)])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(verify.subprocess, "Popen", lambda *a, **k: pytest.fail("runtime executed"))
    monkeypatch.setattr(verify, "_stage_downloaded_models", lambda data: {
        "tts": "error" if failure == "models" else "installed",
    })
    monkeypatch.setattr(verify, "_run_bridge", lambda *a: {"rc": 0, "messages": messages})
    monkeypatch.setattr(verify, "_inspect_database", lambda path: {
        "integrity": "corrupt" if failure == "integrity" else "ok", "foreign_keys": [], "fts5": True,
    })
    monkeypatch.setattr(verify, "_voice_assets_valid", lambda voice: True)
    monkeypatch.setattr(verify, "missing_vc_runtime", lambda path: ["vcruntime.dll"] if failure == "native" else [])
    monkeypatch.setattr(verify, "run_probe", lambda *a, **k: {"ok": failure != "engine"})

    def worker(*args):
        if failure == "drift":
            (root / "resources/worker-cache.txt").write_text("forbidden runtime write")
        return {"ready": True, "observed": {"ok": failure != "worker"}, "rc": 0}

    monkeypatch.setattr(verify, "_run_worker", worker)
    assert verify.main() == (0 if failure is None else 1)
    report = json.loads((evidence / "package-verification.json").read_text(encoding="utf-8"))
    user_data = Path(report["user_data"])
    assert user_data.parent == evidence and user_data.name.startswith("dados do testador çã-")
    assert report["components_inside_resources"] == []
    assert report["resources_drift_totals"]["created"] == (1 if failure == "drift" else 0)


def test_audit_scans_beta_store_using_existing_secret_guards(mounted, verify, monkeypatch, capsys):
    import audit_payload
    root, directories = mounted
    (root / "resources/app.asar").unlink()
    source = directories["python"] / "secrets.txt"
    source.write_bytes(b"sk-" + b"x" * 30)
    old = directories["python"]
    key = verify._pack_content_key(old)
    current = old.rename(old.with_name(f"python-{key}"))
    (current / ".complete").write_text(key)
    manifest = root / "resources/lunmi-components.json"
    keys = json.loads(manifest.read_text())
    keys["python"] = key
    manifest.write_text(json.dumps(keys))
    monkeypatch.setattr(audit_payload.sys, "argv", ["audit_payload.py", str(root)])
    assert audit_payload.main() == 1
    output = capsys.readouterr().out
    assert "resources" in output and "secrets.txt" in output and "credencial" in output
    assert "sk-" not in output

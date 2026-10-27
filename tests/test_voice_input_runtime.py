"""VOICE INPUT V1 — runtime opcional separado: presença honesta e sandbox-aware."""
from __future__ import annotations

from pathlib import Path

from app.voice.input_runtime import (
    REASON_MODELS_MISSING,
    REASON_OUTSIDE_HOME,
    REASON_RUNTIME_MISSING,
    REASON_VAD_MISSING,
    resolve_input_environment,
)


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    python = tmp_path / "input-runtime" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"stub")
    model = tmp_path / "models" / "whisper-large-v3-turbo"
    model.mkdir(parents=True)
    for name in ("model.bin", "config.json", "tokenizer.json"):
        (model / name).write_bytes(b"{}")
    silero = tmp_path / "models" / "silero" / "silero_vad.onnx"
    silero.parent.mkdir(parents=True)
    silero.write_bytes(b"onnx")
    return python, model, silero


def test_absent_runtime_is_unavailable_and_creates_nothing(tmp_path: Path) -> None:
    env = resolve_input_environment(tmp_path)
    assert env.available is False
    assert env.reason == REASON_RUNTIME_MISSING
    assert "indisponível" in env.message
    assert not (tmp_path / ".local").exists()
    assert not (tmp_path / ".local" / "voice").exists()


def test_complete_runtime_is_available_with_contract_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LUNMI_VOICE_HOME", str(tmp_path))
    python, model, silero = _layout(tmp_path)
    env = resolve_input_environment(tmp_path)
    assert env.available is True
    assert env.reason == ""
    assert env.message == ""
    assert env.python == python.resolve()
    assert env.model == model.resolve()
    assert env.silero == silero.resolve()
    assert env.worker.name == "input_worker.py" and env.worker.is_file()


def test_runtime_outside_voice_home_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LUNMI_VOICE_HOME", str(tmp_path / "home"))
    outside = tmp_path / "elsewhere" / "python.exe"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"stub")
    env = resolve_input_environment(tmp_path, runtime_python=outside)
    assert env.available is False
    assert env.reason == REASON_OUTSIDE_HOME


def test_missing_model_file_is_unavailable_not_partially_usable(tmp_path: Path,
                                                                monkeypatch) -> None:
    monkeypatch.setenv("LUNMI_VOICE_HOME", str(tmp_path))
    _python, model, _silero = _layout(tmp_path)
    (model / "model.bin").unlink()
    env = resolve_input_environment(tmp_path)
    assert env.available is False
    assert env.reason == REASON_MODELS_MISSING


def test_missing_vad_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LUNMI_VOICE_HOME", str(tmp_path))
    _python, _model, silero = _layout(tmp_path)
    silero.unlink()
    env = resolve_input_environment(tmp_path)
    assert env.available is False
    assert env.reason == REASON_VAD_MISSING


def test_explicit_worker_path_must_exist(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LUNMI_VOICE_HOME", str(tmp_path))
    _layout(tmp_path)
    env = resolve_input_environment(tmp_path, worker_path=tmp_path / "missing.py")
    assert env.available is False
    assert env.reason == "worker_missing"

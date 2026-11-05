"""Ditado pelo Groq: a chave fica fora da linha de comando e cada falha tem um código fechado."""
from __future__ import annotations

import io
import urllib.error
import urllib.request
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.bridge import protocol, voice_bridge
from app.voice import input_runtime
from app.voice.input_manager import VoiceInputManager
from app.voice.input_worker import CloudSttError, GroqTranscriber, InputWorker


def test_the_upload_is_a_valid_16khz_wav_with_the_language() -> None:
    body = GroqTranscriber("k").body(np.zeros(1600, dtype=np.float32), "b")
    assert b'name="language"' in body and b"whisper-large-v3-turbo" in body
    audio = body[body.index(b"RIFF"):body.rindex(b"\r\n--b--")]
    with wave.open(io.BytesIO(audio)) as parsed:
        assert (parsed.getframerate(), parsed.getnchannels(), parsed.getnframes()) == (16000, 1, 1600)


@pytest.mark.parametrize("status,code", [(401, "cloud_key"), (429, "cloud_limit"), (500, "stt_failed")])
def test_a_refusal_becomes_a_closed_code(monkeypatch, status, code) -> None:
    def refuse(*_args, **_kwargs):
        raise urllib.error.HTTPError("u", status, "secret body", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(CloudSttError) as caught:
        GroqTranscriber("k").transcribe(np.zeros(160, dtype=np.float32))
    assert caught.value.code == code


def test_no_network_says_so(monkeypatch) -> None:
    def down(*_args, **_kwargs):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", down)
    with pytest.raises(CloudSttError) as caught:
        GroqTranscriber("k").transcribe(np.zeros(160, dtype=np.float32))
    assert caught.value.code == "cloud_network"


def test_the_worker_reports_the_cloud_code(monkeypatch) -> None:
    events: list[dict] = []
    worker = InputWorker(emit=events.append, device="cpu")
    worker._model = SimpleNamespace(transcribe=lambda _samples: (_ for _ in ()).throw(CloudSttError("cloud_limit")))
    monkeypatch.setattr(worker, "_capture", lambda _generation: (np.zeros(16, dtype=np.float32), False, "", "release"))
    worker._run_turn(1)
    assert events[-1] == {"type": "error", "generation": 1, "code": "cloud_limit"}


def manager(tmp_path: Path, key: str = "SECRET") -> VoiceInputManager:
    return VoiceInputManager(emit=lambda _payload: None, state_root=tmp_path, session_id="s", groq_key=lambda: key)


def test_the_key_goes_by_environment_and_never_in_the_command_line(tmp_path: Path) -> None:
    owner = manager(tmp_path)
    owner.set_engine("groq")
    environment = owner.environment()
    assert owner._worker_environment()["LUNMI_GROQ_KEY"] == "SECRET"
    assert "SECRET" not in " ".join(owner._worker_argv(environment))
    assert owner._worker_argv(environment)[-2:] == ["--engine", "groq"]
    owner.set_engine("local")
    assert "LUNMI_GROQ_KEY" not in owner._worker_environment()


def test_local_is_the_default_and_an_odd_file_stays_local(tmp_path: Path) -> None:
    owner = manager(tmp_path)
    assert owner.engine() == "local"
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local" / "dictation-engine.txt").write_text("cloud", encoding="utf-8")
    assert owner.engine() == "local"


def _runtime(tmp_path: Path) -> dict:
    home = tmp_path / "home"
    python = home / "runtime" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"x")
    silero = tmp_path / "silero.onnx"
    silero.write_bytes(b"x")
    worker = tmp_path / "worker.py"
    worker.write_bytes(b"x")
    return {"runtime_python": python, "models_dir": tmp_path / "none", "silero_path": silero, "worker_path": worker}


def test_groq_needs_a_key_but_no_whisper_model(tmp_path: Path, monkeypatch) -> None:
    paths = _runtime(tmp_path)
    monkeypatch.setattr(input_runtime, "voice_home", lambda _root: tmp_path / "home")
    assert input_runtime.resolve_input_environment(tmp_path, **paths).reason == input_runtime.REASON_MODELS_MISSING
    assert input_runtime.resolve_input_environment(tmp_path, cloud=True, **paths).reason == input_runtime.REASON_GROQ_KEY
    assert input_runtime.resolve_input_environment(tmp_path, cloud=True, cloud_key=True, **paths).available


def test_choosing_groq_fetches_the_small_detector_and_a_bad_value_is_refused() -> None:
    owner = voice_bridge.VoiceBridge()
    order: list[str] = []
    owner.voice_models = SimpleNamespace(download=lambda group: order.append(group))
    owner.voice_input = SimpleNamespace(set_engine=lambda engine: order.append(engine), engine=lambda: "groq")
    owner.settings = SimpleNamespace(provider="groq", api_key="k")
    assert owner._handle_voice_input_engine({"engine": "groq"}) == {"engine": "groq", "key_ready": True}
    assert order == ["dictation_vad", "groq"]
    with pytest.raises(protocol.ProtocolError):
        owner._handle_voice_input_engine({"engine": "cloud"})

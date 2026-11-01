"""Voice models after install (01/11): a real local HTTP server with Range/ETag, real urllib.

Cases the audit asked for: interruption and resume, truncated/corrupt/changed content, wrong
origin, no space, cancel, links, an older install's models, repair, and paths with accents.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from app.voice import models as voice_models
from app.voice.models import (MESSAGE_CORRUPT, MESSAGE_LINKED, MESSAGE_NO_SPACE, MESSAGE_SOURCE,
                              VoiceModels, load_manifest)

SPEECH = {"models/supertonic-3/onnx/a.onnx": os.urandom(300_000), "models/supertonic-3/voice_styles/F5.json": b"{}" * 50}
DICTATION = {"models/whisper-large-v3-turbo/model.bin": os.urandom(200_000), "models/silero/silero_vad.onnx": b"v" * 999}


class Server:
    """Serves the pinned files; behaviour per test: truncate, corrupt, etag, log ranges."""

    def __init__(self) -> None:
        self.files = {**SPEECH, **DICTATION}
        self.etag = "A"
        self.truncate_once: set[str] = set()
        self.corrupt: set[str] = set()
        self.requests: list[tuple[str, str | None, str | None]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                path = self.path.lstrip("/")
                owner.requests.append((path, self.headers.get("Range"), self.headers.get("If-Range")))
                body = owner.files.get(path)
                if body is None:
                    self.send_error(404)
                    return
                if path in owner.corrupt:
                    body = bytes(len(body))
                start = 0
                rng, if_range = self.headers.get("Range"), self.headers.get("If-Range")
                if rng and (if_range is None or if_range == owner.etag):
                    start = int(rng.split("=")[1].split("-")[0])
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
                else:
                    self.send_response(200)
                self.send_header("ETag", owner.etag)
                self.send_header("Content-Length", str(len(body) - start))
                self.end_headers()
                if path in owner.truncate_once:
                    owner.truncate_once.discard(path)
                    self.wfile.write(body[start:start + (len(body) - start) // 2])
                    self.close_connection = True
                    return
                self.wfile.write(body[start:])

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def manifest(self, folder: Path) -> Path:
        entries = [{"path": path, "url": self.url + path, "sha256": hashlib.sha256(body).hexdigest(),
                    "bytes": len(body)} for path, body in self.files.items()]
        path = folder / "VOICE-SOURCE.json"
        path.write_text(json.dumps({"schema": 1, "files": entries}), encoding="utf-8")
        return path


@pytest.fixture
def server() -> Any:
    instance = Server()
    yield instance
    instance.httpd.shutdown()


def make(tmp_path: Path, server: Server, **kwargs: Any) -> VoiceModels:
    root = tmp_path / "Dados da Lunmi" / "voice-models"  # spaces and accents on purpose
    return VoiceModels(root, server.manifest(tmp_path), allow_hosts={"127.0.0.1"}, require_https=False, **kwargs)


def run(models: VoiceModels, group: str, **kwargs: Any) -> dict[str, Any]:
    models.download(group, **kwargs)
    models.wait(group, 30)
    return models.snapshot()["groups"][group]


def test_a_group_downloads_verifies_and_activates(tmp_path: Path, server: Server) -> None:
    models = make(tmp_path, server)
    assert models.snapshot()["groups"]["speech"]["state"] == "missing" and models.installed_dir("speech") is None
    assert run(models, "speech")["state"] == "installed"
    root = models.installed_dir("speech")
    assert root is not None and all((root / path).read_bytes() == body for path, body in SPEECH.items())
    assert models.installed_dir("dictation") is None  # groups are independent


def test_an_interrupted_file_resumes_with_range_and_the_same_etag(tmp_path: Path, server: Server) -> None:
    models = make(tmp_path, server)
    big = "models/supertonic-3/onnx/a.onnx"
    server.truncate_once.add(big)
    first = run(models, "speech")
    assert first["state"] == "error" and models.installed_dir("speech") is None
    assert run(models, "speech")["state"] == "installed"
    ranged = [(rng, if_range) for path, rng, if_range in server.requests if path == big and rng]
    assert ranged and ranged[-1][1] == "A"


def test_changed_content_restarts_instead_of_being_stitched(tmp_path: Path, server: Server) -> None:
    models = make(tmp_path, server)
    big = "models/supertonic-3/onnx/a.onnx"
    server.truncate_once.add(big)
    run(models, "speech")
    server.etag = "B"  # the server's copy changed: If-Range fails, the whole file comes back
    assert run(models, "speech")["state"] == "installed"
    assert (models.root / big).read_bytes() == SPEECH[big]


def test_corrupt_content_is_discarded_and_never_installed(tmp_path: Path, server: Server) -> None:
    models = make(tmp_path, server)
    server.corrupt.add("models/supertonic-3/onnx/a.onnx")
    status = run(models, "speech")
    assert status["state"] == "error" and status["message"] == MESSAGE_CORRUPT
    assert models.installed_dir("speech") is None
    assert not list((models.root / ".staging").rglob("*.part"))


def test_an_origin_outside_the_model_hosts_is_refused(tmp_path: Path, server: Server) -> None:
    models = VoiceModels(tmp_path / "m", server.manifest(tmp_path), require_https=False)  # 127.0.0.1 not allowed
    status = run(models, "speech")
    assert status["state"] == "error" and status["message"] == MESSAGE_SOURCE and not server.requests


def test_not_enough_space_is_refused_before_downloading(tmp_path: Path, server: Server, monkeypatch) -> None:
    monkeypatch.setattr(voice_models.shutil, "disk_usage", lambda path: type("U", (), {"free": 1024})())
    status = run(make(tmp_path, server), "speech")
    assert status["state"] == "error" and status["message"] == MESSAGE_NO_SPACE and not server.requests


def test_cancel_keeps_the_partial_file_for_later(tmp_path: Path, server: Server) -> None:
    models = make(tmp_path, server)
    models._cancel["speech"] = threading.Event()
    models._cancel["speech"].set()
    models._run("speech", False)  # cancelled before the first block
    assert models.snapshot()["groups"]["speech"]["state"] in {"missing", "paused"}
    assert run(models, "speech")["state"] == "installed"


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows feature")
def test_a_models_folder_that_is_a_junction_is_refused(tmp_path: Path, server: Server) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "link"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(elsewhere)], check=True, capture_output=True)
    models = VoiceModels(link, server.manifest(tmp_path), allow_hosts={"127.0.0.1"}, require_https=False)
    status = run(models, "speech")
    assert status["state"] == "error" and status["message"] == MESSAGE_LINKED and not list(elsewhere.iterdir())


def test_an_older_install_s_models_are_adopted_only_when_their_hashes_match(tmp_path: Path, server: Server) -> None:
    good, bad = tmp_path / "voice-old" , tmp_path / "voice-bad"
    for folder, corrupt in ((good, False), (bad, True)):
        for path, body in SPEECH.items():
            (folder / path).parent.mkdir(parents=True, exist_ok=True)
            (folder / path).write_bytes(bytes(len(body)) if corrupt else body)
    models = make(tmp_path, server, legacy_dirs=lambda: [bad, good])
    assert run(models, "speech")["state"] == "installed" and not server.requests  # nothing downloaded
    assert not (good / "models/supertonic-3/onnx/a.onnx").exists()  # moved, not duplicated
    assert (bad / "models/supertonic-3/onnx/a.onnx").exists()  # a copy that failed the hash is left alone


def test_a_changed_manifest_means_not_installed_and_repair_fixes_a_bad_file(tmp_path: Path, server: Server) -> None:
    models = make(tmp_path, server)
    run(models, "speech")
    target = models.root / "models/supertonic-3/onnx/a.onnx"
    target.write_bytes(bytes(target.stat().st_size))  # same size, wrong bytes: only a full hash sees it
    assert models.installed_dir("speech") is not None
    assert run(models, "speech", repair=True)["state"] == "installed"
    assert target.read_bytes() == SPEECH["models/supertonic-3/onnx/a.onnx"]
    server.files["models/supertonic-3/voice_styles/F5.json"] = b"[]" * 50  # a new pin
    server.manifest(tmp_path)
    assert models.installed_dir("speech") is None


def test_the_manifest_rejects_paths_outside_the_models_folder(tmp_path: Path) -> None:
    bad = tmp_path / "VOICE-SOURCE.json"
    bad.write_text(json.dumps({"files": [{"path": "../evil", "url": "https://huggingface.co/x",
                                          "sha256": "0" * 64, "bytes": 1}]}), encoding="utf-8")
    with pytest.raises(voice_models.ModelsError):
        load_manifest(bad)


def test_speech_resolves_to_downloaded_models_only_once_verified(tmp_path: Path, server: Server, monkeypatch) -> None:
    """The setup no longer ships models: the voice uses the downloaded copy, never a half one."""
    from app.voice.runtime import resolve_environment
    state = tmp_path / "estado"
    home = state / ".local" / "voice"
    (home / "runtime").mkdir(parents=True)
    (home / "runtime" / "python.exe").write_bytes(b"")
    server.manifest(home)  # pins shipped next to the runtime
    monkeypatch.delenv("LUNMI_VOICE_HOME", raising=False)
    assert resolve_environment(state).reason == "models_missing"
    models = VoiceModels(state / ".local" / "voice-models", home / "VOICE-SOURCE.json",
                         allow_hosts={"127.0.0.1"}, require_https=False)
    assert run(models, "speech")["state"] == "installed"
    env = resolve_environment(state)
    assert env.reason == "" and env.models == (state / ".local/voice-models/models/supertonic-3").resolve()
    (home / "models" / "supertonic-3").mkdir(parents=True)  # an older install's own copy wins
    assert resolve_environment(state).models == (home / "models/supertonic-3").resolve()


def test_the_bridge_accepts_only_known_groups(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from app.bridge import protocol
    from app.bridge.service import BridgeService
    calls: list[tuple[str, Any]] = []
    owner = SimpleNamespace(voice_models=SimpleNamespace(
        snapshot=lambda: {"groups": {}}, cancel=lambda group: calls.append(("cancel", group)) or {},
        download=lambda group, repair=False: calls.append(("download", group, repair)) or {}))
    handle = BridgeService._handle_voice_models
    assert handle(owner, "voice.models.get", {}) == {"groups": {}}  # type: ignore[arg-type]
    handle(owner, "voice.models.download", {"group": "dictation", "repair": True})  # type: ignore[arg-type]
    handle(owner, "voice.models.cancel", {"group": "speech"})  # type: ignore[arg-type]
    assert calls == [("download", "dictation", True), ("cancel", "speech")]
    for bad in ({"group": "everything"}, {"group": "speech", "url": "https://x"}, {"group": "speech", "repair": "yes"}):
        with pytest.raises(protocol.ProtocolError):
            handle(owner, "voice.models.download", bad)  # type: ignore[arg-type]


def test_the_bridge_finds_older_voice_components_that_still_hold_models(tmp_path: Path, monkeypatch) -> None:
    from app.bridge.service import BridgeService
    current = tmp_path / "Lunmi-runtime" / "voice-1111111111111111"
    older = tmp_path / "Lunmi-runtime" / "voice-2222222222222222"
    emptied = tmp_path / "Lunmi-runtime" / "voice-3333333333333333"
    for folder in (current, emptied):
        folder.mkdir(parents=True)
    (older / "models").mkdir(parents=True)
    (current / "models").mkdir()  # the current component never counts, even with a models folder
    monkeypatch.setenv("LUNMI_BUNDLED_VOICE_HOME", str(current))
    assert BridgeService._older_voice_components() == [older]
    monkeypatch.setenv("LUNMI_BUNDLED_VOICE_HOME", "relative/path")
    assert BridgeService._older_voice_components() == []

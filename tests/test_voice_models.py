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
import time
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


def test_an_interrupted_file_resumes_by_itself_with_range_and_the_same_etag(
        tmp_path: Path, server: Server, monkeypatch) -> None:
    monkeypatch.setattr(voice_models, "NETWORK_RETRY_WAITS_S", (0.0,))
    models = make(tmp_path, server)
    big = "models/supertonic-3/onnx/a.onnx"
    server.truncate_once.add(big)
    assert run(models, "speech")["state"] == "installed"  # a dropped connection, no click needed
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
    assert (models.root / "models/supertonic-3/onnx/a.onnx").read_bytes() == SPEECH["models/supertonic-3/onnx/a.onnx"]
    # Old copies go only after the new group is active (review P1-3), the bad one included.
    assert not (good / "models/supertonic-3/onnx/a.onnx").exists() and not (bad / "models/supertonic-3/onnx/a.onnx").exists()


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


def test_older_voice_components_follow_an_install_moved_out_of_a_protected_folder(tmp_path: Path, monkeypatch) -> None:
    current = tmp_path / "AppData" / "Lunmi-runtime" / "voice-1111111111111111"
    moved = tmp_path / "Documents" / "Lunmi-runtime" / "voice-2222222222222222"
    current.mkdir(parents=True)
    (moved / "models").mkdir(parents=True)
    monkeypatch.setenv("LUNMI_BUNDLED_VOICE_HOME", str(current))
    assert voice_models.older_voice_components() == []
    (current.parent / voice_models.PREVIOUS_RUNTIME).write_text(str(moved.parent) + "\r\n", encoding="utf-16")
    assert voice_models.older_voice_components() == [moved]
    (current.parent / voice_models.PREVIOUS_RUNTIME).write_text("relative", encoding="utf-16")
    assert voice_models.older_voice_components() == []


# --- review 21a75b0 (orchestrator, 01/11): one test per blocker ------------------------------
def _write(folder: Path, files: dict[str, bytes]) -> None:
    for path, body in files.items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(body)


def test_p1_1_same_size_corrupt_files_without_a_marker_are_never_accepted(tmp_path: Path, server: Server) -> None:
    models = make(tmp_path, server)
    _write(models.root, {path: bytes(len(body)) for path, body in SPEECH.items()})  # right size, wrong bytes
    models.download("speech", network=False)
    models.wait("speech", 30)
    assert models.installed_dir("speech") is None  # no network allowed: not installed, not faked
    assert run(models, "speech")["state"] == "installed" and server.requests  # fetched for real
    assert all((models.root / p).read_bytes() == b for p, b in SPEECH.items())
    server.files["models/supertonic-3/voice_styles/F5.json"] = b"()" * 50  # new pin, same size
    server.manifest(tmp_path)
    before = len(server.requests)
    assert run(models, "speech")["state"] == "installed" and len(server.requests) > before
    assert (models.root / "models/supertonic-3/voice_styles/F5.json").read_bytes() == b"()" * 50


@pytest.mark.skipif(os.name != "nt", reason="junctions are a Windows feature")
def test_p1_2_a_junction_below_the_root_is_refused_and_outside_files_untouched(tmp_path: Path, server: Server) -> None:
    models = make(tmp_path, server)
    outside = tmp_path / "outside"
    _write(outside, {"supertonic-3/onnx/a.onnx": b"keep me"})
    (models.root / ".staging").mkdir(parents=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(models.root / ".staging" / "models"), str(outside)],
                   check=True, capture_output=True)
    status = run(models, "speech")
    assert status["state"] == "error" and status["message"] == MESSAGE_LINKED
    assert (outside / "supertonic-3/onnx/a.onnx").read_bytes() == b"keep me" and models.installed_dir("speech") is None


def test_p1_3_a_failed_migration_keeps_the_older_copy_and_recovers(tmp_path: Path, server: Server) -> None:
    older = tmp_path / "voice-old"
    _write(older, SPEECH)
    models = make(tmp_path, server, legacy_dirs=lambda: [older])
    blocker = models.root / "models/supertonic-3/voice_styles/F5.json"
    blocker.mkdir(parents=True)  # the second file cannot be activated
    models.download("speech", network=False)
    models.wait("speech", 30)
    assert models.installed_dir("speech") is None
    assert all((older / p).read_bytes() == b for p, b in SPEECH.items())  # nothing lost
    blocker.rmdir()
    models.download("speech", network=False)
    models.wait("speech", 30)
    assert models.installed_dir("speech") is not None and not server.requests


def test_p1_4_the_bridge_republishes_voice_and_dictation_when_models_become_usable() -> None:
    from types import SimpleNamespace

    from app.bridge.service import BridgeService
    refreshed: list[str] = []
    events: list[tuple[str, Any]] = []
    owner = SimpleNamespace(_closed=False, session_id="s", _emit=lambda kind, payload: events.append((kind, payload)),
                            voice=SimpleNamespace(refresh=lambda: refreshed.append("voice")),
                            voice_input=SimpleNamespace(refresh=lambda: refreshed.append("input")))
    emit = BridgeService._emit_voice_models
    downloading = {"groups": {"speech": {"state": "downloading"}}}
    installed = {"groups": {"speech": {"state": "installed"}}}
    emit(owner, downloading)  # type: ignore[arg-type]
    assert refreshed == []
    emit(owner, installed)  # type: ignore[arg-type]
    emit(owner, installed)  # type: ignore[arg-type]
    assert refreshed == ["voice", "input"] and events[-1][0] == "voice.models"


def test_p1_5_an_alpha_9_copy_without_f5_keeps_the_voice_until_the_user_downloads(
        tmp_path: Path, server: Server, monkeypatch) -> None:
    """Alpha 9 shipped F4 only. Its copy, verified against its own pins, stays in use offline;
    downloading then fetches only what is new and cleans the old copy up."""
    from app.voice.runtime import resolve_environment
    older_files = {k: v for k, v in SPEECH.items() if not k.endswith("F5.json")}
    older_files["models/supertonic-3/voice_styles/F4.json"] = b"[F4]" * 10
    older = tmp_path / "runtime" / "voice-old"
    _write(older, older_files)
    (older / "VOICE-SOURCE.json").write_text(json.dumps({"files": [
        {"path": p, "url": server.url + p, "sha256": hashlib.sha256(b).hexdigest(), "bytes": len(b)}
        for p, b in {**older_files, **DICTATION}.items()]}), encoding="utf-8")
    _write(older, DICTATION)
    state = tmp_path / "estado"
    home = tmp_path / "runtime" / "voice-new"
    (home / "runtime").mkdir(parents=True)
    (home / "runtime" / "python.exe").write_bytes(b"")
    server.manifest(home)
    monkeypatch.setenv("LUNMI_BUNDLED_VOICE_HOME", str(home))
    monkeypatch.delenv("LUNMI_VOICE_HOME", raising=False)
    models = VoiceModels(state / ".local" / "voice-models", home / "VOICE-SOURCE.json",
                         legacy_dirs=voice_models.older_voice_components,
                         allow_hosts={"127.0.0.1"}, require_https=False)
    models.download("speech", network=False)
    models.wait("speech", 30)
    assert models.installed_dir("speech") is None and models.previous_dir("speech") == older and not server.requests
    assert resolve_environment(state).models == (older / "models/supertonic-3").resolve()  # still speaks
    assert run(models, "speech")["state"] == "installed"
    assert [path for path, _, _ in server.requests] == ["models/supertonic-3/voice_styles/F5.json"]
    assert models.previous_dir("speech") is None and not (older / "models/supertonic-3/onnx/a.onnx").exists()


def test_p2_cancel_mid_file_keeps_the_part_and_resumes_with_range(tmp_path: Path, server: Server, monkeypatch) -> None:
    monkeypatch.setattr(voice_models, "CHUNK", 16 * 1024)
    big = "models/supertonic-3/onnx/a.onnx"
    server.files[big] = os.urandom(2_000_000)
    original = server.httpd.RequestHandlerClass.do_GET
    import time as clock

    class Slow(server.httpd.RequestHandlerClass):  # type: ignore[misc, name-defined]
        def do_GET(self) -> None:  # noqa: N802
            write = self.wfile.write
            self.wfile.write = lambda data: [(write(data[i:i + 65536]), clock.sleep(0.02))  # type: ignore[method-assign]
                                             for i in range(0, len(data), 65536)]
            original(self)
    server.httpd.RequestHandlerClass = Slow
    models = make(tmp_path, server)
    models.download("speech")
    while models.snapshot()["groups"]["speech"]["bytes_done"] < 300_000:
        clock.sleep(0.01)
    models.cancel("speech")
    models.wait("speech", 30)
    part = models.root / ".staging" / (big + ".part")
    assert models.snapshot()["groups"]["speech"]["state"] == "paused" and 0 < part.stat().st_size < 2_000_000
    assert run(models, "speech")["state"] == "installed"
    assert any(path == big and rng for path, rng, _ in server.requests[-3:])


def test_6d0_speech_and_dictation_hash_at_once_without_losing_cache_entries(
        tmp_path: Path, server: Server, monkeypatch) -> None:
    older = tmp_path / "voice-old"
    _write(older, {**SPEECH, **DICTATION})
    (older / "VOICE-SOURCE.json").write_bytes(server.manifest(tmp_path).read_bytes())
    models = make(tmp_path, server, legacy_dirs=lambda: [older])
    real_replace = os.replace

    def slow_replace(src: Any, dst: Any) -> None:
        time.sleep(0.05)  # widens the window where both threads used to share one .tmp name
        real_replace(src, dst)

    monkeypatch.setattr(voice_models.os, "replace", slow_replace)
    models.download("speech", network=False)  # what the bridge starts for an update
    models.download("dictation", network=False)
    models.wait("speech", 30)
    models.wait("dictation", 30)
    groups = models.snapshot()["groups"]
    assert groups["speech"]["state"] == groups["dictation"]["state"] == "installed"
    cache = json.loads((models.root / ".hash-cache.json").read_text(encoding="utf-8"))
    assert {str(models.root / ".staging" / p) for p in {**SPEECH, **DICTATION}} <= set(cache)
    assert not server.requests


def test_6d0_planted_hard_links_at_temp_names_are_never_written_through(tmp_path: Path, server: Server) -> None:
    models = make(tmp_path, server)
    models.root.mkdir(parents=True)
    victims = []
    for name in (".hash-cache.json.tmp", ".lunmi-verified-speech.json.tmp"):
        victim = tmp_path / f"victim-{name}"
        victim.write_bytes(b"keep me")
        os.link(victim, models.root / name)
        victims.append(victim)
    assert run(models, "speech")["state"] == "installed"
    assert all(victim.read_bytes() == b"keep me" for victim in victims)


def test_downloads_check_certificates_through_windows_not_only_cached_roots(
        tmp_path: Path, server: Server, monkeypatch) -> None:
    # A clean Windows has no cached roots; plain urllib fails there with CERTIFICATE_VERIFY_FAILED.
    built: list[object] = []
    real = voice_models.truststore.SSLContext
    monkeypatch.setattr(voice_models.truststore, "SSLContext", lambda *a: built.append(a) or real(*a))
    assert run(make(tmp_path, server), "speech")["state"] == "installed" and built


def test_a_lasting_outage_still_ends_in_an_error_and_cancel_stops_the_pauses(
        tmp_path: Path, server: Server, monkeypatch) -> None:
    models = make(tmp_path, server)
    server.httpd.shutdown()
    server.httpd.server_close()  # nothing listens: every attempt fails to connect
    monkeypatch.setattr(voice_models, "NETWORK_RETRY_WAITS_S", (0.0, 0.0))
    status = run(models, "speech")
    assert status["state"] == "error" and status["message"] == voice_models.MESSAGE_NO_NETWORK
    monkeypatch.setattr(voice_models, "NETWORK_RETRY_WAITS_S", (60.0,))
    models.download("speech")
    time.sleep(0.5)  # now inside the 60 s pause
    models.cancel("speech")
    models.wait("speech", 5)
    assert models.snapshot()["groups"]["speech"]["state"] == "missing"


def test_a_certificate_failure_says_so_and_the_real_cause_is_recorded(
        tmp_path: Path, server: Server, monkeypatch) -> None:
    # Audit 02/11: on a clean Windows the TLS failure showed "Sem ligação" and left no trace.
    import ssl
    import urllib.error

    class Refusing:
        def open(self, *_a: Any, **_k: Any) -> Any:
            raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "unable to get local issuer certificate"))

    monkeypatch.setattr(voice_models, "NETWORK_RETRY_WAITS_S", (0.0,))
    monkeypatch.setattr(voice_models.urllib.request, "build_opener", lambda *_a: Refusing())
    models = make(tmp_path, server)
    status = run(models, "speech")
    assert status["state"] == "error" and status["message"] == voice_models.MESSAGE_TLS
    record = (models.root / voice_models.FAILURE_LOG).read_text(encoding="utf-8")
    assert "speech" in record and "SSLCertVerificationError" in record and "local issuer" in record


def test_a_source_that_stays_down_falls_back_to_its_mirror_with_the_same_pin(
        tmp_path: Path, server: Server, monkeypatch) -> None:
    # Audit 02/11: dictation depended on one third-party repository; a mirror now stands in.
    monkeypatch.setattr(voice_models, "NETWORK_RETRY_WAITS_S", (0.0,))
    manifest = json.loads(server.manifest(tmp_path).read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"].startswith("models/whisper"):
            entry["mirrors"] = [entry["url"]]
            entry["url"] = server.url + "gone/" + entry["path"]  # the upstream answers 404
    (tmp_path / "VOICE-SOURCE.json").write_text(json.dumps(manifest), encoding="utf-8")
    models = VoiceModels(tmp_path / "voice-models", tmp_path / "VOICE-SOURCE.json",
                         allow_hosts={"127.0.0.1"}, require_https=False)
    assert run(models, "dictation")["state"] == "installed"
    assert any(path.startswith("gone/") for path, _, _ in server.requests)
    # A mirror with different bytes is still refused by the pin.
    server.corrupt.add("models/whisper-large-v3-turbo/model.bin")
    fresh = VoiceModels(tmp_path / "fresh", tmp_path / "VOICE-SOURCE.json",
                        allow_hosts={"127.0.0.1"}, require_https=False)
    assert run(fresh, "dictation")["state"] == "error" and fresh.installed_dir("dictation") is None

"""Lifecycle Spotify I1: deteção pura, consentimento, job, backup, rollback e remoção.

Tudo corre num `APPDATA` de fixture (`tmp_path`): nenhum Spotify real, rede, processo
ou CLI é tocado. O broker e o CLI são duplos injetados; o teste observa ficheiros,
journal e a ordem fecho→backup→apply→verify→reabertura.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integrations import spotify_install as install_module
from app.integrations.spotify import SpotifyBroker, SpotifyBrokerError
from app.integrations.spotify_install import (
    CLI_CACHE_MARKER,
    CONFIG_FILE_NAME,
    EXTENSION_NAME,
    EXTENSIONS_DIR_NAME,
    Pins,
    SpotifyInstallError,
    SpotifyInstaller,
)

SPOTIFY = "1.3.5.3058"
SECRET = "ab" * 32
CLI_BYTES = b"fake-spicetify-pin"
# Pacote de fixture honesto: a distribuição completa que o `apply` nativo usa,
# não um `.exe` solto com pin próprio. `spicetify.exe` + helpers + css-map.
CLI_RUNTIME = {
    "spicetify.exe": CLI_BYTES,
    "jsHelper/spicetifyWrapper.js": b"// lunmi fixture wrapper\n",
    "jsHelper/expFeatures.js": b"// lunmi fixture exp\n",
    "jsHelper/homeConfig.js": b"// lunmi fixture home\n",
    "jsHelper/sidebarConfig.js": b"// lunmi fixture side\n",
    "css-map.json": b"{\"version\":1}\n",
}


def _cli_archive() -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data in CLI_RUNTIME.items():
            bundle.writestr(name, data)
    return buffer.getvalue()


CLI_ARCHIVE = _cli_archive()


class FakeBroker:
    def __init__(self, *, verify: bool = True) -> None:
        self._verify = verify
        self._started = False
        self.opened_urls: list[str] = []
        self.opened_apps: list[Path] = []
        self.verify_calls = 0

    @property
    def started(self) -> bool:
        return self._started

    def start(self) -> dict:
        self._started = True
        return self.info()

    def stop(self) -> None:
        self._started = False

    def info(self) -> dict:
        return {
            "authenticated": self._started,
            "mode": "control",
            "device_scope": "local" if self._started else None,
        }

    def verify_read(self, *, timeout: float = 8.0) -> tuple[bool, str]:
        self.verify_calls += 1
        return (True, "") if self._verify else (False, "not_ready")

    def secret_hex(self) -> str:
        return SECRET

    def bundle(self, extension_source: str) -> str:
        header = (
            "/* private bundle */\n"
            "globalThis.LUNMI_SPOTIFY_KIT_CONFIG = Object.freeze("
            '{"url":"ws://127.0.0.1:45871","secretHex":"%s"});\n' % SECRET
        )
        return header + extension_source

    def open_url(self) -> dict:
        self.opened_urls.append("download")
        return {"opened": True}

    def open_app(self, executable: Path) -> dict:
        self.opened_apps.append(Path(executable))
        return {"opened": True}

    def close(self) -> None:
        self._started = False


class World:
    def __init__(self, root: Path, *, verify: bool = True) -> None:
        self.root = root
        self.appdata = root / "appdata"
        self.local = root / "local"
        self.state = root / "state"
        self.spicetify = self.appdata / "spicetify"
        self.extensions = self.spicetify / EXTENSIONS_DIR_NAME
        self.extensions.mkdir(parents=True)
        self.spotify = self.appdata / "Spotify"
        self.spotify.mkdir()
        self.exe = self.spotify / "Spotify.exe"
        self.exe.write_bytes(b"MZ fake spotify")
        self.ini = self.spicetify / CONFIG_FILE_NAME
        self.ini.write_text(
            "[AdditionalOptions]\nextensions = user.js\n"
            "[Setting]\ncurrent_theme = user-theme\nspotify_path = keep-me\n",
            encoding="utf-8",
        )
        (self.extensions / "user.js").write_text("// user extension", encoding="utf-8")
        self.state.mkdir()
        self.cli_dir = self.state / ".local" / "spotify" / "cli"
        self.cli_dir.mkdir(parents=True)
        self.write_cli_runtime()
        self.pins = Pins(exe_sha256=hashlib.sha256(CLI_BYTES).hexdigest(),
                         zip_sha256=hashlib.sha256(CLI_ARCHIVE).hexdigest())
        self.fetch_calls = 0
        self.process = {"running": True}
        self.calls: list[tuple] = []
        self.gate: threading.Event | None = None
        self.broker = FakeBroker(verify=verify)
        self.installer = SpotifyInstaller(
            state_root=self.state,
            broker=self.broker,
            session_id="sess-0123456789abcdef",
            emit=self.calls.append,
            env={"APPDATA": str(self.appdata), "LOCALAPPDATA": str(self.local)},
            machine="AMD64",
            version_reader=lambda path: SPOTIFY,
            process_reader=lambda: bool(self.process["running"]),
            cli_runner=self._cli,
            pins=self.pins,
            extension_source="globalThis.LUNMI_SPOTIFY_EXTENSION = true;\n",
            closer=self._close,
            opener=self._open,
            fetcher=self._fetch_package,
        )

    def write_cli_runtime(self) -> None:
        for name, data in CLI_RUNTIME.items():
            path = self.cli_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        (self.cli_dir / CLI_CACHE_MARKER).write_text(
            json.dumps({"zip_sha256": hashlib.sha256(CLI_ARCHIVE).hexdigest()}),
            encoding="utf-8",
        )

    def _fetch_package(self, url: str) -> bytes:
        # Pacote de teste em memória: nunca toca a rede.
        self.fetch_calls += 1
        return CLI_ARCHIVE

    def _close(self, executable: Path | None) -> bool:
        self.calls.append(("close", executable))
        self.process["running"] = False
        return True

    def _open(self, executable: Path | None) -> bool:
        self.calls.append(("open", executable))
        self.process["running"] = True
        return True

    def _cli(self, argv: list[str], env: dict[str, str]) -> SimpleNamespace:
        self.calls.append(("cli", tuple(argv[1:]), str(env.get("APPDATA"))))
        if self.gate is not None:
            assert self.gate.wait(timeout=5.0)
        # Segue APPDATA como o CLI real: o perfil pessoal não é o do subprocesso.
        ini = Path(env["APPDATA"]) / "spicetify" / CONFIG_FILE_NAME
        if argv[1:3] == ["config", "extensions"]:
            name = argv[3]
            lines = ini.read_text(encoding="utf-8").splitlines()
            for index, raw in enumerate(lines):
                if "=" in raw and raw.split("=", 1)[0].strip().lower() == "extensions":
                    names = [item for item in raw.split("=", 1)[1].split("|") if item]
                    if name not in names:
                        names.append(name)
                    lines[index] = "extensions = " + "|".join(names)
            ini.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    @property
    def private_root(self) -> Path:
        return self.state / ".local" / "spotify" / "profile"

    @property
    def private_ini(self) -> Path:
        return self.private_root / "spicetify" / CONFIG_FILE_NAME

    @property
    def private_extensions(self) -> Path:
        return self.private_root / "spicetify" / EXTENSIONS_DIR_NAME

    @property
    def journal(self) -> dict:
        return json.loads(
            (self.state / ".local" / "spotify" / "install.json").read_text(encoding="utf-8")
        )

    def journal_text(self) -> str:
        return (self.state / ".local" / "spotify" / "install.json").read_text(
            encoding="utf-8"
        )

    def extension(self) -> Path:
        return self.private_extensions / EXTENSION_NAME

    def ini_extensions(self) -> list[str]:
        ini = self.private_ini if self.private_ini.is_file() else self.ini
        for raw in ini.read_text(encoding="utf-8").splitlines():
            if "=" in raw and raw.split("=", 1)[0].strip().lower() == "extensions":
                return [item.strip() for item in raw.split("=", 1)[1].split("|") if item.strip()]
        return []

    def wait_job(self, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.installer.state()
            job = state["job"]
            if job is None or job["status"] != "running":
                return job or {}
            time.sleep(0.02)
        raise AssertionError("job Spotify não terminou")


def make_world(tmp_path: Path, *, verify: bool = True) -> World:
    return World(tmp_path, verify=verify)


def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condição não observada")


def test_plan_is_pure_and_detection_never_invokes_cli_or_network(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    before = world.ini.read_bytes()

    state = world.installer.state()
    assert state["setup"] == "not_configured"
    assert state["detection"] == {
        "app": "win32",
        "version": "supported",
        "architecture": "x64",
        "cli": "pinned",
        "link": "absent",
        "app_running": True,
    }
    first = world.installer.plan("install")
    second = world.installer.plan("install")

    assert first["plan"]["action"] == "install"
    assert len(first["plan"]["plan_hash"]) == 64
    assert first["consent"]["consent_id"] != second["consent"]["consent_id"]
    assert first["consent"]["consent_id"].startswith("cns-")
    assert world.ini.read_bytes() == before
    assert not world.extension().exists()
    assert not (world.state / ".local" / "spotify" / "install.json").exists()
    assert not (world.state / ".local" / "spotify" / "backup").exists()
    assert not any(call[0] in {"close", "open", "cli"} for call in world.calls)


def test_backup_limits_accept_the_measured_spotify_apps_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = make_world(tmp_path)
    apps = world.spotify / "Apps"
    apps.mkdir(exist_ok=True)
    # Envelope real já validado: 1904 ficheiros / 239104134 bytes (alocação esparsa).
    for index in range(1904):
        target = apps / f"measured-{index}.dat"
        with target.open("wb") as output:
            if index == 0:
                output.truncate(239_104_134)

    plan = world.installer.plan("install")["plan"]
    assert plan["changes"]["backup_bytes"] >= 239_104_134

    # A guarda continua finita: o mesmo envelope falha se o teto for menor.
    monkeypatch.setattr(install_module, "MAX_SPOTIFY_APPS_FILES", 1903)
    with pytest.raises(SpotifyInstallError) as limited:
        world.installer.plan("install")
    assert limited.value.code == "disk_space"


def test_apply_runs_outside_dispatch_closes_before_writes_and_journals_without_token(
    tmp_path: Path,
) -> None:
    world = make_world(tmp_path)
    world.gate = threading.Event()
    personal_ini = world.ini.read_bytes()
    personal_extension = world.extensions / EXTENSION_NAME
    planned = world.installer.plan("install")
    started = time.perf_counter()
    accepted = world.installer.apply(planned["consent"]["consent_id"])
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, "apply tem de responder sem esperar pelo job"
    assert accepted["accepted"] is True
    assert accepted["job_id"].startswith("spj-")
    assert accepted["epoch"] == 0
    assert world.installer.state()["job"]["status"] == "running"

    second = world.installer.plan("install")
    with pytest.raises(SpotifyInstallError) as busy:
        world.installer.apply(second["consent"]["consent_id"])
    assert busy.value.code == "job_busy"

    world.gate.set()
    job = world.wait_job()
    assert job["status"] == "done"
    assert job["error_code"] is None
    state = world.installer.state()
    assert state["setup"] == "configured"
    assert state["connection"] == "connected"
    assert state["device"] == "local"

    kinds = [call[0] for call in world.calls if isinstance(call, tuple)]
    assert kinds.index("close") < kinds.index("cli")
    wait_until(lambda: [c[0] for c in world.calls if isinstance(c, tuple)].count("open") == 1)
    assert [c[0] for c in world.calls if isinstance(c, tuple)][-1] == "open"
    assert world.broker.verify_calls == 1

    text = world.extension().read_text(encoding="utf-8")
    assert "LUNMI_SPOTIFY_KIT_CONFIG" in text
    assert '"secretHex":"' + SECRET + '"' in text
    assert world.ini_extensions() == ["user.js", EXTENSION_NAME]
    assert "current_theme = user-theme" in world.private_ini.read_text(encoding="utf-8")
    assert world.ini.read_bytes() == personal_ini, "perfil pessoal tem de ficar intacto"
    assert not personal_extension.exists(), "extensão vive no clone privado, não no perfil"
    cli_envs = [call[2] for call in world.calls if isinstance(call, tuple) and call[0] == "cli"]
    assert cli_envs and all(Path(env).resolve() != world.appdata.resolve() for env in cli_envs)

    journal = world.journal
    assert journal["plan_hash"] == planned["plan"]["plan_hash"]
    assert journal["action"] == "install"
    assert journal["status"] == "done"
    assert journal["epoch"] >= 1
    assert len(journal["backup_sha256"]) == 64
    assert journal["job"]["status"] == "done"
    raw = world.journal_text()
    assert planned["consent"]["consent_id"] not in raw
    assert SECRET not in raw
    assert '"consent' not in raw
    assert not (world.state / ".local" / "spotify" / "install.json.tmp").exists()

    with pytest.raises(SpotifyInstallError) as replay:
        world.installer.apply(planned["consent"]["consent_id"])
    assert replay.value.code == "consent_replayed"


def test_expired_consent_is_expired_and_a_changed_target_is_invalid(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    clock = {"now": 1000.0}
    world.installer._clock = lambda: clock["now"]

    planned = world.installer.plan("install")
    clock["now"] += 601
    with pytest.raises(SpotifyInstallError) as expired:
        world.installer.apply(planned["consent"]["consent_id"])
    assert expired.value.code == "consent_expired"

    world.ini.write_text(
        world.ini.read_text(encoding="utf-8") + "theme = changed-by-user\n",
        encoding="utf-8",
    )
    changed = world.installer.plan("install")
    world.ini.write_text(
        world.ini.read_text(encoding="utf-8") + "[]\n",
        encoding="utf-8",
    )
    with pytest.raises(SpotifyInstallError) as invalid:
        world.installer.apply(changed["consent"]["consent_id"])
    assert invalid.value.code == "consent_invalid"
    assert not any(call[0] in {"close", "cli", "open"} for call in world.calls)
    assert not world.extension().exists()


def test_failed_verify_rolls_back_files_and_reports_not_ready(tmp_path: Path) -> None:
    world = make_world(tmp_path, verify=False)
    baseline = world.ini.read_bytes()
    planned = world.installer.plan("install")
    world.installer.apply(planned["consent"]["consent_id"])
    job = world.wait_job()

    assert job["status"] == "failed"
    assert job["error_code"] == "not_ready"
    assert world.broker.verify_calls == 1
    assert not world.extension().exists()
    assert world.ini.read_bytes() == baseline
    assert (world.extensions / "user.js").read_text(encoding="utf-8") == "// user extension"
    state = world.installer.state()
    assert state["setup"] != "configured"
    assert state["connection"] != "connected"
    assert state["error"] == {"code": "not_ready", "retryable": True}

    journal = world.journal
    assert journal["status"] == "failed"
    assert journal["error_code"] == "not_ready"
    assert journal["backup_sha256"]
    assert journal["plan_hash"] == planned["plan"]["plan_hash"]
    wait_until(lambda: [call[0] for call in world.calls if isinstance(call, tuple)].count("open") == 1)


def test_remove_is_selective_and_keeps_later_external_edits(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.installer.apply(world.installer.plan("install")["consent"]["consent_id"])
    assert world.wait_job()["status"] == "done"
    baseline_digest = world.journal["backup_sha256"]

    (world.extensions / "later.js").write_text("// later", encoding="utf-8")
    lines = []
    for raw in world.ini.read_text(encoding="utf-8").splitlines():
        if "=" in raw and raw.split("=", 1)[0].strip().lower() == "extensions":
            raw = raw + "|later.js"
        lines.append(raw)
    world.ini.write_text("\n".join(lines) + "\n", encoding="utf-8")

    planned = world.installer.plan("remove")
    assert planned["plan"]["action"] == "remove"
    assert planned["plan"]["requires"]["close_spotify"] is True
    world.installer.apply(planned["consent"]["consent_id"])
    job = world.wait_job()

    assert job["status"] == "done"
    assert not world.extension().exists()
    assert (world.extensions / "later.js").read_text(encoding="utf-8") == "// later"
    assert world.ini_extensions() == ["user.js", "later.js"]
    assert "current_theme = user-theme" in world.ini.read_text(encoding="utf-8")
    state = world.installer.state()
    assert state["setup"] == "not_configured"
    assert state["connection"] == "disabled"
    assert world.journal["backup_sha256"] == baseline_digest


def test_disconnect_keeps_setup_configured_and_does_not_require_repair(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.installer.apply(world.installer.plan("install")["consent"]["consent_id"])
    assert world.wait_job()["status"] == "done"

    state = world.installer.disconnect()["spotify"]

    assert state["connection"] == "disabled"
    assert state["setup"] == "configured", "desligar a ligação não apaga a instalação"
    assert world.extension().exists(), "o link instalado tem de sobreviver ao disconnect"


def test_open_is_main_only_closed_target_and_missing_spotify_never_opens(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    assert world.installer.plan("install")["plan"]["action"] == "install"
    assert world.installer.open_target("app") == {"opened": True}
    assert world.broker.opened_apps == [world.exe.resolve()]
    assert world.installer.open_target("download") == {"opened": True}
    assert world.broker.opened_urls == ["download"]

    with pytest.raises(SpotifyInstallError) as unknown:
        world.installer.open_target("url")
    assert unknown.value.code == "invalid_payload"

    world.exe.unlink()
    with pytest.raises(SpotifyInstallError) as missing:
        world.installer.open_target("app")
    assert missing.value.code == "spotify_missing"
    with pytest.raises(SpotifyInstallError) as plan_missing:
        world.installer.plan("install")
    assert plan_missing.value.code == "spotify_missing"
    assert world.broker.opened_apps == [world.exe.resolve()]


def test_store_and_wrong_architecture_are_blocked_without_mutation(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.exe.unlink()
    store = world.local / "Microsoft" / "WindowsApps" / "Spotify.exe"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"store")
    with pytest.raises(SpotifyInstallError) as store_missing:
        world.installer.plan("install")
    assert store_missing.value.code == "store_unsupported"
    assert world.installer.state()["setup"] == "unsupported"

    other = make_world(tmp_path / "arm")
    other.installer._machine = "ARM64"
    with pytest.raises(SpotifyInstallError) as arm:
        other.installer.plan("install")
    assert arm.value.code == "arm_unsupported"
    assert not other.extension().exists()
    assert not any(call[0] in {"close", "cli"} for call in other.calls)


def test_activation_grant_is_invalidated_by_a_new_authentication_session(tmp_path: Path) -> None:
    broker = SpotifyBroker(state_root=tmp_path, port=0)
    grant = broker.grant_local_activation(request_id="req-1", task_id="task-1", epoch=0)

    # Auth/end muda o epoch mesmo sem fechar o broker: a concessão e as refs
    # antigas deixam de valer, sem expor nonce/segredo no DTO.
    broker._session_changed(None)
    assert broker.epoch == 1
    assert broker.info()["epoch"] == 1
    assert grant.consume() is False
    assert broker.info().get("secret") is None
    broker.close()


def test_activation_grant_consumes_once_and_close_invalidates(tmp_path: Path) -> None:
    broker = SpotifyBroker(state_root=tmp_path, port=0)
    first = broker.grant_local_activation(request_id="req-1", task_id="task-1", epoch=0)
    assert first.consume() is True
    assert first.consume() is False

    with pytest.raises(SpotifyBrokerError) as busy:
        broker.grant_local_activation(request_id="req-2", task_id="task-1", epoch=0)
    assert busy.value.code == "unavailable"

    # Fecho invalida uma concessão emitida mas ainda não consumida.
    broker.stop()
    late = broker.grant_local_activation(request_id="req-3", task_id="task-1", epoch=0)
    broker.close()
    assert late.consume() is False
    assert first.epoch == 0


@pytest.mark.parametrize("cli_change", ["missing", "changed"])
def test_remove_requires_the_pinned_cli_and_keeps_recovery(
    tmp_path: Path, cli_change: str
) -> None:
    world = make_world(tmp_path)
    apps = world.spotify / "Apps"
    apps.mkdir()
    asset = apps / "xpui.spa"
    asset.write_bytes(b"owned-original")
    world.installer.apply(world.installer.plan("install")["consent"]["consent_id"])
    assert world.wait_job()["status"] == "done"
    asset.write_bytes(b"owned-patched")

    cli = world.installer._cli_path()
    if cli_change == "missing":
        cli.unlink()
    else:
        cli.write_bytes(b"not-the-pin")
    try:
        with pytest.raises(SpotifyInstallError) as refused:
            world.installer.plan("remove")
        assert refused.value.code in {"cli_unavailable", "hash_mismatch"}
        assert asset.read_bytes() == b"owned-patched"
        assert world.extension().exists(), "material de recuperação tem de sobreviver à recusa"
    finally:
        world.installer.close()


def test_cancelled_removal_preserves_edits_made_after_installation(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    apps = world.spotify / "Apps"
    apps.mkdir()
    asset = apps / "xpui.spa"
    asset.write_bytes(b"owned-original")
    world.installer.apply(world.installer.plan("install")["consent"]["consent_id"])
    assert world.wait_job()["status"] == "done"

    # Edições posteriores à instalação e anteriores ao remove.
    asset.write_bytes(b"later-user-edit")
    later = apps / "later.js"
    later.write_bytes(b"later-user-addition")

    original = world.installer._cli_runner

    def cancel_during_cli(argv: list[str], env: dict[str, str]) -> SimpleNamespace:
        result = original(argv, env)
        world.installer._cancel.set()
        return result

    world.installer._cli_runner = cancel_during_cli
    try:
        world.installer.apply(world.installer.plan("remove")["consent"]["consent_id"])
        assert world.wait_job()["status"] != "done"
        assert asset.read_bytes() == b"later-user-edit"
        assert later.read_bytes() == b"later-user-addition"
    finally:
        world.installer.close()


def test_close_ends_owned_cli_before_a_late_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = make_world(tmp_path)
    world.installer._cli_runner = None
    ready = tmp_path / "child-ready"
    late = tmp_path / "late-write"
    script = ("import time; from pathlib import Path; "
              f"Path({str(ready)!r}).write_text('ready'); time.sleep(1.5); "
              f"Path({str(late)!r}).write_text('bad')")
    real_popen = subprocess.Popen
    children: list = []

    def spawn_owned(argv: list[str], **kwargs: object) -> object:
        child = real_popen([sys.executable, "-B", "-c", script], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(install_module.subprocess, "Popen", spawn_owned)
    errors: list[str] = []

    def run() -> None:
        try:
            world.installer._run_cli(["--no-restart", "backup", "apply"], timeout=5)
        except SpotifyInstallError as exc:
            errors.append(exc.code)

    worker = threading.Thread(target=run)
    world.installer._job_thread = worker
    worker.start()
    try:
        wait_until(ready.exists)
        world.installer.close()
        assert not worker.is_alive()
        assert children and all(child.poll() is not None for child in children)
        assert not late.exists(), "close tem de parar o CLI próprio antes da escrita tardia"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        worker.join(timeout=5)
        world.installer.close()


@pytest.mark.parametrize("when", ["already_cancelled", "during_cli_validation"])
def test_no_cli_dispatch_after_cancellation_was_acknowledged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    world = make_world(tmp_path)
    world.installer._cli_runner = None
    world.installer._job = {"id": "owned-cancel-job", "status": "running",
                            "action": "install", "stage": "apply"}
    launches: list[bool] = []

    def forbidden_spawn(*args: object, **kwargs: object) -> object:
        launches.append(True)
        raise AssertionError("CLI dispatch after cancellation was acknowledged")

    monkeypatch.setattr(install_module.subprocess, "Popen", forbidden_spawn)
    if when == "already_cancelled":
        assert world.installer.cancel("owned-cancel-job")["cancelled"]
    else:
        original = world.installer._ensure_cli

        def validate_then_cancel() -> Path:
            cli = original()
            assert world.installer.cancel("owned-cancel-job")["cancelled"]
            return cli

        world.installer._ensure_cli = validate_then_cancel
    try:
        with pytest.raises(SpotifyInstallError) as refused:
            world.installer._run_cli(["--no-restart", "backup", "apply"], timeout=5)
        assert refused.value.code == "job_cancelled"
        assert launches == []
    finally:
        world.installer.close()


def test_generated_extracted_cache_is_not_cloned_or_charged(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    extracted = world.spicetify / "Extracted"
    extracted.mkdir()
    for index in range(520):
        (extracted / f"generated-{index}.js").write_bytes(b"cache")
    active = world.spicetify / "CustomApps/active-app/index.js"
    active.parent.mkdir(parents=True)
    active.write_bytes(b"active-user-app")
    personal_ini = world.ini.read_bytes()
    try:
        world.installer._build_private_profile(world.installer._detect(force=True))
        assert not (world.private_root / "spicetify/Extracted").exists()
        assert (world.private_root / "spicetify/CustomApps/active-app/index.js").read_bytes() == b"active-user-app"
        assert (world.private_extensions / "user.js").read_bytes() == b"// user extension"
        assert world.ini.read_bytes() == personal_ini
        assert len(list(extracted.iterdir())) == 520
    finally:
        world.installer.close()


def test_cold_closed_install_opens_spotify_before_live_verification(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    world.process["running"] = False
    observations: list[bool] = []

    def observed_verify(*, timeout: float = 8.0) -> tuple[bool, str]:
        observations.append(world.process["running"])
        return (True, "") if world.process["running"] else (False, "not_ready")

    world.broker.verify_read = observed_verify
    try:
        world.installer.apply(world.installer.plan("install")["consent"]["consent_id"])
        assert world.wait_job()["status"] == "done"
        assert observations == [True], "uma leitura viva não autentica um app fechado"
        opens = [call for call in world.calls
                 if isinstance(call, tuple) and call[0] == "open"]
        assert len(opens) == 1
        assert not any(call[0] == "close" for call in world.calls if isinstance(call, tuple))
    finally:
        world.installer.close()


@pytest.mark.parametrize("cached_exe", [False, True])
def test_cli_cache_requires_the_full_pinned_runtime(
    tmp_path: Path, cached_exe: bool
) -> None:
    world = make_world(tmp_path)
    if cached_exe:
        # Layout antigo: só o .exe pinado em cache, sem a distribuição.
        for name in CLI_RUNTIME:
            if name != "spicetify.exe":
                (world.cli_dir / name).unlink()
    else:
        import shutil

        shutil.rmtree(world.cli_dir)
    assert not world.installer._cli_runtime_ready()
    try:
        actual = world.installer._ensure_cli()
        assert actual == world.cli_dir / "spicetify.exe"
        assert world.fetch_calls == 1, "um .exe solto não pode passar silenciosamente"
        for name, data in CLI_RUNTIME.items():
            assert (world.cli_dir / name).read_bytes() == data
    finally:
        world.installer.close()


def _zip_from(entries: list[tuple[str, bytes, int]]) -> bytes:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            bundle.writestr(info, data)
    return buffer.getvalue()


@pytest.mark.parametrize("case", ["traversal", "absolute", "duplicate", "reparse", "badzip"])
def test_cli_extraction_refuses_unsafe_archives(tmp_path: Path, case: str) -> None:
    import shutil

    world = make_world(tmp_path)
    if case == "traversal":
        archive = _zip_from([("jsHelper/spicetifyWrapper.js", b"x", 0o100000),
                             ("../escape.js", b"x", 0o100000)])
    elif case == "absolute":
        archive = _zip_from([("C:/escape.js", b"x", 0o100000)])
    elif case == "duplicate":
        archive = _zip_from([("a.js", b"x", 0o100000), ("A.js", b"y", 0o100000)])
    elif case == "reparse":
        archive = _zip_from([("jsHelper/spicetifyWrapper.js", b"x", 0o120000)])
    else:
        archive = b"not-a-zip"
    world.installer._pins = Pins(
        exe_sha256=hashlib.sha256(CLI_BYTES).hexdigest(),
        zip_sha256=hashlib.sha256(archive).hexdigest(),
    )
    world.installer._fetcher = lambda url: archive
    shutil.rmtree(world.cli_dir)
    try:
        with pytest.raises(SpotifyInstallError) as refused:
            world.installer._ensure_cli()
        assert refused.value.code == "signature_invalid"
        assert not (tmp_path / "escape.js").exists()
        assert not world.cli_dir.exists() or not any(world.cli_dir.iterdir())
    finally:
        world.installer.close()


def test_cli_extraction_enforces_finite_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    archive = _zip_from([(f"f{index}.js", b"x" * 4, 0o100000) for index in range(3)])

    def prepare(sub: Path) -> World:
        world = make_world(sub)
        world.installer._pins = Pins(
            exe_sha256=hashlib.sha256(CLI_BYTES).hexdigest(),
            zip_sha256=hashlib.sha256(archive).hexdigest(),
        )
        world.installer._fetcher = lambda url: archive
        shutil.rmtree(world.cli_dir)
        return world

    files_world = prepare(tmp_path / "files")
    monkeypatch.setattr(install_module, "MAX_CLI_RUNTIME_FILES", 2)
    try:
        with pytest.raises(SpotifyInstallError) as too_many:
            files_world.installer._ensure_cli()
        assert too_many.value.code == "signature_invalid"
    finally:
        files_world.installer.close()

    bytes_world = prepare(tmp_path / "bytes")
    monkeypatch.setattr(install_module, "MAX_CLI_RUNTIME_FILES", 1024)
    monkeypatch.setattr(install_module, "MAX_CLI_RUNTIME_BYTES", 8)
    try:
        with pytest.raises(SpotifyInstallError) as too_big:
            bytes_world.installer._ensure_cli()
        assert too_big.value.code == "signature_invalid"
    finally:
        bytes_world.installer.close()


def test_remove_with_generated_cli_caches_keeps_active_edits(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    apps = world.spotify / "Apps"
    apps.mkdir()
    asset = apps / "xpui.spa"
    asset.write_bytes(b"owned-original")
    world.installer.apply(world.installer.plan("install")["consent"]["consent_id"])
    assert world.wait_job()["status"] == "done"

    # Caches geradas pelo CLI nativo depois do install: Backup acima de 16 MiB e
    # Extracted com mais de 512 ficheiros. Não são ativos e não podem falhar o
    # snapshot da remoção (metadados reais: 17 401 686 B / 1175 ficheiros).
    backup = world.private_root / "spicetify" / "Backup"
    backup.mkdir(parents=True)
    with (backup / "xpui.spa").open("wb") as file:
        file.truncate(17_401_686)
    extracted = world.private_root / "spicetify" / "Extracted"
    extracted.mkdir(parents=True)
    for index in range(520):
        (extracted / f"generated-{index}.js").write_bytes(b"cache")

    # Edições posteriores à instalação no perfil pessoal.
    later = world.extensions / "later.js"
    later.write_text("// later", encoding="utf-8")
    world.ini.write_text(
        world.ini.read_text(encoding="utf-8") + "theme = later\n", encoding="utf-8")
    theme = world.spicetify / "Themes/current/user.css"
    theme.parent.mkdir(parents=True)
    theme.write_bytes(b"user-theme")
    user_app = world.spicetify / "CustomApps/active-app/index.js"
    user_app.parent.mkdir(parents=True)
    user_app.write_bytes(b"active-user-app")
    expected = {asset: asset.read_bytes(), theme: theme.read_bytes(),
                user_app: user_app.read_bytes(), world.ini: world.ini.read_bytes(),
                world.extensions / "user.js": b"// user extension",
                later: b"// later"}

    world.installer.apply(world.installer.plan("remove")["consent"]["consent_id"])
    job = world.wait_job()
    try:
        assert job["status"] == "done", job
        assert job["error_code"] is None
        assert all(path.read_bytes() == value for path, value in expected.items())
        assert not world.extension().exists()
    finally:
        world.installer.close()


def test_remove_snapshot_failure_before_mutation_keeps_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = make_world(tmp_path)
    apps = world.spotify / "Apps"
    apps.mkdir()
    asset = apps / "xpui.spa"
    asset.write_bytes(b"owned-original")
    world.installer.apply(world.installer.plan("install")["consent"]["consent_id"])
    assert world.wait_job()["status"] == "done"
    asset.write_bytes(b"owned-installed")
    personal_ini = world.ini.read_bytes()
    extension = world.extension().read_bytes()

    planned = world.installer.plan("remove")
    # Falha no snapshot ativo antes de qualquer escrita do produto.
    monkeypatch.setattr(install_module, "MAX_PROFILE_BYTES", 1)
    world.installer.apply(planned["consent"]["consent_id"])
    job = world.wait_job()
    try:
        assert job["status"] == "failed"
        assert job["error_code"] == "disk_space", job
        assert job["stage"] == "backup"
        assert world.installer._transaction_dir == ""
        assert asset.read_bytes() == b"owned-installed"
        assert world.ini.read_bytes() == personal_ini
        assert world.extension().read_bytes() == extension
    finally:
        world.installer.close()

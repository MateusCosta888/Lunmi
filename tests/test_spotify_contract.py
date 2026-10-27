"""Paridade do contrato Spotify I0 e guardas dos DTOs públicos.

Fonte única: `desktop/src/shared/ipc_golden_v1.json` (§`spotify`). Este ficheiro compara
Python ↔ golden; `desktop/tests/spotify-contract.test.ts` compara TypeScript ↔ golden.
Nenhum teste aqui arranca Spotify, rede, processos ou instalação.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from app.bridge import protocol
from app.integrations import spotify_contract as sc

GOLDEN = json.loads(
    (Path(__file__).resolve().parents[1] / "desktop" / "src" / "shared" / "ipc_golden_v1.json")
    .read_text(encoding="utf-8")
)
SPOTIFY = GOLDEN["spotify"]


def test_names_are_in_the_shared_v1_contract() -> None:
    assert list(sc.SPOTIFY_COMMAND_TYPES) == SPOTIFY["commands"]
    assert set(sc.SPOTIFY_COMMAND_TYPES) == protocol.SPOTIFY_COMMAND_TYPES
    assert set(sc.SPOTIFY_COMMAND_TYPES) <= protocol.COMMAND_TYPES
    for command in sc.SPOTIFY_COMMAND_TYPES:
        assert protocol.TYPE_PATTERN.fullmatch(command)
        assert command in GOLDEN["commands"]
    assert protocol.SPOTIFY_COMMAND_TYPES.isdisjoint({"integrations.get", "chat.send"})

    assert sc.SPOTIFY_EVENT == SPOTIFY["event"] == "spotify.changed"
    assert sc.SPOTIFY_EVENT in protocol.EVENT_TYPES
    assert sc.SPOTIFY_EVENT in GOLDEN["events"]
    assert SPOTIFY["capability"] == "spotify"


def test_python_enums_keys_and_patterns_match_the_golden() -> None:
    assert list(sc.SPOTIFY_SETUP_STATES) == SPOTIFY["states"]["setup"]
    assert list(sc.SPOTIFY_CONNECTION_STATES) == SPOTIFY["states"]["connection"]
    assert list(sc.SPOTIFY_DEVICES) == SPOTIFY["states"]["device"]
    assert list(sc.SPOTIFY_ACTIONS) == SPOTIFY["actions"]
    assert list(sc.SPOTIFY_JOB_STAGES) == SPOTIFY["stages"]
    assert list(sc.SPOTIFY_JOB_STATUSES) == SPOTIFY["job_statuses"]
    assert list(sc.SPOTIFY_CANCEL_REASONS) == SPOTIFY["cancel_reasons"]
    assert list(sc.SPOTIFY_OPEN_TARGETS) == SPOTIFY["open_targets"]
    assert list(sc.SPOTIFY_APP_CHANNELS) == SPOTIFY["detection"]["app"]
    assert list(sc.SPOTIFY_VERSION_FIT) == SPOTIFY["detection"]["version"]
    assert list(sc.SPOTIFY_ARCHITECTURES) == SPOTIFY["detection"]["architecture"]
    assert list(sc.SPOTIFY_CLI_STATES) == SPOTIFY["detection"]["cli"]
    assert list(sc.SPOTIFY_LINK_STATES) == SPOTIFY["detection"]["link"]
    assert sorted(sc.SPOTIFY_ERROR_CODES) == sorted(SPOTIFY["error_codes"])

    assert sorted(sc.SPOTIFY_STATE_KEYS) == sorted(SPOTIFY["dto_keys"]["state"])
    assert sorted(sc.SPOTIFY_DETECTION_KEYS) == sorted(SPOTIFY["dto_keys"]["detection"])
    assert sorted(sc.SPOTIFY_JOB_KEYS) == sorted(SPOTIFY["dto_keys"]["job"])
    assert sorted(sc.SPOTIFY_ERROR_KEYS) == sorted(SPOTIFY["dto_keys"]["error"])
    assert sorted(sc.SPOTIFY_PLAN_KEYS) == sorted(SPOTIFY["dto_keys"]["plan"])
    assert sorted(sc.SPOTIFY_PLAN_CHANGES_KEYS) == sorted(SPOTIFY["dto_keys"]["plan_changes"])
    assert sorted(sc.SPOTIFY_PLAN_REQUIRES_KEYS) == sorted(SPOTIFY["dto_keys"]["plan_requires"])
    assert sorted(sc.SPOTIFY_CONSENT_KEYS) == sorted(SPOTIFY["dto_keys"]["consent"])
    assert sorted(sc.SPOTIFY_CHANGED_KEYS) == sorted(SPOTIFY["dto_keys"]["changed"])

    for command, keys in sc.SPOTIFY_COMMAND_PAYLOADS.items():
        assert sorted(keys) == sorted(SPOTIFY["command_payloads"][command])
    for command, keys in sc.SPOTIFY_RESULT_KEYS.items():
        assert sorted(keys) == sorted(SPOTIFY["result_keys"][command])

    assert sc.SESSION_ID_PATTERN.pattern == SPOTIFY["patterns"]["session_id"]
    assert sc.CONSENT_ID_PATTERN.pattern == SPOTIFY["patterns"]["consent_id"]
    assert sc.JOB_ID_PATTERN.pattern == SPOTIFY["patterns"]["job_id"]
    assert sc.PLAN_HASH_PATTERN.pattern == SPOTIFY["patterns"]["plan_hash"]

    assert sc.SPOTIFY_PINNED_SPOTIFY == SPOTIFY["pins"]["spotify"]
    assert sc.SPOTIFY_PINNED_CLI == SPOTIFY["pins"]["cli"]
    assert sc.SPOTIFY_CONSENT_TTL_SECONDS == SPOTIFY["pins"]["consent_ttl_seconds"]
    assert sc.SPOTIFY_ACTIVATION_TTL_SECONDS == SPOTIFY["pins"]["activation_ttl_seconds"]
    assert sc.SPOTIFY_LOOPBACK_PORT == SPOTIFY["pins"]["loopback_port"]


def test_state_dto_is_closed_and_never_carries_private_text() -> None:
    state = sc.SpotifyState(
        setup="configured",
        connection="connected",
        device="local",
        revision=7,
        epoch=3,
        job=sc.SpotifyJob(id="spj-0123456789abcdef", action="install", stage="verify",
                          status="done"),
        detection=sc.SpotifyDetection(app="win32", version="supported", architecture="x64",
                                      cli="pinned", link="present", app_running=True),
        error=sc.SpotifyError(code="not_ready", retryable=True),
    )
    payload = state.to_dict()
    assert sorted(payload) == sorted(GOLDEN["spotify"]["dto_keys"]["state"])
    assert sorted(payload["detection"]) == sorted(GOLDEN["spotify"]["dto_keys"]["detection"])
    assert sorted(payload["job"]) == sorted(GOLDEN["spotify"]["dto_keys"]["job"])
    assert sorted(payload["error"]) == sorted(GOLDEN["spotify"]["dto_keys"]["error"])
    assert state.changed_payload() == {"spotify": payload}

    # O header de sessão é do emissor da bridge (como no Gmail), não do estado.
    session_id = "sess-" + "0" * 16
    wire = {**state.changed_payload(), "session_id": sc.validate_session_id(session_id)}
    assert sorted(wire) == sorted(GOLDEN["spotify"]["dto_keys"]["changed"])
    assert wire["session_id"] == session_id
    for bad_session in ("sess-", "sess-" + "Z" * 16, "sess-" + "0" * 15, 7, None):
        with pytest.raises(sc.SpotifyContractError):
            sc.validate_session_id(bad_session)

    defaults = sc.SpotifyState().to_dict()
    assert defaults["setup"] == "not_configured" and defaults["connection"] == "disabled"
    assert defaults["device"] == "unknown" and defaults["job"] is None
    assert defaults["error"] is None

    with pytest.raises(sc.SpotifyContractError):
        sc.require_closed_keys({**payload, "path": "C:/Users/a"}, sc.SPOTIFY_STATE_KEYS,
                               where="state")
    with pytest.raises(sc.SpotifyContractError):
        sc.require_closed_keys({"setup": "configured"}, sc.SPOTIFY_STATE_KEYS, where="state")

    # Nenhum texto privado passa, mesmo que alguém acrescente um campo de texto no futuro.
    for private in ("C:\\Users\\ana", "C:/Users/ana", "https://open.spotify.com/x",
                    "spotify:track:abc", "file:///tmp/x", "a\x00b"):
        with pytest.raises(sc.SpotifyContractError):
            sc.assert_public_payload({"value": private})
    sc.assert_public_payload({"value": ["configured", 0, True, None, {"stage": "verify"}]})

    with pytest.raises(sc.SpotifyContractError):
        sc.SpotifyJob(id="C:\\bad", action="install", stage="detect", status="running")
    with pytest.raises(sc.SpotifyContractError):
        sc.SpotifyError(code="http_500")
    with pytest.raises(sc.SpotifyContractError):
        sc.SpotifyState(device="paused")
    with pytest.raises(sc.SpotifyContractError):
        sc.SpotifyState(revision=-1)


def _plan(**overrides):
    values = dict(
        action="install",
        plan_hash="a" * 64,
        steps=("detect", "obtain", "backup", "apply", "pair", "verify"),
        adds_link=True,
        replaces_files=0,
        backup_bytes=239_104_134,
        requires_close=True,
        requires_network=True,
    )
    values.update(overrides)
    return sc.SpotifyPlan(**values)


def test_plan_never_elevates_and_keeps_a_closed_ordered_shape() -> None:
    payload = _plan().to_dict()
    assert sorted(payload) == sorted(GOLDEN["spotify"]["dto_keys"]["plan"])
    assert sorted(payload["changes"]) == sorted(GOLDEN["spotify"]["dto_keys"]["plan_changes"])
    assert sorted(payload["requires"]) == sorted(GOLDEN["spotify"]["dto_keys"]["plan_requires"])
    assert payload["requires"] == {"close_spotify": True, "network": True, "elevation": False}
    assert payload["expires_in"] == GOLDEN["spotify"]["pins"]["consent_ttl_seconds"]
    assert payload["version_pin"] == sc.SPOTIFY_PINNED_SPOTIFY
    assert payload["cli_version"] == sc.SPOTIFY_PINNED_CLI

    with pytest.raises(sc.SpotifyContractError):
        _plan(steps=("apply", "detect"))
    with pytest.raises(sc.SpotifyContractError):
        _plan(steps=("detect", "detect"))
    with pytest.raises(sc.SpotifyContractError):
        _plan(steps=())
    with pytest.raises(sc.SpotifyContractError):
        _plan(version_pin="1.3.6.1")
    with pytest.raises(sc.SpotifyContractError):
        _plan(expires_in=60)
    with pytest.raises(sc.SpotifyContractError):
        _plan(plan_hash="A" * 64)

    consent = sc.SpotifyConsent(consent_id="cns-" + "b" * 32).to_dict()
    assert sorted(consent) == sorted(GOLDEN["spotify"]["dto_keys"]["consent"])
    assert consent["expires_in"] == sc.SPOTIFY_CONSENT_TTL_SECONDS
    with pytest.raises(sc.SpotifyContractError):
        sc.SpotifyConsent(consent_id="cns-" + "B" * 32)


def test_command_payloads_are_closed() -> None:
    sc.validate_command_payload("spotify.get", {})
    sc.validate_command_payload("spotify.plan", {"action": "remove"})
    sc.validate_command_payload("spotify.apply", {"consent_id": "cns-" + "c" * 32})
    sc.validate_command_payload("spotify.cancel", {"job_id": "spj-0123456789abcdef"})
    sc.validate_command_payload("spotify.open", {"target": "app"})
    sc.validate_command_payload("spotify.open", {"target": "download"})

    rejected = [
        ("spotify.get", {"action": "install"}),
        ("spotify.plan", {}),
        ("spotify.plan", {"action": "uninstall"}),
        ("spotify.plan", {"action": "install", "path": "C:/x"}),
        ("spotify.apply", {"consent_id": "cns-short"}),
        ("spotify.apply", {"consent_id": "cns-" + "c" * 32, "action": "install"}),
        ("spotify.cancel", {"job_id": "job-1"}),
        ("spotify.open", {}),
        ("spotify.open", {"target": "url"}),
        ("spotify.open", {"target": "app", "path": "C:/x"}),
        ("spotify.enable", {}),
    ]
    for command, payload in rejected:
        with pytest.raises(sc.SpotifyContractError) as exc_info:
            sc.validate_command_payload(command, payload)
        assert exc_info.value.code == "invalid_payload"


def test_capability_is_true_only_with_the_six_supported_commands() -> None:
    assert protocol.capabilities_for_commands(())["spotify"] is False
    assert protocol.capabilities_for_commands(protocol.COMMAND_TYPES)["spotify"] is True
    without_one = protocol.COMMAND_TYPES - {"spotify.cancel"}
    assert protocol.capabilities_for_commands(without_one)["spotify"] is False
    assert protocol.default_capabilities()["spotify"] is False
    assert protocol.default_capabilities() == GOLDEN["default_capabilities"]


def test_i1_session_announces_and_dispatches_all_spotify_commands() -> None:
    from app.bridge.server import BridgeServer
    from app.bridge.service import BridgeService

    assert protocol.SPOTIFY_COMMAND_TYPES <= BridgeService.SUPPORTED_COMMANDS
    assert protocol.capabilities_for_commands(BridgeService.SUPPORTED_COMMANDS)["spotify"] is True

    seen: list[str] = []

    def dispatch(envelope, send):
        seen.append(envelope.type)
        if envelope.type == "spotify.get":
            send({"v": 1, "id": envelope.id, "ok": True, "payload": {"spotify": {}}})
        return {}

    hello = protocol.encode_message({
        "v": 1, "id": "h-spotify", "type": "session.hello",
        "payload": {"client": "test", "client_version": "0.1.0"},
    })
    request = protocol.encode_message({
        "v": 1, "id": "sp-1", "type": "spotify.get", "payload": {},
    })
    shutdown = protocol.encode_message({"v": 1, "id": "s-1", "type": "app.shutdown",
                                        "payload": {}})
    stdout = io.BytesIO()
    server = BridgeServer(
        stdin=io.BytesIO((hello + request + shutdown).encode("utf-8")),
        stdout=stdout,
        stderr=io.BytesIO(),
        dispatcher=dispatch,
        supported_commands=BridgeService.SUPPORTED_COMMANDS,
    )
    assert server.run() == 0
    messages = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines()
                if line.strip()]
    ready = next(m for m in messages if m["type"] == "session.ready")
    assert ready["payload"]["capabilities"]["spotify"] is True
    assert "spotify.get" in seen
    assert not any(m.get("type") == "command.error" for m in messages)

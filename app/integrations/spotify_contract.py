"""Contrato público da integração Spotify (I0): nomes, DTOs e validadores fechados.

Deliberadamente puro: sem I/O, rede, processos, relógio ou imports de `output/`. O broker
e o job de instalação (I1) constroem estes DTOs; tudo o que sai para o renderer passa por
`assert_public_payload`, para que um path, URL, segredo ou URI Spotify não viaje por engano.

Fonte de verdade partilhada: `desktop/src/shared/ipc_golden_v1.json` (§`spotify`), comparada
por `tests/test_spotify_contract.py` e `desktop/tests/spotify-contract.test.ts`.
Ver `.agents/SPOTIFY-INTEGRATION-V1-CONTRACT.md`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

SPOTIFY_CAPABILITY = "spotify"
SPOTIFY_EVENT = "spotify.changed"

# Ordem congelada: o golden e o espelho TypeScript comparam-na exatamente.
SPOTIFY_COMMAND_TYPES: tuple[str, ...] = (
    "spotify.get",
    "spotify.plan",
    "spotify.apply",
    "spotify.cancel",
    "spotify.connect",
    "spotify.disconnect",
    "spotify.open",
)

# Payload fechado por comando: qualquer chave extra é `invalid_payload`.
SPOTIFY_COMMAND_PAYLOADS: dict[str, frozenset[str]] = {
    "spotify.get": frozenset(),
    "spotify.plan": frozenset({"action"}),
    "spotify.apply": frozenset({"consent_id"}),
    "spotify.cancel": frozenset({"job_id"}),
    "spotify.connect": frozenset(),
    "spotify.disconnect": frozenset(),
    "spotify.open": frozenset({"target"}),
}

# Chaves do resultado imediato de cada comando (progresso vem por `spotify.changed`).
SPOTIFY_RESULT_KEYS: dict[str, frozenset[str]] = {
    "spotify.get": frozenset({"spotify"}),
    "spotify.plan": frozenset({"plan", "consent"}),
    "spotify.apply": frozenset({"accepted", "job_id", "epoch"}),
    "spotify.cancel": frozenset({"cancelled", "reason"}),
    "spotify.connect": frozenset({"spotify"}),
    "spotify.disconnect": frozenset({"spotify"}),
    "spotify.open": frozenset({"opened"}),
}

SPOTIFY_SETUP_STATES: tuple[str, ...] = (
    "absent",
    "unsupported",
    "not_configured",
    "configured",
    "needs_repair",
)
SPOTIFY_CONNECTION_STATES: tuple[str, ...] = (
    "disabled",
    "connecting",
    "connected",
    "disconnected",
    "failed",
)
SPOTIFY_DEVICES: tuple[str, ...] = ("unknown", "local", "remote")
SPOTIFY_ACTIONS: tuple[str, ...] = ("install", "repair", "remove")
SPOTIFY_JOB_STAGES: tuple[str, ...] = (
    "detect",
    "review",
    "obtain",
    "backup",
    "apply",
    "pair",
    "verify",
    "rollback",
)
SPOTIFY_JOB_STATUSES: tuple[str, ...] = ("running", "done", "failed", "cancelled", "interrupted")
SPOTIFY_APP_CHANNELS: tuple[str, ...] = ("absent", "win32", "store", "other")
SPOTIFY_VERSION_FIT: tuple[str, ...] = ("supported", "newer", "older", "unknown")
SPOTIFY_ARCHITECTURES: tuple[str, ...] = ("x64", "arm64", "x86", "unknown")
SPOTIFY_CLI_STATES: tuple[str, ...] = ("missing", "pinned", "other")
SPOTIFY_LINK_STATES: tuple[str, ...] = ("absent", "present", "unknown")
SPOTIFY_CANCEL_REASONS: tuple[str, ...] = ("cancelled", "no_such_job", "already_finished")
# `app` = executável Spotify já detectado e validado; `download` = página oficial fixa.
SPOTIFY_OPEN_TARGETS: tuple[str, ...] = ("app", "download")

SPOTIFY_ERROR_CODES: frozenset[str] = frozenset({
    "invalid_payload",
    "not_implemented",
    "consent_invalid",
    "consent_expired",
    "consent_replayed",
    "job_busy",
    "unavailable",
    "not_configured",
    "spotify_missing",
    "store_unsupported",
    "arm_unsupported",
    "policy_blocked",
    "write_denied",
    "disk_space",
    "offline",
    "cli_unavailable",
    "signature_invalid",
    "hash_mismatch",
    "version_unsupported",
    "close_refused",
    "not_ready",
    "port_in_use",
    "profile_unsafe",
    "interrupted",
    "job_cancelled",
    "rollback_failed",
})

SPOTIFY_STATE_KEYS: frozenset[str] = frozenset({
    "setup", "connection", "device", "revision", "epoch", "job", "detection", "error",
})
SPOTIFY_DETECTION_KEYS: frozenset[str] = frozenset({
    "app", "version", "architecture", "cli", "link", "app_running",
})
SPOTIFY_JOB_KEYS: frozenset[str] = frozenset({"id", "action", "stage", "status", "error_code"})
SPOTIFY_ERROR_KEYS: frozenset[str] = frozenset({"code", "retryable"})
SPOTIFY_PLAN_KEYS: frozenset[str] = frozenset({
    "action", "plan_hash", "steps", "changes", "requires", "expires_in",
    "version_pin", "cli_version",
})
SPOTIFY_PLAN_CHANGES_KEYS: frozenset[str] = frozenset({
    "adds_link", "replaces_files", "backup_bytes",
})
SPOTIFY_PLAN_REQUIRES_KEYS: frozenset[str] = frozenset({
    "close_spotify", "network", "elevation",
})
SPOTIFY_CONSENT_KEYS: frozenset[str] = frozenset({"consent_id", "expires_in"})
# Evento no fio: `session_id` vem do mesmo emissor que serve o Gmail (`_integration_emit`),
# nunca do `SpotifyStateDto`. Só a mesma sessão ordena por `revision`.
SPOTIFY_CHANGED_KEYS: frozenset[str] = frozenset({"session_id", "spotify"})

SESSION_ID_PATTERN = re.compile(r"^sess-[0-9a-f]{16}$")
CONSENT_ID_PATTERN = re.compile(r"^cns-[0-9a-f]{32}$")
JOB_ID_PATTERN = re.compile(r"^spj-[0-9a-f]{16}$")
PLAN_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# Registo validado (ver contrato §13): só estas versões são "supported" nesta V1.
SPOTIFY_PINNED_SPOTIFY = "1.3.5.3058"
SPOTIFY_PINNED_CLI = "2.46.2"
SPOTIFY_CONSENT_TTL_SECONDS = 600
SPOTIFY_ACTIVATION_TTL_SECONDS = 15.0
SPOTIFY_LOOPBACK_PORT = 45871

_MAX_PUBLIC_TEXT = 200
_FORBIDDEN_TEXT = ("\\", "/", "file:", "http:", "https:", "spotify:", "\x00")


class SpotifyContractError(ValueError):
    """Violação do contrato público; `code` segue o vocabulário de erros congelado."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code if code in SPOTIFY_ERROR_CODES else "invalid_payload"


def validate_session_id(value: Any) -> str:
    """A sessão do evento é a da bridge; um id antigo/forjado não ordena estado novo."""
    if not isinstance(value, str) or not SESSION_ID_PATTERN.fullmatch(value):
        raise SpotifyContractError("invalid_payload", "session_id inválido.")
    return value


def _choice(value: Any, choices: tuple[str, ...], *, where: str) -> str:
    if value not in choices:
        raise SpotifyContractError("invalid_payload", f"Valor inválido em {where}.")
    return value


def _count(value: Any, *, where: str) -> int:
    if type(value) is not int or value < 0:
        raise SpotifyContractError("invalid_payload", f"Valor inválido em {where}.")
    return value


def _flag(value: Any, *, where: str) -> bool:
    if type(value) is not bool:
        raise SpotifyContractError("invalid_payload", f"Valor inválido em {where}.")
    return value


def require_closed_keys(payload: Mapping[str, Any], keys: frozenset[str], *, where: str) -> None:
    """Recusa chaves em falta ou a mais: o DTO não ganha campos por acidente."""
    if not isinstance(payload, Mapping) or set(payload) != keys:
        raise SpotifyContractError("invalid_payload", f"Campos fechados inválidos em {where}.")


def assert_public_payload(value: Any) -> None:
    """Garante que nada privado (path, URL, URI, segredo, NUL) sai no DTO público."""
    if isinstance(value, str):
        if len(value) > _MAX_PUBLIC_TEXT or any(token in value for token in _FORBIDDEN_TEXT):
            raise SpotifyContractError("invalid_payload", "Texto privado no payload público.")
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            raise SpotifyContractError("invalid_payload", "Surrogate no payload público.")
        return
    if isinstance(value, bool) or value is None or type(value) is int:
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            assert_public_payload(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SpotifyContractError("invalid_payload", "Chave não textual no payload público.")
            assert_public_payload(key)
            assert_public_payload(item)
        return
    raise SpotifyContractError("invalid_payload", "Tipo não público no payload.")


@dataclass(frozen=True)
class SpotifyDetection:
    app: str = "absent"
    version: str = "unknown"
    architecture: str = "unknown"
    cli: str = "missing"
    link: str = "unknown"
    app_running: bool = False

    def __post_init__(self) -> None:
        _choice(self.app, SPOTIFY_APP_CHANNELS, where="detection.app")
        _choice(self.version, SPOTIFY_VERSION_FIT, where="detection.version")
        _choice(self.architecture, SPOTIFY_ARCHITECTURES, where="detection.architecture")
        _choice(self.cli, SPOTIFY_CLI_STATES, where="detection.cli")
        _choice(self.link, SPOTIFY_LINK_STATES, where="detection.link")
        _flag(self.app_running, where="detection.app_running")

    def to_dict(self) -> dict[str, Any]:
        out = {
            "app": self.app,
            "version": self.version,
            "architecture": self.architecture,
            "cli": self.cli,
            "link": self.link,
            "app_running": self.app_running,
        }
        require_closed_keys(out, SPOTIFY_DETECTION_KEYS, where="detection")
        assert_public_payload(out)
        return out


@dataclass(frozen=True)
class SpotifyJob:
    id: str
    action: str
    stage: str
    status: str
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not JOB_ID_PATTERN.fullmatch(self.id):
            raise SpotifyContractError("invalid_payload", "job.id inválido.")
        _choice(self.action, SPOTIFY_ACTIONS, where="job.action")
        _choice(self.stage, SPOTIFY_JOB_STAGES, where="job.stage")
        _choice(self.status, SPOTIFY_JOB_STATUSES, where="job.status")
        if self.error_code is not None and self.error_code not in SPOTIFY_ERROR_CODES:
            raise SpotifyContractError("invalid_payload", "job.error_code inválido.")

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "action": self.action,
            "stage": self.stage,
            "status": self.status,
            "error_code": self.error_code,
        }
        require_closed_keys(out, SPOTIFY_JOB_KEYS, where="job")
        assert_public_payload(out)
        return out


@dataclass(frozen=True)
class SpotifyError:
    code: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.code not in SPOTIFY_ERROR_CODES:
            raise SpotifyContractError("invalid_payload", "error.code fora do vocabulário.")
        _flag(self.retryable, where="error.retryable")

    def to_dict(self) -> dict[str, Any]:
        out = {"code": self.code, "retryable": self.retryable}
        require_closed_keys(out, SPOTIFY_ERROR_KEYS, where="error")
        assert_public_payload(out)
        return out


@dataclass(frozen=True)
class SpotifyState:
    setup: str = "not_configured"
    connection: str = "disabled"
    device: str = "unknown"
    revision: int = 0
    epoch: int = 0
    job: SpotifyJob | None = None
    detection: SpotifyDetection = field(default_factory=SpotifyDetection)
    error: SpotifyError | None = None

    def __post_init__(self) -> None:
        _choice(self.setup, SPOTIFY_SETUP_STATES, where="setup")
        _choice(self.connection, SPOTIFY_CONNECTION_STATES, where="connection")
        _choice(self.device, SPOTIFY_DEVICES, where="device")
        _count(self.revision, where="revision")
        _count(self.epoch, where="epoch")
        if self.job is not None and not isinstance(self.job, SpotifyJob):
            raise SpotifyContractError("invalid_payload", "job inválido.")
        if not isinstance(self.detection, SpotifyDetection):
            raise SpotifyContractError("invalid_payload", "detection inválida.")
        if self.error is not None and not isinstance(self.error, SpotifyError):
            raise SpotifyContractError("invalid_payload", "error inválido.")

    def to_dict(self) -> dict[str, Any]:
        out = {
            "setup": self.setup,
            "connection": self.connection,
            "device": self.device,
            "revision": self.revision,
            "epoch": self.epoch,
            "job": self.job.to_dict() if self.job is not None else None,
            "detection": self.detection.to_dict(),
            "error": self.error.to_dict() if self.error is not None else None,
        }
        require_closed_keys(out, SPOTIFY_STATE_KEYS, where="state")
        assert_public_payload(out)
        return out

    def changed_payload(self) -> dict[str, Any]:
        """Payload do broker; o serviço acrescenta `session_id` como faz ao Gmail."""
        payload = {"spotify": self.to_dict()}
        require_closed_keys(payload, frozenset({"spotify"}), where="spotify.changed")
        assert_public_payload(payload)
        return payload


@dataclass(frozen=True)
class SpotifyPlan:
    action: str
    plan_hash: str
    steps: tuple[str, ...]
    adds_link: bool
    replaces_files: int
    backup_bytes: int
    requires_close: bool
    requires_network: bool
    expires_in: int = SPOTIFY_CONSENT_TTL_SECONDS
    version_pin: str = SPOTIFY_PINNED_SPOTIFY
    cli_version: str = SPOTIFY_PINNED_CLI

    def __post_init__(self) -> None:
        _choice(self.action, SPOTIFY_ACTIONS, where="plan.action")
        if not isinstance(self.plan_hash, str) or not PLAN_HASH_PATTERN.fullmatch(self.plan_hash):
            raise SpotifyContractError("invalid_payload", "plan_hash inválido.")
        steps = tuple(self.steps)
        if not steps or len(steps) > len(SPOTIFY_JOB_STAGES) or len(set(steps)) != len(steps):
            raise SpotifyContractError("invalid_payload", "plan.steps inválido.")
        order = {stage: index for index, stage in enumerate(SPOTIFY_JOB_STAGES)}
        for stage in steps:
            if stage not in order:
                raise SpotifyContractError("invalid_payload", "plan.steps inválido.")
        if [order[stage] for stage in steps] != sorted(order[stage] for stage in steps):
            raise SpotifyContractError("invalid_payload", "plan.steps fora de ordem.")
        _flag(self.adds_link, where="plan.adds_link")
        _count(self.replaces_files, where="plan.replaces_files")
        _count(self.backup_bytes, where="plan.backup_bytes")
        _flag(self.requires_close, where="plan.requires_close")
        _flag(self.requires_network, where="plan.requires_network")
        if self.expires_in != SPOTIFY_CONSENT_TTL_SECONDS:
            raise SpotifyContractError("invalid_payload", "plan.expires_in inválido.")
        if self.version_pin != SPOTIFY_PINNED_SPOTIFY or self.cli_version != SPOTIFY_PINNED_CLI:
            raise SpotifyContractError("invalid_payload", "plan pins inválidos.")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "action": self.action,
            "plan_hash": self.plan_hash,
            "steps": list(self.steps),
            # V1 nunca eleva: garantido pela construção, não por um campo que venha do pedido.
            "changes": {
                "adds_link": self.adds_link,
                "replaces_files": self.replaces_files,
                "backup_bytes": self.backup_bytes,
            },
            "requires": {
                "close_spotify": self.requires_close,
                "network": self.requires_network,
                "elevation": False,
            },
            "expires_in": self.expires_in,
            "version_pin": self.version_pin,
            "cli_version": self.cli_version,
        }
        require_closed_keys(out, SPOTIFY_PLAN_KEYS, where="plan")
        require_closed_keys(out["changes"], SPOTIFY_PLAN_CHANGES_KEYS, where="plan.changes")
        require_closed_keys(out["requires"], SPOTIFY_PLAN_REQUIRES_KEYS, where="plan.requires")
        assert_public_payload(out)
        return out


@dataclass(frozen=True)
class SpotifyConsent:
    consent_id: str
    expires_in: int = SPOTIFY_CONSENT_TTL_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.consent_id, str) or not CONSENT_ID_PATTERN.fullmatch(self.consent_id):
            raise SpotifyContractError("invalid_payload", "consent_id inválido.")
        if self.expires_in != SPOTIFY_CONSENT_TTL_SECONDS:
            raise SpotifyContractError("invalid_payload", "consent.expires_in inválido.")

    def to_dict(self) -> dict[str, Any]:
        out = {"consent_id": self.consent_id, "expires_in": self.expires_in}
        require_closed_keys(out, SPOTIFY_CONSENT_KEYS, where="consent")
        return out


def validate_command_payload(command: str, payload: Mapping[str, Any]) -> None:
    """Valida o payload fechado de um comando Spotify antes de qualquer efeito.

    I0 não tem handler; I1 chama isto no dispatch para que um campo desconhecido nunca
    chegue ao instalador/broker.
    """
    if command not in SPOTIFY_COMMAND_PAYLOADS:
        raise SpotifyContractError("invalid_payload", "Comando Spotify desconhecido.")
    if not isinstance(payload, Mapping) or set(payload) != SPOTIFY_COMMAND_PAYLOADS[command]:
        raise SpotifyContractError("invalid_payload", "Payload Spotify inválido.")
    if command == "spotify.plan":
        _choice(payload["action"], SPOTIFY_ACTIONS, where="plan.action")
    elif command == "spotify.apply":
        value = payload["consent_id"]
        if not isinstance(value, str) or not CONSENT_ID_PATTERN.fullmatch(value):
            raise SpotifyContractError("invalid_payload", "consent_id inválido.")
    elif command == "spotify.cancel":
        value = payload["job_id"]
        if not isinstance(value, str) or not JOB_ID_PATTERN.fullmatch(value):
            raise SpotifyContractError("invalid_payload", "job_id inválido.")
    elif command == "spotify.open":
        _choice(payload["target"], SPOTIFY_OPEN_TARGETS, where="open.target")

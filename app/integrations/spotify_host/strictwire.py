"""Strict wire format for the Spotify S1 kit bridge (protocol v2).

Only what the kit needs: strict JSON, a finite envelope schema per message type,
closed per-action arguments, and the mutual HMAC-SHA256 proofs that bind mode,
device scope and declared support. No product imports.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Callable, Iterable

PROTOCOL_VERSION = 2
MAX_FRAME_BYTES = 64 * 1024
DOMAIN = "lunmi-spotify-kit"

ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
URI_RE = re.compile(r"^spotify:[a-z]{1,20}:[A-Za-z0-9._:-]{1,120}$")
TRACK_URI_RE = re.compile(r"^spotify:track:[A-Za-z0-9]{22}$")

ALLOWED_ACTIONS = (
    "get_player_state",
    "get_device_scope",
    "activate_local",
    "play",
    "pause",
    "set_volume",
    "next",
    "previous",
    "search_tracks",
    "play_uri",
)
READ_ACTIONS = frozenset({"get_player_state", "get_device_scope", "search_tracks"})
# Activation is a separate capability, not a music mutation: it may target this PC
# while the active device is remote/unknown, always behind a trusted backend
# approval that never travels on the wire.
ACTIVATION_ACTIONS = frozenset({"activate_local"})
MUTATIONS = frozenset(ALLOWED_ACTIONS) - READ_ACTIONS - ACTIVATION_ACTIONS
SEARCH_LIMIT_MAX = 10
SEARCH_QUERY_MAX = 200
SEARCH_RESULT_MAX = 10
MODES = ("read_only", "control")
DEVICE_SCOPES = ("local", "remote", "unknown")
CAP_STATES = (
    "available",
    "blocked_read_only",
    "blocked_device_scope",
    "unsupported",
    "unknown",
)
STATUSES = (
    "SUCCESS_VERIFIED",
    "FAILED",
    "UNSUPPORTED",
    "NOT_VERIFIED",
    "CANCELLED",
    "TIMEOUT",
)
REPEATS = ("off", "track", "context")
PLAYER_STAGES = frozenset(
    {"player", "volume", "progress", "shuffle", "repeat", "playing"}
)
SEARCH_STAGES = frozenset({"route", "definition", "query", "shape", "errors"})

STATE_KEYS = (
    "track_uri",
    "title",
    "artist",
    "album",
    "is_playing",
    "position_ms",
    "volume",
    "shuffle",
    "repeat",
)

DEVICE_SCOPE_KEYS = (
    "device_scope",
    "connect_active",
    "connect_local",
    "playback_local",
)


class ProtocolError(Exception):
    """A refusal with a fixed, non-sensitive code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail[:120]


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ProtocolError("DUPLICATE_KEY")
        seen[key] = value
    return seen


def _no_constants(token: str) -> Any:
    raise ProtocolError("NON_FINITE_NUMBER")


def loads_strict(raw: bytes | str) -> dict[str, Any]:
    """Decode one frame. Raises ProtocolError; never echoes payload content."""
    if isinstance(raw, bytes):
        if len(raw) > MAX_FRAME_BYTES:
            raise ProtocolError("FRAME_TOO_LARGE")
        try:
            text = raw.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:  # pragma: no cover - defensive
            raise ProtocolError("INVALID_UTF8") from exc
    else:
        text = raw
        if len(text.encode("utf-8")) > MAX_FRAME_BYTES:
            raise ProtocolError("FRAME_TOO_LARGE")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_constant=_no_constants,
        )
    except ProtocolError:
        raise
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError("MALFORMED_JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("MALFORMED_JSON")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _int(value: Any, low: int, high: int) -> bool:
    return _is_int(value) and low <= value <= high


def _number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == value
        and value not in (float("inf"), float("-inf"))
    )


def _hex64(value: Any) -> bool:
    return isinstance(value, str) and HEX64_RE.match(value) is not None


def _hex32(value: Any) -> bool:
    return isinstance(value, str) and HEX32_RE.match(value) is not None


def _ident(value: Any) -> bool:
    return isinstance(value, str) and ID_RE.match(value) is not None


def _maybe_ident(value: Any) -> bool:
    return value == "" or _ident(value)


def _text(value: Any, limit: int = 200) -> bool:
    return isinstance(value, str) and 0 < len(value) <= limit


def _optional_text(value: Any, limit: int = 200) -> bool:
    return value is None or _text(value, limit)


def parse_support(value: Any) -> frozenset[str]:
    """Canonical support list: unique action names, ascending, comma separated."""
    if not isinstance(value, str) or len(value) > 200:
        raise ProtocolError("BAD_FIELD", "support")
    if value == "":
        return frozenset()
    names = value.split(",")
    if names != sorted(names) or len(set(names)) != len(names):
        raise ProtocolError("BAD_FIELD", "support")
    for name in names:
        if name not in ALLOWED_ACTIONS:
            raise ProtocolError("BAD_FIELD", "support")
    return frozenset(names)


def support_string(names: Iterable[str]) -> str:
    return ",".join(sorted(set(names)))


def _search_query(value: Any) -> bool:
    if not isinstance(value, str) or not (1 <= len(value) <= SEARCH_QUERY_MAX):
        return False
    if value.strip() == "":
        return False
    return not any(ord(char) < 32 or ord(char) == 127 for char in value)


def validate_args(action: str, args: Any) -> bool:
    if not isinstance(args, dict):
        return False
    if action == "set_volume":
        if set(args) != {"level"}:
            return False
        level = args["level"]
        return _number(level) and 0 <= level <= 1
    if action == "search_tracks":
        if set(args) != {"query", "limit"}:
            return False
        return _search_query(args["query"]) and _int(
            args["limit"], 1, SEARCH_LIMIT_MAX
        )
    if action == "play_uri":
        if set(args) != {"uri"}:
            return False
        uri = args["uri"]
        return isinstance(uri, str) and TRACK_URI_RE.match(uri) is not None
    # Both activate_local and the no-argument player operations are closed: the
    # activation target is this PC by construction, so no identifier is accepted.
    return len(args) == 0


def validate_state(value: Any) -> bool:
    """Full state, or nulls where the client could not observe a value."""
    if not isinstance(value, dict) or set(value) != set(STATE_KEYS):
        return False
    uri = value["track_uri"]
    title = value["title"]
    if (uri is None) != (title is None):
        return False
    if uri is not None and not (
        isinstance(uri, str) and URI_RE.match(uri) is not None
    ):
        return False
    if title is not None and not _text(title):
        return False
    return (
        _optional_text(value["artist"])
        and _optional_text(value["album"])
        and isinstance(value["is_playing"], bool)
        and _int(value["position_ms"], 0, 86_400_000)
        and _number(value["volume"])
        and 0 <= value["volume"] <= 1
        and isinstance(value["shuffle"], bool)
        and value["repeat"] in REPEATS
    )


def validate_search_data(value: Any) -> bool:
    """Strict list of limited tracks; never a placeholder for an unknown answer."""
    if not isinstance(value, dict) or set(value) != {"tracks"}:
        return False
    tracks = value["tracks"]
    if not isinstance(tracks, list) or len(tracks) > SEARCH_RESULT_MAX:
        return False
    for track in tracks:
        if not isinstance(track, dict) or set(track) != {
            "uri",
            "title",
            "artist",
            "album",
        }:
            return False
        uri = track["uri"]
        if not isinstance(uri, str) or TRACK_URI_RE.match(uri) is None:
            return False
        if not _text(track["title"], 300):
            return False
        if not _optional_text(track["artist"], 200):
            return False
        if not _optional_text(track["album"], 200):
            return False
    return True


def validate_device_scope_data(value: Any) -> bool:
    """Closed diagnostic snapshot: only enums and booleans/nulls, no identity."""
    if not isinstance(value, dict) or set(value) != set(DEVICE_SCOPE_KEYS):
        return False
    if value["device_scope"] not in DEVICE_SCOPES:
        return False
    return all(
        value[key] is None or isinstance(value[key], bool)
        for key in ("connect_active", "connect_local", "playback_local")
    )


def validate_result_data(action: str, value: Any) -> bool:
    """The success payload is closed per dispatched action, never a wildcard."""
    if action == "search_tracks":
        return validate_search_data(value)
    if action == "get_device_scope" or action in ACTIVATION_ACTIONS:
        return validate_device_scope_data(value)
    return validate_state(value)


def validate_capabilities(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(ALLOWED_ACTIONS)
        and all(state in CAP_STATES for state in value.values())
    )


def effective_capabilities(
    support: frozenset[str], mode: str, device_scope: str
) -> dict[str, str]:
    out: dict[str, str] = {}
    for action in ALLOWED_ACTIONS:
        if action not in support:
            out[action] = "unsupported"
        elif action in READ_ACTIONS:
            out[action] = "available"
        elif action in ACTIVATION_ACTIONS:
            # Actionable in control mode whatever the signed device scope; this is
            # capability, never backend approval, and never a waiver for music.
            out[action] = "available" if mode == "control" else "blocked_read_only"
        elif mode != "control":
            out[action] = "blocked_read_only"
        elif device_scope != "local":
            out[action] = "blocked_device_scope"
        else:
            out[action] = "available"
    return out


def _evidence(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"kind", "observed"}
        and value["kind"] in ("player_state", "device_scope", "effect")
        and isinstance(value["observed"], bool)
    )


_FIELD: dict[str, Callable[[Any], bool]] = {
    "v": lambda value: value == PROTOCOL_VERSION and _is_int(value),
    "id": _maybe_ident,
    "server_nonce": _hex64,
    "client_nonce": _hex64,
    "client_proof": _hex64,
    "server_proof": _hex64,
    "session": _hex32,
    "epoch": lambda value: _int(value, 1, 2**31),
    "auth_deadline_ms": lambda value: _int(value, 100, 10_000),
    "deadline_ms": lambda value: _int(value, 100, 5_000),
    "duration_ms": lambda value: _int(value, 0, 60_000),
    "role": lambda value: value == "client",
    "mode": lambda value: value in MODES,
    "device_scope": lambda value: value in DEVICE_SCOPES,
    "support": lambda value: isinstance(value, str) and len(value) <= 200,
    "action": lambda value: value in ALLOWED_ACTIONS,
    "args": lambda value: isinstance(value, dict),
    "status": lambda value: value in STATUSES,
    "capabilities": validate_capabilities,
    "origin_filter": lambda value: value == "kit",
    "data": lambda value: value is None
    or validate_state(value)
    or validate_search_data(value)
    or validate_device_scope_data(value),
    "evidence": _evidence,
    "code": lambda value: _text(value, 64),
    "detail": lambda value: _text(value, 120),
}

_SCHEMAS: dict[str, frozenset[str]] = {
    "hello": frozenset(
        {"v", "type", "server_nonce", "epoch", "auth_deadline_ms", "mode"}
    ),
    "auth": frozenset(
        {
            "v",
            "type",
            "id",
            "client_nonce",
            "client_proof",
            "role",
            "mode",
            "device_scope",
            "support",
        }
    ),
    "ready": frozenset(
        {
            "v",
            "type",
            "id",
            "server_proof",
            "session",
            "epoch",
            "mode",
            "device_scope",
            "capabilities",
            "origin_filter",
        }
    ),
    "command": frozenset(
        {"v", "type", "id", "session", "action", "args", "deadline_ms"}
    ),
    "result": frozenset(
        {"v", "type", "id", "session", "status", "data", "evidence", "duration_ms"}
    ),
    "error": frozenset({"v", "type", "id", "code", "detail"}),
}


def validate(msg: dict[str, Any]) -> str:
    """Validate the envelope. Returns the type; never includes payload values."""
    kind = msg.get("type")
    if not isinstance(kind, str) or kind not in _SCHEMAS:
        raise ProtocolError("UNKNOWN_TYPE")
    allowed = _SCHEMAS[kind]
    keys = set(msg)
    if kind != "error":
        if keys != allowed:
            missing = sorted(allowed - keys)
            extra = sorted(keys - allowed)
            raise ProtocolError(
                "BAD_FIELDS",
                "missing:" + ",".join(missing) + ";extra:" + ",".join(extra),
            )
    else:
        if not {"v", "type", "id", "code"} <= keys or not keys <= allowed:
            raise ProtocolError("BAD_FIELDS")
    for key, check in _FIELD.items():
        if key in msg and key in allowed and not check(msg[key]):
            raise ProtocolError("BAD_FIELD", key)
    if "id" in allowed and kind != "error" and not msg["id"]:
        raise ProtocolError("BAD_FIELD", "id")
    if kind == "auth":
        parse_support(msg["support"])
    if kind == "command" and not validate_args(msg["action"], msg["args"]):
        raise ProtocolError("BAD_ARGS")
    if kind == "result":
        # A failure must be representable without inventing player data; a success
        # must carry the state it claims to have observed.
        if msg["status"] == "SUCCESS_VERIFIED":
            # An unobserved success claim is representable so the server can
            # downgrade it to NOT_VERIFIED; it is never counted as verified.
            if msg["data"] is None:
                raise ProtocolError("BAD_FIELD", "data")
        else:
            if msg["data"] is not None:
                raise ProtocolError("BAD_FIELD", "data")
            if msg["evidence"]["observed"]:
                raise ProtocolError("BAD_FIELD", "evidence")
    return kind


def frame(kind: str, **fields: Any) -> dict[str, Any]:
    payload = {"v": PROTOCOL_VERSION, "type": kind}
    payload.update(fields)
    validate(payload)
    return payload


def encode(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def proof_key(secret: bytes) -> bytes:
    if len(secret) != 32:
        raise ProtocolError("BAD_SECRET_LENGTH")
    return secret


def _proof(
    role: str,
    secret: bytes,
    server_nonce: str,
    client_nonce: str,
    epoch: int,
    mode: str,
    device_scope: str,
    support: str,
) -> str:
    message = (
        f"{DOMAIN}/{role}/v2\n{server_nonce}\n{client_nonce}\n{epoch}\n"
        f"{mode}\n{device_scope}\n{support}"
    ).encode("utf-8")
    return hmac.new(proof_key(secret), message, hashlib.sha256).hexdigest()


def client_proof(
    secret: bytes,
    server_nonce: str,
    client_nonce: str,
    epoch: int,
    mode: str,
    device_scope: str,
    support: str,
) -> str:
    return _proof(
        "client", secret, server_nonce, client_nonce, epoch, mode, device_scope, support
    )


def server_proof(
    secret: bytes,
    server_nonce: str,
    client_nonce: str,
    epoch: int,
    mode: str,
    device_scope: str,
    support: str,
) -> str:
    return _proof(
        "server", secret, server_nonce, client_nonce, epoch, mode, device_scope, support
    )


def proof_matches(expected: str, received: str) -> bool:
    return isinstance(received, str) and hmac.compare_digest(expected, received)

"""Ponte IPC JSONL v1 entre o shell desktop (Electron) e o backend Python."""
from __future__ import annotations

from app.bridge.protocol import (
    COMMAND_TYPES,
    EVENT_TYPES,
    MAX_CHAT_TEXT_CHARS,
    MAX_ID_CHARS,
    MAX_IMPORT_BYTES,
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    Envelope,
    JsonlFramer,
    ProtocolError,
    default_capabilities,
    encode_message,
    make_error,
    make_event,
    make_result,
    parse_envelope,
)

__all__ = [
    "COMMAND_TYPES",
    "EVENT_TYPES",
    "MAX_CHAT_TEXT_CHARS",
    "MAX_ID_CHARS",
    "MAX_IMPORT_BYTES",
    "MAX_LINE_BYTES",
    "PROTOCOL_VERSION",
    "Envelope",
    "JsonlFramer",
    "ProtocolError",
    "default_capabilities",
    "encode_message",
    "make_error",
    "make_event",
    "make_result",
    "parse_envelope",
]

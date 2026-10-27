"""Segredo por instalação Spotify: DPAPI no Windows, plaintext só com QA explícito.

32 bytes aleatórios, protegidos com DPAPI no âmbito do utilizador e entropia da
integração. Este segredo de pareamento **não** é uma credencial da conta Spotify:
a cópia utilizável pelo bundle da extensão fica no perfil do mesmo utilizador
Windows. Nunca é logado, impresso ou passado em argv.
"""

from __future__ import annotations

import ctypes
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from .config import KitError

MAGIC = b"LKS1"
KIND_DPAPI = b"DPAPI"
KIND_PLAIN = b"PLAIN"
ENTROPY = b"lunmi-spotify/v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x01


def generate_secret() -> bytes:
    return secrets.token_bytes(32)


def _allow_plaintext() -> bool:
    return os.environ.get("LUNMI_KIT_ALLOW_PLAINTEXT_SECRET") == "1"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(payload: bytes) -> tuple[_DATA_BLOB, Any]:
    buffer = ctypes.create_string_buffer(payload, len(payload))
    blob = _DATA_BLOB(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise KitError("SECRET_STORAGE_UNAVAILABLE", "dpapi")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    input_blob, _input_buffer = _blob(data)
    entropy_blob, _entropy_buffer = _blob(ENTROPY)
    output_blob = _DATA_BLOB()
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "lunmi-spotify-kit",
            ctypes.byref(entropy_blob),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    if not ok:
        raise KitError("SECRET_STORAGE_FAILED", f"dpapi:{ctypes.get_last_error()}")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def protect(secret: bytes) -> bytes:
    if len(secret) != 32:
        raise KitError("SECRET_INVALID", "length")
    if sys.platform == "win32":
        return MAGIC + KIND_DPAPI + _dpapi(secret, True)
    if _allow_plaintext():
        return MAGIC + KIND_PLAIN + secret
    raise KitError("SECRET_STORAGE_UNAVAILABLE", "non-windows")


def unprotect(raw: bytes) -> bytes:
    if len(raw) < len(MAGIC) + 5 or not raw.startswith(MAGIC):
        raise KitError("SECRET_INVALID", "header")
    kind = raw[len(MAGIC) : len(MAGIC) + 5]
    body = raw[len(MAGIC) + 5 :]
    if kind == KIND_DPAPI:
        secret = _dpapi(body, False)
    elif kind == KIND_PLAIN:
        if not _allow_plaintext():
            raise KitError("SECRET_STORAGE_UNAVAILABLE", "plaintext disabled")
        secret = body
    else:
        raise KitError("SECRET_INVALID", "kind")
    if len(secret) != 32:
        raise KitError("SECRET_INVALID", "length")
    return secret


def save_secret(path: str | Path, secret: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = protect(secret)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, target)


def load_secret(path: str | Path) -> bytes:
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise KitError("SECRET_MISSING", str(target)) from exc
    return unprotect(raw)


def storage_kind() -> str:
    if sys.platform == "win32":
        return "dpapi"
    return "plaintext-qa" if _allow_plaintext() else "unavailable"

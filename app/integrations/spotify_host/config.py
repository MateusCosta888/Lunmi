"""Configuração do host Spotify e códigos de erro estáveis (porte do kit).

Mantém os limites do kit: porta fixa validada, origens CEF allowlistadas e modo
`control` por omissão no produto (os efeitos continuam atrás de scope/approval).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import strictwire as wire

DEFAULT_PORT = 45871
DEFAULT_MODE = "control"
# A lab filter, never identity. The real kit accepts only the Spotify CEF
# origin; QA fixture servers must pass an explicit origin_allowlist instead of
# widening this default.
ALLOWED_ORIGINS = ("https://xpui.app.spotify.com",)
CONFIG_VERSION = 1


class KitError(Exception):
    """A bounded, non-sensitive kit failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail[:160]


def qa_enabled() -> bool:
    return os.environ.get("LUNMI_KIT_QA") == "1"


@dataclass(frozen=True)
class KitConfig:
    port: int
    mode: str = DEFAULT_MODE
    origins: tuple[str, ...] = ALLOWED_ORIGINS

    def validate(self) -> None:
        if not isinstance(self.port, int) or isinstance(self.port, bool):
            raise KitError("KIT_CONFIG_INVALID", "port")
        if self.port == 0:
            if not qa_enabled():
                raise KitError("KIT_CONFIG_INVALID", "port 0 is QA only")
        elif not 1024 <= self.port <= 65535:
            raise KitError("KIT_CONFIG_INVALID", "port range")
        if self.mode not in ("read_only", "control"):
            raise KitError("KIT_CONFIG_INVALID", "mode")
        if not self.origins:
            raise KitError("KIT_CONFIG_INVALID", "origins")


def config_from_dict(data: Any) -> KitConfig:
    if not isinstance(data, dict):
        raise KitError("KIT_CONFIG_INVALID", "json")
    allowed = {"version", "port", "mode", "origins", "created"}
    if set(data) - allowed:
        raise KitError("KIT_CONFIG_INVALID", "fields")
    if data.get("version") != CONFIG_VERSION:
        raise KitError("KIT_CONFIG_INVALID", "version")
    origins = data.get("origins", list(ALLOWED_ORIGINS))
    if not isinstance(origins, list) or not all(isinstance(o, str) for o in origins):
        raise KitError("KIT_CONFIG_INVALID", "origins")
    port = data.get("port")
    if not isinstance(port, int) or isinstance(port, bool):
        raise KitError("KIT_CONFIG_INVALID", "port")
    config = KitConfig(
        port=port,
        mode=data.get("mode", DEFAULT_MODE),
        origins=tuple(origins),
    )
    config.validate()
    return config


def load_config_file(path: str | Path) -> KitConfig:
    target = Path(path)
    try:
        raw = target.read_text("utf-8")
    except OSError as exc:
        raise KitError("KIT_CONFIG_MISSING", str(target)) from exc
    try:
        data = wire.loads_strict(raw)
    except wire.ProtocolError as exc:
        raise KitError("KIT_CONFIG_INVALID", exc.code) from exc
    except ValueError as exc:  # pragma: no cover - defensive
        raise KitError("KIT_CONFIG_INVALID", "json") from exc
    return config_from_dict(data)


def write_config_file(path: str | Path, config: KitConfig) -> None:
    config.validate()
    payload = {
        "version": CONFIG_VERSION,
        "port": config.port,
        "mode": config.mode,
        "origins": list(config.origins),
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".kit-config-", dir=str(target.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise

"""Preferências locais da voz (ficheiro próprio, atómico, sandbox-aware).

Separado de `lunmi-ui.json` de propósito: uma preferência de áudio que falha a gravar não pode
corromper o tema/janela do utilizador, e o sandbox do `BridgeService` redireciona `root_dir` para o
diretório isolado sem nenhum caminho especial neste módulo.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

PREFS_FILENAME = "voice-prefs.json"
PREFS_VERSION = 1


@dataclass(frozen=True)
class VoicePrefs:
    enabled: bool = False
    auto_speak: bool = False


def prefs_path(root_dir: str | Path) -> Path:
    return Path(root_dir) / ".local" / PREFS_FILENAME


def load_prefs(path: str | Path) -> VoicePrefs:
    """Leitura tolerante: ficheiro ausente/corrompido volta ao default desligado.

    Nada do conteúdo é registado ou ecoado; um erro de I/O nunca ativa voz por acidente.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return VoicePrefs()
    if not isinstance(raw, dict):
        return VoicePrefs()
    enabled = raw.get("enabled")
    auto_speak = raw.get("auto_speak")
    return VoicePrefs(
        enabled=True if enabled is True else False,
        auto_speak=True if auto_speak is True else False,
    )


def save_prefs(path: str | Path, prefs: VoicePrefs) -> bool:
    """Gravação atómica (tmp no mesmo diretório + `os.replace`). `False` em falha de I/O."""
    target = Path(path)
    payload = {
        "version": PREFS_VERSION,
        "enabled": bool(prefs.enabled),
        "auto_speak": bool(prefs.auto_speak),
    }
    temp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        return True
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        return False

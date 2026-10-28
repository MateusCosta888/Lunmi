"""Localização confiável do runtime externo de voz e dos modelos pinados.

O Python do produto não instala nem importa `numpy`/`onnxruntime`/`sounddevice`/`supertonic` para
voz. O worker corre num runtime separado, preparado fora deste processo:

    <state_root>/.local/voice/runtime/Scripts/python.exe
    <state_root>/.local/voice/models/supertonic-3/

`state_root` vem de `Settings` (sandbox quando a ponte é sandbox). O override
`LUNMI_VOICE_HOME` existe apenas para o processo principal/backend confiável apontar ao runtime de
QA; o renderer nunca fornece caminhos e nada aqui aceita entrada do cliente.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

VOICE_HOME_ENV = "LUNMI_VOICE_HOME"
VOICE_DIRNAME = "voice"
RUNTIME_RELATIVE = ("runtime", "Scripts", "python.exe")
MODELS_RELATIVE = ("models", "supertonic-3")
WORKER_FILENAME = "worker_main.py"

REASON_RUNTIME_MISSING = "runtime_missing"
REASON_MODELS_MISSING = "models_missing"
REASON_WORKER_MISSING = "worker_missing"
REASON_OUTSIDE_HOME = "runtime_outside_home"


@dataclass(frozen=True)
class VoiceEnvironment:
    home: Path
    python: Path
    models: Path
    worker: Path
    reason: str = ""

    @property
    def available(self) -> bool:
        return not self.reason


def voice_home(state_root: str | Path) -> Path:
    """Raiz do runtime de voz: override absoluto confiável ou `<state_root>/.local/voice`."""
    override = os.environ.get(VOICE_HOME_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
    local = (Path(state_root) / ".local" / VOICE_DIRNAME).resolve()
    if (local.joinpath(*RUNTIME_RELATIVE).is_file() or (local / 'runtime/python.exe').is_file()):
        return local
    bundled = os.environ.get('LUNMI_BUNDLED_VOICE_HOME', '').strip()
    if bundled and Path(bundled).is_absolute() and (Path(bundled) / 'runtime/python.exe').is_file():
        return Path(bundled).resolve()
    return local


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def resolve_environment(
    state_root: str | Path,
    *,
    worker_path: str | Path | None = None,
    runtime_python: str | Path | None = None,
    models_dir: str | Path | None = None,
) -> VoiceEnvironment:
    """Resolve os caminhos e diz honestamente se estão utilizáveis. Nunca cria nem descarrega nada."""
    home = voice_home(state_root)
    legacy = home.joinpath(*RUNTIME_RELATIVE)
    python = Path(runtime_python).expanduser().resolve() if runtime_python is not None else (
        (home / 'runtime/python.exe').resolve() if (home / 'runtime/python.exe').is_file() else legacy.resolve())
    models = Path(models_dir).expanduser().resolve() if models_dir is not None else (
        home.joinpath(*MODELS_RELATIVE).resolve())
    worker = Path(worker_path).expanduser().resolve() if worker_path is not None else (
        Path(__file__).resolve().with_name(WORKER_FILENAME))

    reason = ""
    if not _within(python, home):
        reason = REASON_OUTSIDE_HOME
    elif not python.is_file():
        reason = REASON_RUNTIME_MISSING
    elif not models.is_dir():
        reason = REASON_MODELS_MISSING
    elif not worker.is_file():
        reason = REASON_WORKER_MISSING
    return VoiceEnvironment(home=home, python=python, models=models, worker=worker, reason=reason)

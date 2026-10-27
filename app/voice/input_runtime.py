"""Localização confiável do runtime externo de ditado (separado do TTS).

O Python do produto não importa `numpy`/`scipy`/`onnxruntime`/`sounddevice`/`ctranslate2`/
`faster_whisper` para ditado. O worker corre no runtime externo preparado fora deste processo:

    <voice_home>/input-runtime/Scripts/python.exe
    <voice_home>/models/whisper-large-v3-turbo/   (faster-whisper local pinado)
    <voice_home>/models/silero/silero_vad.onnx

`voice_home` é o mesmo do TTS (`LUNMI_VOICE_HOME` confiável ou `<state_root>/.local/voice`).
Nada aqui cria, descarrega ou repara artefactos; a ausência é reportada honestamente.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.voice.runtime import voice_home

INPUT_RUNTIME_RELATIVE = ("input-runtime", "Scripts", "python.exe")
WHISPER_MODEL_RELATIVE = ("models", "whisper-large-v3-turbo")
SILERO_MODEL_RELATIVE = ("models", "silero", "silero_vad.onnx")
WORKER_FILENAME = "input_worker.py"

# Ficheiros mínimos de um modelo CTranslate2/faster-whisper local. Não se tenta inferir
# formatos alternativos: melhor indisponível honesto do que carregar um modelo parcial.
REQUIRED_MODEL_FILES = ("model.bin", "config.json", "tokenizer.json")

REASON_RUNTIME_MISSING = "runtime_missing"
REASON_MODELS_MISSING = "models_missing"
REASON_VAD_MISSING = "vad_missing"
REASON_WORKER_MISSING = "worker_missing"
REASON_OUTSIDE_HOME = "runtime_outside_home"

MESSAGES = {
    REASON_RUNTIME_MISSING: "Entrada de voz indisponível: runtime de ditado não instalado.",
    REASON_MODELS_MISSING: "Entrada de voz indisponível: modelo de ditado ausente.",
    REASON_VAD_MISSING: "Entrada de voz indisponível: modelo de voz (VAD) ausente.",
    REASON_WORKER_MISSING: "Entrada de voz indisponível: worker de ditado ausente.",
    REASON_OUTSIDE_HOME: "Entrada de voz indisponível: runtime fora da pasta de voz.",
}


@dataclass(frozen=True)
class VoiceInputEnvironment:
    home: Path
    python: Path
    model: Path
    silero: Path
    worker: Path
    reason: str = ""

    @property
    def available(self) -> bool:
        return not self.reason

    @property
    def message(self) -> str:
        return "" if not self.reason else MESSAGES.get(self.reason, MESSAGES[REASON_RUNTIME_MISSING])


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def resolve_input_environment(
    state_root: str | Path,
    *,
    worker_path: str | Path | None = None,
    runtime_python: str | Path | None = None,
    models_dir: str | Path | None = None,
    silero_path: str | Path | None = None,
) -> VoiceInputEnvironment:
    """Resolve os caminhos e diz honestamente se os artefactos estão presentes.

    Nunca cria diretórios, nunca importa o runtime e nunca aceita caminhos do renderer.
    `available=True` significa apenas que os artefactos existem; a prova real é o arranque do
    worker (`ready` só depois de modelo/VAD carregados).
    """
    home = voice_home(state_root)
    python = Path(runtime_python).expanduser().resolve() if runtime_python is not None else (
        home.joinpath(*INPUT_RUNTIME_RELATIVE).resolve())
    model = Path(models_dir).expanduser().resolve() if models_dir is not None else (
        home.joinpath(*WHISPER_MODEL_RELATIVE).resolve())
    silero = Path(silero_path).expanduser().resolve() if silero_path is not None else (
        home.joinpath(*SILERO_MODEL_RELATIVE).resolve())
    worker = Path(worker_path).expanduser().resolve() if worker_path is not None else (
        Path(__file__).resolve().with_name(WORKER_FILENAME))

    reason = ""
    if not _within(python, home):
        reason = REASON_OUTSIDE_HOME
    elif not python.is_file():
        reason = REASON_RUNTIME_MISSING
    elif not model.is_dir() or any(
            not (model / name).is_file() for name in REQUIRED_MODEL_FILES):
        reason = REASON_MODELS_MISSING
    elif not silero.is_file():
        reason = REASON_VAD_MISSING
    elif not worker.is_file():
        reason = REASON_WORKER_MISSING
    return VoiceInputEnvironment(home=home, python=python, model=model, silero=silero,
                                 worker=worker, reason=reason)

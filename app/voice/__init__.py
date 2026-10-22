"""VOICE V1 — saída de voz opcional da Lunmi (sem captura de microfone).

Contrato público: `.agents/VOICE-V1-OUTPUT-CONTRACT.md`.
"""
from app.voice.eligibility import speakable_reply
from app.voice.filter import MAX_SPEECH_CHARS, filter_text, speakable_text
from app.voice.manager import (
    VoiceError,
    VoiceManager,
    VoicePrefsError,
)
from app.voice.prefs import VoicePrefs, load_prefs, prefs_path, save_prefs
from app.voice.runtime import VoiceEnvironment, resolve_environment, voice_home

__all__ = [
    "MAX_SPEECH_CHARS",
    "VoiceEnvironment",
    "VoiceError",
    "VoiceManager",
    "VoicePrefs",
    "VoicePrefsError",
    "filter_text",
    "load_prefs",
    "prefs_path",
    "resolve_environment",
    "save_prefs",
    "speakable_reply",
    "speakable_text",
    "voice_home",
]

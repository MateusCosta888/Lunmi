"""Filtro de fala conservador e limitação de tamanho (stdlib puro).

Defesa em profundidade, **não** um detector universal de segredos. A regra forte da V1 vive em
`app/voice/eligibility.py`: a automação só fala templates fixos do backend; respostas conversacionais
passam por este filtro antes de qualquer síntese.

Adaptado do laboratório V0 (`output/voice-v0/experimental/src/lunmi_voice_v0/speech_filter.py`) sem
importar nada de `output/` no produto.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_SPEECH_CHARS = 600

_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_CODE = re.compile(r"```|`[^`]+`")
_JSON = re.compile(r"^\s*[\[{]")
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s]+")
_UNC_PATH = re.compile(r"\\\\[^\s]+")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")
_SECRET = re.compile(
    r"\b(?:sk|ghp|gho|github_pat|xox[baprs]|AIza)[-_][A-Za-z0-9_\-]{8,}"
    r"|\beyJ[A-Za-z0-9_\-]{12,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
# Um bloco cercado por ``` é código: todo o conteúdo entre fences é descartado, não apenas a
# linha dos delimitadores. Sem fence de fecho, o resto do texto é tratado como código.
_FENCED_CODE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class SpeechFilterResult:
    text: str
    dropped: tuple[str, ...]

    @property
    def speakable(self) -> bool:
        return bool(self.text.strip())


def _reason(sentence: str) -> str:
    if _URL.search(sentence):
        return "url"
    if _EMAIL.search(sentence):
        return "email"
    if _CODE.search(sentence):
        return "code"
    if _JSON.match(sentence):
        return "json"
    if _WINDOWS_PATH.search(sentence) or _UNC_PATH.search(sentence):
        return "path"
    if _SECRET.search(sentence):
        return "secret"
    if _LONG_TOKEN.search(sentence):
        return "long_token"
    return ""


def filter_text(text: str) -> SpeechFilterResult:
    """Remove frases que não devem ser lidas em voz alta; nunca devolve diagnóstico interno."""
    if not isinstance(text, str) or not text.strip():
        return SpeechFilterResult("", ("empty",))
    without_fences = _FENCED_CODE.sub(" ", text)
    kept: list[str] = []
    dropped: list[str] = []
    if without_fences != text:
        dropped.append("code_block")
    for sentence in _SENTENCE_END.split(without_fences.strip()) or ():
        sentence = sentence.strip()
        if not sentence:
            continue
        reason = _reason(sentence)
        if reason:
            dropped.append(reason)
        else:
            kept.append(sentence)
    return SpeechFilterResult(" ".join(kept), tuple(dropped))


def bound_text(text: str, max_chars: int = MAX_SPEECH_CHARS) -> str:
    """Corta em fronteira de frase quando possível, nunca a meio de uma palavra."""
    if not isinstance(text, str):
        return ""
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_chars:
        return cleaned
    window = cleaned[:max_chars]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if cut >= max_chars // 3:
        return window[:cut + 1].strip()
    space = window.rfind(" ")
    return (window[:space] if space > 0 else window).strip()


def speakable_text(text: str, *, max_chars: int = MAX_SPEECH_CHARS) -> str:
    """Texto filtrado e limitado, pronto para uma única utterance; `""` quando não há nada."""
    bounded = bound_text(text, max_chars)
    if not bounded:
        return ""
    return bound_text(filter_text(bounded).text, max_chars)


def is_speakable(text: str) -> bool:
    return bool(speakable_text(text))

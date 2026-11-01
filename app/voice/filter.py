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


# --- forma falada (pt-BR) ------------------------------------------------------------------
# O Supertonic só tem "pt": o sotaque segue o texto, e a grafia/vocabulário de Portugal puxava a
# pronúncia para lá (pedido do utilizador, 01/11: só sotaque do Brasil). Só o texto falado muda.
_PT_BR_WORDS = {
    "ficheiro": "arquivo", "ficheiros": "arquivos", "ecrã": "tela", "ecrãs": "telas",
    "utilizador": "usuário", "utilizadores": "usuários", "utilizadora": "usuária",
    "telemóvel": "celular", "telemóveis": "celulares", "definições": "configurações",
    "partilhar": "compartilhar", "partilha": "compartilha", "partilhado": "compartilhado",
    "registo": "registro", "registos": "registros", "contacto": "contato", "contactos": "contatos",
    "facto": "fato", "factos": "fatos", "actual": "atual", "actualmente": "atualmente",
    "óptimo": "ótimo", "óptima": "ótima", "directo": "direto", "directa": "direta",
    "projecto": "projeto", "projectos": "projetos", "objecto": "objeto", "objectos": "objetos",
    "equipa": "equipe", "equipas": "equipes", "rato": "mouse", "guardar": "salvar",
    "guardado": "salvo", "guardada": "salva", "guardei": "salvei", "guarda": "salva",
    "transferências": "downloads", "connosco": "conosco", "contigo": "com você",
    "tu": "você", "teu": "seu", "tua": "sua", "teus": "seus", "tuas": "suas",
    "queres": "quer", "podes": "pode", "tens": "tem", "sabes": "sabe", "fazes": "faz",
    "precisas": "precisa", "estás": "está", "és": "é", "vais": "vai", "consegues": "consegue",
    "quiseres": "quiser", "puderes": "puder", "preferires": "preferir", "preferes": "prefere",
    "estiveres": "estiver", "fizeres": "fizer", "achas": "acha", "gostas": "gosta", "vês": "vê",
    "deves": "deve", "usas": "usa", "costumas": "costuma", "escreves": "escreve", "abres": "abre",
    "pensas": "pensa", "lembras": "lembra", "importas": "importa", "desejas": "deseja",
    "diz-me": "me diga", "dá-me": "me dá", "mostra-me": "me mostra", "avisa-me": "me avisa",
    "pequeno-almoço": "café da manhã", "autocarro": "ônibus", "comboio": "trem",
}
# Masculine pt-PT nouns whose Brazilian word is feminine take their article along.
_FEMININE = {"ambiente de trabalho": "área de trabalho", "ecrã": "tela"}
_ARTICLES = {"o": "a", "no": "na", "do": "da", "ao": "à", "um": "uma", "este": "esta", "esse": "essa",
             "neste": "nesta", "nesse": "nessa", "deste": "desta", "desse": "dessa", "pelo": "pela"}
_PT_BR_PHRASES = {f"{article} {noun}": f"{_ARTICLES[article]} {word}"
                  for noun, word in _FEMININE.items() for article in _ARTICLES}
_PT_BR_PHRASES.update(_FEMININE)
_PT_BR_PHRASES["casa de banho"] = "banheiro"
_PT_BR_PHRASES["já agora"] = "aliás"
_PT_BR_WORD = re.compile(r"(?<![\w-])(" + "|".join(sorted(map(re.escape, _PT_BR_WORDS), key=len,
                                                             reverse=True)) + r")(?![\w-])", re.IGNORECASE)
_PT_BR_PHRASE = re.compile(r"(?<!\w)(" + "|".join(sorted(map(re.escape, _PT_BR_PHRASES), key=len,
                                                          reverse=True)) + r")(?!\w)", re.IGNORECASE)
# "estou a escrever" -> "estou escrevendo" (o gerúndio é a forma do Brasil).
_PROGRESSIVE = re.compile(r"\b(est(?:ou|á|ás|amos|ão|ava|avam|ive|eja)|fic(?:o|a|ou|am)|continu(?:o|a|am)"
                          r"|and(?:o|a|am))\s+a\s+(\w+)\b", re.IGNORECASE)
_GERUND = {"ar": "ando", "er": "endo", "ir": "indo"}
# Short verbs spelled out: a bare "-ar" rule would turn "estar a par" into "estar pando".
_SHORT_GERUND = {"ver": "vendo", "ler": "lendo", "dar": "dando", "ter": "tendo", "ser": "sendo",
                 "rir": "rindo", "pôr": "pondo", "vir": "vindo", "ir": "indo"}


def _gerund(match: re.Match[str]) -> str:
    verb = match.group(2)
    folded = verb.casefold()
    if folded in _SHORT_GERUND:
        return f"{match.group(1)} {_SHORT_GERUND[folded]}"
    if len(folded) >= 4 and folded[-2:] in _GERUND:
        return f"{match.group(1)} {verb[:-2]}{_GERUND[folded[-2:]]}"
    return match.group(0)
# pt-PT "é/ó" antes de m/n + vogal é "ê/ô" no Brasil (económico, fenómeno, ténis, António).
_OPEN_VOWEL = re.compile(r"([éó])(?=[mn][aeiouáéíóú])")
_SYMBOLS = ((re.compile(r"(?<=\d)\s*[×x*]\s*(?=\d)"), " vezes "), (re.compile(r"(?<=\d)\s*÷\s*(?=\d)"), " dividido por "),
            (re.compile(r"\s=\s|(?<=\d)=(?=\d)"), " igual a "), (re.compile(r"(?<=\d)\s*\+\s*(?=\d)"), " mais "),
            (re.compile(r"(?<=\d)\s*%"), " por cento"), (re.compile(r"(?<=\d)\s*°C"), " graus"),
            (re.compile(r"\s*(?:→|->)\s*"), ", "))
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_MD_MARK = re.compile(r"(\*\*|__|~~|(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w))")
_MD_LINE = re.compile(r"^\s*(?:#{1,6}\s+|>\s*|[-*+•]\s+|\d{1,2}[.)]\s+)")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def _keep_case(original: str, replacement: str) -> str:
    return replacement[:1].upper() + replacement[1:] if original[:1].isupper() else replacement


def spoken_form(text: str) -> str:
    """Markdown em frases, símbolos por extenso e português do Brasil, só para a voz."""
    if not isinstance(text, str):
        return ""
    lines = []
    for line in text.splitlines():
        line = _MD_LINE.sub("", _MD_LINK.sub(r"\1", line)).strip()
        line = _EMOJI.sub("", _MD_MARK.sub("", line)).strip()
        if line:
            # A list item or heading is its own sentence: a pause, not a run-on (measured 01/11).
            lines.append(line if line[-1] in ".!?:;…" or "```" in line else line + ".")
    spoken = " ".join(lines) if len(lines) > 1 else (lines[0] if lines else "")
    for pattern, words in _SYMBOLS:
        spoken = pattern.sub(words, spoken)
    spoken = _PT_BR_PHRASE.sub(lambda m: _keep_case(m.group(0), _PT_BR_PHRASES[m.group(0).casefold()]), spoken)
    spoken = _PT_BR_WORD.sub(lambda m: _keep_case(m.group(0), _PT_BR_WORDS[m.group(0).casefold()]), spoken)
    spoken = _PROGRESSIVE.sub(_gerund, spoken)
    spoken = _OPEN_VOWEL.sub(lambda m: {"é": "ê", "ó": "ô"}[m.group(1)], spoken)
    return " ".join(spoken.split())


def speakable_text(text: str, *, max_chars: int = MAX_SPEECH_CHARS) -> str:
    """Texto filtrado e limitado, pronto para uma única utterance; `""` quando não há nada."""
    bounded = bound_text(spoken_form(text), max_chars)
    if not bounded:
        return ""
    return bound_text(filter_text(bounded).text, max_chars)


def is_speakable(text: str) -> bool:
    return bool(speakable_text(text))

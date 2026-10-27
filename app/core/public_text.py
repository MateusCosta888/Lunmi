"""Extract only the public top-level answer field from a bounded JSON prefix."""
from __future__ import annotations

import json
import re
import unicodedata

_PT_CLAIM = (r'(?:pronto[,!]?\s*)?(?:já\s+)?'
             r'(?:abri|apaguei|eliminei|criei|guardei|salvei|enviei|fechei|instalei|movi|copiei|renomeei)\b')
_EN_CLAIM = (r'(?:(?:ok|okay|sure|done)[,!]?\s*)?'
             r'(?:I(?:\s+have|\x27ve)?\s+(?:already\s+)?(?:opened|deleted|created|saved|sent|closed|installed|moved|copied|renamed))\b')
# Two compiled branches let the engine reject a non-claim at the first character and let
# search start from the literal sentence delimiter. Semantics match the old single regex.
_CLAIM_AT_START = re.compile(f'(?:{_PT_CLAIM}|{_EN_CLAIM})', re.IGNORECASE)
_CLAIM_AFTER_BOUNDARY = re.compile(f'[.!?\n]\s*(?:{_PT_CLAIM}|{_EN_CLAIM})', re.IGNORECASE)


def unverified_action_claim(text: str) -> bool:
    """A conversational answer has no authority to announce a desktop mutation."""
    return bool(_CLAIM_AT_START.match(text) or _CLAIM_AFTER_BOUNDARY.search(text))


# IDs opacos de fonte são metadados do ledger, nunca apresentação pública. A validação
# continua a correr sobre o texto cru no controller; esta projeção só remove o token.
_WEB_SOURCE_TOKEN = re.compile(r'\[[ \t]*src-[^\[\]\n]{1,80}?[ \t]*\]', re.IGNORECASE)
_WEB_SOURCE_START = re.compile(r'\[[ \t]*src-', re.IGNORECASE)
_WEB_SOURCE_ANY = re.compile(r'\[[ \t]*src-[^\]\n]{0,80}\]?', re.IGNORECASE)


def web_public_text(text: str) -> str:
    """Remove IDs internos `[src-*]` da apresentação web sem tocar no ledger.

    Recebe texto já validado (IDs desconhecidos continuam a produzir `NOT_VERIFIED`
    antes desta projeção). Texto normal, código não-web e URLs não são alterados; um
    token incompleto no fim do texto também é removido para não expor metadados.
    """
    return _WEB_SOURCE_ANY.sub('', str(text or ''))


class WebCitationStream:
    """Projeção incremental de `[src-*]` para streaming, estável por prefixo.

    A entrada é o texto público acumulado (não o delta). Um `[` que ainda pode vir a
    ser `[src-…]` fica retido, por isso nenhum fragmento do ID chega ao renderer. Se o
    token fechar, é removido; se for texto normal (ex.: `arr[0]`), é libertado.
    """

    def __init__(self) -> None:
        self._seen = ''
        self._out = ''
        self._pos = 0

    def feed(self, text: str) -> str:
        text = str(text or '')
        if not text.startswith(self._seen):
            self._seen, self._out, self._pos = '', '', 0
        self._seen = text
        while self._pos < len(text):
            start = text.find('[', self._pos)
            if start < 0:
                self._out += text[self._pos:]
                self._pos = len(text)
                break
            self._out += text[self._pos:start]
            token = _WEB_SOURCE_TOKEN.match(text, start)
            if token is not None:
                self._pos = token.end()
                continue
            if _WEB_SOURCE_START.match(text, start):
                close = text.find(']', start)
                newline = text.find('\n', start)
                if close != -1 and (newline == -1 or close < newline) and close - start <= 90:
                    # Token malformado/curto: esconde-o inteiro em vez de expor parte.
                    self._pos = close + 1
                    continue
                if newline != -1:
                    # Token partido por fim de linha: esconde a linha, mantém o \n.
                    self._pos = newline
                    continue
                self._pos = start
                break
            if _could_start_citation(text, start):
                self._pos = start
                break
            self._out += '['
            self._pos = start + 1
        return self._out

    def finish(self) -> str:
        tail = self._seen[self._pos:]
        if not _WEB_SOURCE_START.match(tail):
            self._out += tail
        self._pos = len(self._seen)
        return self._out


def _could_start_citation(text: str, start: int) -> bool:
    """True when a trailing `[` can still become `[src-…]` in a later fragment."""
    rest = text[start + 1:].lstrip(' \t')
    if not rest:
        return True
    return rest.casefold() in {'s', 'sr', 'src', 'src-'}


# Answer-level admissions: the public answer says the evidence cannot answer, summarise
# or confirm. The guard downgrades status; it never retries or re-asks the model.
_INSUFFICIENCY_PATTERNS = (
    r'\b(?:excertos?|trechos?|snippets?|fontes?|resultados?)\b[^.]{0,120}?'
    r'\bnao (?:contem|tem|trazem|incluem|fornecem|apresentam)\b[^.]{0,80}?'
    r'\b(?:detalhes?|informac\w*|dados|elementos|conteudo)\b[^.]{0,40}?\bsuficientes?\b',
    r'\bnao (?:e|foi) possivel (?:resumir|responder|confirmar|concluir|determinar)\b',
    r'\bnao (?:consigo|consegui|conseguimos|posso|podemos) '
    r'(?:resumir|responder|confirmar|concluir|determinar)\b',
    r'\b(?:informacao|informacoes|dados|detalhes|evidencia|fontes)\b[^.]{0,60}?'
    r'\b(?:insuficiente|insuficientes|limitad[ao]s?|nao (?:e|sao) suficientes?)\b',
    r'\bnao (?:ha|existe|existem|tenho|temos) '
    r'(?:informacao|informacoes|dados|detalhes|evidencia)\b[^.]{0,40}?'
    r'\b(?:suficiente|suficientes|para)\b',
    r'\b(?:informacao|evidencia|dados|detalhes)\b[^.]{0,30}?'
    r'\bnao (?:permitem|permite) (?:resumir|responder|confirmar|concluir|determinar)\b',
    # A fonte existe (e pode ter sido lida), mas a própria resposta diz que ela não
    # responde ao atributo pedido: contagem, valor atual, nome ou data. É uma admissão
    # observável, não uma resposta verificada; nenhuma segunda chamada é feita.
    r'\b(?:excertos?|trechos?|snippets?|fontes?|resultados?|paginas?|textos?)\b'
    r'[^.]{0,140}?\b(?:nao|nunca)\s+'
    r'(?:indicam|informam|mostram|revelam|dizem|especificam|confirmam|comprovam|'
    r'permitem|possibilitam|bastam|sao suficientes|trazem)\b',
    r'\b(?:excerpts?|snippets?|sources?|results?|pages?|texts?)\b'
    r'[^.]{0,140}?\b(?:do not|don t|cannot|can t|never)\s+'
    r'(?:indicate|inform|show|reveal|say|specify|confirm|prove|allow|state|contain|suffice)\b',
    r'\b(?:discrepancia|divergencia|contradicao|conflito)\b[^.]{0,60}?'
    r'\b(?:entre|nas|das)\s+(?:as\s+)?fontes\b',
    r'\b(?:fontes|excertos|resultados)\b[^.]{0,80}?'
    r'\b(?:divergem|discordam|contradizem|contraditorias?|conflitantes)\b',
    r'\bnot enough (?:information|details|evidence|data)\b',
    r'\b(?:insufficient|incomplete) (?:information|details|evidence|data)\b',
    r"\b(?:cannot|can't|can not|could not|couldn't|unable to) "
    r'(?:confirm|summari[sz]e|answer|determine)\b',
)
_INSUFFICIENCY = tuple(re.compile(pattern, re.IGNORECASE)
                       for pattern in _INSUFFICIENCY_PATTERNS)


def _without_accents(text: str) -> str:
    return ''.join(char for char in unicodedata.normalize('NFKD', text.casefold())
                   if not unicodedata.combining(char))


def web_answer_admits_insufficiency(text: str) -> bool:
    """True when the public answer itself says the evidence cannot answer.

    Pure and offline: the caller changes status to `NOT_VERIFIED` (partial evidence),
    never calls another model and never retries. An answer with an unrelated caveat is
    not downgraded by this alone.
    """
    normalized = _without_accents(str(text or ''))
    return any(pattern.search(normalized) for pattern in _INSUFFICIENCY)


def web_synthesis_text(raw: str) -> str | None:
    """Texto público de uma síntese web read-only; nunca aceita uma decisão de tool.

    O caminho web é answer-only e não executa nada: por isso uma resposta em texto
    simples é segura quando o JSON do modelo fica inválido (aspas, Unicode, blocos de
    código). Um objeto `{` só é aceite na forma FECHADA `kind="answer"`, `action=null`,
    `status="FINISH"`; `kind=tool`, `operation` ou `action` preenchida são rejeitados, tal
    como JSON inválido. O texto devolvido é integral — nenhum limite oculto o trunca; a
    validação de alegação de ação fica com o chamador.
    """
    text = str(raw or '').strip()
    if not text:
        return None
    if text.startswith('```'):
        text = text.split('\n', 1)[1] if '\n' in text else text[3:]
        if text.rstrip().endswith('```'):
            text = text.rstrip()[:-3]
        text = text.strip()
        if not text:
            return None
    if text.startswith('{'):
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        if (str(data.get('kind') or '').strip() != 'answer'
                or data.get('action') is not None
                or data.get('operation') is not None
                or str(data.get('status') or '').strip().upper() != 'FINISH'):
            return None
        message = data.get('message')
        if isinstance(message, str):
            text = message.strip()
        elif isinstance(message, list) and all(isinstance(item, str) for item in message):
            text = '\n'.join(message).strip()
        else:
            return None
    if not text:
        return None
    return text


def _string_prefix(raw: str, start: int) -> tuple[str, int, bool]:
    # A quote closes the string only after an even number of backslashes. str.find scans
    # in C, so a one-character fragment stream does not rescan Python-side per fragment.
    end = raw.find('"', start + 1)
    while end != -1:
        backslashes, index = 0, end - 1
        while index > start and raw[index] == '\\':
            backslashes += 1
            index -= 1
        if backslashes % 2 == 0:
            return json.loads(raw[start:end + 1]), end + 1, True
        end = raw.find('"', end + 1)
    # A provider can split immediately after a backslash or inside a Unicode escape.
    for trim in range(min(7, len(raw) - start)):
        part = raw[start:len(raw) - trim] if trim else raw[start:]
        try:
            text = json.loads(part + '"')
            if text and 0xD800 <= ord(text[-1]) <= 0xDBFF:
                text = text[:-1]
            return text, len(raw), False
        except (ValueError, UnicodeError):
            continue
    return '', len(raw), False


def public_prefix(raw: str, *, automatic: bool = False) -> str:
    """Never scan nested thought/argument strings for a public-looking key."""
    if len(raw) > 16000:
        raise ValueError('Resposta excedeu o limite.')
    value = raw.lstrip()
    if value.startswith('```json\n'):
        value = value[8:]
    elif value.startswith('```\n'):
        value = value[4:]
    if not value.startswith('{'):
        return ''  # Plain text is accepted only by the final conversation validator.
    decoder = json.JSONDecoder()
    pos, kind, status, action_seen = 1, None, None, False
    try:
        while pos < len(value):
            while pos < len(value) and value[pos] in ' \r\n\t,':
                pos += 1
            key, pos = decoder.raw_decode(value, pos)
            if not isinstance(key, str):
                return ''
            while pos < len(value) and value[pos].isspace():
                pos += 1
            if pos >= len(value) or value[pos] != ':':
                return ''
            pos += 1
            while pos < len(value) and value[pos].isspace():
                pos += 1
            if pos == len(value):
                return ''
            if key in {'message', 'text', 'answer'}:
                if not (status in {'FINISH', 'DONE'} and action_seen and (not automatic or kind == 'answer')):
                    return ''
                if value[pos] == '"':
                    text, _, _ = _string_prefix(value, pos)
                    return text.strip()
                if value[pos] == '[':
                    texts: list[str] = []
                    pos += 1
                    while pos < len(value):
                        while pos < len(value) and value[pos] in ' \n\r\t,':
                            pos += 1
                        if pos >= len(value) or value[pos] != '"':
                            break
                        text, pos, done = _string_prefix(value, pos)
                        texts.append(text)
                        if not done:
                            break
                    return '\n'.join(texts).strip()
                return ''
            field, pos = decoder.raw_decode(value, pos)
            if key == 'kind':
                kind = field
            elif key == 'status':
                status = field
            elif key == 'action':
                action_seen = field is None
    except (ValueError, TypeError, RecursionError):
        return ''
    return ''

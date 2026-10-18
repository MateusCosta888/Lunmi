"""Extract only the public top-level answer field from a bounded JSON prefix."""
from __future__ import annotations

import json
import re

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

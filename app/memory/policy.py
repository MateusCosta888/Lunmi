"""Fail closed on identifiable secrets/private payloads; never echo rejected content.

This is conservative data minimization, not a promise to detect every possible secret.
All persistent routes (including imports/edits/episodes/history) use this policy.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from app.memory.contracts import KINDS, SOURCES, STATUSES, MemoryItem

MAX_TEXT = 800
MAX_IMPORT_ITEMS = 1000
_METADATA_KEYS = {"expires_at", "conflict_key", "session_id", "task_id", "instruction", "outcome",
                  "apps", "plan_source", "llm_calls", "paths_are_hypotheses", "failure_kind"}
_SECRET = re.compile(
    r"\b(senha|password|passwd|passphrase|segredo|secret|api[ _-]?key|chave[ _-]?(api|privada)|"
    r"access[ _-]?token|refresh[ _-]?token|token de (?:acesso|atualiza[cç][aã]o)|authorization|bearer|cookie|credencial|credentials?)\b|"
    r"\btoken\s*[:=]|\b(sk|ghp|gho|github_pat|xox[baprs])[-_][a-z0-9_-]{8,}|"
    r"-----BEGIN .*PRIVATE KEY|\beyJ[a-z0-9_-]{12,}\.[a-z0-9_-]+\.[a-z0-9_-]+|"
    r"[a-z][a-z0-9+.-]*://[^\s/]+:[^\s/]+@", re.I)
_PRIVATE = re.compile(
    r"\b(mensagem|mensagens|email|e-mail|conversa|transcri[cç][aã]o|conte[uú]do|resposta)\b"
    r".{0,45}\b(discord|terceir[oa]s?|recebid[oa]|enviad[oa] por|de outra pessoa)\b|"
    r"\b(discord|e-mail|email)\s*[:\n]|\b(?:disse|escreveu|respondeu|relatou)\s*[:\"“]|"
    r"\b(?:cita[cç][aã]o|transcri[cç][aã]o|conte[uú]do de terceiros|mensagem de outra pessoa)\b|"
    r"\b(tool_result|tool_output|uia_dump|control_text|file_content|clipboard)\b", re.I | re.S)
_SENSITIVE = re.compile(r"\b(cpf|rg|cart[aã]o de cr[eé]dito|diagn[oó]stico|sa[uú]de|relig(?:iao|ião)|"
                        r"orienta[cç][aã]o sexual|endere[cç]o residencial)\b", re.I)


class MemoryPolicyError(ValueError):
    def __init__(self) -> None:
        super().__init__("Memória recusada: conteúdo privado, sensível ou formato inválido.")


def normalized(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(c))


def safe_text(text: Any, *, explicit: bool = False, maximum: int = MAX_TEXT) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > maximum:
        raise MemoryPolicyError()
    if any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise MemoryPolicyError()
    value = unicodedata.normalize("NFKC", text)
    # Use normalized and original forms to prevent accent/compatibility evasions.
    for candidate in (value, normalized(value)):
        if (_SECRET.search(candidate) or _PRIVATE.search(candidate) or
                re.search(r'\b(?:senha|password|token|api[_ -]?key|secret)[a-z0-9_-]*\s*[:=]', candidate, re.I)):
            raise MemoryPolicyError()
    if text.count("\n") > 8 or "```" in text:
        raise MemoryPolicyError()
    if (_SENSITIVE.search(value) or _SENSITIVE.search(normalized(value))) and not explicit:
        raise MemoryPolicyError()
    return value.strip()


def safe_document(text: Any, *, maximum: int = 200_000) -> str:
    """Valida o documento de perfil livre sem o impor limites de item.

    O texto é devolvido **exatamente** como veio (sem strip nem normalização):
    parágrafos, listas e espaços fazem parte do que o utilizador escreveu.
    Só segredos/conteúdo privado e bytes de controlo são recusados.
    """
    if not isinstance(text, str):
        raise MemoryPolicyError()
    if len(text) > maximum:
        raise MemoryPolicyError()
    if any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise MemoryPolicyError()
    for candidate in (text, normalized(text)):
        if _SECRET.search(candidate) or _PRIVATE.search(candidate):
            raise MemoryPolicyError()
    return text


def timestamp(value: Any) -> str:
    try:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except (ValueError, OverflowError):
        raise MemoryPolicyError() from None


def identifier(value: Any, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty) or len(value) > 100:
        raise MemoryPolicyError()
    if value and not re.fullmatch(r"[a-zA-Z0-9_.:-]+", value):
        raise MemoryPolicyError()
    safe_text(value or "empty")
    return value


def metadata(values: Any, *, explicit: bool) -> dict[str, Any]:
    if not isinstance(values, dict) or set(values) - _METADATA_KEYS:
        raise MemoryPolicyError()
    try:
        if len(json.dumps(values, allow_nan=False)) > 2000:
            raise MemoryPolicyError()
    except (ValueError, TypeError, RecursionError):
        raise MemoryPolicyError() from None
    result: dict[str, Any] = {}
    for key, value in values.items():
        if key == "expires_at":
            result[key] = timestamp(value)
        elif key in {"session_id", "task_id", "conflict_key"}:
            result[key] = identifier(value)
        elif key == "llm_calls":
            if type(value) is not int or not 0 <= value <= 10000:
                raise MemoryPolicyError()
            result[key] = value
        elif key == "paths_are_hypotheses":
            if type(value) is not bool:
                raise MemoryPolicyError()
            result[key] = value
        elif key == "apps":
            if not isinstance(value, list) or len(value) > 5:
                raise MemoryPolicyError()
            result[key] = [safe_text(v, explicit=explicit, maximum=80) for v in value]
        else:
            result[key] = safe_text(value, explicit=explicit, maximum=400)
    return result


def validate(item: MemoryItem) -> MemoryItem:
    if not isinstance(item, MemoryItem):
        raise MemoryPolicyError()
    if item.kind not in KINDS or item.status not in STATUSES or item.source not in SOURCES:
        raise MemoryPolicyError()
    if type(item.confidence) not in {int, float} or not math.isfinite(item.confidence) or not 0 <= item.confidence <= 1:
        raise MemoryPolicyError()
    explicit = item.source == "user_explicit"
    return replace(item, id=identifier(item.id, empty=True), text=safe_text(item.text, explicit=explicit),
                   created_at=timestamp(item.created_at) if item.created_at else "",
                   updated_at=timestamp(item.updated_at) if item.updated_at else "",
                   supersedes_id=identifier(item.supersedes_id) if item.supersedes_id is not None else None,
                   metadata=metadata(item.metadata, explicit=explicit))

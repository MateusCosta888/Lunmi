"""A única decisão backend de texto falável (auto-fala e manual).

`response.text.stable` **não** é permissão para falar: a fala acontece depois da resposta final.
Tudo o que não é provadamente elegível falha fechado (devolve `""`), e a mesma função serve a
auto-fala e `voice.speak_last` para não existirem duas políticas.

Regras (contrato `.agents/VOICE-V1-OUTPUT-CONTRACT.md` §6):
- só `SUCCESS`;
- operação/consulta verificada: apenas o template fixo do backend (`verified_prefix`);
- conversa sem evidência, sem anexos: texto público, depois do filtro de fala;
- memória, motor, anexos, evidência sem template, falha/incerto/parcial: nada.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from app.voice.filter import MAX_SPEECH_CHARS, speakable_text

CONVERSATIONAL_SCOPES = frozenset({"conversation"})
VERIFIED_OPERATION_STATE = "completed_verified"
# Citações de fonte são metadados, não fala: nunca se lê `[src-…]` em voz alta.
_WEB_CITATION = re.compile(r"\[src-[0-9a-f]{16}\]", re.IGNORECASE)


def operations_block_speech(operations: Iterable[Mapping[str, Any]] | None) -> bool:
    """Verdadeiro quando alguma operação não está provada como concluída e verificada.

    Qualquer `started`, `not_started`, `completed_unverified`, `failed_no_effect` ou `uncertain`
    bloqueia a fala mesmo que outra operação irmã tenha prova: dizer "operação verificada" ao
    lado de uma mutação incerta seria anunciar sucesso sem prova.
    """
    for operation in operations or ():
        if not isinstance(operation, Mapping):
            return True
        if operation.get("state") != VERIFIED_OPERATION_STATE:
            return True
        if operation.get("verified") is False:
            return True
    return False


def speakable_reply(
    *,
    message: str,
    status: str,
    scope: str,
    verified_prefix: str = "",
    attachments: bool = False,
    evidence_count: int = 0,
    operations: Iterable[Mapping[str, Any]] | None = None,
    web_outcome: str | None = None,
    web_sources: int = 0,
) -> str:
    """Texto pronto a sintetizar, ou `""` quando nada está provado como falável."""
    if status != "SUCCESS":
        return ""
    operation_list = list(operations or ())
    if operation_list and operations_block_speech(operation_list):
        # Uma operação por provar (incerta/parcial/falhada) invalida a fala do turno inteiro.
        return ""
    prefix = speakable_text(verified_prefix or "", max_chars=MAX_SPEECH_CHARS)
    if prefix:
        # Operação/consulta verificada: o template é a única coisa com prova; resumo/corpo nunca.
        return prefix
    if web_outcome is not None:
        # WEB-SEARCH-V1 §6: só um resumo web SUCCESS com pelo menos uma fonte válida pode ser
        # falado; não é template verificado (`verified_result` continua falso).
        web_only = all(op.get('category') == 'read_only' and op.get('tool') in {
            'web.search', 'web.open', 'web.research',
        } for op in operation_list)
        if (web_outcome == "SUCCESS" and web_sources > 0 and scope in CONVERSATIONAL_SCOPES
                and not attachments and web_only):
            return speakable_text(_WEB_CITATION.sub("", message or ""), max_chars=MAX_SPEECH_CHARS)
        return ""
    if evidence_count:
        # Evidência sem template fixo não autoriza fala (leitura de dados, resumo do agente).
        return ""
    if attachments:
        # Conteúdo externo/anexos sem elegibilidade provada: falha fechado.
        return ""
    if operation_list:
        # Conversa com operações declaradas: `scope` sozinho nunca autoriza fala.
        return ""
    if scope not in CONVERSATIONAL_SCOPES:
        return ""
    return speakable_text(message or "", max_chars=MAX_SPEECH_CHARS)

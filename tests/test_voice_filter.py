"""VOICE V1 — filtro de fala, limite de tamanho e a decisão única de elegibilidade."""
from __future__ import annotations

import pytest

from app.voice.eligibility import speakable_reply
from app.voice.filter import MAX_SPEECH_CHARS, bound_text, filter_text, is_speakable, speakable_text


def test_fenced_code_block_content_never_survives_even_without_backticks_per_line() -> None:
    text = (
        "A resposta é simples.\n"
        "```python\n"
        "def roubo():\n"
        "    return 'token secreto do cliente'\n"
        "```\n"
        "Fim da explicação."
    )
    result = filter_text(text)
    assert result.text == "A resposta é simples. Fim da explicação."
    assert "code_block" in result.dropped
    assert "roubo" not in result.text
    assert "token secreto" not in result.text


def test_unclosed_fence_drops_everything_after_it() -> None:
    result = filter_text("Antes. ```python\nprint(1)\nprint(2)")
    assert result.text == "Antes."
    assert "print" not in result.text


@pytest.mark.parametrize(
    "sentence, reason",
    [
        ("Veja https://exemplo.pt/segredo agora.", "url"),
        ("Envia para pessoa@exemplo.pt.", "email"),
        ("O ficheiro C:\\Users\\dono\\nota.txt foi lido.", "path"),
        ("Use `pip install supertonic`.", "code"),
        ('{"chave": "valor"}', "json"),
        ("Chave ghp_abcdefghijklmnop.", "secret"),
        ("Identificador aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.", "long_token"),
    ],
)
def test_sensitive_sentences_are_dropped(sentence: str, reason: str) -> None:
    result = filter_text(f"Frase segura. {sentence}")
    assert sentence not in result.text
    assert reason in result.dropped


def test_empty_and_code_only_text_is_not_speakable() -> None:
    assert not is_speakable("")
    assert not is_speakable("   ")
    assert not is_speakable("```\nsó código\n```")
    assert not filter_text("```\nsó código\n```").speakable


def test_bound_text_cuts_on_a_sentence_and_never_mid_word() -> None:
    sentence = "Esta é uma frase curta. "
    long_text = sentence * 100
    bounded = bound_text(long_text, max_chars=MAX_SPEECH_CHARS)
    assert 0 < len(bounded) <= MAX_SPEECH_CHARS
    assert bounded.endswith(".")
    # Não começa a meio de uma palavra depois do corte.
    rest = long_text[len(bounded):].lstrip()
    assert rest.startswith("Esta") or not rest

    no_sentence = "palavra " * 200
    bounded2 = bound_text(no_sentence, max_chars=MAX_SPEECH_CHARS)
    assert len(bounded2) <= MAX_SPEECH_CHARS
    assert not bounded2.endswith("pala")


def test_speakable_text_combines_filter_and_bound() -> None:
    text = "Olá. Veja https://x.pt. " + ("Frase normal. " * 100)
    out = speakable_text(text)
    assert "https" not in out
    assert len(out) <= MAX_SPEECH_CHARS
    assert out.startswith("Olá.")


def test_eligibility_speaks_a_successful_conversation_without_evidence() -> None:
    assert speakable_reply(message="Tudo bem por aqui.", status="SUCCESS",
                           scope="conversation") == "Tudo bem por aqui."


def test_eligibility_speaks_only_the_verified_backend_template() -> None:
    prefix = "Consulta concluída e verificada. Conteúdo consultado (dados):"
    spoken = speakable_reply(
        message="O ficheiro diz: chave=abc123.",
        status="SUCCESS",
        scope="automation",
        verified_prefix=prefix,
        evidence_count=1,
    )
    assert spoken == prefix


def test_eligibility_fails_closed_on_every_unproven_case() -> None:
    prefix = "Operação verificada. Resumo do agente (não é evidência):"
    # Falha, cancelamento, incerteza/timeout/parcial: nada.
    for status in ("FAILED", "CANCELLED", "PARTIAL"):
        assert speakable_reply(message="ok", status=status, scope="conversation") == ""
    # Evidência sem template: nunca o resumo do agente.
    assert speakable_reply(message="Resposta", status="SUCCESS", scope="automation",
                           evidence_count=2) == ""
    # Anexos e conteúdo externo: falha fechado.
    assert speakable_reply(message="Resposta", status="SUCCESS", scope="conversation",
                           attachments=True) == ""
    # Memória/outros escopos: falha fechado.
    assert speakable_reply(message="Resposta", status="SUCCESS", scope="memory_command") == ""
    assert speakable_reply(message="Resposta", status="SUCCESS", scope="engine_only") == ""
    # Template vazio não vira fala.
    assert speakable_reply(message="", status="SUCCESS", scope="automation",
                           verified_prefix=prefix, evidence_count=1) == prefix
    assert speakable_reply(message="", status="SUCCESS", scope="automation",
                           verified_prefix="", evidence_count=1) == ""


def test_scope_alone_never_authorizes_a_conversation_with_operations() -> None:
    assert speakable_reply(
        message="Já fiz a alteração.",
        status="SUCCESS",
        scope="conversation",
        operations=[{"state": "completed_unverified", "category": "mutation"}],
    ) == ""
    # Mesmo uma operação "verificada" declarada em conversa não transforma `scope` em permissão.
    assert speakable_reply(
        message="Já fiz a alteração.",
        status="SUCCESS",
        scope="conversation",
        operations=[{"state": "completed_verified", "category": "mutation"}],
    ) == ""


@pytest.mark.parametrize("state", ["not_started", "started", "completed_unverified",
                                   "failed_no_effect", "uncertain"])
def test_any_unproven_operation_blocks_a_verified_sibling(state: str) -> None:
    prefix = "Operação verificada. Resumo do agente (não é evidência):"
    assert speakable_reply(
        message="Resumo não comprovado.",
        status="SUCCESS",
        scope="end_to_end",
        verified_prefix=prefix,
        evidence_count=1,
        operations=[{"state": "completed_verified", "verified": True},
                    {"state": state, "category": "mutation"}],
    ) == ""


def test_all_operations_verified_keeps_only_the_template() -> None:
    prefix = "Consulta concluída e verificada. Conteúdo consultado (dados):"
    assert speakable_reply(
        message="conteúdo do ficheiro",
        status="SUCCESS",
        scope="end_to_end",
        verified_prefix=prefix,
        evidence_count=1,
        operations=[{"state": "completed_verified", "verified": True, "category": "read_only"}],
    ) == prefix


def test_eligibility_filters_sensitive_conversation_text() -> None:
    spoken = speakable_reply(
        message="Aqui está: https://exemplo.pt/a e o caminho C:\\temp\\x.py. Bom dia.",
        status="SUCCESS",
        scope="conversation",
    )
    assert spoken == "Bom dia."

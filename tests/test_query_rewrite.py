"""Pesquisa de seguimento (`app/core/query_rewrite.py`).

Caso real de 29/10/2026: depois de "como funciona uma suspenção de um carro?", o pedido
"pesquisa como funciona o mequanismo" foi pesquisado literalmente e esgotou o tempo. A reescrita
tem de trazer o assunto da conversa sem deixar passar nada que o utilizador não escreveu.
"""
from __future__ import annotations

from app.core import query_rewrite as qr

SUSPENSION = ["como funciona uma suspenção de um carro?"]


def test_short_referential_query_in_a_conversation_needs_context() -> None:
    assert qr.needs_context("como funciona o mequanismo", SUSPENSION)
    assert not qr.needs_context("como funciona o mequanismo", [])  # sem conversa, nada a resolver
    assert not qr.needs_context("quem é o CEO da Apple", SUSPENSION)  # nome próprio já identifica
    long_query = "diferenças entre suspensão independente, eixo rígido, multilink e mcpherson em carros"
    assert not qr.needs_context(long_query, SUSPENSION)


def test_rewrite_with_the_users_subject_and_spelling_fix_is_accepted() -> None:
    accepted = qr.accept_rewrite("como funciona o mecanismo da suspensão de um carro",
                                 "como funciona o mequanismo", SUSPENSION)
    assert accepted == "como funciona o mecanismo da suspensão de um carro"


def test_rewrite_cannot_add_words_the_user_never_wrote() -> None:
    # Factos inventados ou vindos da resposta do assistente nunca chegam a uma pesquisa pública.
    assert qr.accept_rewrite("mecanismo da suspensão do carro do MateusBonito",
                             "como funciona o mequanismo", SUSPENSION) is None
    assert qr.accept_rewrite("papa Leão XIV eleito 2025", "pesquisa o atual papa", ["oi"]) is None
    assert qr.accept_rewrite("", "pesquisa o atual papa", ["oi"]) is None
    # Um ano e uma palavra nova com o mesmo prefixo não vêm do utilizador.
    assert qr.accept_rewrite("suspensão do carro 2037", "como funciona o mequanismo", SUSPENSION) is None
    assert qr.accept_rewrite("suspensão do carrossel", "como funciona o mequanismo", SUSPENSION) is None
    assert qr.accept_rewrite("suspensão dos carros", "como funciona o mequanismo", SUSPENSION) is not None
    assert qr.accept_rewrite("x" * 300, "x", ["x"]) is None


def test_parse_rewrite_reads_only_the_query_field() -> None:
    assert qr.parse_rewrite('Claro: {"query": "suspensão de carro"}') == "suspensão de carro"
    assert qr.parse_rewrite("sem json") == ""
    assert qr.parse_rewrite('{"query": 3}') == ""


def test_present_and_latest_searches_carry_the_current_year() -> None:
    from datetime import date
    from app.integrations.web_quality import constraints, search_query
    today = date(2026, 10, 29)
    for query in ("o nome dos 8 últimos papas", "quem é o atual papa", "último jogo do Flamengo"):
        assert search_query(query, constraints(query), today=today).endswith(" 2026")
    for query in ("papas eleitos em 1978", "como funciona uma suspensão"):
        assert search_query(query, constraints(query), today=today) == query

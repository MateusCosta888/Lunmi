"""Falhas da revisão de código antes da beta.3 (05/11), cada uma medida na função real."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app import settings as settings_module
from app.brain.cli_web import KILO_WEB_AGENT, parse_codex
from app.computer import fastpath
from app.core.automatic import direct_web_operation
from app.integrations.web_quality import (_event_dates, _supports_release, attribute_evidence_problem,
                                          structured_lookup)

TODAY = date(2026, 11, 5)


@pytest.mark.parametrize('text', ['Crie uma pasta chamada Trabalho de Matemática na Área de Trabalho',
                                  'cria uma pasta Fotos da Praia no desktop'])
def test_a_folder_name_i_cannot_read_stays_with_the_brain(text):
    assert fastpath.resolve(text) is None


def test_a_folder_without_a_name_is_still_a_new_folder():
    assert fastpath.resolve('me cria uma pasta na área de trabalho').paths == ('Desktop/Nova pasta',)


@pytest.mark.parametrize('text', [
    'escreve um poema sobre a chuva em Lisboa',
    'abre o bloco de notas e escreve que vai chover em Lisboa',
    'traduz para inglês: amanhã vai chover em Lisboa',
    'cria um lembrete para o feriado de dezembro',
])
def test_writing_about_the_weather_is_not_a_weather_question(text):
    assert direct_web_operation(text) is None


@pytest.mark.parametrize('text', ['vai chover amanhã em Lisboa?', 'faz frio em Lisboa hoje?',
                                  'quando é o próximo feriado?', 'quanto vale o bitcoin hoje?'])
def test_real_questions_still_reach_the_data_sources(text):
    assert direct_web_operation(text)['name'] == 'web.search'


@pytest.mark.parametrize('text', [
    'quais os feriados em Itália em 2026?', 'feriados no Japão', 'feriados de angola',
    'quanto valia o bitcoin em 2017?', 'quanto valia o dólar em 2010',
    'qual a temperatura normal do corpo humano em graus',
])
def test_a_data_source_never_answers_another_place_or_year(text):
    assert structured_lookup(text, 'Portugal', today=TODAY) is None


@pytest.mark.parametrize('text,expected', [
    ('quando é o próximo feriado?', 'public holidays portugal 2026'),
    ('feriados de dezembro', 'public holidays portugal 2026'),
    ('feriados no Brasil em 2027', 'public holidays Brazil 2027'),
    ('feriados de 2020', 'public holidays portugal 2020'),
])
def test_holidays_keep_working_for_the_places_known(text, expected):
    assert structured_lookup(text, 'Portugal', today=TODAY) == ('holidays', expected)


@pytest.mark.parametrize('text', [
    'abre https://youtube.com no Chrome', 'cria um atalho para https://example.com na área de trabalho',
    'escreve https://example.com no bloco de notas', 'envia https://example.com para o João por email',
    'este link é phishing? https://example.com/unsubscribe?id=1',
])
def test_doing_something_else_with_a_link_stays_with_the_brain(text):
    assert direct_web_operation(text) is None


def test_a_date_heading_survives_a_blank_line():
    assert _event_dates('## Sábado, 8 de novembro de 2026\n\nFlamengo x Palmeiras às 16h') == [date(2026, 11, 8)]


@pytest.mark.parametrize('text,ok', [
    ('Avatar 4 estreia a 2026-12-12', True),
    ('Avatar 4 estreia a 12 de dezembro de 2026', True),
    ('Avatar 4 estreou em 2022. Copyright 2027.', False),
])
def test_a_release_needs_a_real_future_date(text, ok):
    assert _supports_release('quando estreia o filme Avatar 4', text, today=TODAY) is ok


def test_a_structured_answer_for_today_still_passes():
    documents = [{'structured': True, 'content': '1 USD = 0.88 EUR'}]
    assert attribute_evidence_problem('quanto vale o dólar hoje', documents) == ''


@pytest.mark.parametrize('text,query', [
    ('pesquisa o PIB do Brasil e da Argentina e diz qual é maior', 'o PIB do Brasil e da Argentina'),
    ('pesquisa a história de Portugal e da Espanha', 'a história de Portugal e da Espanha'),
    ('pesquisa o euro e da um resumo', 'o euro'),
])
def test_a_second_subject_stays_in_the_search(text, query):
    assert direct_web_operation(text)['args']['query'] == query


def test_a_key_can_be_saved_over_an_unreadable_file(tmp_path: Path):
    path = tmp_path / 'provider.dpapi'
    path.write_bytes(b'not a protected file')
    settings_module.save_provider('groq', 'llama', 'gsk_key_1234567890', '', path=path)
    assert settings_module._read_provider_file(path)['provider'] == 'groq'


def test_kilo_refuses_the_private_addresses_the_review_listed():
    for address in ('"http://172.*"', '"https://169.254.*"', '"http://0.*"', '"http://*.nip.io*"'):
        assert address + ': deny' in KILO_WEB_AGENT


def test_a_retry_error_does_not_fail_a_codex_turn_that_finished():
    assert parse_codex([{'type': 'error', 'message': 'retrying'}, {'type': 'turn.completed'}]).failed is False
    assert parse_codex([{'type': 'error', 'message': 'gone'}]).failed is True

"""Política de evidência: identidade do sujeito, período histórico e data do evento."""
from datetime import date

import pytest

from app.integrations.web_quality import attribute_evidence_problem, supports_requested_attribute

TODAY = date(2026, 11, 4)
THE_LAST = 'Quantas temporadas tem The Last of Us na HBO em Portugal?'
ONE_PUNCH = 'Quantos capítulos tem One-Punch Man hoje?'
REAL_MADRID = 'Próximo jogo do Real Madrid'
PRESIDENT = 'Pesquisa na internet quem é o atual presidente de Portugal.'


@pytest.mark.parametrize('query,text', [
    (ONE_PUNCH, 'One Piece tem 1100 capítulos publicados.'),
    (THE_LAST, 'Game of Thrones tem 8 temporadas publicadas na HBO em Portugal.'),
    (THE_LAST, 'The Last Kingdom tem 5 temporadas.'),
    (REAL_MADRID, 'O Real Betis enfrenta o Sevilha em 2 de janeiro de 2027.'),
    (PRESIDENT, 'O presidente de França é João Exemplo, disse o jornal na internet.'),
    ('Qual o valor de mercado da Apple?', 'O valor de mercado da Microsoft é 3 mil milhões de dólares.'),
])
def test_another_entity_is_not_support(query, text):
    assert not supports_requested_attribute(query, text, today=TODAY)


@pytest.mark.parametrize('query,text', [
    (ONE_PUNCH, 'One Punch Man tem 210 capítulos publicados.'),
    (ONE_PUNCH.lower(), 'One-Punch Man tem 210 capítulos publicados.'),
    (THE_LAST, 'The Last of Us tem 3 temporadas.'),
    (REAL_MADRID, 'O Real Madrid enfrenta o Sevilha em 2 de janeiro de 2027.'),
    (PRESIDENT, 'O presidente de Portugal é João Exemplo.'),
    ('Quantos capítulos tem No Game No Life?', 'No Game No Life tem 70 capítulos publicados.'),
    ('Qual o valor de mercado da Apple?', 'O valor de mercado da Apple é 3 mil milhões de dólares.'),
])
def test_the_right_entity_stays_supported(query, text):
    assert supports_requested_attribute(query, text, today=TODAY)


def test_a_name_with_a_preposition_is_cut_to_its_first_part():
    # Limite declarado: sem classificador, a preposição corta o nome e sobra só `homem`.
    query = 'Quantos episódios tem Homem no Castelo Alto?'
    assert supports_requested_attribute(query, 'Homem no Castelo Alto tem 40 episódios.', today=TODAY)
    assert supports_requested_attribute(query, 'Homem Aranha tem 40 episódios.', today=TODAY)


def _doc(content, published='2026-11-01', title='iPhone 12 em Portugal'):
    return {'url': 'https://a.example/x', 'title': title, 'content': content,
            'published_at': published, 'cached': False}


PRICE_2020 = 'Pesquisa o preço do iPhone 12 em Portugal em 2020'
PRICE_TEXT = 'O iPhone 12 custa 909 euros em Portugal.'


@pytest.mark.parametrize('content,published', [
    (PRICE_TEXT, '2020-10-14'),
    ('Em 2020 o iPhone 12 custava 909 euros em Portugal.', '2023-01-01'),
])
def test_a_past_period_is_met_by_a_fact_tied_to_that_year(content, published):
    assert attribute_evidence_problem(PRICE_2020, [_doc(content, published)], today=TODAY) == ''


@pytest.mark.parametrize('content,published', [
    (PRICE_TEXT + '\n© 2020 Loja Exemplo.', '2023-01-01'),
    ('Arquivo 2020\n' + PRICE_TEXT, None),
    ('Em 2018 o iPhone 12 custava 909 euros em Portugal.', '2020-10-14'),
])
def test_a_year_elsewhere_does_not_date_the_fact(content, published):
    problem = attribute_evidence_problem(PRICE_2020, [_doc(content, published)], today=TODAY)
    assert problem == 'date_unconfirmed'


def test_a_sentence_with_two_years_passes_and_the_synthesis_picks_the_value():
    # Limite declarado: a política confirma que o ano está no facto, não qual dos valores é.
    text = 'O iPhone 12 custava 800 euros em Portugal em 2018 e custa 909 euros em 2020.'
    assert attribute_evidence_problem(PRICE_2020, [_doc(text, None)], today=TODAY) == ''


@pytest.mark.parametrize('query', ['Preço atual do iPhone 12 em Portugal',
                                   'Preço do iPhone 12 de 2020 em Portugal'])
def test_a_current_question_keeps_the_freshness_rule(query):
    assert attribute_evidence_problem(query, [_doc(PRICE_TEXT, '2020-10-14')], today=TODAY) == 'stale'


NEXT_GAME = 'Próximo jogo do Brasil'
UPDATED = 'Atualizado em 4 de novembro de 2026. '


@pytest.mark.parametrize('text', [
    'O Brasil enfrenta a Argentina em 2 de janeiro de 2027.',
    '2 de janeiro de 2027\nBrasil x Argentina',
    'Sábado, 2 de janeiro de 2027\nBrasil x Argentina em Lisboa',
    '02/01/2027 | Brasil x Argentina',
    '2027-01-02 Brasil x Argentina',
])
def test_a_full_event_date_supports_the_next_game(text):
    assert attribute_evidence_problem(NEXT_GAME, [_doc(text, title='Brasil')], today=TODAY) == ''


@pytest.mark.parametrize('text', [
    'O Brasil enfrenta a Argentina em 2 de janeiro, em Lisboa.',
    UPDATED + 'O Brasil enfrenta a Argentina em 2 de janeiro.',
    UPDATED + 'O Brasil enfrenta a Argentina em 02/01.',
    'Atualizado em 4 de novembro de 2026: O Brasil enfrenta a Argentina em 02/01.',
])
def test_an_update_date_or_a_date_without_year_is_not_the_event_date(text):
    problem = attribute_evidence_problem(NEXT_GAME, [_doc(text, title='Brasil')], today=TODAY)
    assert problem == 'date_unconfirmed'


def test_a_past_event_stays_stale():
    text = 'O Brasil enfrenta a Argentina em 2 de janeiro de 2020.'
    assert attribute_evidence_problem(NEXT_GAME, [_doc(text, title='Brasil')], today=TODAY) == 'stale'

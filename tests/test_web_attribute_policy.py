"""Política de evidência: identidade do sujeito, período histórico e data do evento."""
from datetime import date

import pytest

from app.integrations.web_quality import supports_requested_attribute

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

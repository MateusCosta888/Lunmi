"""Limites da restauração de geometria: retângulo inteiro na tela ou `None`.

Sem display e sem tocar em arquivo nenhum: é aritmética pura de `ui_preferences.usable_rect`,
coletada pelo `pytest`. O que só se prova com janela mapeada mora no smoke.
"""
from __future__ import annotations

import pytest

from app.ui import ui_preferences as prefs

SCREEN_W = 1920
SCREEN_H = 1080


def rect(window: tuple[int, int, int, int] | None) -> prefs.Preferences:
    return prefs.Preferences(window=window)


def usable(window: tuple[int, int, int, int] | None,
           screen_w: int = SCREEN_W, screen_h: int = SCREEN_H) -> tuple[int, int, int, int] | None:
    return prefs.usable_rect(rect(window), screen_w, screen_h)


def test_janela_que_transborda_a_direita_e_trazida_inteira_para_dentro() -> None:
    """x=1500 com 1400 de largura numa tela de 1920: sobrava quase metade fora do mouse."""
    bounds = usable((1500, 100, 1400, 800))
    assert bounds == (520, 100, 1400, 800)
    assert bounds is not None
    x, _, width, _ = bounds
    assert x + width == SCREEN_W


def test_transbordo_e_medido_em_cada_eixo_separadamente() -> None:
    """Largura que cabe, altura que não: só o eixo culpado se move."""
    assert usable((100, 700, 800, 600)) == (100, 480, 800, 600)
    assert usable((1500, 700, 1400, 600)) == (520, 480, 1400, 600)


def test_bordas_negativas_sao_trazidas_para_zero() -> None:
    """Segundo monitor à esquerda/acima: a janela volta, mas o título precisa aparecer."""
    assert usable((-1200, -300, 1400, 800)) == (0, 0, 1400, 800)
    assert usable((-10, -10, 1280, 800)) == (0, 0, 1280, 800)


def test_monitor_ausente_devolve_none() -> None:
    """Sem sobreposição útil não há onde restaurar: 2600 numa tela de 1920 é monitor removido."""
    assert usable((2600, 100, 1400, 800)) is None
    assert usable((100, 2000, 800, 600)) is None
    assert usable((-2000, 0, 1400, 800)) is None
    assert usable((1920, 1080, 800, 600)) is None
    assert prefs.usable_rect(prefs.Preferences(window=None), SCREEN_W, SCREEN_H) is None


def test_sobreposicao_abaixo_do_minimo_visivel_devolve_none() -> None:
    """Dez pixels na borda não são uma janela restaurável, são um risco de perder a janela."""
    assert usable((1910, 100, 800, 600)) is None
    assert usable((100, 1070, 800, 600)) is None
    assert usable((1910 - prefs.MIN_VISIBLE + 1, 100, 800, 600)) is not None


def test_tela_pequena_demais_devolve_none() -> None:
    assert usable((10, 10, 800, 600), 300, 200) is None
    assert usable((10, 10, 800, 600), SCREEN_W, prefs.MIN_WINDOW_EDGE - 1) is None
    assert usable((10, 10, 800, 600), prefs.MIN_WINDOW_EDGE - 1, SCREEN_H) is None


def test_dimensoes_abaixo_do_minimo_devolvem_none() -> None:
    assert usable((0, 0, 300, 300)) is None
    assert usable((0, 0, 1280, prefs.MIN_WINDOW_EDGE - 1)) is None
    assert usable((0, 0, prefs.MIN_WINDOW_EDGE - 1, 800)) is None


def test_janela_maior_que_a_tela_e_reduzida_a_tela() -> None:
    """Retângulo maior que a tela nunca cabe inteiro: a dimensão cede, e o x/y voltam a zero."""
    assert usable((1500, 100, 3000, 2000)) == (0, 0, SCREEN_W, SCREEN_H)


def test_janela_ja_dentro_nao_se_move() -> None:
    """A restauração não é desculpa para reposicionar quem já cabia."""
    assert usable((40, 30, 1280, 800)) == (40, 30, 1280, 800)
    assert usable((0, 0, SCREEN_W, SCREEN_H)) == (0, 0, SCREEN_W, SCREEN_H)


@pytest.mark.parametrize("window", [
    (1500, 100, 1400, 800),
    (-1200, -300, 1400, 800),
    (100, 700, 800, 600),
    (1500, 100, 3000, 2000),
    (0, 0, 1280, 800),
    (1800, 900, 800, 600),
])
def test_todo_retangulo_devolvido_cabe_na_tela(window: tuple[int, int, int, int]) -> None:
    """A promessa inteira da função, medida em cada retângulo que ela aceita."""
    bounds = usable(window)
    assert bounds is not None
    x, y, width, height = bounds
    assert x >= 0 and y >= 0
    assert x + width <= SCREEN_W and y + height <= SCREEN_H
    assert width >= prefs.MIN_WINDOW_EDGE and height >= prefs.MIN_WINDOW_EDGE
    assert (width, height) == (min(window[2], SCREEN_W), min(window[3], SCREEN_H))


def test_nenhuma_janela_aceita_fica_com_a_dimensao_do_arquivo_quando_ela_nao_cabe() -> None:
    """O contrato que a A2 corrigiu: nunca devolver dimensão maior que a tela."""
    bounds = usable((2600 - SCREEN_W, 100, 4000, 900))
    assert bounds is not None
    assert bounds[2] == SCREEN_W
    assert bounds[0] + bounds[2] == SCREEN_W

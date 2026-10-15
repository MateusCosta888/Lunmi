"""Checagens de U2 que não precisam de janela: matemática de tema, tokens e o adaptador.

Separado do `tests/lunmi_ui_smoke.py` de propósito: o smoke abre Tk real e roda à mão; isto é
coletado pelo `pytest` junto com o resto da suíte e tem de valer sem display, sem sandbox e sem
tocar em arquivo nenhum. O que só se prova com widget na tela mora lá.
"""
from __future__ import annotations

from pathlib import Path

from app.ui import theme
from app.ui.composer import MAX_LINES, MIN_CHARS, entry_anchor
from app.ui.transcript import BUBBLE_INSET, BUBBLE_RADIUS, VERDICT_TONES, is_measurement


def test_toda_par_de_contraste_passa_o_piso_dele() -> None:
    assert theme.failing_pairs() == []


def test_a_lista_de_pares_cobre_o_que_as_bolhas_usam() -> None:
    """A lista é o juramento; bolha nova sem par na lista é promessa não assinada."""
    named = {(label, surface) for label, surface, _, _, _ in theme.CONTRAST_PAIRS}
    for wanted in (("texto principal", "bolha do usuário"), ("texto discreto", "bolha do usuário"),
                   ("texto principal", "cartão"), ("sucesso", "cartão"), ("atenção", "cartão"),
                   ("falha", "cartão")):
        assert wanted in named, wanted


def test_piso_de_texto_e_maior_que_o_de_grafico() -> None:
    assert theme.TEXT_FLOOR > theme.LARGE_TEXT_FLOOR == theme.GRAPHIC_FLOOR
    assert theme.DECORATIVE_FLOOR == 0.0


def test_retangulo_arredondado_preserva_arestas_retas_e_limita_raio() -> None:
    points = theme.round_rect_points(0, 0, 200, 60, 12)
    assert points[:4] == [12, 0, 12, 0]
    xs = points[0::2]
    ys = points[1::2]
    assert min(xs) == 0 and max(xs) == 200 and min(ys) == 0 and max(ys) == 60
    # Raio maior que metade do lado menor é cortado: o canto nunca vira óvalo.
    squashed = theme.round_rect_points(0, 0, 40, 20, 99)
    assert max(squashed[1::2]) == 20 and min(squashed[0::2]) == 0
    assert 10 in squashed  # o raio efetivo ficou em 10 = metade da altura


def test_inset_da_bolha_esconde_o_canto_quadrado_do_texto() -> None:
    """Geometria, não gosto: a 3 px o canto ficaria fora do círculo (12,73 > 12)."""
    diagonal = ((BUBBLE_RADIUS - BUBBLE_INSET) ** 2 + (BUBBLE_RADIUS - BUBBLE_INSET) ** 2) ** 0.5
    assert diagonal < BUBBLE_RADIUS
    assert ((BUBBLE_RADIUS - 3) ** 2 * 2) ** 0.5 > BUBBLE_RADIUS


def test_adaptador_traduz_so_o_que_entry_sabe_dizer() -> None:
    assert entry_anchor(0) == "1.0"
    assert entry_anchor("0") == "1.0"
    assert entry_anchor(5) == "1.5"
    for passthrough in ("end", "insert", "1.4", "end-1c"):
        assert entry_anchor(passthrough) == passthrough


def test_compositor_pede_pouco_e_para_no_teto() -> None:
    """O pedido de largura alimenta a conta da doca: 80 caracteres padrão cortavam a janela.

    Medido em 1280x800: com o padrão do `Text`, o compositor pedia 741 px, a doca 912, e a
    varredura de corte acusava a janela inteira de truncada.
    """
    assert MIN_CHARS <= 16
    assert MAX_LINES == 6


def test_linha_de_medicao_e_reconhecida_pela_tipografia() -> None:
    assert is_measurement("Total 902 ms · LLM 2890 ms · UI 121 ms")
    assert not is_measurement("Abri o Discord e comecei a playlist.")
    assert not is_measurement("só uma vírgula · aqui")


def test_vereditos_do_controller_tem_tom() -> None:
    """Os quatro estados que o `TaskResult` produz; um nome a menos seria cor faltando na tela."""
    assert set(VERDICT_TONES) == {"SUCCESS", "NOT_VERIFIED", "FAILED", "CANCELLED"}
    assert VERDICT_TONES["SUCCESS"] == theme.SUCCESS
    assert VERDICT_TONES["NOT_VERIFIED"] == theme.WARNING
    assert VERDICT_TONES["FAILED"] == theme.ERROR


def test_preferencias_invalidas_devolvem_o_padrao_sem_levantar(tmp_path: Path) -> None:
    """Preferência quebrada não pode impedir de abrir: arquivo ruim é arquivo ausente."""
    from app.ui import ui_preferences as prefs

    path = prefs.path_for(tmp_path)
    assert prefs.load(path) is prefs.DEFAULT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ isto não é json", encoding="utf-8")
    assert prefs.load(path) == prefs.DEFAULT
    path.write_bytes(b'{"actions": ' + b"x" * (prefs.MAX_BYTES + 10))
    assert prefs.load(path) == prefs.DEFAULT


def test_preferencias_descartam_tipos_e_tamanhos_impossiveis(tmp_path: Path) -> None:
    from app.ui import ui_preferences as prefs

    parsed = prefs.parse({"window": [0, 0, 10, 10], "scale_pct": 133,
                          "actions": [{"label": "ok", "draft": "texto"}, "não é objeto",
                                      {"label": 12, "draft": "x"}, {"label": "", "draft": "y"}]})
    assert parsed.window is None, "janela menor que o mínimo não é restaurável"
    assert parsed.scale_pct == 100, "escala fora da lista não é aplicada"
    assert [action.label for action in parsed.actions] == ["ok"] or \
        parsed.actions == prefs.DEFAULT_ACTIONS
    big = prefs.parse({"actions": [{"label": "l", "draft": "d"}] * 40})
    assert len(big.actions) <= prefs.MAX_ACTIONS


def test_preferencia_grava_e_le_de_volta_e_nao_toca_em_nada_alem_do_arquivo(tmp_path: Path) -> None:
    from app.ui import ui_preferences as prefs

    path = prefs.path_for(tmp_path)
    wanted = prefs.Preferences(window=(40, 30, 1280, 800), scale_pct=150,
                               actions=(prefs.QuickAction("Abrir o editor", "Abra o editor "),))
    assert prefs.save(path, wanted) is True
    assert prefs.load(path) == wanted
    assert list(path.parent.iterdir()) == [path], "a gravação não deixou arquivo temporário para trás"
    assert prefs.usable_rect(wanted, 1920, 1080) == (40, 30, 1280, 800)
    assert prefs.usable_rect(prefs.Preferences(window=(3000, 10, 800, 600)), 1920, 1080) is None


def test_atalhos_padrao_sao_a_mesma_lista_em_dois_lugares() -> None:
    """`panels.QUICK_ACTIONS` é derivado, não copiado: duas listas iguais divergiriam."""
    from app.ui import panels, ui_preferences

    assert panels.QUICK_ACTIONS == tuple((a.label, a.draft)
                                         for a in ui_preferences.DEFAULT_ACTIONS)


def test_botao_arredondado_compoe_cantos_no_fundo_e_preserva_centro() -> None:
    import struct
    import zlib

    png = theme.rounded_tile(theme.ACCENT, theme.BORDER_SUBTLE)
    assert png[:8] == b'\x89PNG\r\n\x1a\n'
    width, height = struct.unpack('!II', png[16:24])
    offset = 8
    compressed = bytearray()
    while offset < len(png):
        size = struct.unpack('!I', png[offset:offset + 4])[0]
        if png[offset + 4:offset + 8] == b'IDAT':
            compressed.extend(png[offset + 8:offset + 8 + size])
        offset += size + 12
    pixels = zlib.decompress(compressed)
    assert png[25] == 2  # RGB: no alpha compositing in the native ttk renderer.
    assert pixels[1:4] == bytes.fromhex(theme.BG_CHAT[1:])
    middle = (height // 2) * (width * 3 + 1) + 1 + (width // 2) * 3
    assert pixels[middle:middle + 3] == bytes((125, 175, 255))


def test_preferencias_limitam_a_leitura_antes_de_carregar_arquivo_grande(tmp_path: Path,
                                                                       monkeypatch) -> None:
    from app.ui import ui_preferences as prefs
    import io

    class Bounded(io.BytesIO):
        def read(self, size=-1):
            assert size == prefs.MAX_BYTES + 1
            return super().read(size)

    monkeypatch.setattr(Path, 'open', lambda *a, **k: Bounded(b'x' * (prefs.MAX_BYTES + 2)))
    assert prefs.load(tmp_path / 'large.json') == prefs.DEFAULT

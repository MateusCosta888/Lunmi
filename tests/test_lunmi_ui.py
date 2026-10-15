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


def test_perfil_valida_nome_e_avatar_png_jpeg_com_pillow_sem_mutar_globais(tmp_path: Path) -> None:
    import base64
    import io
    from PIL import Image, ImageFile
    import pytest
    from app.ui import ui_preferences as prefs

    orig_max_pixels = Image.MAX_IMAGE_PIXELS
    orig_truncated = ImageFile.LOAD_TRUNCATED_IMAGES

    # Nome: normaliza espaços, aceita até 40 chars, recusa controle/quebras e >40 em strict
    assert prefs.validate_profile_name("  Ana   Maria  ", strict=True) == "Ana Maria"
    assert prefs.validate_profile_name("", strict=True) == ""
    with pytest.raises(ValueError, match="controle"):
        prefs.validate_profile_name("Ana\nIgnorar", strict=True)
    with pytest.raises(ValueError, match="40"):
        prefs.validate_profile_name("A" * 41, strict=True)

    # Gera PNG e JPEG reais válidos via Pillow
    png_buf = io.BytesIO()
    Image.new("RGBA", (64, 64), (90, 140, 255, 255)).save(png_buf, format="PNG")
    png_url = "data:image/png;base64," + base64.b64encode(png_buf.getvalue()).decode("ascii")

    jpg_buf = io.BytesIO()
    Image.new("RGB", (64, 64), (40, 80, 160)).save(jpg_buf, format="JPEG", quality=80)
    jpg_url = "data:image/jpeg;base64," + base64.b64encode(jpg_buf.getvalue()).decode("ascii")

    assert prefs.validate_avatar_data_url(png_url, strict=True) == png_url
    assert prefs.validate_avatar_data_url(jpg_url, strict=True) == jpg_url

    # Magic bytes de PNG com corpo truncado/corrompido devem ser recusados pelo Pillow verify/load
    corrupted_png = b"\x89PNG\r\n\x1a\n" + b"corrompido-sem-ihdr-valido"
    corrupted_url = "data:image/png;base64," + base64.b64encode(corrupted_png).decode("ascii")
    with pytest.raises(ValueError, match="inválida|corrompida"):
        prefs.validate_avatar_data_url(corrupted_url, strict=True)
    assert prefs.validate_avatar_data_url(corrupted_url, strict=False) == ""

    # Imagem com dimensão acima de 512x512 é recusada antes do decode completo
    oversized_buf = io.BytesIO()
    Image.new("RGB", (600, 64), (10, 20, 30)).save(oversized_buf, format="PNG")
    oversized_url = "data:image/png;base64," + base64.b64encode(oversized_buf.getvalue()).decode("ascii")
    with pytest.raises(ValueError, match="Dimensões"):
        prefs.validate_avatar_data_url(oversized_url, strict=True)

    # SVG, URL remota e path de arquivo são recusados
    for forbidden in (
        "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
        "https://example.com/avatar.png",
        "file:///C:/Users/avatar.png",
        "C:\\Users\\avatar.png",
    ):
        with pytest.raises(ValueError):
            prefs.validate_avatar_data_url(forbidden, strict=True)

    # Globais do Pillow permanecem intactos
    assert Image.MAX_IMAGE_PIXELS == orig_max_pixels
    assert ImageFile.LOAD_TRUNCATED_IMAGES == orig_truncated

    # Persistência round-trip em lunmi-ui.json
    path = prefs.path_for(tmp_path)
    saved_prefs = prefs.Preferences(
        profile=prefs.UserProfile(name="Bia", avatar_data_url=png_url)
    )
    assert prefs.save(path, saved_prefs) is True
    loaded = prefs.load(path)
    assert loaded.profile.name == "Bia"
    assert loaded.profile.avatar_data_url == png_url

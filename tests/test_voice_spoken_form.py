"""The spoken form: markdown as sentences, symbols in words, Brazilian Portuguese (01/11)."""
from __future__ import annotations

import pytest

from app.voice.filter import speakable_text, spoken_form


@pytest.mark.parametrize(("written", "spoken"), [
    ("Guardei o ficheiro no ambiente de trabalho.", "Salvei o arquivo na área de trabalho."),
    ("Estou a escrever a tua lista.", "Estou escrevendo a sua lista."),
    ("Estou a ver e estava a ler.", "Estou vendo e estava lendo."),
    ("Fica a par das definições do ecrã.", "Fica a par das configurações da tela."),
    ("Diz-me se queres o contacto actual.", "Me diga se quer o contato atual."),
    ("Se quiseres, diz-me o que preferes. Já agora, obrigada.", "Se quiser, me diga o que prefere. Aliás, obrigada."),
    ("Um fenómeno económico, o António e o prémio.", "Um fenômeno econômico, o Antônio e o prêmio."),
    ("12 × 7 = 84 e 50% de 3+4.", "12 vezes 7 igual a 84 e 50 por cento de 3 mais 4."),
])
def test_written_portuguese_is_spoken_as_brazilian(written: str, spoken: str) -> None:
    assert spoken_form(written) == spoken


def test_markdown_becomes_sentences_with_pauses() -> None:
    text = "## Resumo\n**Tudo pronto** 🎉\n- abri o Bloco de Notas\n- escrevi a [lista](https://x.y)\n1. guarda-chuva"
    assert spoken_form(text) == ("Resumo. Tudo pronto. abri o Bloco de Notas. "
                                 "escrevi a lista. guarda-chuva.")


def test_code_and_urls_are_still_dropped_after_the_spoken_form() -> None:
    text = "Feito.\n```python\nprint('x')\n```\nVê https://exemplo.com agora."
    assert speakable_text(text) == "Feito."

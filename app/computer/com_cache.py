"""Geração de módulos COM em memória, para os recursos do pacote ficarem somente-leitura.

O `comtypes` gera wrappers dos typelibs (UIAutomation, stdole) em arquivos `.py` dentro de
`comtypes/gen/`. Por padrão, no pacote instalado essa pasta é
`resources/python/Lib/site-packages/comtypes/gen` — ou seja, **recursos**, que o produto trata
como somente-leitura. Era um defeito real: depois de instalar e usar a interface, a pasta de
instalação ganhava wrappers gerados em execução, e a desinstalação (que remove só o que
instalou) deixava restos para trás.

`comtypes.client.gen_dir = None` faz o `_generate.py` compilar os wrappers **em memória**, sem
tocar o disco (comportamento suportado pelo próprio pacote: ver
`comtypes/client/_generate.py`, que checa `if comtypes.client.gen_dir is None`). O pin do UFO
não é alterado: nada aqui mexe em `vendor/UFO`.

Uso nos dois processos que podem importar consumidores de UIA:

    from app.computer.com_cache import use_in_memory_com_modules
    use_in_memory_com_modules()   # antes de importar pywinauto/comtypes
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def use_in_memory_com_modules() -> bool:
    """Impede a gravação de wrappers COM no disco; devolve True quando a configuração valeu.

    Idempotente e tolerante a ausência do `comtypes`: um processo que nunca toca UIA não deve
    falhar por causa disso. Nunca levanta.
    """
    try:
        import comtypes.client  # noqa: PLC0415 - import tardio de propósito
    except Exception:  # pragma: no cover - comtypes ausente não é erro aqui
        return False
    try:
        comtypes.client.gen_dir = None
    except Exception:  # pragma: no cover - API do upstream mudou
        _log.warning("Não foi possível configurar a geração COM em memória.", exc_info=True)
        return False
    return True

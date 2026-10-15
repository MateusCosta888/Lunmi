"""Preferências de apresentação da Lunmi: janela, escala e atalhos do painel.

Arquivo `.local/lunmi-ui.json`, **separado** de segredo, de memória e da seleção de Brain: nada
deste módulo lê ou escreve `.local/brain-selection.json`, DPAPI ou o banco. É só tela.

Três decisões que não são gosto:

- **Gravação fora da thread Tk; leitura única e limitada.** Gravar acontece num fio próprio,
  nunca no laço de eventos. Ler acontece **uma só vez**, com teto de `MAX_BYTES`, antes do
  primeiro widget: `tk scaling` tem de valer antes de a primeira fonte nascer, senão a janela
  abriria numa escala e viveria em outra. Nenhuma outra leitura ou gravação entra no laço.
- **Tolerante por construção.** Arquivo ausente, inválido, gigante ou com tipo errado devolve o
  padrão e não levanta: preferência quebrada não pode impedir ninguém de abrir o aplicativo.
- **Gravação atômica.** `tempfile` no mesmo diretório + `fsync` + `os.replace`, com modo 0600. Um
  corte no meio da escrita deixa o arquivo anterior inteiro, não um JSON pela metade.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

MAX_ACTIONS = 8
LABEL_MAX = 40
DRAFT_MAX = 200
MAX_BYTES = 64 * 1024
SCALE_CHOICES = (100, 125, 150, 200)
MIN_WINDOW_EDGE = 400
# Pixels da janela que precisam estar na tela para a restauração valer a pena. Abaixo disto a
# janela está esquecida num monitor que não existe mais, e o lugar padrão é a resposta honesta.
MIN_VISIBLE = 40


@dataclass(frozen=True)
class QuickAction:
    """Um atalho do painel: rótulo curto e o texto que ele põe no compositor."""

    label: str
    draft: str


@dataclass(frozen=True)
class Preferences:
    window: tuple[int, int, int, int] | None = None      # x, y, largura, altura
    scale_pct: int = 100
    actions: tuple[QuickAction, ...] = ()
    theme: str = "celestial"


DEFAULT_ACTIONS: tuple[QuickAction, ...] = (
    QuickAction("Abrir aplicativo", "Abra o "),
    QuickAction("Criar pasta", "Crie uma pasta na Área de Trabalho chamada "),
    QuickAction("Pesquisar na web", "Pesquise no navegador: "),
    QuickAction("Escrever no campo", "Escreva no campo ativo: "),
)
DEFAULT = Preferences(actions=DEFAULT_ACTIONS)


def path_for(root: Path) -> Path:
    return Path(root) / ".local" / "lunmi-ui.json"


def _action(raw: Any) -> QuickAction | None:
    if not isinstance(raw, dict):
        return None
    label = raw.get("label")
    draft = raw.get("draft")
    if not isinstance(label, str) or not isinstance(draft, str):
        return None
    label = " ".join(label.split())[:LABEL_MAX]
    draft = draft.replace("\r", "")[:DRAFT_MAX]
    if not label or not draft.strip():
        return None
    return QuickAction(label, draft)


def parse(payload: Any) -> Preferences:
    """Converte o que veio do disco no que a tela usa, descartando o que não é confiável."""
    if not isinstance(payload, dict):
        return DEFAULT
    window = payload.get("window")
    rect: tuple[int, int, int, int] | None = None
    if isinstance(window, (list, tuple)) and len(window) == 4 and all(
            isinstance(v, int) and not isinstance(v, bool) for v in window):
        x, y, width, height = window
        if (MIN_WINDOW_EDGE <= width <= 16384 and MIN_WINDOW_EDGE <= height <= 10240
                and -16384 <= x <= 16384 and -16384 <= y <= 10240):
            rect = (x, y, width, height)
    scale = payload.get("scale_pct")
    scale_pct = scale if isinstance(scale, int) and not isinstance(scale, bool) \
        and scale in SCALE_CHOICES else 100
    raw_actions = payload.get("actions")
    actions: list[QuickAction] = []
    if isinstance(raw_actions, list):
        for item in raw_actions[:MAX_ACTIONS]:
            built = _action(item)
            if built is not None:
                actions.append(built)
    raw_theme = payload.get("theme", "celestial")
    # IDs are inert metadata. The frontend registry chooses a safe fallback for unknown themes.
    theme = raw_theme if isinstance(raw_theme, str) and re.fullmatch(r"[a-z][a-z0-9-]{0,47}", raw_theme) else "celestial"
    return Preferences(window=rect, scale_pct=scale_pct,
                       actions=tuple(actions) or DEFAULT_ACTIONS, theme=theme)


def load(path: Path) -> Preferences:
    """Lê sem nunca falhar: arquivo ruim é ausente, e ausente é o padrão."""
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
    except OSError:
        return DEFAULT
    if len(raw) > MAX_BYTES:
        return DEFAULT
    try:
        return parse(json.loads(raw.decode("utf-8")))
    except (ValueError, UnicodeDecodeError):
        return DEFAULT


def dump(prefs: Preferences) -> str:
    return json.dumps({"window": list(prefs.window) if prefs.window else None,
                       "scale_pct": prefs.scale_pct,
                       "theme": prefs.theme,
                       "actions": [{"label": a.label, "draft": a.draft} for a in prefs.actions]},
                      ensure_ascii=False, indent=2)


def save(path: Path, prefs: Preferences) -> bool:
    """Escrita atômica. `False` diz que não gravou; nunca levanta para a UI."""
    target = Path(path)
    body = dump(prefs).encode("utf-8")
    if len(body) > MAX_BYTES:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile("wb", dir=target.parent, prefix=".lunmi-ui-",
                                             suffix=".tmp", delete=False)
        try:
            with handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
                os.chmod(handle.name, 0o600)
            os.replace(handle.name, target)
        except OSError:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            return False
        return True
    except OSError:
        return False


def usable_rect(prefs: Preferences, screen_w: int, screen_h: int) -> tuple[int, int, int, int] | None:
    """O retângulo que volta cabe **inteiro** na tela, ou `None` se não houver como.

    Monitor removido é o caso do `None`: uma janela salva em x=2600 numa tela de 1920 não tem
    sobreposição útil nenhuma, e abrir no lugar padrão é melhor que abrir fora do alcance do
    mouse. Já a janela que apenas **transborda** a borda é trazida para dentro — `x` e `y` são
    movidos até o retângulo caber, com as dimensões preservadas sempre que elas caibam na tela (e
    reduzidas à tela quando não caibam, porque retângulo maior que a tela nunca cabe inteiro).
    """
    if not prefs.window:
        return None
    if screen_w < MIN_WINDOW_EDGE or screen_h < MIN_WINDOW_EDGE:
        return None
    x, y, width, height = prefs.window
    width = min(width, screen_w)
    height = min(height, screen_h)
    if width < MIN_WINDOW_EDGE or height < MIN_WINDOW_EDGE:
        return None
    # Sobreposição real com a tela, eixo a eixo, **antes** de mover: sem esta medida, mover-para-
    # dentro transformaria qualquer janela esquecida num retângulo na origem, decidindo por engano
    # onde o usuário quer a janela em vez de admitir que não há tela para ela.
    if min(x + width, screen_w) - max(x, 0) < MIN_VISIBLE:
        return None
    if min(y + height, screen_h) - max(y, 0) < MIN_VISIBLE:
        return None
    x = max(0, min(x, screen_w - width))
    y = max(0, min(y, screen_h - height))
    return (x, y, width, height)


def with_actions(prefs: Preferences, actions: tuple[QuickAction, ...]) -> Preferences:
    return replace(prefs, actions=actions[:MAX_ACTIONS])

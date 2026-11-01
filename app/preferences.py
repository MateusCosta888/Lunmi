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

import base64
import binascii
import io
import json
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from PIL import Image

MAX_ACTIONS = 8
LABEL_MAX = 40
DRAFT_MAX = 200
MAX_BYTES = 64 * 1024
SCALE_CHOICES = (100, 125, 150, 200)
MIN_WINDOW_EDGE = 400
# Pixels da janela que precisam estar na tela para a restauração valer a pena. Abaixo disto a
# janela está esquecida num monitor que não existe mais, e o lugar padrão é a resposta honesta.
MIN_VISIBLE = 40

# Limites do perfil local do usuário (armazenado no mesmo `.local/lunmi-ui.json` dentro de 64 KiB).
MAX_PROFILE_NAME_CHARS = 40
MAX_PROFILE_AVATAR_CHARS = 40 * 1024      # 40960 caracteres em data URL base64
MAX_PROFILE_AVATAR_BYTES = 30 * 1024      # 30720 bytes binários
MAX_PROFILE_AVATAR_DIMENSION = 512        # largura/altura máximas permitidas
MAX_PROFILE_AVATAR_PIXELS = 512 * 512     # proteção contra decompression bomb antes do decode

_AVATAR_PREFIX_RE = re.compile(r"^data:image/(png|jpeg);base64,([A-Za-z0-9+/=\s]+)$", re.ASCII)


@dataclass(frozen=True)
class UserProfile:
    """Identidade local escolhida pelo usuário (nome de tratamento + avatar local PNG/JPEG)."""

    name: str = ""
    avatar_data_url: str = ""


DEFAULT_PROFILE = UserProfile()


def validate_profile_name(raw: Any, *, strict: bool = False) -> str:
    """Valida e normaliza o nome do perfil do usuário.

    Em modo `strict=True` (usado por `ui.preferences.set`), levanta `ValueError` se o tipo for
    inválido, contiver caracteres de controle/quebra de linha ou ultrapassar 40 caracteres.
    Em modo `strict=False` (leitura tolerante do disco), descarta valores inválidos para `""`.
    """
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str):
        if strict:
            raise ValueError("O nome do perfil deve ser texto.")
        return ""
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        if strict:
            raise ValueError("O nome do perfil não pode conter quebras de linha ou caracteres de controle.")
        raw = " ".join(raw.split())
    normalized = " ".join(raw.split()).strip()
    if len(normalized) > MAX_PROFILE_NAME_CHARS:
        if strict:
            raise ValueError(f"O nome do perfil deve ter no máximo {MAX_PROFILE_NAME_CHARS} caracteres.")
        normalized = normalized[:MAX_PROFILE_NAME_CHARS].rstrip()
    return normalized


def validate_avatar_data_url(raw: Any, *, strict: bool = False) -> str:
    """Valida Data URL de avatar local (`data:image/png;base64,...` ou `data:image/jpeg;base64,...`).

    Garantias:
    - Rejeita URLs remotas (`http:`, `https:`, `file:`), SVG, GIF, WebP ou caminhos de arquivo.
    - Verifica limites de caracteres (`MAX_PROFILE_AVATAR_CHARS`) e de bytes (`MAX_PROFILE_AVATAR_BYTES`)
      antes de decodificar a imagem.
    - Usa Pillow (`Image.open` + checagem de formato real PNG/JPEG + dimensões `<= 512x512` antes do
      decode + `verify()` + decode completo `load()`) para recusar imagens corrompidas ou bombas de
      descompressão.
    """
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str):
        if strict:
            raise ValueError("A foto do perfil deve ser uma Data URL PNG ou JPEG válida.")
        return ""
    cleaned = raw.strip()
    if not cleaned:
        return ""
    if len(cleaned) > MAX_PROFILE_AVATAR_CHARS:
        if strict:
            raise ValueError(
                f"A foto do perfil excede o limite de {MAX_PROFILE_AVATAR_CHARS} caracteres."
            )
        return ""
    match = _AVATAR_PREFIX_RE.fullmatch(cleaned)
    if not match:
        if strict:
            raise ValueError(
                "Formato de foto não suportado: use apenas Data URL PNG ou JPEG local (sem SVG, WebP, GIF ou URL remota)."
            )
        return ""
    declared_subtype, b64_body = match.group(1), re.sub(r"\s+", "", match.group(2))
    if not b64_body:
        if strict:
            raise ValueError("A foto do perfil está vazia.")
        return ""
    try:
        binary = base64.b64decode(b64_body, validate=True)
    except (binascii.Error, ValueError) as exc:
        if strict:
            raise ValueError("Codificação base64 da foto do perfil é inválida.") from exc
        return ""
    if not binary or len(binary) > MAX_PROFILE_AVATAR_BYTES:
        if strict:
            raise ValueError(
                f"A foto do perfil excede o limite de {MAX_PROFILE_AVATAR_BYTES} bytes."
            )
        return ""

    expected_format = "PNG" if declared_subtype == "png" else "JPEG"
    if expected_format == "PNG" and not binary.startswith(b"\x89PNG\r\n\x1a\n"):
        if strict:
            raise ValueError("Conteúdo da foto não corresponde a um arquivo PNG válido.")
        return ""
    if expected_format == "JPEG" and not binary.startswith(b"\xff\xd8\xff"):
        if strict:
            raise ValueError("Conteúdo da foto não corresponde a um arquivo JPEG válido.")
        return ""

    # Validação real com Pillow sem mutar variáveis globais do módulo (seguro para leituras concorrentes):
    # `Image.open` lê apenas o cabeçalho; checamos formato e dimensões (<= 512x512, <= 262144 pixels)
    # antes de `verify()` e antes do decode completo em `load()`.
    try:
        with Image.open(io.BytesIO(binary), formats=["PNG", "JPEG"]) as img:
            actual_format = (img.format or "").upper()
            width, height = img.size
            if actual_format != expected_format:
                raise ValueError("Formato interno da imagem difere do tipo declarado.")
            if (
                width <= 0
                or height <= 0
                or width > MAX_PROFILE_AVATAR_DIMENSION
                or height > MAX_PROFILE_AVATAR_DIMENSION
                or (width * height) > MAX_PROFILE_AVATAR_PIXELS
            ):
                raise ValueError(
                    f"Dimensões da foto ({width}x{height}) excedem o limite de "
                    f"{MAX_PROFILE_AVATAR_DIMENSION}x{MAX_PROFILE_AVATAR_DIMENSION} pixels."
                )
            img.verify()

        with Image.open(io.BytesIO(binary), formats=["PNG", "JPEG"]) as img2:
            img2.load()
    except Exception as exc:
        if strict:
            if isinstance(exc, ValueError) and ("Dimensões" in str(exc) or "Formato" in str(exc)):
                raise
            raise ValueError("Imagem do perfil inválida, truncada ou corrompida.") from exc
        return ""

    canonical_b64 = base64.b64encode(binary).decode("ascii")
    canonical_url = f"data:image/{declared_subtype};base64,{canonical_b64}"
    if len(canonical_url) > MAX_PROFILE_AVATAR_CHARS:
        if strict:
            raise ValueError(
                f"A foto do perfil excede o limite de {MAX_PROFILE_AVATAR_CHARS} caracteres."
            )
        return ""
    return canonical_url


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
    profile: UserProfile = field(default_factory=UserProfile)
    # Lunmi's own motion setting; Windows "animation effects" no longer decide it.
    reduce_motion: bool = False
    # A introdução (boas-vindas + cérebro) já foi concluída ou saltada.
    onboarding_done: bool = False
    # Optional diagnostic card; answers and safety controls are always independent.
    show_task_results: bool = False


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
    raw_profile = payload.get("profile")
    profile = DEFAULT_PROFILE
    if isinstance(raw_profile, dict):
        profile = UserProfile(
            name=validate_profile_name(raw_profile.get("name"), strict=False),
            avatar_data_url=validate_avatar_data_url(raw_profile.get("avatar_data_url"), strict=False),
        )
    return Preferences(window=rect, scale_pct=scale_pct,
                       actions=tuple(actions) or DEFAULT_ACTIONS, theme=theme, profile=profile,
                       reduce_motion=payload.get("reduce_motion") is True,
                       onboarding_done=payload.get("onboarding_done") is True,
                       show_task_results=payload.get("show_task_results") is True)


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
                       "reduce_motion": prefs.reduce_motion,
                       "onboarding_done": prefs.onboarding_done,
                       "show_task_results": prefs.show_task_results,
                       "actions": [{"label": a.label, "draft": a.draft} for a in prefs.actions],
                       "profile": {"name": prefs.profile.name,
                                   "avatar_data_url": prefs.profile.avatar_data_url}},
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

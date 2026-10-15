"""Contrato semântico de temas entre Tk legado e Electron.

O pedido celestial de 2026-10-14 separou a paleta Electron da UI Tk. O pedido de temas de
2026-10-15 acrescentou uma paleta clara. Cores iguais deixaram de ser o contrato; os papéis
semânticos e pisos de contraste continuam comuns. A suíte TS verifica cada tema registrado.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY_THEME = ROOT / "app" / "ui" / "theme.py"
TS_THEME = ROOT / "desktop" / "src" / "shared" / "theme.ts"

# Paleta compartilhada: constante Python -> chave camelCase do TS.
SHARED_PALETTE: dict[str, str] = {
    "BG_WINDOW": "bgWindow",
    "BG_SIDEBAR": "bgSidebar",
    "BG_CHAT": "bgChat",
    "BG_CARD": "bgCard",
    "BG_CARD_SUBTLE": "bgCardSubtle",
    "BG_USER": "bgUser",
    "BG_INPUT": "bgInput",
    "BORDER_SUBTLE": "borderSubtle",
    "BORDER_HAIRLINE": "borderHairline",
    "BORDER_ON_USER": "borderOnUser",
    "TEXT_PRIMARY": "textPrimary",
    "TEXT_SECONDARY": "textSecondary",
    "TEXT_MUTED": "textMuted",
    "TEXT_MUTED_ON_USER": "textMutedOnUser",
    "ACCENT": "accent",
    "ACCENT_HOVER": "accentHover",
    "ACCENT_PRESSED": "accentPressed",
    "ON_ACCENT": "onAccent",
    "SUCCESS": "success",
    "WARNING": "warning",
    "ERROR": "error",
    "DISABLED": "disabled",
    "SURFACE_HOVER": "surfaceHover",
}

# Pisos WCAG compartilhados (o decorativo existe só no Tk e não é compartilhado).
SHARED_FLOORS: dict[str, str] = {
    "TEXT_FLOOR": "TEXT_FLOOR",
    "LARGE_TEXT_FLOOR": "LARGE_TEXT_FLOOR",
    "GRAPHIC_FLOOR": "GRAPHIC_FLOOR",
}


def _python_values() -> dict[str, str]:
    text = PY_THEME.read_text(encoding="utf-8")
    return dict(re.findall(r'^([A-Z][A-Z0-9_]*)\s*=\s*"?(#[0-9A-Fa-f]{6}|\d+\.\d+)"?', text, re.M))


def _typescript_values() -> dict[str, str]:
    """Lê a paleta celestial completa, sem misturá-la às paletas adicionais."""
    text = TS_THEME.read_text(encoding="utf-8")
    palette = re.search(r'export const CELESTIAL_COLORS = \{(.*?)\} as const;', text, re.S)
    assert palette is not None, "registro perdeu a paleta celestial"
    object_entries = re.findall(r'^\s*([a-zA-Z]+):\s*"(#[0-9A-Fa-f]{6})",', palette.group(1), re.M)
    const_entries = re.findall(r'^\s*export const ([A-Z_][A-Z0-9_]*)\s*=\s*(#[0-9A-Fa-f]{6}|\d+\.\d+);', text, re.M)
    return dict(object_entries + const_entries)


def test_shared_semantic_color_roles_exist_in_tk_and_electron() -> None:
    python_values = _python_values()
    typescript_values = _typescript_values()

    missing_python = [name for name in SHARED_PALETTE if name not in python_values]
    missing_typescript = [key for key in SHARED_PALETTE.values() if key not in typescript_values]
    assert not missing_python, f"app/ui/theme.py perdeu: {missing_python}"
    assert not missing_typescript, f"desktop/src/shared/theme.ts perdeu: {missing_typescript}"

    for name, key in SHARED_PALETTE.items():
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", python_values[name])
        assert re.fullmatch(r"#[0-9A-Fa-f]{6}", typescript_values[key])


def test_shared_contrast_floors_match_between_tk_theme_and_electron_theme() -> None:
    python_values = _python_values()
    typescript_values = _typescript_values()

    divergent = {
        name: (python_values.get(name), typescript_values.get(key))
        for name, key in SHARED_FLOORS.items()
        if python_values.get(name) != typescript_values.get(key)
    }
    assert not divergent, f"pisos de contraste divergentes: {divergent}"

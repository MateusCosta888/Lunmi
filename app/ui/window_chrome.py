"""Cores da barra de título nativa do Windows via DWM, sem tocar na moldura da janela.

Só pede ao DWM que pinte a barra de título padrão: modo escuro e as cores de legenda, texto e
contorno do tema. Não muda registro, estilo da janela, `overrideredirect` nem desenha barra
própria. Fora do Windows, ou com o Tcl já destruído, não faz nada e devolve o que conseguiu.

Referência: https://learn.microsoft.com/en-us/windows/win32/api/dwmapi/ne-dwmapi-dwmwindowattribute
"""
from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from ctypes import wintypes

from app.ui import theme

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
GA_ROOT = 2
E_FAIL = -2147467259  # 0x80004005, devolvido quando a chamada nativa não pode ser feita

_ATTRIBUTES = (
    ("dark_mode", DWMWA_USE_IMMERSIVE_DARK_MODE, None),
    ("caption", DWMWA_CAPTION_COLOR, theme.BG_WINDOW),
    ("text", DWMWA_TEXT_COLOR, theme.TEXT_PRIMARY),
    ("border", DWMWA_BORDER_COLOR, theme.BORDER_HAIRLINE),
)


def colorref(hex_color: str) -> int:
    """Converte `#RRGGBB` para COLORREF, que o Win32 espera em ordem BGR (0x00BBGGRR)."""
    digits = hex_color.lstrip("#")
    red, green, blue = (int(digits[i:i + 2], 16) for i in (0, 2, 4))
    return (blue << 16) | (green << 8) | red


def _is_windows() -> bool:
    return sys.platform == "win32"


def _root_hwnd(winid: int) -> int:
    """`winfo_id` devolve a janela filha do Tk; a moldura que o DWM pinta é a raiz dela."""
    user32 = ctypes.windll.user32
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    return int(user32.GetAncestor(winid, GA_ROOT) or 0)


def _dwm_set(hwnd: int, attribute: int, value: ctypes.Structure | ctypes._SimpleCData) -> int:
    dwmapi = ctypes.windll.dwmapi
    dwmapi.DwmSetWindowAttribute.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
    ]
    dwmapi.DwmSetWindowAttribute.restype = ctypes.HRESULT
    return int(dwmapi.DwmSetWindowAttribute(
        hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)))


def apply(root: tk.Misc) -> dict[str, int]:
    """Aplica o tema à barra de título de `root`.

    Devolve `hwnd` (0 se não houve janela nativa) e, para cada atributo, o HRESULT da chamada
    ao DWM. Um HRESULT negativo indica falha; `E_FAIL` marca chamada que nem chegou a ocorrer.
    """
    if not _is_windows():
        return {}
    try:
        winid = root.winfo_id()
    except tk.TclError:
        return {}

    result: dict[str, int] = {}
    try:
        hwnd = _root_hwnd(winid)
    except OSError:
        hwnd = 0
    result["hwnd"] = hwnd
    if not hwnd:
        return result

    for key, attribute, color in _ATTRIBUTES:
        if color is None:
            value: ctypes._SimpleCData = wintypes.BOOL(True)
        else:
            value = wintypes.DWORD(colorref(color))
        try:
            result[key] = _dwm_set(hwnd, attribute, value)
        except OSError:
            result[key] = E_FAIL
    return result

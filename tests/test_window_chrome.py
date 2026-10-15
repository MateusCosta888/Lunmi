"""Testes de `app.ui.window_chrome` com duplos: sem janela Tk, sem DWM, sem desktop."""
from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ui import theme, window_chrome  # noqa: E402


class FakeRoot:
    def __init__(self, winid: int = 0x1234, destroyed: bool = False) -> None:
        self._winid = winid
        self._destroyed = destroyed

    def winfo_id(self) -> int:
        if self._destroyed:
            raise tk.TclError("can't invoke \"winfo\" command: application has been destroyed")
        return self._winid


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, object]]:
    recorded: list[tuple[int, int, object]] = []
    monkeypatch.setattr(window_chrome, "_is_windows", lambda: True)
    monkeypatch.setattr(window_chrome, "_root_hwnd", lambda winid: 0xABC0 + winid)

    def fake_set(hwnd, attribute, value):
        recorded.append((hwnd, attribute, value.value))
        return 0

    monkeypatch.setattr(window_chrome, "_dwm_set", fake_set)
    return recorded


def test_colorref_is_bgr_order() -> None:
    assert window_chrome.colorref("#06101E") == 0x001E1006
    assert window_chrome.colorref("#FF0000") == 0x000000FF
    assert window_chrome.colorref("#00FF00") == 0x0000FF00
    assert window_chrome.colorref("#0000FF") == 0x00FF0000


def test_apply_sets_four_attributes_with_theme_colors(calls) -> None:
    result = window_chrome.apply(FakeRoot(winid=0x10))

    assert result["hwnd"] == 0xABC0 + 0x10
    assert [(attr, val) for _, attr, val in calls] == [
        (20, 1),
        (35, window_chrome.colorref(theme.BG_WINDOW)),
        (36, window_chrome.colorref(theme.TEXT_PRIMARY)),
        (34, window_chrome.colorref(theme.BORDER_HAIRLINE)),
    ]
    assert {h for h, _, _ in calls} == {result["hwnd"]}


def test_apply_reports_hresult_per_attribute(monkeypatch) -> None:
    monkeypatch.setattr(window_chrome, "_is_windows", lambda: True)
    monkeypatch.setattr(window_chrome, "_root_hwnd", lambda winid: 77)
    codes = {20: 0, 35: 0, 36: -2147024809, 34: 0}  # 36 falha com E_INVALIDARG
    monkeypatch.setattr(window_chrome, "_dwm_set", lambda h, a, v: codes[a])

    result = window_chrome.apply(FakeRoot())

    assert result == {"hwnd": 77, "dark_mode": 0, "caption": 0,
                      "text": -2147024809, "border": 0}


def test_apply_marks_failed_native_call_as_e_fail(monkeypatch) -> None:
    monkeypatch.setattr(window_chrome, "_is_windows", lambda: True)
    monkeypatch.setattr(window_chrome, "_root_hwnd", lambda winid: 5)

    def raising(h, a, v):
        if a == 35:
            raise OSError("dwmapi indisponível")
        return 0

    monkeypatch.setattr(window_chrome, "_dwm_set", raising)
    result = window_chrome.apply(FakeRoot())

    assert result["caption"] == window_chrome.E_FAIL
    assert result["dark_mode"] == 0


def test_apply_without_native_window_makes_no_dwm_call(monkeypatch) -> None:
    monkeypatch.setattr(window_chrome, "_is_windows", lambda: True)
    monkeypatch.setattr(window_chrome, "_root_hwnd", lambda winid: 0)

    def must_not_run(*_):
        raise AssertionError("DWM não deve ser chamado sem hwnd")

    monkeypatch.setattr(window_chrome, "_dwm_set", must_not_run)
    assert window_chrome.apply(FakeRoot()) == {"hwnd": 0}


def test_apply_outside_windows_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(window_chrome, "_is_windows", lambda: False)

    def must_not_run(*_):
        raise AssertionError("fora do Windows nada deve ser tocado")

    monkeypatch.setattr(window_chrome, "_root_hwnd", must_not_run)
    assert window_chrome.apply(FakeRoot()) == {}


def test_apply_after_tcl_destroyed_degrades_without_raising(calls) -> None:
    assert window_chrome.apply(FakeRoot(destroyed=True)) == {}
    assert calls == []

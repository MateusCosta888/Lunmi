"""Avisa quando há um clique do rato no Windows, enquanto o chat do Node está aberto.

O chat aberto por voz nunca recebe foco, por isso o `blur` não chega e clicar fora não fechava.
O Electron não expõe cliques fora das suas janelas. Lê-se o estado do botão, sem hook global:
um hook passaria cada movimento do rato por Python. Nunca lê posição, janela nem teclado.
"""
from __future__ import annotations

import ctypes
import threading
from typing import Callable

POLL_SECONDS = 0.04
MOUSE_BUTTONS = (0x01, 0x02)  # VK_LBUTTON, VK_RBUTTON
PRESSED_NOW_OR_SINCE_LAST_READ = 0x8001


class ClickWatch:
    def __init__(self, emit: Callable[[], None]) -> None:
        self._emit = emit
        self._stop: threading.Event | None = None

    def set(self, active: bool) -> None:
        if not active:
            if self._stop is not None:
                self._stop.set()
            self._stop = None
        elif self._stop is None:
            self._stop = threading.Event()
            threading.Thread(target=self._run, args=(self._stop,), name="node-click-watch", daemon=True).start()

    def _run(self, stop: threading.Event) -> None:
        read = ctypes.windll.user32.GetAsyncKeyState  # type: ignore[attr-defined]
        read.restype = ctypes.c_short
        pressed = self._pressed(read)  # descarta o clique que abriu o chat
        while not stop.wait(POLL_SECONDS):
            now = self._pressed(read)
            if now and not pressed:
                self._emit()
            pressed = now

    @staticmethod
    def _pressed(read: Callable[[int], int]) -> bool:
        # A soma, e não `any`: cada botão tem de ser lido sempre, para limpar o seu "desde a última".
        return sum(1 for button in MOUSE_BUTTONS if read(button) & PRESSED_NOW_OR_SINCE_LAST_READ) > 0

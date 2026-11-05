"""Clique fora do chat do Node: um aviso por clique, e o estado do botão lido sempre."""
from __future__ import annotations

import pytest

from app.bridge import click_watch, protocol
from app.bridge.click_watch import ClickWatch


def test_every_button_is_read_so_old_presses_do_not_linger() -> None:
    reads: list[int] = []

    def read(button: int) -> int:
        reads.append(button)
        return 0x8001

    assert ClickWatch._pressed(read) is True
    assert reads == list(click_watch.MOUSE_BUTTONS)


def test_a_click_is_reported_once_and_watching_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    states = iter([0, 0, 0x8000, 0x8000, 0x8000, 0x8000, 0, 0])
    clicks: list[int] = []
    watch = ClickWatch(lambda: clicks.append(1))

    class Stop:
        polls = 0

        def wait(self, _seconds: float) -> bool:
            Stop.polls += 1
            return Stop.polls > 3

    class Read:
        restype = None

        def __call__(self, _button: int) -> int:
            return next(states, 0)

    user32 = type("User32", (), {"GetAsyncKeyState": Read()})
    monkeypatch.setattr(click_watch.ctypes, "windll", type("Windll", (), {"user32": user32}), raising=False)
    watch._run(Stop())  # type: ignore[arg-type]
    assert clicks == [1]


def test_the_command_exists_and_the_event_carries_nothing() -> None:
    assert "node.clicks.watch" in protocol.COMMAND_TYPES
    assert "node.click" in protocol.EVENT_TYPES

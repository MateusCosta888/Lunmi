"""VOICE REFINEMENTS V2 — autómato puro do helper Windows (`app/voice/hotkey_worker.py`).

Nenhum hook real, nenhum `ctypes.windll`, nenhuma tecla física: os eventos são injetados no
autómato. Provam a gramática V2, o matching por conjunto, repetição vs tecla extra, AltGr/input
injetado, pause/resume/configure e a garantia de que nenhuma linha carrega teclas brutas.
"""
from __future__ import annotations

import pytest

from app.voice.hotkey_worker import (
    AcceleratorError,
    HoldStateMachine,
    KeyEvent,
    apply_command,
    canonical_accelerator,
    parse_accelerator,
)

CTRL = 0xA2
CTRL_RIGHT = 0xA3
ALT = 0xA4
ALT_RIGHT = 0xA5
SHIFT = 0xA0
V = 0x56
F5 = 0x74


def event(vk: int, *, up: bool = False, ext: bool = False, injected: bool = False) -> KeyEvent:
    return KeyEvent(vk=vk, extended=ext, injected=injected, keyup=up)


class Harness:
    def __init__(self, accelerator: str = "Ctrl+Alt") -> None:
        self.machine = HoldStateMachine(parse_accelerator(accelerator))

    def down(self, vk: int, **kwargs) -> list[dict]:
        return self.machine.process(event(vk, **kwargs))

    def up(self, vk: int, **kwargs) -> list[dict]:
        return self.machine.process(event(vk, up=True, **kwargs))


@pytest.fixture
def h() -> Harness:
    return Harness()


def kinds(lines: list[dict]) -> list[str]:
    return [line["type"] for line in lines]


def test_parser_accepts_v2_and_v1_and_preserves_set_not_order() -> None:
    assert canonical_accelerator("Ctrl+Alt") == "Ctrl+Alt"
    assert canonical_accelerator("Alt+Ctrl") == "Ctrl+Alt"
    assert canonical_accelerator("V+Ctrl") == "Ctrl+V"
    assert canonical_accelerator("Ctrl+Alt+V") == "Ctrl+Alt+V"
    assert canonical_accelerator("Control+Meta+A") == "Ctrl+Super+A"
    assert canonical_accelerator("Shift+Ctrl+F5") == "Ctrl+Shift+F5"
    assert canonical_accelerator("Super+Space") == "Super+Space"
    assert canonical_accelerator("Ctrl+Alt+Shift+Super") == "Ctrl+Alt+Shift+Super"
    assert canonical_accelerator("Ctrl+Alt+Shift+Super+F24") == "Ctrl+Alt+Shift+Super+F24"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "Ctrl",
        "Alt",
        "Ctrl+",
        "+Ctrl+Alt",
        "Ctrl++Alt",
        "ctrl+alt",
        "Ctrl+Alt+",
        "Ctrl+Alt+V+W",
        "Ctrl+Alt+Ctrl",
        "Ctrl+Control+A",
        "Alt+Alt+V",
        "Ctrl+Alt+Delete",
        "Ctrl+Alt+F25",
        "Ctrl+Alt+RightAlt",
        "Ctrl+Alt+Shift+Super+Meta+A",
        "A",
        None,
        42,
    ],
)
def test_parser_rejects_invalid_duplicates_and_unknown_keys(raw: object) -> None:
    with pytest.raises(AcceleratorError):
        parse_accelerator(raw)


def test_modifier_only_press_release_and_repeat_ignored(h: Harness) -> None:
    assert h.down(CTRL) == []
    assert kinds(h.down(ALT)) == ["pressed"]
    assert h.down(ALT) == []  # auto-repeat da mesma tecla não cria segundo turno
    assert kinds(h.up(CTRL)) == ["released"]
    assert h.up(ALT) == []
    assert h.up(CTRL) == []


def test_matching_is_order_independent(h: Harness) -> None:
    assert h.down(ALT) == []
    assert kinds(h.down(CTRL)) == ["pressed"]
    assert kinds(h.up(ALT)) == ["released"]


def test_modifier_plus_key_matches_any_physical_order() -> None:
    h = Harness("Ctrl+V")
    assert h.down(V) == []
    assert kinds(h.down(CTRL)) == ["pressed"]
    assert h.down(V) == []
    assert kinds(h.up(V)) == ["released"]
    assert h.up(CTRL) == []


def test_any_required_release_closes_once() -> None:
    h = Harness("Ctrl+Alt+Shift")
    h.down(CTRL)
    h.down(ALT)
    assert kinds(h.down(SHIFT)) == ["pressed"]
    assert kinds(h.up(ALT)) == ["released"]
    assert h.up(CTRL) == []
    assert h.up(SHIFT) == []


def test_extra_key_during_chord_cancels_and_never_releases(h: Harness) -> None:
    h.down(CTRL)
    h.down(ALT)
    assert kinds(h.down(V)) == ["cancelled"]
    assert h.down(V) == []  # repetição da mesma tecla extra não repete o cancelamento
    assert h.up(V) == []
    assert h.up(ALT) == []
    assert h.up(CTRL) == []
    # O gesto cancelado não liberta release; um novo gesto completo sim.
    assert h.down(CTRL) == []
    assert kinds(h.down(ALT)) == ["pressed"]
    assert kinds(h.up(CTRL)) == ["released"]


def test_extra_key_held_before_chord_blocks_until_all_keys_up() -> None:
    h = Harness()
    assert h.down(SHIFT) == []
    assert h.down(CTRL) == []
    assert h.down(ALT) == []  # tecla extra ainda down: não arma
    assert h.up(SHIFT) == []  # só rearma com todas as teclas livres
    assert h.up(CTRL) == []
    assert h.up(ALT) == []
    assert h.down(CTRL) == []
    assert kinds(h.down(ALT)) == ["pressed"]


def test_right_alt_altgr_never_satisfies_alt(h: Harness) -> None:
    assert h.down(CTRL) == []
    assert h.down(ALT_RIGHT) == []
    assert h.up(ALT_RIGHT) == []
    assert h.down(ALT) == []  # RightAlt já poluiu o gesto até tudo subir
    assert h.up(ALT) == []
    assert h.up(CTRL) == []
    assert h.down(CTRL) == []
    assert kinds(h.down(ALT)) == ["pressed"]


def test_injected_input_never_arms_and_cancels_armed_gesture(h: Harness) -> None:
    assert h.down(CTRL) == []
    assert h.down(ALT, injected=True) == []
    assert h.down(ALT) == []  # bloqueado enquanto Ctrl físico estiver down
    assert h.up(ALT) == []
    assert h.up(CTRL) == []
    assert h.down(CTRL) == []
    assert kinds(h.down(ALT)) == ["pressed"]
    assert kinds(h.down(V, injected=True)) == ["cancelled"]
    assert h.up(ALT) == []
    assert h.up(CTRL) == []


def test_injected_keyup_does_not_release_physical_key(h: Harness) -> None:
    h.down(CTRL)
    assert h.down(ALT) == [{"type": "pressed"}]
    assert h.up(CTRL, injected=True) == []
    assert kinds(h.up(ALT)) == ["released"]
    assert h.up(CTRL) == []


def test_pause_cancels_and_resume_only_after_keys_up(h: Harness) -> None:
    h.down(CTRL)
    h.down(ALT)
    assert kinds(h.machine.pause()) == ["cancelled"]
    assert h.machine.resume() == []
    assert h.down(SHIFT) == []
    assert h.up(SHIFT) == []
    assert h.up(CTRL) == []
    assert h.up(ALT) == []
    assert h.down(CTRL) == []
    assert kinds(h.down(ALT)) == ["pressed"]


def test_configure_during_gesture_cancels_and_rearms_only_after_free(h: Harness) -> None:
    h.down(CTRL)
    h.down(ALT)
    reconfigured = h.machine.configure(parse_accelerator("Ctrl+V"))
    assert kinds(reconfigured) == ["cancelled"]
    assert reconfigured[0]["reason"] == "reconfigured"
    assert h.up(CTRL) == []
    assert h.up(ALT) == []
    assert h.down(V) == []
    assert kinds(h.down(CTRL)) == ["pressed"]


def test_shutdown_cancels_but_never_releases(h: Harness) -> None:
    h.down(CTRL)
    h.down(ALT)
    assert kinds(h.machine.shutdown()) == ["cancelled"]
    assert h.machine.shutdown() == []
    assert h.up(CTRL) == []
    assert h.up(ALT) == []


def test_release_requires_all_keys_up_before_new_turn(h: Harness) -> None:
    h.down(CTRL)
    h.down(ALT)
    assert kinds(h.up(CTRL)) == ["released"]
    assert h.down(CTRL) == []  # Alt ainda down: não rearma
    assert h.up(ALT) == []
    assert h.up(CTRL) == []  # todas livres: limpa o bloqueio
    assert h.down(CTRL) == []
    assert kinds(h.down(ALT)) == ["pressed"]


def test_unknown_key_is_extra_not_a_second_turn(h: Harness) -> None:
    h.down(CTRL)
    h.down(ALT)
    assert kinds(h.down(0x2C)) == ["cancelled"]  # PrintScreen: extra
    assert h.up(0x2C) == []
    assert h.up(CTRL) == []
    assert h.up(ALT) == []


def test_apply_command_is_a_closed_protocol_without_raw_keys(h: Harness) -> None:
    lines, stop = apply_command(
        h.machine, {"type": "configure", "accelerator": "Alt+Ctrl+V"}
    )
    assert lines == [] and stop is False
    lines, stop = apply_command(h.machine, {"type": "configure", "accelerator": "Ctrl+"})
    assert lines == [{"type": "error", "code": "invalid_accelerator"}]
    assert stop is False
    lines, stop = apply_command(h.machine, {"type": "unknown"})
    assert lines == [] and stop is False
    lines, stop = apply_command(h.machine, {"type": "shutdown"})
    assert lines == [] and stop is True


def test_protocol_lines_never_carry_vk_scan_text_or_timestamp(h: Harness) -> None:
    h.down(CTRL)
    lines = h.down(ALT)
    lines += h.machine.pause()
    assert lines
    forbidden = {"vk", "vkCode", "scan", "scanCode", "text", "key", "timestamp", "time"}
    for line in lines:
        assert set(line).isdisjoint(forbidden)

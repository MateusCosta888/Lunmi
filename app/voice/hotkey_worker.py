"""Helper Windows do atalho de ditado hold (`app/voice/hotkey_worker.py`).

Fronteira (`.agents/VOICE-REFINEMENTS-V2-CONTRACT.md` §4.4): processo owned, stdlib apenas,
`WH_KEYBOARD_LL`, sem polling em repouso, sem texto, sem persistência de outras teclas, sem
`SendInput` e sem suprimir eventos do desktop. O helper transmite apenas fases fechadas:

    {"type":"ready","protocol":1}
    {"type":"pressed"}
    {"type":"released"}
    {"type":"cancelled","reason":"..."}
    {"type":"error","code":"..."}

O Electron é o único dono do processo. A ordem visual do atalho é preservada pelo main; o helper
recebe sempre o accelerator canónico e compara o **conjunto** de teclas, nunca a cronologia. O
estado transitório de teclas down existe apenas em RAM, para distinguir repetição de tecla extra;
nunca é escrito, logado ou transmitido.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
from dataclasses import dataclass
from typing import Any, Iterable

MAX_LINE_BYTES = 4096
PROTOCOL_VERSION = 1
CANCELLED_REASONS = frozenset({
    "extra_key", "injected", "pause", "reconfigured", "hook_lost", "blur",
    "shutdown", "lifecycle",
})
ERROR_CODES = frozenset({"hook_unavailable", "platform_unsupported", "invalid_accelerator"})

MODIFIER_ORDER = ("Ctrl", "Alt", "Shift", "Super")
MODIFIER_ALIASES = {
    "Ctrl": "Ctrl",
    "Control": "Ctrl",
    "Alt": "Alt",
    "Shift": "Shift",
    "Super": "Super",
    "Meta": "Super",
}
TERMINAL_KEYS = frozenset(
    {chr(code) for code in range(ord("A"), ord("Z") + 1)}
    | {chr(code) for code in range(ord("0"), ord("9") + 1)}
    | {f"F{number}" for number in range(1, 25)}
    | {"Space", "Tab", "Enter", "Escape", "Up", "Down", "Left", "Right"}
)

# Teclas virtuais usadas na normalização. O helper não guarda scan codes nem caracteres.
VK_CONTROL = 0x11
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_MENU = 0x12
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_SHIFT = 0x10
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LWIN = 0x5B
VK_RWIN = 0x5C
LLKHF_EXTENDED = 0x01
LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_INJECTED = 0x10


class AcceleratorError(ValueError):
    """Accelerator fora da gramática fechada V2."""


@dataclass(frozen=True)
class Accelerator:
    modifiers: tuple[str, ...]
    key: str | None

    @property
    def required(self) -> frozenset[str]:
        return frozenset(self.modifiers + ((self.key,) if self.key else ()))

    @property
    def canonical(self) -> str:
        return "+".join(self.modifiers + ((self.key,) if self.key else ()))


def parse_accelerator(raw: object) -> Accelerator:
    """Gramática V2: 2–4 modificadores sozinhos ou 1–4 modificadores + uma tecla final.

    Aceita a ordem visual (a captura física) e aliases V1 (`Control`, `Meta`), mas devolve o
    accelerator canónico estável para o helper. Duplicados/aliases duplicados são recusados.
    """
    if not isinstance(raw, str) or not raw or len(raw) > 64:
        raise AcceleratorError("accelerator ausente ou demasiado longo")
    parts = raw.split("+")
    if any(not part for part in parts):
        raise AcceleratorError("token vazio")
    modifiers: list[str] = []
    key: str | None = None
    for part in parts:
        canonical_modifier = MODIFIER_ALIASES.get(part)
        if canonical_modifier is not None:
            if canonical_modifier in modifiers:
                raise AcceleratorError("modificador duplicado")
            modifiers.append(canonical_modifier)
            continue
        if part in TERMINAL_KEYS:
            if key is not None:
                raise AcceleratorError("mais de uma tecla final")
            key = part
            continue
        raise AcceleratorError("tecla desconhecida")
    if key is None:
        if not 2 <= len(modifiers) <= 4:
            raise AcceleratorError("sem tecla: exige 2–4 modificadores")
    elif not 1 <= len(modifiers) <= 4:
        raise AcceleratorError("com tecla: exige 1–4 modificadores")
    ordered = tuple(modifier for modifier in MODIFIER_ORDER if modifier in modifiers)
    return Accelerator(ordered, key)


def canonical_accelerator(raw: object) -> str:
    return parse_accelerator(raw).canonical


@dataclass(frozen=True)
class KeyEvent:
    vk: int
    extended: bool = False
    injected: bool = False
    keyup: bool = False


def _terminal_token(vk: int) -> str | None:
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    return {
        0x20: "Space",
        0x09: "Tab",
        0x0D: "Enter",
        0x1B: "Escape",
        0x26: "Up",
        0x28: "Down",
        0x25: "Left",
        0x27: "Right",
    }.get(vk)


def physical_key(event: KeyEvent) -> tuple[str | None, str]:
    """Devolve (token, identidade física).

    `token` é o nome canónico usado no conjunto do atalho; `None` marca uma tecla que nunca
    pertence a um atalho suportado (tecla extra). A identidade distingue repetição da mesma tecla
    física sem transmitir o código.
    """
    vk = event.vk
    extended = event.extended
    if vk in (VK_CONTROL, VK_LCONTROL):
        return "Ctrl", "CtrlR" if vk == VK_CONTROL and extended else "CtrlL"
    if vk == VK_RCONTROL:
        return "Ctrl", "CtrlR"
    if vk == VK_MENU:
        return ("Alt", "AltL") if not extended else (None, "AltR")
    if vk == VK_LMENU:
        return "Alt", "AltL"
    if vk == VK_RMENU:
        return None, "AltR"
    if vk == VK_SHIFT:
        return ("Shift", "ShiftR") if extended else ("Shift", "ShiftL")
    if vk == VK_LSHIFT:
        return "Shift", "ShiftL"
    if vk == VK_RSHIFT:
        return "Shift", "ShiftR"
    if vk == VK_LWIN:
        return "Super", "WinL"
    if vk == VK_RWIN:
        return "Super", "WinR"
    token = _terminal_token(vk)
    if token is not None:
        return token, f"key:{token}"
    return None, f"vk:{vk}"


class HoldStateMachine:
    """Autómato puro: eventos de teclado → fases fechadas, sem I/O e sem teclas brutas.

    O estado `_down` guarda apenas identidades físicas transitórias (para repetição/extra). O
    `released` só existe depois de um `pressed` do mesmo gesto; nunca é sintetizado no shutdown.
    """

    def __init__(self, accelerator: Accelerator | str) -> None:
        self.chord = (
            parse_accelerator(accelerator) if isinstance(accelerator, str) else accelerator
        )
        self._down: dict[str, str | None] = {}
        self._pressed = False
        self._blocked = False
        self._paused = False

    @property
    def pressed(self) -> bool:
        return self._pressed

    def _line(self, kind: str, **extra: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": kind}
        payload.update(extra)
        return payload

    def _required_satisfied(self) -> bool:
        tokens = {token for token in self._down.values() if token is not None}
        return self.chord.required <= tokens

    def _has_extra(self) -> bool:
        required = self.chord.required
        return any(token is None or token not in required for token in self._down.values())

    def _maybe_press(self) -> list[dict[str, Any]]:
        if self._pressed or self._paused or self._blocked:
            return []
        if self._required_satisfied() and not self._has_extra():
            self._pressed = True
            return [self._line("pressed")]
        return []

    def _cancel(self, reason: str) -> list[dict[str, Any]]:
        if reason not in CANCELLED_REASONS:
            return []
        if not self._pressed:
            return []
        self._pressed = False
        self._blocked = True
        return [self._line("cancelled", reason=reason)]

    def _clear_block_when_free(self) -> None:
        if not self._down:
            self._blocked = False

    def process(self, event: KeyEvent) -> list[dict[str, Any]]:
        if event.injected:
            if event.keyup:
                return []
            if self._pressed:
                return self._cancel("injected")
            self._blocked = True
            self._clear_block_when_free()
            return []

        token, identity = physical_key(event)
        if event.keyup:
            if identity not in self._down:
                return []
            released_token = self._down.pop(identity)
            lines: list[dict[str, Any]] = []
            if self._pressed and released_token is not None and released_token in self.chord.required:
                remaining_same = any(
                    value == released_token for value in self._down.values()
                )
                if not remaining_same:
                    self._pressed = False
                    self._blocked = True
                    lines.append(self._line("released"))
            self._clear_block_when_free()
            return lines

        if identity in self._down:
            # Auto-repeat da MESMA tecla física: não cria turno nem cancela.
            return []
        self._down[identity] = token
        if self._pressed:
            if token is None or token not in self.chord.required:
                return self._cancel("extra_key")
            return []
        if token is None or token not in self.chord.required:
            self._blocked = True
            return []
        return self._maybe_press()

    def cancel(self, reason: str) -> list[dict[str, Any]]:
        return self._cancel(reason)

    def pause(self) -> list[dict[str, Any]]:
        lines = self._cancel("pause")
        self._paused = True
        self._blocked = True
        return lines

    def resume(self) -> list[dict[str, Any]]:
        self._paused = False
        self._blocked = bool(self._down)
        return []

    def configure(self, accelerator: Accelerator) -> list[dict[str, Any]]:
        if accelerator == self.chord:
            return []
        lines = self._cancel("reconfigured")
        self.chord = accelerator
        # A nova combinação só arma depois de todas as teclas físicas subirem.
        self._blocked = True
        self._clear_block_when_free()
        return lines

    def shutdown(self) -> list[dict[str, Any]]:
        lines = self._cancel("shutdown")
        self._pressed = False
        return lines


def apply_command(
    machine: HoldStateMachine, message: object
) -> tuple[list[dict[str, Any]], bool]:
    """Aplica uma linha de stdin. Devolve (linhas de saída, deve_encerrar)."""
    if not isinstance(message, dict):
        return [], False
    kind = message.get("type")
    if kind == "configure":
        try:
            accelerator = parse_accelerator(message.get("accelerator"))
        except (AcceleratorError, TypeError):
            return [{"type": "error", "code": "invalid_accelerator"}], False
        return machine.configure(accelerator), False
    if kind == "pause":
        return machine.pause(), False
    if kind == "resume":
        return machine.resume(), False
    if kind == "shutdown":
        return machine.shutdown(), True
    return [], False


def _encode_line(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _write_line(payload: dict[str, Any]) -> bool:
    data = _encode_line(payload)
    if len(data) > MAX_LINE_BYTES:
        return False
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Hook Windows (não é instanciado em testes puros; só o Root faz a prova real)
# ---------------------------------------------------------------------------

def _run_windows_hook(accelerator: Accelerator) -> int:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return 1

    WH_KEYBOARD_LL = 13
    HC_ACTION = 0
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    WM_QUIT = 0x0012

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hook_proc_type = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t
    )
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, hook_proc_type, ctypes.c_void_p, wintypes.DWORD
    ]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.CallNextHookEx.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t
    ]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT
    ]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
    ]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT,
        wintypes.UINT, wintypes.UINT,
    ]
    user32.PeekMessageW.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p

    machine = HoldStateMachine(accelerator)
    lock = threading.Lock()
    output: queue.SimpleQueue[dict[str, Any] | None] = queue.SimpleQueue()
    stop = threading.Event()
    thread_id = kernel32.GetCurrentThreadId()

    def post_quit() -> None:
        try:
            user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        except Exception:
            pass

    def writer() -> None:
        while True:
            item = output.get()
            if item is None:
                return
            if not _write_line(item):
                stop.set()
                post_quit()
                return

    def low_level_proc(n_code: int, w_param: int, l_param: int) -> int:
        try:
            if n_code == HC_ACTION:
                info = ctypes.cast(
                    l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)
                ).contents
                keyup = int(w_param) in (WM_KEYUP, WM_SYSKEYUP)
                keydown = int(w_param) in (WM_KEYDOWN, WM_SYSKEYDOWN)
                if keyup or keydown:
                    event = KeyEvent(
                        vk=int(info.vkCode),
                        extended=bool(int(info.flags) & LLKHF_EXTENDED),
                        injected=bool(
                            int(info.flags) & (LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED)
                        ),
                        keyup=keyup,
                    )
                    with lock:
                        lines = machine.process(event)
                    for line in lines:
                        output.put(line)
        except Exception:
            # O callback nunca pode derrubar o hook nem bloquear o desktop.
            pass
        return int(user32.CallNextHookEx(None, n_code, w_param, l_param))

    callback = hook_proc_type(low_level_proc)
    hook = user32.SetWindowsHookExW(
        WH_KEYBOARD_LL, callback, kernel32.GetModuleHandleW(None), 0
    )
    if not hook:
        _write_line({"type": "error", "code": "hook_unavailable"})
        return 1

    # Cria a fila de mensagens desta thread ANTES de publicar `ready`: um `shutdown`/EOF imediato
    # pode chamar `PostThreadMessageW(WM_QUIT)` antes de `GetMessageW`; sem fila, o post falhava
    # e o message pump ficava bloqueado para sempre.
    seed_message = wintypes.MSG()
    user32.PeekMessageW(ctypes.byref(seed_message), None, 0, 0, 0)

    # `ready` só depois do hook instalado, antes do message pump. A escrita síncrona garante a
    # ordem; o callback ainda não corre porque a thread não bombeia mensagens.
    if not _write_line({"type": "ready", "protocol": PROTOCOL_VERSION}):
        try:
            user32.UnhookWindowsHookEx(hook)
        except Exception:
            pass
        return 1

    writer_thread = threading.Thread(target=writer, name="hotkey-writer", daemon=True)
    writer_thread.start()

    def stdin_loop() -> None:
        try:
            for raw in sys.stdin:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                with lock:
                    lines, should_exit = apply_command(machine, message)
                for line in lines:
                    output.put(line)
                if should_exit:
                    post_quit()
                    return
        except (OSError, ValueError):
            pass
        post_quit()

    stdin_thread = threading.Thread(target=stdin_loop, name="hotkey-stdin", daemon=True)
    stdin_thread.start()

    message = wintypes.MSG()
    try:
        while True:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result == 0:
                break
            if result == -1:
                with lock:
                    lines = machine.cancel("hook_lost")
                for line in lines:
                    output.put(line)
                break
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
    except Exception:
        with lock:
            lines = machine.cancel("hook_lost")
        for line in lines:
            output.put(line)
    finally:
        try:
            user32.UnhookWindowsHookEx(hook)
        except Exception:
            pass
        output.put(None)
        writer_thread.join(timeout=1.0)
    return 0


def _parse_cli(argv: list[str]) -> str | None:
    accelerator: str | None = None
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--accelerator" and index + 1 < len(argv):
            accelerator = argv[index + 1]
            index += 2
            continue
        return None
    return accelerator


def main(argv: Iterable[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    raw = _parse_cli(arguments)
    if raw is None:
        _write_line({"type": "error", "code": "invalid_accelerator"})
        return 1
    try:
        accelerator = parse_accelerator(raw)
    except AcceleratorError:
        _write_line({"type": "error", "code": "invalid_accelerator"})
        return 1
    if sys.platform != "win32":
        _write_line({"type": "error", "code": "platform_unsupported"})
        return 1
    return _run_windows_hook(accelerator)


if __name__ == "__main__":
    raise SystemExit(main())

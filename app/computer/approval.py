"""Which actions need the user's word, and why — the runtime's single policy.

Vendor-free and desktop-free, like ``batches.py``, ``controls.py`` and ``protocol.py``: the
caller brings what it observed (the control's type, its accessible label when it has one,
the keys, the process, whether this task already typed into that field) and this module
returns a verdict that can be tested without Windows.

The reported behaviour this exists for (2026-10-09, CASO A/B/C in the chat): Enter was
classified as dangerous on its own, so *confirming a search* asked for authorisation and —
when the answer did not travel — the search never happened. Enter is not dangerous; what
Enter *commits* is. In a query field it commits a query (normal), in a message composer it
publishes text (sensitive), in a document it edits content (sensitive). The verdict below
is built from the control, the keys and what this task already did to that field.

Three levels, and the mode only moves the middle one:

``LOW``      proven normal: typing, navigating, selecting a tab or a list item, confirming
             a query with Enter, clicking a control nobody labelled as destructive.
``HIGH``     proven sensitive: keys that save/close/delete, a control whose own label says
             send/delete/save/close, replacing the contents of a document this task did not
             create, toggling a setting, driving a terminal.
``UNKNOWN``  not decidable from what was observed: an unknown tool, a shortcut with no
             context, Enter in a field the observation could not identify.

Manual mode asks about ``HIGH`` and ``UNKNOWN``; auto-approval runs ``UNKNOWN`` and still
asks about ``HIGH``. Auto-approval therefore never lowers the risk of an action — it only
answers the question the runtime would have asked because it could not *tell*.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MANUAL = "manual"
AUTO = "auto"
MODES = (MANUAL, AUTO)

LOW = "low"
HIGH = "high"
UNKNOWN = "unknown"

# Keys that commit whatever the focused field holds. Enter alone; a shortcut that ends in
# one of these is handled by the shortcut rules, not here.
_SUBMIT_KEYS = ("{enter}", "{return}", "{vk_return}", "{vk_enter}", "enter", "return")

# A control that turns typing into a message, a comment or a post: its own label says so.
# This is what keeps Enter safe in a query field and asks for it in a composer, and it is
# exactly the distinction the reported Discord/Steam cases needed.
_COMPOSER_WORDS = ("message", "mensagem", "reply", "responder", "resposta", "compose",
                   "comment", "comentário", "post", "publicar", "publish", "tweet",
                   "type a message", "escreva uma mensagem")

# A query field: the word list is UI vocabulary, not application vocabulary.
_SEARCH_WORDS = ("search", "pesquis", "buscar", "busca", "find", "procur", "query",
                 "filtro", "filter", "localizar", "type to search", "searchbox")

# Keys that save, close, quit, cut or force-close: their effect outlives the round.
_DESTRUCTIVE_KEYS = ("delete", "{del}", "%{f4}", "{f4}", "^s", "^+s", "^w", "^q", "^x", "^d")

# Shortcuts that only read or navigate. Everything else with a modifier is not *proven*
# safe, so it is ``UNKNOWN``: manual asks, auto-approval proceeds.
_LOW_SHORTCUTS = frozenset({"^a", "^c", "^f", "^n", "^z", "^y"})

# Keys that move around the interface without changing content.
_NAVIGATION_KEYS = ("{tab}", "{esc}", "{escape}", "{up}", "{down}", "{left}", "{right}",
                    "{home}", "{end}", "{pgup}", "{pgdn}", "{pageup}", "{pagedown}",
                    "{space}", "{backspace}")

# What a control's own accessible label can prove, for a click.
_DESTRUCTIVE_WORDS = ("excluir", "delete", "remove", "remover", "apagar", "instalar",
                      "uninstall", "enviar", "send", "salvar", "save", "fechar", "close",
                      "publish", "publicar", "comprar", "buy", "formatar", "reset",
                      "restaurar", "confirmar", "confirm", "guardar", "eliminar",
                      "substituir", "replace", "descartar", "discard", "esvaziar", "empty",
                      "pagar", "pagamento", "payment", "sign out", "log out", "terminar sessão")

# A dialog's answer button commits whatever the dialog asked ("Excluir permanentemente?",
# "Substituir o arquivo?"): its own label says nothing, so the short answers are judged by
# exact name — a substring "ok" would catch "Book", "sim" would catch "Simular".
_CONFIRM_LABELS = frozenset({"sim", "yes", "ok", "aceitar", "accept", "permitir", "allow",
                             "sair", "exit", "quit"})

# Control types whose click changes a persisted state rather than navigating.
_SETTING_TYPES = ("CheckBox", "RadioButton", "Slider", "Spinner")

# Item types where "select" is navigation inside a list, tab strip or menu.
_NAVIGATION_ITEM_TYPES = ("TabItem", "ListItem", "MenuItem", "TreeItem", "DataItem")

# UIA operations that only read or reveal: a click on them cannot destroy anything.
_READ_OPERATIONS = ("expand", "collapse", "scroll", "scroll_into_view", "realize",
                    "focus", "find_item", "select_text")

# Processes whose interaction can change the machine's behaviour.
_TERMINAL_PROCESSES = ("cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe",
                       "regedit.exe")


@dataclass(frozen=True)
class Verdict:
    """One action's risk, with the sentence a user could be shown as the reason."""

    level: str
    reason: str = ""

    def needs_consent(self, mode: str) -> bool:
        """Manual asks about anything not proven normal; auto-approval only about ``HIGH``."""
        if self.level == HIGH:
            return True
        if self.level == LOW:
            return False
        return mode != AUTO


def low(reason: str) -> Verdict:
    return Verdict(LOW, reason)


def high(reason: str) -> Verdict:
    return Verdict(HIGH, reason)


def unknown(reason: str) -> Verdict:
    return Verdict(UNKNOWN, reason)


_VK_MODIFIER = re.compile(r"\{VK_(?:L|R)?(CONTROL|MENU|SHIFT)\}([A-Za-z0-9]|\{[^}]+\})", re.IGNORECASE)
_NAMED_COMBO = re.compile(r"\s*((?:(?:ctrl|control|alt|shift)\s*\+\s*)+)"
                          r"([a-z0-9]|f\d{1,2}|enter|tab|esc|escape|delete|del|home|end)\s*", re.IGNORECASE)


def normalize_keys(keys: str) -> str:
    """The shortcut the model meant, in the syntax the input layer and the rules below read.

    UFO's own tool text teaches ``{VK_CONTROL}c`` for Ctrl+C; pywinauto types that as a lone
    Ctrl tap and a literal ``c`` (measured: "Ctrl+S" wrote an "s" into a Notepad document).
    A bare modifier right before a key, or a spelled "Ctrl+S", becomes ``^s``.
    """
    text = str(keys or "")
    marks = {"control": "^", "ctrl": "^", "menu": "%", "alt": "%", "shift": "+"}
    while True:
        changed = _VK_MODIFIER.sub(lambda m: marks[m[1].casefold()] + (m[2].casefold() if len(m[2]) == 1 else m[2]), text)
        if changed == text:
            break
        text = changed
    combo = _NAMED_COMBO.fullmatch(text)
    if combo:
        key = combo[2]
        named = {"esc": "ESC", "escape": "ESC", "del": "DELETE", "delete": "DELETE"}
        text = ("".join(marks[m.casefold()] for m in re.findall(r"ctrl|control|alt|shift", combo[1], re.IGNORECASE))
                + (key.casefold() if len(key) == 1 else "{" + named.get(key.casefold(), key.upper()) + "}"))
    return text


def submit_keys(keys: str) -> bool:
    """True when the keys commit the focused field's content (Enter, in any spelling)."""
    lowered = keys.casefold()
    if any(token in lowered for token in _SUBMIT_KEYS):
        return True
    return "\n" in keys or "\r" in keys


def looks_like_composer(control_type: str, control_label: str) -> bool:
    """A field where Enter publishes what was typed instead of confirming it."""
    if str(control_type) == "Document":
        # A document body: Enter edits content, and in a chat client it may send it.
        return True
    label = control_label.casefold()
    return any(word in label for word in _COMPOSER_WORDS)


def looks_like_search(control_label: str) -> bool:
    label = control_label.casefold()
    return any(word in label for word in _SEARCH_WORDS)


def keyboard_verdict(*, keys: str, control_type: str = "", control_label: str = "",
                     typed_into_control: bool = False) -> Verdict:
    """What pressing these keys does, given the control they land in.

    ``typed_into_control`` is the runtime's own record that this task typed the query into
    exactly this field a moment ago — the write-then-confirm pattern, which is what makes
    Enter a search confirmation rather than an anonymous keypress.
    """
    text = normalize_keys(keys)
    if not text.strip():
        return low("nenhuma tecla foi enviada")
    lowered = text.casefold()
    if submit_keys(text):
        if re.search(r"[\^%]\s*(?:\{(?:vk_)?(?:enter|return)\}|enter|return)", lowered):
            return high("Ctrl/Alt+Enter envia a mensagem ou o formulário em quase todos os programas")
        if looks_like_composer(control_type, control_label):
            return high("Enter em campo de mensagem/conteúdo: pode enviar ou publicar o texto")
        if typed_into_control or looks_like_search(control_label):
            return low("Enter confirma o texto já digitado neste campo")
        return unknown("Enter em campo que a observação não identificou como pesquisa nem "
                       "como mensagem")
    if any(token in lowered for token in _DESTRUCTIVE_KEYS):
        return high("tecla de salvar/fechar/excluir: o efeito sobrevive à rodada")
    if lowered in _NAVIGATION_KEYS or re.fullmatch(
            r"(?:" + "|".join(re.escape(key) for key in _NAVIGATION_KEYS) + r")+", lowered):
        return low("tecla de navegação")
    if any(marker in text for marker in ("^", "%", "+")):
        if lowered in _LOW_SHORTCUTS:
            return low("atalho de leitura/navegação")
        return unknown("atalho de teclado sem contexto que o torne seguro ou destrutivo")
    if "{" in text:
        # A named key nobody classified ({F5}, {VK_APPS}...): a keypress whose effect is not
        # known is exactly what the mode is for.
        return unknown(f"tecla '{text}' sem classificação de risco")
    return low("digitação de texto em um campo")


def click_verdict(*, control_label: str, control_type: str = "",
                  process: str = "", settings_process: bool = False) -> Verdict:
    """What clicking this control does, from its own label and type."""
    if str(control_type) in _SETTING_TYPES:
        return high("controle de estado (marca/opção/valor): alterar muda uma configuração")
    label = str(control_label or "").casefold()
    matched = next((word for word in _DESTRUCTIVE_WORDS if word in label), "")
    if not matched and label.strip(" .!&") in _CONFIRM_LABELS:
        matched = label.strip(" .!&")
    if matched:
        return high(f"o próprio controle se chama '{matched}': pode excluir, enviar ou salvar")
    if settings_process and str(control_type) in {"ComboBox", *_SETTING_TYPES}:
        return high("alteração de configuração do sistema")
    if str(process).casefold() in _TERMINAL_PROCESSES:
        return high("interação em ferramenta de comandos/configuração")
    if (str(control_type) in {"TabItem", "TreeItem", "ListItem", "Hyperlink"}
            or label.strip() in {"pesquisar", "search", "procurar", "find", "página inicial", "home"}):
        return low("controle identificado como navegação")
    return unknown("o rótulo e o tipo do controle não comprovam um clique de navegação")


def uia_action_verdict(*, operation: str, control_type: str = "") -> Verdict:
    """What a UIA pattern operation does, from the operation and the control it acts on."""
    name = str(operation or "").casefold()
    if name in _READ_OPERATIONS:
        return low(f"operação {name} não altera conteúdo")
    if name == "select" and str(control_type) in _NAVIGATION_ITEM_TYPES:
        return low(f"seleção de item de navegação ({control_type})")
    if name == "toggle":
        return high("toggle altera um estado persistente do controle")
    if name == "select":
        return unknown("seleção de controle que não é item de navegação")
    return unknown(f"operação {name or 'desconhecida'} sem classificação")


def set_text_verdict(*, replaces_content: bool, owned: bool) -> Verdict:
    """Writing into a field, and whether it replaces content this task did not create."""
    if replaces_content and not owned:
        return high("substitui o conteúdo de um documento que não foi criado nesta tarefa")
    return low("escrita de texto em um campo")


def terminal_verdict(process: str) -> Verdict:
    """Interacting with a command/config tool is sensitive however it is done."""
    if str(process).casefold() in _TERMINAL_PROCESSES:
        return high("interação em ferramenta de comandos/configuração")
    return low("")


def unknown_tool_verdict(name: str) -> Verdict:
    return unknown(f"ferramenta '{name}' não está na política de risco")

"""Deterministic ranking of the UIA control list sent to the AppAgent (FASE 5).

The AppAgent chooses a control from a list the *window* produced, not from the whole
desktop: measured windows ranged from 29 controls (Notepad) to 135 (Explorer), and
every one of them was sent to the model. That list is a context budget, so it belongs
to a ranking rule that can be read, explained and unit-tested — not to the size of the
window it happens to be looking at.

Three properties matter more than the size of the narrowed list:

- the result is a pure function of its inputs, so the same window and the same request
  produce the same list (no sampling, no time-dependence);
- the original order is preserved, so the ids stay in the familiar ascending order;
- the window anchor is never dropped and a control whose name matches the request always
  survives the cut. A list that is too narrow costs at most one round: the runtime
  escalates back to the complete list after a failed round.
"""
from __future__ import annotations

import os
import unicodedata
from typing import Any, Iterable

# The smallest window that completed a measured task had 29-30 controls (Notepad), so the
# default never cuts a list of that size: only windows clearly larger than the smallest
# successful workload are narrowed. ``UFO_CONTROL_LIMIT`` sets it per machine, and ``0``
# means "send the complete list", which is how the narrowing was measured against itself.
CONTROL_LIMIT_ENV = "UFO_CONTROL_LIMIT"
CONTROL_LIMIT_FALLBACK = 30


def _control_limit() -> int:
    try:
        return max(0, int(os.getenv(CONTROL_LIMIT_ENV, str(CONTROL_LIMIT_FALLBACK))))
    except ValueError:
        return CONTROL_LIMIT_FALLBACK


CONTROL_LIMIT_DEFAULT = _control_limit()
MIN_TOKEN_LENGTH = 3
# Words that carry no targeting information and would match everything.
STOPWORDS = frozenset({
    "das", "dos", "uma", "que", "com", "por", "para", "the", "and", "with", "from",
    "this", "that", "you", "your", "seu", "sua", "aos", "como", "mais", "menos",
    "editor", "texto", "text", "window", "janela", "control", "controle",
})
# How usable a control is for acting. Name matches always outrank this: interactivity
# only decides among controls the request did not name.
TYPE_WEIGHT = {
    "Edit": 3, "Document": 3, "ComboBox": 3, "Spinner": 3, "Slider": 3,
    "CheckBox": 3, "RadioButton": 3,
    "Button": 2, "ListItem": 2, "TreeItem": 2, "MenuItem": 2, "TabItem": 2,
    "Hyperlink": 2, "SplitButton": 2, "DataItem": 2, "List": 2, "Tree": 2,
    "Menu": 2, "Tab": 2, "DataGrid": 2,
    "Pane": 1, "Group": 1, "ToolBar": 1, "Custom": 1, "Image": 1, "Text": 1,
}
ANCHOR_TYPES = frozenset({"Window"})


def fold(text: Any) -> str:
    """Case- and accent-insensitive form, with punctuation turned into spaces."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    replaced = "".join(char if char.isalnum() else " " for char in stripped)
    return " ".join(replaced.casefold().split())


def tokens(text: Any) -> tuple[str, ...]:
    """Words worth matching on: no stopwords, no fragments too short to be meaningful."""
    return tuple(word for word in fold(text).split()
                 if len(word) >= MIN_TOKEN_LENGTH and word not in STOPWORDS)


def apply_escalation(prompter: Any, runtime: Any) -> bool:
    """Give this round the complete control list if the previous one failed, once.

    Delivered *before* the prompt is built and consumed immediately, so exactly one round
    goes back to the full list: a new failure asks for it again, a success narrows again.
    """
    if prompter is None:
        return False
    prompter.full_controls = bool(getattr(runtime, "escalate_controls", False))
    if prompter.full_controls:
        runtime.controls_escalated += 1
    runtime.escalate_controls = False
    return bool(prompter.full_controls)


def rank_controls(controls: list[dict[str, Any]], *, keywords: Iterable[Any] = (),
                  limit: int = CONTROL_LIMIT_DEFAULT) -> list[dict[str, Any]]:
    """Narrow a control list to its most usable entries, keeping the original order.

    Ranked by, in order: how many request words the control name matches, how usable the
    control type is for acting, and the window's own order. ``limit <= 0`` means "send
    everything", which is what the escalation path asks for.
    """
    if limit <= 0 or len(controls) <= limit:
        return list(controls)
    wanted = {word for text in keywords for word in tokens(text)}
    anchors = [index for index, control in enumerate(controls)
               if index == 0 or str(control.get("type", "")) in ANCHOR_TYPES]
    ranked = []
    for index, control in enumerate(controls):
        if index in anchors:
            continue
        name_tokens = tokens(control.get("name", ""))
        matched = sum(1 for word in set(name_tokens) if word in wanted)
        ranked.append((-matched, -TYPE_WEIGHT.get(str(control.get("type", "")), 0), index))
    ranked.sort()
    keep = set(anchors) | {entry[2] for entry in ranked[: max(0, limit - len(anchors))]}
    return [controls[index] for index in sorted(keep)]

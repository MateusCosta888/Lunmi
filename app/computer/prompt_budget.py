"""Final prompt budget for UFO rounds.

The budget is applied to the prompt the product actually sends, after the runtime
policy, history, pending evidence and recent actions have been added. AppAgent context
is selected down from the observed control list; mandatory prose, request, proof and
literal arguments are never truncated. If the mandatory part alone exceeds the
budget, the caller reports ``REQUEST_TOO_LARGE`` instead of sending a shortened prompt.
"""
from __future__ import annotations

import json
from typing import Any

from app.computer.controls import TYPE_WEIGHT, tokens

APP_PROMPT_BUDGET = 32_000
CONTROL_PREFIX = "<Available Control Item:> "
# Fields kept in the compact rows. ``context`` used to duplicate the parent and was
# dropped from every row; the parent itself is still available through ``contexts``
# (including when it is outside the selected rows).
CONTROL_FIELDS = ("id", "name", "type", "automation_id", "runtime_id", "parent", "patterns",
                  "access_key", "enabled", "offscreen", "focusable", "focused", "protected",
                  "framework", "class_name", "rect", "pid", "native_handle")


def text_chars(messages: list[dict[str, Any]]) -> int:
    """Characters of textual content, the same basis used by the controller metrics."""
    total = 0
    for message in messages:
        content = message.get("content", "")
        if isinstance(content, list):
            total += sum(len(str(block.get("text", ""))) for block in content
                         if isinstance(block, dict) and block.get("type") == "text")
        else:
            total += len(str(content))
    return total


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _control_location(messages: list[dict[str, Any]]) -> tuple[int, int | None, str] | None:
    for message_index, message in enumerate(messages):
        content = message.get("content")
        if isinstance(content, list):
            for block_index, block in enumerate(content):
                if (isinstance(block, dict) and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                        and CONTROL_PREFIX in block["text"]):
                    return message_index, block_index, block["text"]
        elif isinstance(content, str) and CONTROL_PREFIX in content:
            return message_index, None, content
    return None


def _payload(text: str) -> dict[str, Any] | None:
    start = text.find(CONTROL_PREFIX)
    if start < 0:
        return None
    line = text[start:].split("\n", 1)[0]
    try:
        value = json.loads(line[len(CONTROL_PREFIX):])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _rows(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], str,
                                            list[str]] | None:
    elements = payload.get("elements")
    if isinstance(elements, dict) and isinstance(elements.get("columns"), list) \
            and isinstance(elements.get("rows"), list):
        columns = [str(column) for column in elements["columns"]]
        rows = [dict(zip(columns, row, strict=True)) for row in elements["rows"]
                if isinstance(row, list) and len(row) == len(columns)]
        raw_contexts = elements.get("contexts")
        contexts: dict[str, Any] = ({str(key): value for key, value in raw_contexts.items()
                                     if isinstance(value, dict)}
                                    if isinstance(raw_contexts, dict) else {})
        return rows, contexts, "rows", columns
    if isinstance(elements, list):
        rows = [dict(row) for row in elements if isinstance(row, dict)]
        return rows, {}, "list", []
    return None


def _new_payload(payload: dict[str, Any], rows: list[dict[str, Any]],
                 contexts: dict[str, Any], kind: str, columns: list[str],
                 selection: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    if kind == "rows":
        result["elements"] = {"columns": list(columns),
                              "rows": [[row.get(field) for field in columns] for row in rows]}
        if contexts:
            result["elements"]["contexts"] = contexts
    else:
        result["elements"] = rows
    result["selection"] = selection
    return result


def _replace(messages: list[dict[str, Any]], location: tuple[int, int | None, str],
             text: str) -> None:
    message_index, block_index, _ = location
    if block_index is None:
        messages[message_index]["content"] = text
    else:
        messages[message_index]["content"][block_index]["text"] = text


def fit_app_prompt(messages: list[dict[str, Any]], budget: int = APP_PROMPT_BUDGET) -> dict[str, Any]:
    """Fit the AppAgent control context to ``budget`` without cutting mandatory text.

    The first control is the window anchor. Controls whose name matches the highest
    request score are treated as targets and are never dropped. Other controls are
    dropped lowest-value first; parent metadata is moved to ``contexts`` when the
    parent row itself is dropped. The caller still checks the final bytes/transport.
    """
    original_chars = text_chars(messages)
    if original_chars <= budget:
        return {"fits": True, "changed": False, "chars": original_chars,
                "selected": None, "collected": None, "omitted": None,
                "reason": "within_budget", "targets": []}
    location = _control_location(messages)
    if location is None:
        return {"fits": False, "changed": False, "chars": original_chars,
                "selected": None, "collected": None, "omitted": None,
                "reason": "no_control_payload", "targets": []}
    payload = _payload(location[2])
    if payload is None:
        return {"fits": False, "changed": False, "chars": original_chars,
                "selected": None, "collected": None, "omitted": None,
                "reason": "unreadable_control_payload", "targets": []}
    parsed = _rows(payload)
    if parsed is None:
        return {"fits": False, "changed": False, "chars": original_chars,
                "selected": None, "collected": None, "omitted": None,
                "reason": "no_control_rows", "targets": []}
    rows, contexts, kind, columns = parsed
    if not rows:
        return {"fits": False, "changed": False, "chars": original_chars,
                "selected": 0, "collected": 0, "omitted": 0,
                "reason": "empty_control_list", "targets": []}
    raw_selection = payload.get("selection")
    selection: dict[str, Any] = raw_selection if isinstance(raw_selection, dict) else {}
    collected = selection.get("collected")
    if not isinstance(collected, int) or collected < len(rows):
        collected = len(rows)
    source = {str(row.get("id")): dict(row) for row in rows}
    for key, value in contexts.items():
        if isinstance(value, dict):
            source[str(key)] = dict(value)
    control_start = location[2].find(CONTROL_PREFIX)
    control_line_end = location[2].find("\n", control_start)
    if control_line_end < 0:
        control_line_end = len(location[2])
    text_without_controls = location[2][:control_start] + location[2][control_line_end:]
    wanted = set(tokens(text_without_controls))
    scores = []
    for index, row in enumerate(rows):
        name_tokens = set(tokens(row.get("name", "")))
        scores.append(sum(1 for word in name_tokens if word in wanted))
    top = max(scores) if scores else 0
    # The window anchor plus every control at the best request match survive. A
    # request that matches every generic name has no target distinction; in that
    # case only the anchor is protected, so the budget can still select context.
    protected = {0} | {index for index, score in enumerate(scores) if top > 0 and score == top}
    droppable = [index for index in range(len(rows)) if index not in protected]
    # Lowest relevance/type first; the highest index wins ties so the original order
    # of the surviving rows stays stable.
    droppable.sort(key=lambda index: (scores[index],
                                      TYPE_WEIGHT.get(str(rows[index].get("type", "")), 0),
                                      -index))
    kept = list(range(len(rows)))
    current = payload
    while True:
        current_rows = [rows[index] for index in kept]
        kept_ids = {str(row.get("id")) for row in current_rows}
        current_contexts = {}
        for row in current_rows:
            parent = str(row.get("parent", ""))
            if parent and parent not in kept_ids:
                context = source.get(parent, contexts.get(parent))
                if context:
                    current_contexts[parent] = context
        selection_out = {
            "collected": collected,
            "sent": len(current_rows),
            "omitted": max(0, collected - len(current_rows)),
            "reason": "prompt_budget" if len(current_rows) < len(rows) else selection.get("reason", "ranked"),
            "targets": [str(rows[index].get("id")) for index in sorted(protected)
                        if top > 0 and scores[index] == top],
        }
        current = _new_payload(payload, current_rows, current_contexts, kind, columns, selection_out)
        new_text = (location[2][:control_start] + CONTROL_PREFIX + _compact(current)
                    + location[2][control_line_end:])
        _replace(messages, location, new_text)
        chars = text_chars(messages)
        if chars <= budget:
            return {"fits": True, "changed": len(current_rows) < len(rows),
                    "chars": chars, "selected": len(current_rows), "collected": collected,
                    "omitted": max(0, collected - len(current_rows)),
                    "reason": "prompt_budget" if len(current_rows) < len(rows) else "within_budget",
                    "targets": selection_out["targets"]}
        if not droppable:
            return {"fits": False, "changed": len(current_rows) < len(rows), "chars": chars,
                    "selected": len(current_rows), "collected": collected,
                    "omitted": max(0, collected - len(current_rows)),
                    "reason": "mandatory_exceeds_budget", "targets": selection_out["targets"]}
        removed = droppable.pop(0)
        kept.remove(removed)


def check_prompt(messages: list[dict[str, Any]], *, budget: int = APP_PROMPT_BUDGET,
                 cli_limit: int | None = None) -> dict[str, Any]:
    """Check the final text and, for a CLI transport, its rendered prompt."""
    text = text_chars(messages)
    cli = None
    if cli_limit is not None:
        from app.brain.cli.headless import render_prompt
        cli = len(render_prompt(messages))
    limit = min(budget, cli_limit) if cli_limit is not None else budget
    used = cli if cli is not None else text
    ok = used <= limit
    kind = "CLI" if cli is not None else "texto"
    message = ""
    if not ok:
        message = (f"O prompt deste passo excede o limite seguro de {limit} caracteres "
                   f"({used} {kind}); nada foi enviado ao cérebro. Reduza o pedido/contexto.")
        if cli_limit is not None and cli_limit < budget:
            message = (f"O transporte usado limita o prompt a {cli_limit} caracteres; este passo "
                       f"tem {used} {kind} e nada foi enviado ao cérebro.")
    return {"ok": ok, "text": text, "cli": cli, "limit": limit, "used": used,
            "kind": kind, "message": message}

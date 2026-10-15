"""The Brain↔UFO protocol boundary: reply translation and end-of-round causes.

Deliberately vendor-free, like ``batches.py`` and ``controls.py``: what the model answered
has to be turned into the JSON object the pinned UFO protocol requires, and a round that
executed nothing has to be explained — both are ours to get right, and both must be
testable without importing UFO.

Measured (2026-10-09, the five reported failures): two of them were a model reply the
protocol could not read — one decorated with text around the object, one empty — and both
ended as a generic "Fluxo UFO encerrado; veja evidências das operações." with zero actions.
The message named neither cause, and the round ended after one attempt because the official
retry count was 1.
"""
from __future__ import annotations

import json
import re
from typing import Any

# The pinned parser accepts a bare JSON object, or exactly one leading ```json fence with
# nothing after it. A model very often answers with a sentence before the object, a fenced
# block, or both, and the parser then rejects a perfectly good plan. Finding the object is
# translation, not a prompt trick, and nothing here invents a field or an action.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# Said to the model when its previous answer to the *same* prompt was unreadable. It states
# the protocol, not the task: the decision stays the model's.
REPLY_PROTOCOL_NUDGE = (
    "Your previous answer could not be read: it was not one JSON object. Reply with a single "
    "JSON object and nothing else — no text before or after it and no markdown fence — using "
    "the official fields of this agent (observation, thought/thoughts, the action or action "
    "list, status).")


class MalformedReply(RuntimeError):
    """The Brain answered something the UFO protocol cannot read as one JSON object.

    Raised at the boundary, so the official retry loop treats it like any other parse
    failure — it asks again — and, if the second answer is unreadable too, the round ends
    naming this cause instead of a generic ending.
    """


def _first_json_object(text: str) -> str | None:
    """The first balanced ``{...}`` in ``text`` that is already valid JSON, or ``None``.

    Braces inside strings are ignored, so a JSON string containing ``{`` does not truncate
    the object. Nothing is rewritten: an object that is not valid JSON is not parsed, and a
    reply that is not JSON at all stays unreadable rather than being guessed at.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:index + 1]
                    try:
                        json.loads(candidate)
                    except ValueError:
                        break
                    return candidate
        start = text.find("{", start + 1)
    return None


def _call(step: Any) -> dict[str, Any] | None:
    """The call a ``plan`` entry really is, or ``None`` when it is not one to move.

    Only the fields the pinned ``ActionCommandInfo`` declares survive, each keeping the type
    that schema declares: an entry whose ``function`` is empty, or whose ``arguments`` is
    something other than an object, is not a call this translation understands and is left
    for the parser to judge instead of being repaired into one.
    """
    if not isinstance(step, dict) or not str(step.get("function") or "").strip():
        return None
    arguments = step.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        return None
    call: dict[str, Any] = {"function": str(step["function"]), "arguments": arguments or {}}
    status = step.get("status")
    if isinstance(status, str) and status.strip():
        call["status"] = status
    return call


def _in_protocol_shape(payload: str) -> str:
    """The answer in the shape the pinned protocol defines, or the answer untouched.

    The official schema carries the *steps* in ``plan`` as strings and the *calls* in
    ``action`` — an ``ActionCommandInfo``, which is exactly ``{function, arguments, status}``.
    Measured (2026-10-09, the reported "pesquisa por outer wilds na steam"): the model
    answered

        "plan": [{"function": "uia_verify", "arguments": {...}, "status": "FINISH"}]

    — a valid call, written into the field that holds step descriptions. Pydantic rejected
    the type, the AppAgent's LLM phase failed, and the round ended with zero actions after
    40 s of inference, which is the failure the user reported twice (cases 2 and 4).

    Moving a call the model *did* write into the field that carries calls is translation:
    the call is preserved verbatim, no field and no action is invented, a plan that is
    already a list of strings comes back byte-identical, and a ``plan`` entry that is neither
    a string nor a call is left for the pinned parser to judge — never guessed at here.

    Only a reply without a top-level ``status`` is translated: the HostAgent's schema
    *requires* ``status``, so a payload without it is an AppAgent reply, and that is the one
    whose calls live in ``action``.
    """
    try:
        data = json.loads(payload)
    except ValueError:
        return payload
    if (not isinstance(data, dict) or "status" in data
            or not isinstance(data.get("plan"), list)):
        return payload
    calls: list[dict[str, Any]] = []
    steps: list[str] = []
    for step in data["plan"]:
        if isinstance(step, str):
            steps.append(step)
            continue
        call = _call(step)
        if call is None:
            return payload
        calls.append(call)
    if not calls:
        return payload
    if not data.get("action"):
        # The protocol's own field is empty, so this is the only place the decision exists.
        data["action"] = calls
    # A non-empty `action` already carries the calls that will be dispatched; the copies in
    # the plan would be the same decision written twice, and nothing dispatches a plan.
    data["plan"] = steps or None
    return json.dumps(data, ensure_ascii=False)


def normalize_reply(raw: str) -> str:
    """The one JSON object the UFO protocol requires, out of whatever the Brain returned.

    A fenced block wins over the surrounding text, because that is where a model that
    decorates its answer puts it; otherwise the first balanced object is taken. An empty
    answer and an answer with no readable object both raise ``MalformedReply`` — never a
    fabricated object, and never a silent zero-action round. A readable object whose calls
    are in the wrong field is translated, not rejected: see ``_in_protocol_shape``.
    """
    text = str(raw or "").replace("\ufeff", "").strip()
    if not text:
        raise MalformedReply("A resposta do cérebro veio vazia: nenhum objeto JSON foi enviado.")
    for candidate in [*[match.group(1) for match in _FENCE.finditer(text)], text]:
        found = _first_json_object(candidate)
        if found is not None:
            return _in_protocol_shape(found)
    raise MalformedReply("A resposta do cérebro não contém nenhum objeto JSON válido: "
                         + repr(text[:400]))


def no_action_reason(processors: list[Any]) -> tuple[str, str]:
    """Why a round ended with zero actions, as ``(failure_kind, sentence)``.

    Read from the phase that actually failed, never guessed: the LLM interaction phase's own
    error text is the evidence. "O fluxo encerrou" made a provider failure, an unreadable
    reply and a model that chose nothing look identical in the chat, and each of them needs a
    different reaction from the user. The vocabulary stays the project's own: a provider
    error and a reply the protocol could not read are both ``PROVIDER_FAILURE``.
    """
    for agent in processors:
        processor = getattr(agent, "processor", None)
        if processor is None:
            continue
        for phase in processor.processing_context.phase_results.values():
            if phase.success:
                continue
            text = str(phase.error or "")
            lowered = text.casefold()
            name = str(getattr(phase.phase, "name", "") or "")
            if "provider" in lowered:
                return "PROVIDER_FAILURE", (
                    "Nenhuma ação foi executada: o provedor de IA falhou durante a "
                    f"inferência. {text}")
            if "json" in lowered or "expecting value" in lowered:
                return "PROVIDER_FAILURE", (
                    "Nenhuma ação foi executada: o modelo respondeu algo que não é o objeto "
                    f"JSON que o protocolo do UFO exige. {text}")
            return "INTERNAL_ERROR", (
                f"Nenhuma ação foi executada: a fase {name or 'desconhecida'} do agente "
                f"falhou. {text}")
    return "", ""

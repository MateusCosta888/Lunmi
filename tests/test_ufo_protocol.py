"""The Brain↔UFO boundary: what the model answered, and why a round ended with nothing.

Measured (2026-10-09, the five reported failures): two of them were a model reply the pinned
protocol could not read — one decorated with text, one empty — and both ended as a generic
"Fluxo UFO encerrado; veja evidências das operações." with zero actions, which named neither
cause. Two properties are held here: every readable JSON object in an answer is found, and a
round that executed nothing is reported with the phase that actually failed.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import json

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.computer.protocol import (  # noqa: E402
    REPLY_PROTOCOL_NUDGE,
    MalformedReply,
    no_action_reason,
    normalize_reply,
)


def test_a_bare_object_is_returned_unchanged() -> None:
    assert normalize_reply('{"status": "FINISH"}') == '{"status": "FINISH"}'


@pytest.mark.parametrize("raw", [
    '```json\n{"status": "FINISH", "thought": "done"}\n```',
    '```\n{"status": "FINISH", "thought": "done"}\n```',
    'Here is my answer:\n```json\n{"status": "FINISH", "thought": "done"}\n```\nHope it helps.',
    '{"status": "FINISH", "thought": "done"}\n\nLet me know if you need more.',
    '\n\n  {"status": "FINISH", "thought": "done"}  ',
])
def test_the_object_is_found_inside_whatever_decorates_it(raw: str) -> None:
    """The pinned parser accepts a bare object only; a sentence around it is still an answer."""
    assert normalize_reply(raw).startswith('{"status": "FINISH"')


def test_braces_inside_a_string_do_not_truncate_the_object() -> None:
    raw = 'Sure: {"status": "CONTINUE", "observation": "the { window } shows", "n": 2} end'
    assert normalize_reply(raw) == ('{"status": "CONTINUE", "observation": "the { window } shows"'
                                    ', "n": 2}')


def test_the_first_object_wins_over_a_later_one() -> None:
    raw = '{"status": "ASSIGN", "action": {"function": "click_input"}} {"status": "FINISH"}'
    assert normalize_reply(raw).startswith('{"status": "ASSIGN"')


def test_a_pretty_printed_object_survives_the_round_trip() -> None:
    raw = '{\n  "observation": "window",\n  "thought": "open the app",\n  "status": "CONTINUE"\n}'
    assert normalize_reply(raw) == raw


def test_an_object_that_is_not_json_is_never_repaired_into_one() -> None:
    """A guess is worse than a retry: the object is only *found*, never rewritten."""
    with pytest.raises(MalformedReply):
        normalize_reply('{"status": "FINISH", "thought": "ok",}')   # trailing comma


@pytest.mark.parametrize("raw", ["", "   \n  ", "I cannot help with that.", "[]", "42"])
def test_an_unreadable_answer_is_refused_and_says_why(raw: str) -> None:
    with pytest.raises(MalformedReply) as raised:
        normalize_reply(raw)
    assert "JSON" in str(raised.value)


def test_a_call_written_into_the_plan_is_moved_into_action() -> None:
    """Measured (2026-10-09, "pesquisa por outer wilds na steam" and the Discord message):
    the model answered

        "plan": [{"function": "uia_verify", "arguments": {...}, "status": "FINISH"}]

    — a valid call, in the field that holds step descriptions. Pydantic rejected the type,
    the AppAgent's LLM phase failed, and the round ended with zero actions after 40 s of
    inference. That is the reported "LLM chamado, nenhuma ação executada", twice.
    """
    raw = ('{"observation": "notepad", "thought": "type the text", "plan": ['
           '{"function": "uia_action", "arguments": {"id": "3", "operation": "focus"},'
           ' "status": "CONTINUE"}]}')
    assert json.loads(normalize_reply(raw)) == {
        "observation": "notepad", "thought": "type the text", "plan": None,
        "action": [{"function": "uia_action",
                    "arguments": {"id": "3", "operation": "focus"}, "status": "CONTINUE"}]}


def test_the_calls_keep_the_order_and_every_field_the_schema_declares() -> None:
    raw = ('{"observation": "o", "thought": "t", "plan": ['
           '{"function": "first", "arguments": {"a": 1}}, {"function": "second"}]}')
    data = json.loads(normalize_reply(raw))
    assert [call["function"] for call in data["action"]] == ["first", "second"]
    # `arguments` missing is the schema's own default, not a value invented here.
    assert data["action"][1]["arguments"] == {}


def test_a_plan_of_text_with_its_calls_beside_it_is_left_as_it_was() -> None:
    raw = ('{"observation": "o", "thought": "t", "plan": ["find the field", "type it"],'
           ' "action": {"function": "set_edit_text", "arguments": {"text": "x"}}}')
    written = json.loads(normalize_reply(raw))
    assert written["plan"] == ["find the field", "type it"]
    assert written["action"] == {"function": "set_edit_text", "arguments": {"text": "x"}}


def test_a_plan_of_plain_strings_is_returned_byte_identical() -> None:
    raw = '{"observation": "o", "thought": "t", "plan": ["step one", "step two"]}'
    assert normalize_reply(raw) == raw


def test_a_plan_entry_that_is_not_a_call_is_never_repaired_into_one() -> None:
    """An entry this translation cannot read is the parser's to judge — not ours to guess.

    Repairing it would be inventing an action, and a fabricated action is worse than a
    retry: the retry restates the protocol and the model sends a readable one.
    """
    for plan in ('[{"description": "click the button"}]',
                 '[{"function": "click", "arguments": "not an object"}]',
                 '[{"arguments": {"x": 1}}]'):
        raw = '{"observation": "o", "thought": "t", "plan": ' + plan + '}'
        assert normalize_reply(raw) == raw


def test_a_host_reply_is_never_translated() -> None:
    """The HostAgent's schema *requires* `status`, and its call fields are its own."""
    raw = ('{"observation": "o", "thought": "t", "status": "ASSIGN",'
           ' "plan": [{"function": "select_application_window", "arguments": {}}]}')
    assert normalize_reply(raw) == raw


def test_what_we_return_is_what_the_pinned_protocol_accepts() -> None:
    """The contract is the vendor's parser, not ours: it is the one that has to read it.

    Importing UFO loads its configuration relative to the working directory (which is what
    the worker does with `os.chdir`), so this one test borrows that directory and restores
    the original one afterwards.
    """
    import os
    original = os.getcwd()
    os.chdir(ROOT / "vendor" / "UFO")
    sys.path.insert(0, str(ROOT / "vendor" / "UFO"))
    try:
        from ufo.agents.processors.schemas.response_schema import AppAgentResponse
        from ufo.utils import json_parser
        raw = ('Here is the plan:\n```json\n{"status": "FINISH", "thought": "done",'
               ' "observation": "{}"}\n```\nAnything else?')
        assert json_parser(normalize_reply(raw))["status"] == "FINISH"
        # The vendor parser alone does not read the decorated answer: that is the whole point,
        # and it is what the reported failures hit ("Expecting value: line 3 column 1").
        with pytest.raises(ValueError):
            json_parser(raw)
        # And the translation is judged by the schema that rejected it on the real machine:
        # the plan-embedded call is the one AppAgentResponse now validates.
        misplaced = ('```json\n{"observation": "notepad", "thought": "type it", "plan": ['
                     '{"function": "uia_action", "arguments": {"id": "3"},'
                     ' "status": "CONTINUE"}]}\n```')
        with pytest.raises(Exception):
            AppAgentResponse.model_validate(json_parser(misplaced))
        parsed = AppAgentResponse.model_validate(json_parser(normalize_reply(misplaced)))
        assert parsed.action is not None and parsed.action[0].function == "uia_action"
    finally:
        os.chdir(original)


def test_the_retry_nudge_states_the_protocol_not_the_task() -> None:
    """The second attempt restates the format; the decision stays the model's."""
    assert "JSON" in REPLY_PROTOCOL_NUDGE
    assert "FINISH" not in REPLY_PROTOCOL_NUDGE and "ASSIGN" not in REPLY_PROTOCOL_NUDGE


def processor(*phases: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(processor=SimpleNamespace(
        processing_context=SimpleNamespace(phase_results={index: phase
                                                          for index, phase in enumerate(phases)})))


def failed(error: str, name: str = "LLM_INTERACTION") -> SimpleNamespace:
    return SimpleNamespace(success=False, error=error, phase=SimpleNamespace(name=name))


def ok() -> SimpleNamespace:
    return SimpleNamespace(success=True, error="", phase=SimpleNamespace(name="DATA_COLLECTION"))


def test_a_provider_failure_is_named_instead_of_a_generic_ending() -> None:
    kind, message = no_action_reason([processor(ok(), failed(
        "HostLLMInteractionStrategy failed: LLM interaction failed after 2 attempts: "
        "Falha do provider durante inferência UFO."))])
    assert kind == "PROVIDER_FAILURE"
    assert "provedor de IA falhou" in message and "Nenhuma ação foi executada" in message


@pytest.mark.parametrize("error", [
    "LLM interaction failed after 2 attempts: Expecting value: line 3 column 1 (char 2)",
    "LLM interaction failed after 2 attempts: A resposta do cérebro veio vazia: nenhum objeto "
    "JSON foi enviado.",
])
def test_an_unreadable_reply_is_named_as_a_protocol_failure(error: str) -> None:
    kind, message = no_action_reason([processor(failed(error))])
    assert kind == "PROVIDER_FAILURE"
    assert "JSON" in message


def test_another_phase_failure_names_the_phase_instead_of_blaming_the_provider() -> None:
    kind, message = no_action_reason([processor(failed("janela não identificada", name="DATA_COLLECTION"))])
    assert kind == "INTERNAL_ERROR"
    assert "DATA_COLLECTION" in message


def test_a_round_whose_phases_all_succeeded_has_no_cause_to_report() -> None:
    assert no_action_reason([processor(ok(), ok())]) == ("", "")
    assert no_action_reason([]) == ("", "")

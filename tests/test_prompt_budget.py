"""B3: final prompt budget, context selection, transport ceiling and runtime wiring."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from app.brain.contracts import BrainError, ErrorKind
from app.computer.batches import NoProgressGuard
from app.computer.prompt_budget import (APP_PROMPT_BUDGET, CONTROL_FIELDS, check_prompt,
                                        fit_app_prompt, text_chars)

ROOT = Path(__file__).resolve().parents[1]


def _payload(rows: int = 30, *, target_parent: str = "999") -> dict[str, Any]:
    columns = list(CONTROL_FIELDS)
    elements = []
    for index in range(rows):
        values = {field: None for field in columns}
        values.update({"id": str(index), "name": "Alpha target" if index == 1 else f"Generic {index}",
                        "type": "Window" if index == 0 else "Button", "automation_id": f"a-{index}",
                        "runtime_id": [index, 1], "parent": target_parent if index == 1 else "0",
                        "patterns": ["Invoke"], "enabled": True, "offscreen": False,
                        "focusable": True, "focused": False, "protected": False,
                        "framework": "synthetic", "class_name": "Synthetic",
                        "rect": [0, index, 100, index + 1], "pid": 7, "native_handle": 0})
        elements.append([values[field] for field in columns])
    contexts = {target_parent: {field: (target_parent if field == "id" else
                                        "Parent target" if field == "name" else
                                        "Window" if field == "type" else
                                        True if field == "enabled" else None)
                                for field in columns}}
    return {"observation": {"complete": True, "truncated": False},
            "selection": {"collected": 337, "sent": rows, "omitted": 337 - rows,
                          "reason": "ranked"},
            "elements": {"columns": columns, "rows": elements, "contexts": contexts}}


def _messages(rows: int = 30, *, system_chars: int = 15_000) -> list[dict[str, Any]]:
    user = ("prefix\n<Available Control Item:> " + json.dumps(_payload(rows), ensure_ascii=False)
            + "\n<Overall User Request:> Find alpha target and keep this literal: {x: 1}\n"
              "<Your response:>")
    return [{"role": "system", "content": "S" * system_chars},
            {"role": "user", "content": [{"type": "text", "text": user}]}]


def test_fit_selects_context_and_preserves_target_parent_and_literal() -> None:
    messages = _messages(system_chars=28_000)
    before = text_chars(messages)
    assert before > APP_PROMPT_BUDGET
    result = fit_app_prompt(messages, APP_PROMPT_BUDGET)
    assert result["fits"] is True and result["changed"] is True
    assert result["selected"] < 30, result
    assert result["omitted"] > 0
    assert result["targets"] == ["1"]
    after = text_chars(messages)
    assert after <= APP_PROMPT_BUDGET and after < before
    user = messages[1]["content"][0]["text"]
    assert "Find alpha target" in user
    assert "{x: 1}" in user
    assert "Alpha target" in user
    assert "Generic 29" not in user
    payload = json.loads(user.split("<Available Control Item:> ", 1)[1].split("\n", 1)[0])
    assert payload["selection"] == {"collected": 337, "sent": result["selected"],
                                   "omitted": result["omitted"],
                                   "reason": "prompt_budget", "targets": ["1"]}
    columns = payload["elements"]["columns"]
    target = next(row for row in payload["elements"]["rows"]
                  if row[columns.index("name")] == "Alpha target")
    assert target[columns.index("runtime_id")] == [1, 1]
    assert target[columns.index("patterns")] == ["Invoke"]
    assert payload["elements"]["contexts"]["999"]["name"] == "Parent target"


def test_real_prompter_keeps_parent_context_after_budget(monkeypatch: Any) -> None:
    _runtime_module(monkeypatch)
    from app.computer.prompts import CompactAppPrompter

    controls = [{"id": "0", "name": "Main window", "type": "Window"},
                {"id": "1", "name": "Alpha search", "type": "Edit"}]
    controls += [{"id": str(index), "name": f"Generic {index}", "type": "Button"}
                 for index in range(2, 10)]
    metadata = {row["id"]: {**row, "parent": "99" if row["id"] == "1" else "",
                             "runtime_id": [int(row["id"])], "pid": 7}
                for row in controls}
    metadata["99"] = {"id": "99", "name": "Account-specific header", "type": "Group",
                      "parent": "", "runtime_id": [99], "pid": 7,
                      "patterns": ["Invoke"], "enabled": True}
    prompter = CompactAppPrompter.__new__(CompactAppPrompter)
    prompter.control_limit = 10
    prompter.full_controls = False
    prompter.prompt_template = {"user": "<Available Control Item:> {control_item}\nRequest: {user_request}"}
    prompter.uia_metadata = metadata
    prompter.uia_observation = {"complete": True, "truncated": False}
    text = prompter.user_prompt_construction(controls, [], [], "Alpha search",
                                            "Alpha search", "Synthetic.exe", [])
    data = json.loads(text.split("<Available Control Item:> ", 1)[1].split("\n", 1)[0])
    assert data["selection"]["sent"] == 10
    assert data["elements"]["contexts"]["99"]["name"] == "Account-specific header"

    messages = [{"role": "system", "content": "S" * 1_000},
                {"role": "user", "content": [{"type": "text", "text": text}]}]
    before = text_chars(messages)
    result = fit_app_prompt(messages, before - 1)
    assert result["fits"] is True and result["changed"] is True
    assert result["selected"] < 10
    after = json.loads(messages[1]["content"][0]["text"].split(
        "<Available Control Item:> ", 1)[1].split("\n", 1)[0])
    assert after["elements"]["contexts"]["99"]["name"] == "Account-specific header"


def test_record_minimizes_fast_path_declined_without_mutating_result(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from app.core.controller import TaskController, TaskResult

    sentinel = "PRIVATE_B3_SENTINEL_local_path_or_attachment"
    for index, (attachment_turn, log_instruction) in enumerate(
            ((False, False), (True, True))):
        logs = tmp_path / f"logs-{index}"
        controller = TaskController.__new__(TaskController)
        controller.settings = SimpleNamespace(logs_path=logs, log_instruction=log_instruction)
        result = TaskResult(status="NOT_VERIFIED", fast_path_declined={
            "kind": "window", "label": sentinel, "reason": sentinel, "actions": 1,
            "effects": [{"operation": "launch", "verified": True, "executed": True}],
            "pending": "O pedido original continua pendente."})
        controller._record(result, "synthetic request", attachment_turn=attachment_turn)
        record = json.loads((logs / "tasks.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert sentinel not in json.dumps(record, ensure_ascii=False)
        assert record["fast_path_declined"]["kind"] == "window"
        assert record["fast_path_declined"]["actions"] == 1
        assert record["fast_path_declined"]["effects"][0]["operation"] == "launch"
        assert result.fast_path_declined["label"] == sentinel


def test_mandatory_exceeds_is_reported_without_truncation() -> None:
    messages = _messages(system_chars=40_000)
    result = fit_app_prompt(messages, APP_PROMPT_BUDGET)
    assert result["fits"] is False
    assert result["reason"] == "mandatory_exceeds_budget"
    assert "Find alpha target" in messages[1]["content"][0]["text"]
    assert text_chars(messages) > APP_PROMPT_BUDGET


def test_final_gate_rejects_mandatory_over_32k_before_inference() -> None:
    messages = _messages(system_chars=40_000)
    from app.core.controller import _prompt_budget_gate

    with pytest.raises(BrainError) as error:
        _prompt_budget_gate(messages, object())  # type: ignore[arg-type]
    assert error.value.kind == ErrorKind.REQUEST_TOO_LARGE
    assert "32000" in str(error.value)


def test_check_prompt_respects_text_and_cli_ceiling() -> None:
    messages = _messages(system_chars=26_000)
    assert check_prompt(messages)["ok"] is True  # API text is below 32k
    cli = check_prompt(messages, cli_limit=28_000)
    assert cli["ok"] is False and "28000" in cli["message"]
    with pytest.raises(BrainError) as error:
        from app.core.controller import _prompt_budget_gate
        from app.brain.cli.catalog import find_spec
        from app.brain.cli.headless import HeadlessCliBrain
        from app.settings import Settings
        brain = HeadlessCliBrain(Settings(provider="agy", model="fixture"), find_spec("agy"),
                                 executable="fixture")
        _prompt_budget_gate(messages, brain)
    assert error.value.kind == ErrorKind.REQUEST_TOO_LARGE


def test_progress_signature_deduplicates_ids_and_matches_only_relevant_state(monkeypatch: Any) -> None:
    module = _runtime_module(monkeypatch)
    base = [{"operation": "invoke", "handle": 7, "runtime_id": [7, 1],
             "verified": False, "needs_postcondition": True, "before": {"expanded": 0}}]
    duplicate = [*base, {"operation": "uia_verify", "id": "uia-new", "runtime_id": [7, 1],
                          "handle": 7, "property": "expanded", "expected_state": 1,
                          "observed": 0, "verified": False}]
    baseline = module.RuntimeDispatcher.evidence_signature(base)
    later = [*duplicate, {"operation": "uia_verify", "id": "uia-newer", "runtime_id": [7, 1],
                           "handle": 7, "property": "expanded", "expected_state": 1,
                           "observed": 0, "verified": False}]
    assert module.RuntimeDispatcher.evidence_signature(duplicate) == \
        module.RuntimeDispatcher.evidence_signature(later)
    assert module.RuntimeDispatcher.evidence_signature(duplicate) != baseline

    # Same HWND but another runtime_id is another control, not target progress.
    unrelated = {"complete": True, "root_id": "root-1", "elements": [
        {"id": "other", "handle": 7, "runtime_id": [8, 1], "parent": "parent-1",
         "name": "Unrelated", "value": 1}]}
    assert module.RuntimeDispatcher.evidence_signature(base, unrelated) == baseline

    relevant = {"complete": True, "root_id": "root-1", "elements": [
        {"id": "target", "runtime_id": [7, 1], "parent": "parent-1",
         "name": "Loading 20 percent", "value": 20}]}
    first = module.RuntimeDispatcher.evidence_signature(base, relevant)
    # Regenerated parent/root id must not turn the same state into progress.
    relevant["root_id"] = "root-2"
    relevant["elements"][0]["parent"] = "parent-2"
    assert module.RuntimeDispatcher.evidence_signature(base, relevant) == first
    relevant["elements"][0].update({"name": "Loading 40 percent", "value": 40})
    second = module.RuntimeDispatcher.evidence_signature(base, relevant)
    assert first != second
    assert first != baseline


def test_stalled_guard_ignores_duplicate_ids_without_pending_and_stalls_with_pending(monkeypatch: Any) -> None:
    module = _runtime_module(monkeypatch)
    base = [{"operation": "invoke", "handle": 7, "runtime_id": [7, 1],
             "verified": False, "needs_postcondition": True}]
    for pending, expected in ((False, False), (True, True)):
        guard = NoProgressGuard(stalled_limit=3)
        for index in range(5):
            evidence = [*base, {"operation": "uia_verify", "id": f"new-{index}",
                                 "handle": 7, "runtime_id": [7, 1], "verified": False,
                                 "observed": 0}]
            guard.after_batch(None, failed=True, evidence_count=len(evidence), read_only=True,
                              pending=pending,
                              evidence_signature=module.RuntimeDispatcher.evidence_signature(evidence))
        assert guard.stalled is expected


def test_stalled_read_guard_counts_only_same_pending_evidence() -> None:
    guard = NoProgressGuard(stalled_limit=3)
    for index in range(4):
        guard.after_batch(None, failed=False, evidence_count=index, read_only=True,
                          pending=True, evidence_signature="same")
    assert guard.stalled is True
    assert guard.before_batch(None, idle=False) is True
    fresh = NoProgressGuard(stalled_limit=3)
    fresh.after_batch(None, failed=False, evidence_count=1, read_only=True,
                      pending=True, evidence_signature="one")
    fresh.after_batch(None, failed=False, evidence_count=2, read_only=True,
                      pending=True, evidence_signature="two")
    assert fresh.stalled is False


def _runtime_module(monkeypatch: Any) -> Any:
    monkeypatch.chdir(ROOT / "vendor" / "UFO")
    monkeypatch.syspath_prepend(str(ROOT / "vendor" / "UFO"))
    import app.computer.ufo_runtime as runtime_module
    return runtime_module


def test_runtime_applies_budget_after_policy_pending_and_recent_actions(monkeypatch: Any) -> None:
    module = _runtime_module(monkeypatch)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    captured: list[list[dict[str, Any]]] = []

    async def exchange(event: dict[str, Any]) -> dict[str, Any]:
        captured.append(json.loads(json.dumps(event["messages"], ensure_ascii=False)))
        return {"text": '{"observation":"","thought":"","action":[],"plan":[],"comment":"",'
                        '"save_screenshot":{"save":false,"reason":""},"result":""}'}

    runtime = module.UFORuntime.__new__(module.UFORuntime)
    runtime.llm_rounds = 0
    runtime.short_policy_rounds = 0
    runtime.tools = type("Tools", (), {
        "pending_ui": True,
        "evidence": [{"operation": "click_input", "submitted": True,
                      "needs_postcondition": True, "verified": False}],
    })()
    runtime.recent_actions = [{"operation": "click_input", "verified": False}]
    runtime.attachments = []
    runtime.initial_result = None
    runtime.last_reply_malformed = False
    runtime.last_prompt_signature = ""
    runtime.loop = loop
    runtime.exchange = exchange
    try:
        runtime.response(_messages(system_chars=24_000), "AgentType.APP")
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
    assert captured
    final = captured[0]
    assert text_chars(final) <= APP_PROMPT_BUDGET
    system = final[0]["content"]
    assert "Unverified dispatches" in system
    assert "Recent successful deterministic actions" in system
    assert runtime.last_prompt_budget["fits"] is True
    assert runtime.last_prompt_budget["targets"] == ["1"]

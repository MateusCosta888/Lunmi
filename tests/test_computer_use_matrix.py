from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from benchmarks.computer_use_matrix import CASES, evaluate


def record(case: str = "write") -> dict[str, Any]:
    return {"case": case, "revision": "test-revision", "provider": "fixture", "model": "fake",
            "run_id": "run-1", "target_id": "owned-window-1", "start": "cold", "repeat": 1,
            "flags": {"COMPUTER_WRITE_MACRO": False}, "fixture_prepared": True,
            "safety_checks": {"no_unexpected_effects": True, "confirmations_respected": True,
                              "no_duplicate_writes": True},
            "product_status": "SUCCESS", "finished_at": 10, "seconds": 3,
            "calls": [{"attempts": 1, "chars": 100, "input_tokens": 25, "output_tokens": 8}],
            "observation": {"method": "independent_read", "run_id": "run-1",
                            "target_id": "owned-window-1", "captured_at": 11,
                            "text": CASES[case].get("expected_text")}}


def test_all_seven_tasks_have_independent_fixture_instructions() -> None:
    assert len(CASES) == 7
    assert all(case["fixture"] and case["instruction"] for case in CASES.values())
    assert "preparar de novo" in CASES["append"]["fixture"]


@pytest.mark.parametrize("case", ["write", "append", "calculate"])
def test_exact_independent_text_accepts_fixture(case: str) -> None:
    assert evaluate(record(case))["status"] == "SUCCESS"


@pytest.mark.parametrize("change", ["absent", "wrong_text", "stale", "other_window", "other_run", "self_report", "unprepared"])
def test_product_success_alone_never_passes(change: str) -> None:
    row = record()
    if change == "absent":
        row.pop("observation")
    elif change == "unprepared":
        row["fixture_prepared"] = False
    else:
        key, value = {"wrong_text": ("text", "incorrect"), "stale": ("captured_at", 9),
                      "other_window": ("target_id", "other"), "other_run": ("run_id", "other"),
                      "self_report": ("method", "task.finished")}[change]
        row["observation"][key] = value
    result = evaluate(row)
    assert result["status"] == "NOT_ACCEPTED" and result["false_success"]


def test_old_file_or_wrong_path_does_not_prove_save() -> None:
    row = record("save")
    row.update(destination="C:/owned/new.txt", destination_absent_before=True)
    row["observation"].update(path=row["destination"], exists=True)
    assert evaluate(row)["status"] == "SUCCESS"
    row["destination_absent_before"] = False
    assert evaluate(row)["status"] == "NOT_ACCEPTED"
    row["destination_absent_before"] = True
    row["observation"]["path"] = "C:/other.txt"
    assert evaluate(row)["status"] == "NOT_ACCEPTED"


def test_creative_text_requires_operator_acceptance_and_exact_readback() -> None:
    row = record("poem")
    row["observation"]["text"] = "O mar canta."
    assert evaluate(row)["status"] == "NOT_ACCEPTED"
    row["accepted_text"] = "O mar canta."
    assert evaluate(row)["status"] == "SUCCESS"


def test_browser_edit_field_alone_is_insufficient() -> None:
    row = record("edge")
    row["observation"].update(url="https://wikipedia.org/", page_text="Wikipedia")
    assert evaluate(row)["status"] == "NOT_ACCEPTED"
    row["observation"]["loaded"] = True
    assert evaluate(row)["status"] == "SUCCESS"
    row["observation"]["url"] = "https://wikipedia.org.evil.example/"
    assert evaluate(row)["status"] == "NOT_ACCEPTED"


def test_downloads_uses_resolved_fixture_path() -> None:
    row = record("downloads")
    row["expected_path"] = "C:/owned/Downloads"
    row["observation"]["path"] = row["expected_path"]
    assert evaluate(row)["status"] == "SUCCESS"
    row["observation"]["path"] = "C:/owned/Documents"
    assert evaluate(row)["status"] == "NOT_ACCEPTED"


def test_unknown_tokens_are_not_reported_as_zero_and_input_is_unchanged() -> None:
    row = record()
    row["calls"].append({"attempts": 2, "chars": 200})
    before = deepcopy(row)
    result = evaluate(row)
    assert result["input_tokens"] == {"known": 25, "unknown_calls": 2, "total": None}
    assert result["attempts"] == 3 and result["chars"] == 300
    assert row == before


def test_tokens_include_every_retry_when_attempt_usage_is_available() -> None:
    row = record()
    row["calls"] = [{"attempts": 2, "chars": 100, "input_tokens": 999,
                     "usage_attempts": [{"input_tokens": 25}, {"input_tokens": 30}]}]
    assert evaluate(row)["input_tokens"] == {"known": 55, "unknown_calls": 0, "total": 55}
    row["calls"][0]["usage_attempts"].pop()
    with pytest.raises(ValueError):
        evaluate(row)


@pytest.mark.parametrize("key,value", [("seconds", -1), ("seconds", float("nan")),
                                       ("repeat", True), ("flags", {"x": 1}), ("model", "")])
def test_invalid_measurement_rejected(key: str, value: Any) -> None:
    row = record()
    row[key] = value
    with pytest.raises(ValueError):
        evaluate(row)


def test_negative_product_result_is_not_upgraded_by_oracle() -> None:
    row = record()
    row["product_status"] = "NOT_VERIFIED"
    assert evaluate(row)["status"] == "NOT_ACCEPTED"


@pytest.mark.parametrize("key", ["no_unexpected_effects", "confirmations_respected", "no_duplicate_writes"])
def test_correct_content_does_not_hide_missing_safety_observation(key: str) -> None:
    row = record()
    row["safety_checks"].pop(key)
    result = evaluate(row)
    assert not result["safety_passed"] and result["status"] == "NOT_ACCEPTED"

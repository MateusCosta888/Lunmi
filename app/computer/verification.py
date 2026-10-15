"""Live evidence rules shared by the tool boundary and task completion."""
from __future__ import annotations

from typing import Any


def covers_dispatch(action: dict[str, Any], proof: dict[str, Any]) -> bool:
    if proof.get("verified") is not True or not action.get("handle") \
            or str(proof.get("handle")) != str(action["handle"]):
        return False
    operation = proof.get("operation")
    if operation == "verify_window":
        if action.get("submitted"):
            return bool(proof.get("closed") or proof.get("path"))
        return bool(proof.get("closed") or proof.get("expected_text") or proof.get("path"))
    if operation != "uia_verify" or proof.get("property") in {"enabled", "focused"}:
        return False
    action_id = action.get("runtime_id") or action.get("id")
    proof_id = proof.get("runtime_id") or proof.get("id")
    if action_id and action_id == proof_id:
        before = action.get("before", {})
        prop = proof.get("property")
        if prop in before and proof.get("observed") == before[prop]:
            return False
    # Enter changes a result region; re-reading its unchanged query is not that result.
    if action.get("submitted"):
        if not action_id or not proof_id or action_id == proof_id:
            return False
    return True


def unresolved_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain failures unless a later live read proves their postcondition."""
    def key(item: dict[str, Any]) -> tuple[str, str]:
        return (str(item.get("operation") or item.get("kind") or ""), str(item.get("handle") or ""))

    unresolved = []
    for index, item in enumerate(evidence):
        if item.get("verified") is True:
            continue
        later = evidence[index + 1:]
        if item.get("needs_postcondition"):
            resolved = any(covers_dispatch(item, proof) for proof in later)
        else:
            purpose = ("property", "expected_text", "expected_state", "path", "control_type", "expected_active", "closed")
            resolved = any(proof.get("verified") is True and key(proof) == key(item)
                           and all(proof.get(field) == item[field] for field in purpose if field in item)
                           for proof in later)
            if not resolved and item.get("operation") == "uia_verify":
                # A failed popup read stays false; only a proven associated recovery retires it.
                for click_index in range(index + 1, len(evidence)):
                    click = evidence[click_index]
                    native_index = click.get("recovery_of")
                    if (type(native_index) is not int or not 0 <= native_index < index
                            or click.get("recovery_read") != index or click.get("operation") != "click_input"):
                        continue
                    native = evidence[native_index]
                    if (native.get("operation") not in {"invoke", "default_action"}
                            or native.get("executed") is not True or native.get("dispatch_uncertain")
                            or native.get("before", {}).get("expanded") != 0
                            or item.get("property") != "expanded" or item.get("expected_state") != 1
                            or item.get("observed") != 0
                            or not native.get("runtime_id") or not native.get("handle")
                            or any(entry.get("runtime_id") != native["runtime_id"]
                                   or entry.get("handle") != native["handle"] for entry in (item, click))):
                        continue
                    resolved = any(covers_dispatch(native, proof) and covers_dispatch(click, proof)
                                   for proof in evidence[click_index + 1:])
                    if resolved:
                        break
        if not resolved:
            unresolved.append(item)
    return unresolved

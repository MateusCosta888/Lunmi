"""Live evidence rules shared by the tool boundary and task completion."""
from __future__ import annotations

import hashlib
from typing import Any


def issue_association(proof: dict[str, Any], query_sha256: str) -> str:
    """Bind a result proof to its own live identity and exact submitted text.

    The token is derived, not stored: it survives the worker/controller boundary,
    has no unbounded task-global table, and a proof from another control or another
    query does not recompute to the same value.
    """
    material = f"{_identity(proof)!r}|{proof.get('handle')}|{query_sha256}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _identity(item: dict[str, Any]) -> tuple[str, Any] | None:
    """The strongest identity the record actually carries, never just an HWND."""
    runtime_id = item.get("runtime_id")
    if isinstance(runtime_id, (list, tuple)) and runtime_id:
        return ("runtime", tuple(runtime_id))
    control_id = item.get("id")
    if isinstance(control_id, str) and control_id:
        return ("id", control_id)
    return None


def _same_control(action: dict[str, Any], proof: dict[str, Any]) -> bool:
    left, right = _identity(action), _identity(proof)
    return left is not None and left == right


def _typed_length(action: dict[str, Any], history: tuple[dict[str, Any], ...]) -> int | None:
    """The verified text written into this control before the submitting key."""
    for item in reversed(history):
        if (item.get("operation") == "set_edit_text" and item.get("verified") is True
                and _same_control(item, action)):
            length = item.get("length")
            if type(length) is int and length >= 0:
                return length
    return None


def _typed_digest(action: dict[str, Any], history: tuple[dict[str, Any], ...]) -> str | None:
    for item in reversed(history):
        if (item.get("operation") == "set_edit_text" and item.get("verified") is True
                and _same_control(item, action)):
            digest = item.get("text_sha256")
            if isinstance(digest, str) and len(digest) == 64:
                return digest
    return None


def covers_dispatch(action: dict[str, Any], proof: dict[str, Any], *,
                    history: tuple[dict[str, Any], ...] = (),
                    associated: bool = False) -> bool:
    if proof.get("verified") is not True or not action.get("handle") \
            or str(proof.get("handle")) != str(action["handle"]):
        return False
    operation = proof.get("operation")
    if operation == "verify_window":
        # A window read is not the postcondition of a control that was acted on.
        if not associated:
            return False
        return bool(proof.get("closed") or proof.get("path") or proof.get("expected_text"))
    if operation != "uia_verify" or proof.get("property") in {"enabled", "focused"}:
        return False
    prop = proof.get("property")
    if action.get("submitted"):
        # Enter is a dispatch to the result region. Re-reading the query field, even if
        # its text changed, is never the result; only a tool-issued foreign association is.
        if _same_control(action, proof):
            return False
        if proof.get("results_verified") is not True or proof.get("workflow") != "spotify_search":
            return False
        typed = _typed_length(action, history)
        digest = _typed_digest(action, history)
        if typed is None or digest is None or proof.get("query_length") != typed \
                or proof.get("query_sha256") != digest:
            return False
        return proof.get("association") == issue_association(proof, digest)
    if _same_control(action, proof):
        before = action.get("before") or {}
        if prop in before:
            return proof.get("observed") != before[prop]
        # Only an observed state transition proves a control with no prior state.
        return False
    return False


def _recovery_proof_pertinent(proof: dict[str, Any], click: dict[str, Any]) -> bool:
    """A popup recovery proves the invoked control's own state changed.

    A name/text read from some other control, even a Window/Pane with an explicit
    expected_text, has no recorded link to the menu that was clicked, so it stays
    pending instead of retiring the dispatch.
    """
    if proof.get("verified") is not True or proof.get("operation") != "uia_verify":
        return False
    if not _same_control(click, proof):
        return False
    prop = proof.get("property")
    before = click.get("before") or {}
    return prop in {"expanded", "selected", "toggle", "name", "text"} \
        and prop in before and proof.get("observed") != before[prop]


def _recovery_pairs(evidence: list[dict[str, Any]]) -> dict[int, tuple[int, int]]:
    """Validated popup recoveries: failed read -> authorized click -> real positive read."""
    pairs: dict[int, tuple[int, int]] = {}
    for read_index, item in enumerate(evidence):
        if (item.get("operation") != "uia_verify" or item.get("verified") is not False
                or item.get("property") != "expanded" or item.get("expected_state") != 1
                or item.get("observed") != 0):
            continue
        for click_index in range(read_index + 1, len(evidence)):
            click = evidence[click_index]
            native_index = click.get("recovery_of")
            if (type(native_index) is not int or not 0 <= native_index < read_index
                    or click.get("recovery_read") != read_index or click.get("operation") != "click_input"):
                continue
            native = evidence[native_index]
            if (native.get("operation") not in {"invoke", "default_action"}
                    or native.get("executed") is not True or native.get("dispatch_uncertain")
                    or native.get("before", {}).get("expanded") != 0
                    or not native.get("runtime_id") or not native.get("handle")
                    or any(entry.get("runtime_id") != native["runtime_id"]
                           or entry.get("handle") != native["handle"] for entry in (item, click))):
                continue
            if any(_recovery_proof_pertinent(proof, click)
                   and covers_dispatch(native, proof, associated=True)
                   and covers_dispatch(click, proof, associated=True)
                   for proof in evidence[click_index + 1:]):
                pairs[read_index] = (native_index, click_index)
                break
    return pairs


def recovered_dispatch_indices(evidence: list[dict[str, Any]]) -> set[int]:
    """Actions whose popup recovery was completed by a real positive read."""
    return {index for pair in _recovery_pairs(evidence).values() for index in pair}


def unresolved_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain failures unless a later live read proves their postcondition."""
    def key(item: dict[str, Any]) -> tuple[str, str]:
        return (str(item.get("operation") or item.get("kind") or ""), str(item.get("handle") or ""))

    recoveries = _recovery_pairs(evidence)
    recovered_indices = recovered_dispatch_indices(evidence)
    unresolved = []
    for index, item in enumerate(evidence):
        if item.get("verified") is True:
            continue
        later = evidence[index + 1:]
        history = tuple(evidence[:index])
        if item.get("needs_postcondition"):
            resolved = any(covers_dispatch(item, proof, history=history) for proof in later)
            if not resolved and index in recovered_indices:
                resolved = any(covers_dispatch(item, proof, associated=True)
                               for proof in later if proof.get("verified") is True)
        else:
            purpose = ("property", "expected_text", "expected_state", "path", "control_type", "expected_active", "closed")
            resolved = any(proof.get("verified") is True and key(proof) == key(item)
                           and (item.get("operation") != "uia_verify" or not _identity(item)
                                or not _identity(proof) or _same_control(item, proof))
                           and all(proof.get(field) == item[field] for field in purpose if field in item)
                           for proof in later)
            if not resolved and index in recoveries:
                resolved = True
        if not resolved:
            unresolved.append(item)
    return unresolved

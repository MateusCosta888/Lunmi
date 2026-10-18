"""Batch accounting and the no-progress guard for one UFO round (FASE 3).

Deliberately vendor-free: the rules that decide what counts as an *action* and
when a round is making no progress are what the measurement and the guard depend
on, so both must be unit-testable without importing UFO.
"""
from __future__ import annotations

import json
from typing import Any

# Meta tools ask the executor what it can do; they are not actions on the desktop.
_META_TOOLS = frozenset({"list_tools", "N/A", "None", "none"})
# The same failing action repeated this many times without any new evidence is a
# loop, not a retry: repeating identical work cannot change the outcome.
MAX_REPEATED_FAILURES = 3
# Measured on T08 (Codex, 2026-10-07): 21 rounds, no action dispatched, 118 s spent. A
# round that carries no action cannot accomplish anything, so the streak is capped
# well above the few rounds a real task may spend observing before acting.
MAX_IDLE_BATCHES = 5
# Read-only rounds that keep the same pending postcondition and the same evidence are
# not progress, even when the IDs are regenerated. The first read establishes the
# signature; three identical repeats after it (four batches measured) are a dead end.
MAX_STALLED_READS = 3


def is_action(command: Any) -> bool:
    """True for a real desktop action — not an observation and not a meta call."""
    name = getattr(command, "tool_name", None)
    return (getattr(command, "tool_type", "") == "action" and bool(name)
            and name not in _META_TOOLS)


def count_actions(commands: list[Any]) -> int:
    """How many actions one batch (one LLM round) carries."""
    return sum(1 for command in commands if is_action(command))


def action_signature(commands: list[Any], identities: dict[str, Any] | None = None) -> tuple[str, str] | None:
    """Signature of a batch carrying exactly one action, or ``None`` otherwise.

    Only single-action batches get a signature: a batch that carries several actions
    is already making progress, and comparing multi-action batches would need a
    canonical ordering that the agent never promises.
    """
    actions = [command for command in commands if is_action(command)]
    if len(actions) != 1:
        return None
    try:
        values = dict(getattr(actions[0], "parameters", None) or {})
        identifier = str(values.get("id", ""))
        if identities and identifier in identities:
            values["id"] = identities[identifier]
        parameters = json.dumps(values,
                                sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    return (str(getattr(actions[0], "tool_name", "")), parameters)


class NoProgressGuard:
    """Stops a round that repeats the same failing action without changing anything.

    Deliberately narrow: only identical *failures* count. Repeating an action that
    succeeded can be legitimate (paging, scrolling, walking a list), but an action
    that fails and changes nothing cannot succeed by being tried again, so aborting
    it is safe. Any new evidence clears the streak, because that is progress.
    """

    def __init__(self, limit: int = MAX_REPEATED_FAILURES,
                 idle_limit: int = MAX_IDLE_BATCHES,
                 stalled_limit: int = MAX_STALLED_READS) -> None:
        self.limit = limit
        self.idle_limit = idle_limit
        self.stalled_limit = stalled_limit
        self._signature: tuple[str, str] | None = None
        self._streak = 0
        self._idle = 0
        self._evidence_seen = 0
        self._stalled = 0
        self._last_evidence_signature: object = None
        self._failures: dict[tuple[str, str], int] = {}

    @property
    def stalled(self) -> bool:
        """True once the read-only evidence has repeated without resolving pending work."""
        return self._stalled >= self.stalled_limit

    def before_batch(self, signature: tuple[str, str] | None, *, idle: bool = False) -> bool:
        """True when this batch is repeating a known dead end.

        Either the same failing action again, or another round that carries no action
        at all. A successful action clears the failure streak in ``after_batch``.
        """
        if idle:
            return self._idle >= self.idle_limit or self.stalled
        return (signature is not None and self._failures.get(signature, 0) >= self.limit) \
            or self.stalled

    def _note_stall(self, *, read_only: bool, pending: bool,
                    evidence_signature: object) -> None:
        if read_only and pending and evidence_signature == self._last_evidence_signature:
            self._stalled += 1
        else:
            self._stalled = 0
        self._last_evidence_signature = evidence_signature

    def after_batch(self, signature: tuple[str, str] | None, *, failed: bool,
                    evidence_count: int, idle: bool = False, read_only: bool = False,
                    pending: bool = False, evidence_signature: object = None) -> None:
        """Record one batch. Only identical failures, empty or stalled reads count as waste."""
        if idle:
            grew = evidence_count != self._evidence_seen
            self._idle = 0 if grew else self._idle + 1
            self._signature, self._streak, self._evidence_seen = None, 0, evidence_count
            self._note_stall(read_only=read_only, pending=pending,
                             evidence_signature=evidence_signature)
            return
        self._idle = 0
        if signature is None or not failed:
            if signature is not None:
                self._failures.pop(signature, None)
            self._signature, self._streak, self._evidence_seen = None, 0, evidence_count
            self._note_stall(read_only=read_only, pending=pending,
                             evidence_signature=evidence_signature)
            return
        self._streak = self._failures.get(signature, 0) + 1
        self._failures[signature] = self._streak
        self._signature = signature
        self._evidence_seen = evidence_count
        self._note_stall(read_only=read_only, pending=pending,
                         evidence_signature=evidence_signature)

"""Public records for controller-owned local memory (no worker dependency)."""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

PROFILE_KINDS = ("preference", "fact", "habit", "decision")
KINDS = (*PROFILE_KINDS, "episode", "conversation")
STATUSES = ("active", "superseded", "deleted")
SOURCES = ("user_explicit", "user_confirmed", "episode", "system")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class MemoryItem:
    id: str = ""
    kind: str = "fact"
    text: str = ""
    status: str = "active"
    source: str = "user_explicit"
    confidence: float = 1.0
    created_at: str = ""
    updated_at: str = ""
    supersedes_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class WorkingMemory:
    """RAM only, by task/session. Copies prevent mutations outside the lock."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, dict[str, Any]] = {}

    def get(self, session_id: str = "current") -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._items.get(session_id, {}))

    def set(self, values: dict[str, Any], session_id: str = "current") -> None:
        # Working state is not persisted or logged. Bound it even though it stays in RAM.
        if not isinstance(values, dict) or len(repr(values)) > 4000:
            raise ValueError("Estado de trabalho inválido ou muito grande.")
        with self._lock:
            self._items[session_id] = copy.deepcopy(values)

    def clear(self, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is None:
                self._items.clear()
            else:
                self._items.pop(session_id, None)

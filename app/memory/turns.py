"""Janela de turnos em RAM, por sessão.

Independente do histórico persistido e do provider: só existe para dar continuidade
imediata à conversa. Nunca é gravada, logada ou enviada ao AppAgent, e um turno com
anexos não entra nela.
"""
from __future__ import annotations

import threading
from collections import deque

TURN_MAX_PAIRS = 6
TURN_MAX_CHARS = 12000


class TurnWindow:
    def __init__(self, max_pairs: int = TURN_MAX_PAIRS, max_chars: int = TURN_MAX_CHARS) -> None:
        self.max_pairs = max_pairs
        self.max_chars = max_chars
        self._lock = threading.RLock()
        self._items: dict[str, deque[tuple[str, str]]] = {}

    def add(self, session_id: str, user: str, assistant: str) -> None:
        if not user or not assistant:
            return
        with self._lock:
            pairs = self._items.setdefault(session_id, deque())
            pairs.append((user, assistant))
            while len(pairs) > self.max_pairs:
                pairs.popleft()
            while pairs and sum(len(u) + len(a) for u, a in pairs) > self.max_chars:
                pairs.popleft()

    def pairs(self, session_id: str) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._items.get(session_id, ()))

    def clear(self, session_id: str | None = None) -> None:
        with self._lock:
            if session_id is None:
                self._items.clear()
            else:
                self._items.pop(session_id, None)

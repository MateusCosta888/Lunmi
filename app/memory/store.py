"""SQLite access serialized across UI/controller threads.

Source: https://docs.python.org/3.11/library/sqlite3.html#sqlite3.connect
Source: https://www.sqlite.org/fts5.html#external_content_tables
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from app.memory.contracts import MemoryItem, now
from app.memory.policy import MemoryPolicyError, normalized

SCHEMA_VERSION = 1
DEFAULT_OPTIONS = {"enabled": True, "episodes": True, "proposals": True, "history": False}

_SCHEMA = (
    "CREATE TABLE memories (id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, "
    "status TEXT NOT NULL CHECK(status IN ('active','superseded','deleted')), source TEXT NOT NULL, "
    "confidence REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
    "supersedes_id TEXT, metadata_json TEXT NOT NULL, expires_at TEXT)",
    "CREATE INDEX memory_active ON memories(status,kind,updated_at)",
    "CREATE VIRTUAL TABLE memories_fts USING fts5(text, content='memories', content_rowid='rowid', "
    "tokenize='unicode61 remove_diacritics 2')",
    "CREATE TRIGGER memory_insert AFTER INSERT ON memories BEGIN "
    "INSERT INTO memories_fts(rowid,text) VALUES(new.rowid,new.text); END",
    "CREATE TRIGGER memory_delete AFTER DELETE ON memories BEGIN "
    "INSERT INTO memories_fts(memories_fts,rowid,text) VALUES('delete',old.rowid,old.text); END",
    "CREATE TRIGGER memory_update AFTER UPDATE OF text ON memories BEGIN "
    "INSERT INTO memories_fts(memories_fts,rowid,text) VALUES('delete',old.rowid,old.text); "
    "INSERT INTO memories_fts(rowid,text) VALUES(new.rowid,new.text); END",
    "CREATE TABLE options (key TEXT PRIMARY KEY, value INTEGER NOT NULL CHECK(value IN (0,1)))",
    "CREATE TABLE proposals (id TEXT PRIMARY KEY, payload_json TEXT NOT NULL, "
    "state TEXT NOT NULL CHECK(state IN ('pending','accepted','rejected')), memory_id TEXT)",
    "CREATE TABLE episodes (id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE, "
    "instruction TEXT NOT NULL, outcome TEXT NOT NULL, apps_json TEXT NOT NULL, summary TEXT NOT NULL, "
    "lessons TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE, metadata_json TEXT NOT NULL)",
    "CREATE VIRTUAL TABLE episodes_fts USING fts5(summary, content='episodes', content_rowid='rowid', "
    "tokenize='unicode61 remove_diacritics 2')",
    "CREATE TRIGGER episode_insert AFTER INSERT ON episodes BEGIN "
    "INSERT INTO episodes_fts(rowid,summary) VALUES(new.rowid,new.summary); END",
    "CREATE TRIGGER episode_delete AFTER DELETE ON episodes BEGIN "
    "INSERT INTO episodes_fts(episodes_fts,rowid,summary) VALUES('delete',old.rowid,old.summary); END",
    "CREATE TRIGGER episode_update AFTER UPDATE OF summary ON episodes BEGIN "
    "INSERT INTO episodes_fts(episodes_fts,rowid,summary) VALUES('delete',old.rowid,old.summary); "
    "INSERT INTO episodes_fts(rowid,summary) VALUES(new.rowid,new.summary); END",
    "CREATE TABLE conversations (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, title TEXT NOT NULL)",
    "CREATE TABLE messages (id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL "
    "REFERENCES conversations(id) ON DELETE CASCADE, role TEXT NOT NULL, content TEXT NOT NULL, ts TEXT NOT NULL)",
    "CREATE TABLE conversation_summaries (id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE, "
    "conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, summary TEXT NOT NULL)",
)


def fts_query(query: str) -> str:
    if not isinstance(query, str) or len(query) > 4000:
        raise MemoryPolicyError()
    # User punctuation/operators are data, not the FTS expression language.
    tokens = re.findall(r"[^\W_]+", normalized(query), re.UNICODE)[:20]
    return " OR ".join('"' + token + '"' for token in tokens)


def row_item(row: sqlite3.Row) -> MemoryItem:
    return MemoryItem(id=row['id'], kind=row['kind'], text=row['text'], status=row['status'],
                      source=row['source'], confidence=row['confidence'], created_at=row['created_at'],
                      updated_at=row['updated_at'], supersedes_id=row['supersedes_id'],
                      metadata=json.loads(row['metadata_json']))


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.closed = False
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA secure_delete=ON")
            # DELETE journaling avoids leaving an unmanaged long-lived WAL sidecar.
            self.db.execute("PRAGMA journal_mode=DELETE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError("Banco de memória pertence a uma versão mais nova.")
            if version == 0:
                with self.transaction():
                    for statement in _SCHEMA:
                        self.db.execute(statement)
                    self.db.executemany("INSERT INTO options VALUES(?,?)", DEFAULT_OPTIONS.items())
                    self.db.execute("PRAGMA user_version=1")
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            if self.closed:
                raise RuntimeError("Memória fechada.")
            # Nested calls reuse the outer transaction; only the owner commits/rolls back.
            nested = self.db.in_transaction
            if not nested:
                self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                if not nested:
                    self.db.commit()
            except BaseException:
                if not nested:
                    self.db.rollback()
                raise

    def insert(self, item: MemoryItem) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (item.id, item.kind, item.text, item.status, item.source, item.confidence,
                        item.created_at, item.updated_at, item.supersedes_id,
                        json.dumps(item.metadata, ensure_ascii=False), item.metadata.get('expires_at')))

    def get(self, item_id: str) -> MemoryItem:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM memories WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise KeyError("Memória não encontrada.")
            return row_item(row)

    def search(self, query: str, *, status: str, limit: int,
               kinds: tuple[str, ...] | None = None, session_id: str | None = None) -> list[MemoryItem]:
        expression = fts_query(query)
        params: list[object] = [status, now()]
        where = "m.status=? AND (m.expires_at IS NULL OR m.expires_at>?)"
        if kinds is not None:
            where += " AND m.kind IN (" + ",".join("?" for _ in kinds) + ")"
            params.extend(kinds)
        if session_id is not None:
            where += " AND (json_extract(m.metadata_json,'$.session_id') IS NULL OR "
            where += "json_extract(m.metadata_json,'$.session_id')=?)"
            params.append(session_id)
        join = ""
        order = "CASE m.source WHEN 'user_explicit' THEN 0 WHEN 'user_confirmed' THEN 1 ELSE 2 END, m.updated_at DESC"
        if query.strip():
            if not expression:
                return []
            join = " JOIN memories_fts ON memories_fts.rowid=m.rowid"
            where += " AND memories_fts MATCH ?"
            params.append(expression)
            order = "bm25(memories_fts), m.updated_at DESC"
        params.append(limit)
        with self.transaction() as db:
            rows = db.execute("SELECT m.* FROM memories m" + join + " WHERE " + where +
                              " ORDER BY " + order + " LIMIT ?", params).fetchall()
            return [row_item(row) for row in rows]

    def options(self) -> dict[str, bool]:
        with self.transaction() as db:
            return {row['key']: bool(row['value']) for row in db.execute("SELECT * FROM options")}

    def export(self) -> dict[str, object]:
        with self.transaction() as db:
            return {"version": 1, "memories": [asdict(row_item(row)) for row in
                    db.execute("SELECT * FROM memories ORDER BY created_at,id")],
                    "proposals": [json.loads(row['payload_json']) for row in
                    db.execute("SELECT * FROM proposals WHERE state='pending'")],
                    "options": self.options(),
                    "conversations": [dict(row) for row in db.execute("SELECT * FROM conversations")],
                    "messages": [dict(row) for row in db.execute("SELECT * FROM messages ORDER BY rowid")]}

    def close(self) -> None:
        with self.lock:
            if not self.closed:
                self.db.close()
                self.closed = True

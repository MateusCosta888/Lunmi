"""SQLite access serialized across UI/controller threads.

Source: https://docs.python.org/3.11/library/sqlite3.html#sqlite3.connect
Source: https://www.sqlite.org/fts5.html#external_content_tables
"""
from __future__ import annotations

import json
import re
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Iterator

from app.memory.contracts import MemoryItem, now
from app.memory.policy import MemoryPolicyError, normalized
from app.memory.profile import DEFAULT_KIND, PROFILE_KINDS, segment

SCHEMA_VERSION = 2
DEFAULT_OPTIONS = {"enabled": True, "episodes": True, "proposals": True, "history": False,
                   "auto_learn": True}

_SCHEMA_V1 = (
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

# Schema 2: documento canónico do perfil livre + índice derivado com proveniência.
_SCHEMA_V2 = (
    "CREATE TABLE profile_document (id INTEGER PRIMARY KEY CHECK(id=1), text TEXT NOT NULL, "
    "revision INTEGER NOT NULL, previous_text TEXT, previous_chunks_json TEXT, updated_at TEXT NOT NULL)",
    "CREATE TABLE profile_chunks (chunk_id TEXT PRIMARY KEY, position INTEGER NOT NULL, "
    "text TEXT NOT NULL, kind TEXT NOT NULL, origin TEXT NOT NULL CHECK(origin IN ('manual','learned')), "
    "learned_key TEXT, expires_at TEXT, start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, "
    "supersedes_id TEXT, source TEXT NOT NULL DEFAULT 'user_explicit')",
    "CREATE INDEX profile_chunk_position ON profile_chunks(position)",
    "CREATE VIRTUAL TABLE profile_chunks_fts USING fts5(text, content='profile_chunks', "
    "content_rowid='rowid', tokenize='unicode61 remove_diacritics 2')",
    "CREATE TRIGGER profile_chunk_insert AFTER INSERT ON profile_chunks BEGIN "
    "INSERT INTO profile_chunks_fts(rowid,text) VALUES(new.rowid,new.text); END",
    "CREATE TRIGGER profile_chunk_delete AFTER DELETE ON profile_chunks BEGIN "
    "INSERT INTO profile_chunks_fts(profile_chunks_fts,rowid,text) VALUES('delete',old.rowid,old.text); END",
    "CREATE TRIGGER profile_chunk_update AFTER UPDATE OF text ON profile_chunks BEGIN "
    "INSERT INTO profile_chunks_fts(profile_chunks_fts,rowid,text) VALUES('delete',old.rowid,old.text); "
    "INSERT INTO profile_chunks_fts(rowid,text) VALUES(new.rowid,new.text); END",
    "CREATE TABLE profile_forgotten (digest TEXT PRIMARY KEY)",
)

_SCHEMA = (*_SCHEMA_V1, *_SCHEMA_V2)


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
        self.path = path
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
                    self.db.execute("INSERT INTO profile_document"
                                    "(id,text,revision,previous_text,previous_chunks_json,updated_at)"
                                    " VALUES(1,'',0,NULL,NULL,?)", (now(),))
                    self.db.execute("PRAGMA user_version=2")
            elif version == 1:
                self._migrate_v1_to_v2()
        except BaseException:
            self.db.close()
            raise

    @staticmethod
    def _valid_v1_copy(path: Path) -> bool:
        """Uma cópia só serve se for SQLite íntegra e ainda em schema 1."""
        try:
            copy = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    return False
                if copy.execute("PRAGMA user_version").fetchone()[0] != 1:
                    return False
                copy.execute("SELECT COUNT(*) FROM memories").fetchone()
                copy.execute("SELECT COUNT(*) FROM options").fetchone()
                return True
            finally:
                copy.close()
        except (sqlite3.Error, OSError):
            return False

    def _backup_v1(self) -> None:
        """Cópia SQLite consistente antes do schema 2.

        Uma cópia anterior só é preservada se for válida (integrity ok e schema 1); um
        marcador vazio/truncado deixado por uma tentativa interrompida é substituído por
        uma cópia nova, validada em ficheiro temporário e só depois promovida. Em falha,
        nenhum temporário ou marcador inválido fica para ser confiado na tentativa seguinte.
        """
        target = self.path.with_name(self.path.name + ".pre-v2.bak")
        if target.exists() and self._valid_v1_copy(target):
            return
        temp = target.with_name(target.name + ".tmp")
        if temp.exists():
            temp.unlink()
        dest = sqlite3.connect(temp)
        try:
            self.db.backup(dest)
        finally:
            dest.close()
        try:
            if not self._valid_v1_copy(temp):
                raise RuntimeError("Cópia de segurança v1 inválida; migração abortada.")
            os.replace(temp, target)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    def _migrate_v1_to_v2(self) -> None:
        """Migração real 1→2: backup, documento canónico e legado preservado.

        Numa transação única: cria as tabelas novas, concatena os perfis ativos no
        documento (texto intacto, um parágrafo por item), gera o índice derivado,
        marca os registos antigos como histórico (`superseded`) e liga `auto_learn`
        a `enabled AND proposals`. Em falha, nada é aplicado e a base fica em v1.
        """
        self._backup_v1()
        with self.transaction():
            for statement in _SCHEMA_V2:
                self.db.execute(statement)
            rows = self.db.execute(
                "SELECT id,kind,text,created_at,expires_at FROM memories WHERE status='active' "
                "AND kind IN (?,?,?,?) ORDER BY created_at,id", PROFILE_KINDS
            ).fetchall()
            pieces: list[str] = []
            # Expiração do item atravessa a migração: um facto temporário antigo não
            # pode passar a permanente só porque o documento não tem coluna de validade.
            ranges: list[tuple[int, int, str, str | None]] = []
            cursor = 0
            for row in rows:
                text = row["text"]
                if pieces:
                    cursor += 2
                pieces.append(text)
                ranges.append((cursor, cursor + len(text), row["kind"], row["expires_at"]))
                cursor += len(text)
            document = "\n".join([]) or "\n\n".join(pieces)
            revision = 1 if document.strip() else 0
            self.db.execute(
                "INSERT INTO profile_document(id,text,revision,previous_text,"
                "previous_chunks_json,updated_at) VALUES(1,?,?,NULL,NULL,?)",
                (document, revision, now()),
            )
            chunk_rows = []
            for position, span in enumerate(segment(document)):
                owner = next(((kind, expires) for start, end, kind, expires in ranges
                              if start <= span.start < end), (DEFAULT_KIND, None))
                chunk_rows.append(("chunk:" + uuid.uuid4().hex[:16], position, span.text,
                                   owner[0], "manual", None, owner[1], span.start, span.end, None,
                                   "user_explicit"))
            self.db.executemany(
                "INSERT INTO profile_chunks(chunk_id,position,text,kind,origin,"
                "learned_key,expires_at,start_offset,end_offset,supersedes_id,source) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                chunk_rows,
            )
            self.db.execute(
                "UPDATE memories SET status='superseded',updated_at=? WHERE status='active' "
                "AND kind IN (?,?,?,?)", (now(), *PROFILE_KINDS)
            )
            options = {row["key"]: bool(row["value"])
                       for row in self.db.execute("SELECT * FROM options")}
            auto_learn = options.get("enabled", True) and options.get("proposals", True)
            self.db.execute("INSERT OR IGNORE INTO options VALUES('auto_learn',?)",
                            (1 if auto_learn else 0,))
            self.db.execute("PRAGMA user_version=2")

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
            if row is not None:
                return row_item(row)
            # Perfil v2: o id público é o do trecho no índice derivado.
            chunk = db.execute("SELECT * FROM profile_chunks WHERE chunk_id=?", (item_id,)).fetchone()
            if chunk is None:
                raise KeyError("Memória não encontrada.")
            return MemoryItem(id=chunk["chunk_id"], kind=chunk["kind"], text=chunk["text"],
                              status="active",
                              source="system" if chunk["origin"] == "learned" else chunk["source"],
                              supersedes_id=chunk["supersedes_id"])

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

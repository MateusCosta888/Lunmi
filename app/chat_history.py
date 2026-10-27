"""Local public chat archive, independent of personal memory and execution logs."""
from __future__ import annotations

import json
import builtins
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.memory.contracts import now
from app.memory.policy import MemoryPolicyError, identifier, normalized, safe_chat_text, safe_text
from app.memory.store import fts_query

PAGE_SIZE = 50
NOT_SAVED = "Esta conversa não foi guardada por completo. Pode continuar a conversar."
PRIVATE = "[Conteúdo não guardado pela proteção de dados.]"
DEFAULT_OPTIONS: dict[str, Any] = {"enabled": True, "startup": "resume", "last_id": None}


def public_text(text: str) -> str:
    try:
        # Preserve paragraphs/code; reuse the same secret/private-content boundary as the profile.
        safe_chat_text(text)
        return text
    except MemoryPolicyError:
        return PRIVATE


def title_text(text: Any) -> str:
    return safe_text(text, maximum=80)[:80]


class HistoryStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.create_function('normalized', 1, normalized, deterministic=True)
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA secure_delete=ON")
            self.db.execute("PRAGMA journal_mode=DELETE")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Versão de histórico incompatível.")
            with self.transaction() as db:
                if version == 0:
                    db.execute("CREATE TABLE conversations(id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                               "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
                    db.execute("CREATE INDEX conversation_activity ON conversations(updated_at DESC,id)")
                    db.execute("CREATE TABLE messages(seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, "
                               "conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, "
                               "task_id TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, "
                               "created_at TEXT NOT NULL, state TEXT NOT NULL, eligible INTEGER NOT NULL, "
                               "metadata TEXT NOT NULL)")
                    db.execute("CREATE INDEX message_conversation ON messages(conversation_id,seq)")
                    db.execute("CREATE VIRTUAL TABLE message_fts USING fts5(text,content='messages',"
                               "content_rowid='seq',tokenize='unicode61 remove_diacritics 2')")
                    db.execute("CREATE TRIGGER message_insert AFTER INSERT ON messages BEGIN "
                               "INSERT INTO message_fts(rowid,text) VALUES(new.seq,new.text); END")
                    db.execute("CREATE TRIGGER message_delete AFTER DELETE ON messages BEGIN "
                               "INSERT INTO message_fts(message_fts,rowid,text) "
                               "VALUES('delete',old.seq,old.text); END")
                    db.execute("CREATE TABLE options(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                    db.executemany("INSERT INTO options VALUES(?,?)",
                                   [(key, json.dumps(value)) for key, value in DEFAULT_OPTIONS.items()])
                    db.execute("PRAGMA user_version=1")
                # Recovery is data-only. No task/approval/source/speech is replayed.
                db.execute("UPDATE messages SET state='interrupted',eligible=0 WHERE state='pending'")
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
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

    def options(self) -> dict[str, Any]:
        with self.lock:
            return {row['key']: json.loads(row['value']) for row in self.db.execute("SELECT * FROM options")}

    def set_options(self, values: dict[str, Any]) -> None:
        with self.transaction() as db:
            db.executemany("UPDATE options SET value=? WHERE key=?",
                           [(json.dumps(value), key) for key, value in values.items()])

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        with self.lock:
            row = self.db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if row is None:
                raise KeyError("Conversa não encontrada.")
            return dict(row)

    def create(self, conversation: dict[str, Any]) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO conversations VALUES(?,?,?,?)",
                       tuple(conversation[key] for key in ('id', 'title', 'created_at', 'updated_at')))
            db.execute("UPDATE options SET value=? WHERE key='last_id'", (json.dumps(conversation['id']),))

    def list(self, query: str = '', offset: int = 0) -> dict[str, Any]:
        expression = fts_query(query)
        where, params = '', []
        if query.strip():
            where = " WHERE (instr(normalized(c.title),normalized(?))>0"
            params.append(query.strip())
            if expression:
                where += " OR c.id IN (SELECT conversation_id FROM messages JOIN message_fts "
                where += "ON messages.seq=message_fts.rowid WHERE message_fts MATCH ?)"
                params.append(expression)
            where += ")"
        with self.lock:
            rows = self.db.execute("SELECT c.* FROM conversations c" + where +
                                   " ORDER BY c.updated_at DESC,c.id LIMIT 51 OFFSET ?",
                                   (*params, offset)).fetchall()
        return {"conversations": [dict(row) for row in rows[:PAGE_SIZE]], "has_more": len(rows) > PAGE_SIZE}

    def read(self, conversation_id: str, before: int | None = None) -> dict[str, Any]:
        self.conversation(conversation_id)
        with self.lock:
            rows = self.db.execute("SELECT * FROM messages WHERE conversation_id=? AND seq<? "
                                   "ORDER BY seq DESC LIMIT 51", (conversation_id, before or 2**63-1)).fetchall()
        page = rows[:PAGE_SIZE]
        messages = [{**json.loads(row['metadata']), 'message_id': row['id'],
                     'conversation_id': conversation_id, 'task_id': row['task_id'], 'role': row['role'],
                     'text': row['text'], 'created_at': row['created_at'],
                     'state': 'interrupted' if row['state'] == 'pending' else row['state']}
                    for row in reversed(page)]
        return {"messages": messages, "has_more": len(rows) > PAGE_SIZE,
                "before": page[-1]['seq'] if page else None}

    def append(self, message: dict[str, Any], *, eligible: bool = False) -> None:
        fixed = {'message_id', 'conversation_id', 'task_id', 'role', 'text', 'created_at', 'state'}
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO messages(id,conversation_id,task_id,role,text,created_at,"
                       "state,eligible,metadata) VALUES(?,?,?,?,?,?,?,?,?)",
                       (message['message_id'], message['conversation_id'], message['task_id'], message['role'],
                        message['text'], message['created_at'], message['state'], int(eligible),
                        json.dumps({k: v for k, v in message.items() if k not in fixed}, ensure_ascii=False)))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?",
                       (message['created_at'], message['conversation_id']))

    def finish(self, task_id: str, state: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE messages SET state=? WHERE task_id=? AND state='pending'", (state, task_id))

    def context(self, conversation_id: str) -> builtins.list[tuple[str, str]]:
        with self.lock:
            rows = self.db.execute("SELECT u.text,a.text FROM messages a JOIN messages u "
                                   "ON u.task_id=a.task_id AND u.role='user' "
                                   "AND u.conversation_id=a.conversation_id "
                                   "WHERE a.conversation_id=? AND a.role='assistant' AND a.eligible=1 "
                                   "AND a.state='completed' AND u.state='completed' ORDER BY a.seq DESC LIMIT 6",
                                   (conversation_id,)).fetchall()
        return [(row[0], row[1]) for row in reversed(rows)]

    def rename(self, conversation_id: str, title: str) -> None:
        self.conversation(conversation_id)
        with self.transaction() as db:
            db.execute("UPDATE conversations SET title=? WHERE id=?", (title, conversation_id))

    def delete(self, conversation_id: str | None) -> None:
        with self.transaction() as db:
            if conversation_id is None:
                db.execute("DELETE FROM conversations")
                db.execute("UPDATE options SET value='null' WHERE key='last_id'")
            else:
                db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
                if self.options()['last_id'] == conversation_id:
                    db.execute("UPDATE options SET value='null' WHERE key='last_id'")

    def close(self) -> None:
        with self.lock:
            self.db.close()


class ChatHistory:
    """Backend authority; unavailable storage degrades to RAM, with an explicit notice."""

    def __init__(self, path: Path) -> None:
        self.store: HistoryStore | None = None
        self.error: str | None = None
        self.options = dict(DEFAULT_OPTIONS)
        self.cache: dict[str, dict[str, Any]] = {}
        self.active: dict[str, Any]
        self.pending: dict[str, tuple[bool, bool]] = {}
        try:
            self.store = HistoryStore(path)
            self.options = self.store.options()
            last = self.options['last_id']
            if self.options['startup'] == 'resume' and last:
                self.select(last)
                return
        except KeyError:
            pass
        except (sqlite3.Error, OSError, ValueError):
            self.error = NOT_SAVED
        self.new()

    def new(self, temporary: bool = False) -> dict[str, Any]:
        stamp = now()
        self.active = {'id': 'conv-' + uuid.uuid4().hex, 'title': 'Nova conversa',
                       'created_at': stamp, 'updated_at': stamp, 'temporary': temporary,
                       'saved': False, 'messages': [], 'pairs': [], 'has_more': False, 'before': None}
        self.cache[self.active['id']] = self.active
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {'conversation': {k: self.active[k] for k in
                                ('id', 'title', 'created_at', 'updated_at', 'temporary', 'saved')},
                'messages': self.active['messages'][-PAGE_SIZE:], 'has_more': self.active['has_more'],
                'before': self.active['before'], 'options': {k: self.options[k] for k in ('enabled', 'startup')},
                'error': self.error}

    def select(self, conversation_id: str) -> dict[str, Any]:
        identifier(conversation_id)
        if conversation_id in self.cache:
            target = self.cache[conversation_id]
        elif self.store:
            target = {**self.store.conversation(conversation_id), **self.store.read(conversation_id),
                      'temporary': False, 'saved': True, 'pairs': self.store.context(conversation_id)}
            self.cache[conversation_id] = target
        else:
            raise KeyError("Conversa não encontrada.")
        # Only saved conversations can become a startup target.
        if target['saved'] and self.store:
            self.store.set_options({'last_id': conversation_id})
        self.active = target
        return self.snapshot()

    def accept(self, message: dict[str, Any], *, attachments: bool) -> None:
        active = self.active
        first = not active['messages']
        active['messages'].append(message)
        active['updated_at'] = message['created_at']
        if first:
            try:
                active['title'] = title_text(' '.join(message['text'].split())[:80])
            except MemoryPolicyError:
                active['title'] = 'Conversa'
        allowed = bool(self.options['enabled'] and not active['temporary'])
        saved = False
        if allowed:
            try:
                if not self.store:
                    raise OSError()
                if not active['saved']:
                    # Enabling recording later must not archive the title of an unsaved message.
                    try:
                        active['title'] = title_text(' '.join(message['text'].split())[:80])
                    except MemoryPolicyError:
                        active['title'] = 'Conversa'
                persisted = {**message, 'text': public_text(message['text']), 'state': 'pending'}
                if 'attachments' in persisted:
                    persisted['attachments'] = [{**item, 'name': public_text(item['name'])}
                                                for item in persisted['attachments']]
                with self.store.transaction():
                    self.store.create(active)
                    self.store.append(persisted)
                active['saved'] = True
                saved = True
            except (sqlite3.Error, OSError):
                self.error = NOT_SAVED
        self.pending[message['task_id']] = (saved, attachments)

    def complete(self, message: dict[str, Any], *, eligible: bool) -> None:
        saved, attachments = self.pending.pop(message['task_id'], (False, False))
        self.active['messages'].append(message)
        self.active['updated_at'] = message['created_at']
        if saved and self.store:
            text = PRIVATE if attachments else public_text(message['text'])
            metadata = {**message, 'text': text}
            if attachments or text == PRIVATE:
                metadata.pop('sources', None)
            try:
                # Validate all metadata too: a URL/title/filename can contain a secret.
                if public_text(json.dumps(metadata, ensure_ascii=False)) == PRIVATE:
                    metadata = {k: v for k, v in metadata.items() if k in
                                {'message_id', 'conversation_id', 'task_id', 'role', 'created_at', 'state'}}
                    metadata['text'] = PRIVATE
                user = next((m for m in self.active['messages'] if m['task_id'] == message['task_id']
                             and m['role'] == 'user'), None)
                safe_user = bool(user and public_text(user['text']) != PRIVATE)
                with self.store.transaction():
                    self.store.append(metadata, eligible=eligible and metadata['text'] != PRIVATE
                                      and not attachments and safe_user)
                    self.store.finish(message['task_id'], message['state'])
                page = self.store.read(self.active['id'])
                self.active.update(has_more=page['has_more'], before=page['before'])
            except (sqlite3.Error, OSError):
                self.error = NOT_SAVED

    def remember_context(self, pairs: list[tuple[str, str]]) -> None:
        self.active['pairs'] = pairs

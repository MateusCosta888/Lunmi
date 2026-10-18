"""Documento canónico do perfil pessoal: texto livre, proveniência e revisão CAS.

Regras que este módulo garante:
- O texto é a fonte de verdade; parágrafos/listas/espaços nunca são reinterpretados como
  registos. ``Enter`` não cria item.
- Um índice derivado segmenta o documento em trechos de até ``CHUNK_MAX_CHARS`` para busca,
  contexto e APIs antigas, com offsets corretos sobre o texto canónico.
- A proveniência (``manual``/``learned``) vive nas linhas do índice e é transferida por
  reconciliação; um trecho editado pelo utilizador torna-se ``manual`` e nunca é alterado
  pela aprendizagem.
- Uma revisão global monotónica (CAS) e um único snapshot anterior (texto + proveniência)
  dão desfazer de um nível.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from app.memory.contracts import now
from app.memory.policy import MemoryPolicyError, normalized, safe_document, safe_text

PROFILE_MAX_CHARS = 8000
CHUNK_MAX_CHARS = 800
DEFAULT_KIND = "preference"
PROFILE_KINDS = ("preference", "fact", "habit", "decision")
ORIGINS = ("manual", "learned")
TEMPORAL = re.compile(r"\b(hoje|agora|esta semana|today|currently)\b")

_PARAGRAPH = re.compile(r"(?s)(?:[^\n]|\n(?!\s*\n))+")
_SENTENCE_CUTS = (". ", "! ", "? ", "; ", ":")


class StaleRevision(MemoryPolicyError):
    def __init__(self, revision: int, can_undo: bool) -> None:
        super().__init__()
        self.revision = revision
        self.can_undo = can_undo


class NothingToUndo(MemoryPolicyError):
    def __init__(self, revision: int) -> None:
        super().__init__()
        self.revision = revision


class ProfileTooLarge(MemoryPolicyError):
    def __init__(self, char_count: int, max_chars: int) -> None:
        super().__init__()
        self.char_count = char_count
        self.max_chars = max_chars


class MemoryDisabled(MemoryPolicyError):
    pass


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    text: str
    kind: str
    origin: str
    learned_key: str | None
    expires_at: str | None
    start: int = 0
    end: int = 0
    supersedes_id: str | None = None
    source: str = "user_explicit"


@dataclass(frozen=True)
class Snapshot:
    text: str
    revision: int
    can_undo: bool
    chunks: tuple[Chunk, ...] = ()

    @property
    def char_count(self) -> int:
        return len(self.text)


def paragraph_spans(text: str) -> list[Span]:
    """Parágrafos separados por linha em branco, com offsets no texto original."""
    spans: list[Span] = []
    for match in _PARAGRAPH.finditer(text):
        raw = match.group(0)
        stripped = raw.strip()
        if not stripped:
            continue
        lead = len(raw) - len(raw.lstrip())
        start = match.start() + lead
        spans.append(Span(start, start + len(stripped), stripped))
    return spans


def _best_cut(window: str) -> int:
    floor = max(1, int(len(window) * 0.4))
    newline = window.rfind("\n")
    if newline >= floor:
        return newline
    best = -1
    for marker in _SENTENCE_CUTS:
        found = window.rfind(marker)
        if found > best:
            best = found + len(marker) - 1
    if best >= floor:
        return best + 1
    space = window.rfind(" ")
    if space >= floor:
        return space
    return 0


def _segment_paragraph(base: int, paragraph: str) -> list[Span]:
    if len(paragraph) <= CHUNK_MAX_CHARS:
        return [Span(base, base + len(paragraph), paragraph)]
    spans: list[Span] = []
    position = 0
    while position < len(paragraph):
        limit = position + CHUNK_MAX_CHARS
        if limit >= len(paragraph):
            piece = paragraph[position:].strip()
            if piece:
                lead = len(paragraph[position:]) - len(paragraph[position:].lstrip())
                spans.append(Span(base + position + lead, base + position + lead + len(piece), piece))
            break
        window = paragraph[position:limit]
        cut = _best_cut(window)
        if cut <= 0:
            cut = CHUNK_MAX_CHARS
        raw = paragraph[position:position + cut]
        piece = raw.strip()
        if piece:
            lead = len(raw) - len(raw.lstrip())
            spans.append(Span(base + position + lead, base + position + lead + len(piece), piece))
        position += cut
        while position < len(paragraph) and paragraph[position].isspace():
            position += 1
    return spans


def segment(text: str) -> list[Span]:
    """Índice derivado do documento: parágrafos, divididos internamente em <=800 chars."""
    spans: list[Span] = []
    for paragraph in paragraph_spans(text):
        spans.extend(_segment_paragraph(paragraph.start, paragraph.text))
    return spans


def digest(text: str) -> str:
    return hashlib.sha256(normalized(text).strip().encode("utf-8")).hexdigest()


def _new_id() -> str:
    return "chunk:" + uuid.uuid4().hex[:16]


def _chunk_from_row(row: Any) -> Chunk:
    return Chunk(
        chunk_id=row["chunk_id"],
        text=row["text"],
        kind=row["kind"],
        origin=row["origin"],
        learned_key=row["learned_key"],
        expires_at=row["expires_at"],
        start=int(row["start_offset"]),
        end=int(row["end_offset"]),
        supersedes_id=row["supersedes_id"],
        source=row["source"],
    )


def reconcile(text: str, old: Iterable[Chunk]) -> list[Chunk]:
    """Reprojeta o índice sobre o texto novo, preservando proveniência.

    Um trecho cujo texto não mudou conserva id, origem e chave aprendida. Um trecho novo
    (texto editado, inserido ou dividido) herda o id do antigo correspondente e passa a
    ``manual``: é a edição do utilizador que confirma e protege aquele conteúdo.
    """
    pool = list(old)
    used: set[int] = set()
    spans = segment(text)
    result: list[Chunk | None] = [None] * len(spans)

    for index, span in enumerate(spans):
        for position, chunk in enumerate(pool):
            if position in used:
                continue
            if chunk.text == span.text:
                used.add(position)
                result[index] = Chunk(chunk.chunk_id, span.text, chunk.kind, chunk.origin,
                                      chunk.learned_key, chunk.expires_at, span.start, span.end,
                                      chunk.supersedes_id, chunk.source)
                break

    remaining = [i for i, item in enumerate(result) if item is None]
    available = [c for position, c in enumerate(pool) if position not in used]
    for offset, index in enumerate(remaining):
        span = spans[index]
        if offset < len(available):
            previous = available[offset]
            result[index] = Chunk(previous.chunk_id, span.text, previous.kind, "manual",
                                  None, None, span.start, span.end, previous.supersedes_id,
                                  "user_explicit")
        else:
            result[index] = Chunk(_new_id(), span.text, DEFAULT_KIND, "manual",
                                  None, None, span.start, span.end, None, "user_explicit")

    return [item for item in result if item is not None]


def join_paragraphs(existing: str, piece: str) -> str:
    """Acrescenta um parágrafo mantendo o texto existente como prefixo byte a byte.

    Espaços à direita, linhas em branco e parágrafos vazios do utilizador são conteúdo:
    nenhum ``rstrip``/``strip`` é aplicado ao que já estava no documento.
    """
    if existing == "":
        return piece
    return existing + "\n\n" + piece


class ProfileDocument:
    """Fonte canónica do perfil; todas as escritas passam pela revisão global."""

    def __init__(self, store: Any) -> None:
        self.store = store

    # ------------------------------------------------------------- leitura --
    def _doc(self, db: Any) -> dict[str, Any]:
        row = db.execute("SELECT * FROM profile_document WHERE id=1").fetchone()
        if row is None:
            raise MemoryPolicyError()
        return {
            "text": row["text"],
            "revision": int(row["revision"]),
            "previous_text": row["previous_text"],
            "previous_chunks_json": row["previous_chunks_json"],
        }

    def _options(self, db: Any) -> dict[str, bool]:
        return {row["key"]: bool(row["value"]) for row in db.execute("SELECT * FROM options")}

    def _chunks(self, db: Any) -> list[Chunk]:
        rows = db.execute("SELECT * FROM profile_chunks ORDER BY position").fetchall()
        return [_chunk_from_row(row) for row in rows]

    def snapshot(self) -> Snapshot:
        with self.store.transaction() as db:
            doc = self._doc(db)
            chunks = self._chunks(db)
            return Snapshot(doc["text"], doc["revision"], doc["previous_text"] is not None,
                            tuple(chunks))

    @property
    def revision(self) -> int:
        with self.store.transaction() as db:
            return int(self._doc(db)["revision"])

    def chunks(self) -> list[Chunk]:
        with self.store.transaction() as db:
            return self._chunks(db)

    # ------------------------------------------------------------- escrita --
    def _write_chunks(self, db: Any, chunks: Iterable[Chunk]) -> None:
        db.execute("DELETE FROM profile_chunks")
        db.executemany(
            "INSERT INTO profile_chunks(chunk_id,position,text,kind,origin,learned_key,"
            "expires_at,start_offset,end_offset,supersedes_id,source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(chunk.chunk_id, position, chunk.text, chunk.kind, chunk.origin,
              chunk.learned_key, chunk.expires_at, chunk.start, chunk.end, chunk.supersedes_id,
              chunk.source) for position, chunk in enumerate(chunks)],
        )

    def _commit(self, db: Any, text: str, chunks: list[Chunk], doc: dict[str, Any],
                *, keep_undo: bool, previous_chunks: list[Chunk]) -> Snapshot:
        # O snapshot descreve o estado ANTERIOR: texto antigo + proveniência antiga.
        revision = doc["revision"] + 1
        previous_text = doc["text"] if keep_undo else None
        previous_chunks_json = json.dumps(
            [{"id": chunk.chunk_id, "text": chunk.text, "kind": chunk.kind,
              "origin": chunk.origin, "learned_key": chunk.learned_key,
              "expires_at": chunk.expires_at, "supersedes": chunk.supersedes_id,
              "source": chunk.source} for chunk in previous_chunks],
            ensure_ascii=False,
        ) if keep_undo else None
        self._write_chunks(db, chunks)
        db.execute("UPDATE profile_document SET text=?,revision=?,previous_text=?,"
                   "previous_chunks_json=?,updated_at=? WHERE id=1",
                   (text, revision, previous_text, previous_chunks_json, _stamp()))
        return Snapshot(text, revision, previous_text is not None, tuple(chunks))

    def save(self, text: str, base_revision: int) -> Snapshot:
        value = safe_document(text)
        with self.store.transaction() as db:
            doc = self._doc(db)
            options = self._options(db)
            if not options.get("enabled"):
                raise MemoryDisabled()
            if base_revision != doc["revision"]:
                raise StaleRevision(doc["revision"], doc["previous_text"] is not None)
            if len(value) > PROFILE_MAX_CHARS and len(value) > len(doc["text"]):
                raise ProfileTooLarge(len(value), PROFILE_MAX_CHARS)
            previous = self._chunks(db)
            chunks = reconcile(value, previous)
            return self._commit(db, value, chunks, doc, keep_undo=True,
                                previous_chunks=previous)

    def undo(self, base_revision: int) -> Snapshot:
        with self.store.transaction() as db:
            doc = self._doc(db)
            options = self._options(db)
            if not options.get("enabled"):
                raise MemoryDisabled()
            if base_revision != doc["revision"]:
                raise StaleRevision(doc["revision"], doc["previous_text"] is not None)
            if doc["previous_text"] is None or doc["previous_chunks_json"] is None:
                raise NothingToUndo(doc["revision"])
            text = doc["previous_text"]
            saved = json.loads(doc["previous_chunks_json"])
            spans = segment(text)
            chunks: list[Chunk] = []
            for position, span in enumerate(spans):
                record = saved[position] if position < len(saved) else {}
                origin = record.get("origin") if record.get("origin") in ORIGINS else "manual"
                chunks.append(Chunk(
                    str(record.get("id") or _new_id()), span.text,
                    str(record.get("kind") or DEFAULT_KIND),
                    str(origin), record.get("learned_key"), record.get("expires_at"),
                    span.start, span.end, record.get("supersedes"),
                    str(record.get("source") or "user_explicit"),
                ))
            revision = doc["revision"] + 1
            self._write_chunks(db, chunks)
            db.execute("UPDATE profile_document SET text=?,revision=?,previous_text=NULL,"
                       "previous_chunks_json=NULL,updated_at=? WHERE id=1",
                       (text, revision, _stamp()))
            return Snapshot(text, revision, False, tuple(chunks))

    # ------------------------------------------------------------ legado/API --
    def append(self, text: str, kind: str, *, origin: str = "manual",
               learned_key: str | None = None,
               expires_at: str | None = None,
               replaces_id: str | None = None,
               source: str = "user_explicit") -> str:
        value = safe_text(text, maximum=PROFILE_MAX_CHARS)
        if kind not in PROFILE_KINDS or origin not in ORIGINS:
            raise MemoryPolicyError()
        with self.store.transaction() as db:
            doc = self._doc(db)
            chunks = self._chunks(db)
            target = normalized(value).strip(" .")
            replacement = next((chunk for chunk in chunks if chunk.chunk_id == replaces_id), None)
            if replacement is not None:
                # Substituição: o trecho antigo sai e o novo entra como parágrafo próprio,
                # apontando para o que substituiu (a supersessão não é esquecimento).
                moved = doc["text"][:replacement.start] + doc["text"][replacement.end:]
                start = len(moved) + (0 if moved == "" else 2)
                moved = join_paragraphs(moved, value)
                if len(moved) > PROFILE_MAX_CHARS and len(moved) > len(doc["text"]):
                    raise ProfileTooLarge(len(moved), PROFILE_MAX_CHARS)
                new_chunks = reconcile(moved, chunks)
                for position, chunk in enumerate(new_chunks):
                    if chunk.start == start and chunk.text == value:
                        new_chunks[position] = Chunk(_new_id(), chunk.text, kind, origin,
                                                     learned_key, expires_at, chunk.start,
                                                     chunk.end, replaces_id, source)
                        self._commit(db, moved, new_chunks, doc, keep_undo=True,
                                     previous_chunks=chunks)
                        return new_chunks[position].chunk_id
            for chunk in chunks:
                if normalized(chunk.text).strip(" .") == target:
                    return chunk.chunk_id
            moved = join_paragraphs(doc["text"], value)
            if len(moved) > PROFILE_MAX_CHARS and len(moved) > len(doc["text"]):
                raise ProfileTooLarge(len(moved), PROFILE_MAX_CHARS)
            new_chunks = reconcile(moved, chunks)
            start = len(doc["text"]) + (0 if doc["text"] == "" else 2)
            for position, chunk in enumerate(new_chunks):
                if chunk.start == start and chunk.text == value:
                    new_chunks[position] = Chunk(chunk.chunk_id, chunk.text, kind, origin,
                                                 learned_key, expires_at, chunk.start, chunk.end,
                                                 None, source)
                    break
            self._commit(db, moved, new_chunks, doc, keep_undo=True, previous_chunks=chunks)
            for chunk in new_chunks:
                if normalized(chunk.text).strip(" .") == target:
                    return chunk.chunk_id
            return new_chunks[-1].chunk_id

    def edit(self, chunk_id: str, text: str, *, kind: str | None = None,
             expires_at: str | None = None) -> Chunk:
        value = safe_text(text, maximum=CHUNK_MAX_CHARS)
        if kind is not None and kind not in PROFILE_KINDS:
            raise MemoryPolicyError()
        with self.store.transaction() as db:
            doc = self._doc(db)
            chunks = self._chunks(db)
            target = next((chunk for chunk in chunks if chunk.chunk_id == chunk_id), None)
            if target is None:
                raise KeyError("Memória não encontrada.")
            moved = doc["text"][:target.start] + value + doc["text"][target.end:]
            if len(moved) > PROFILE_MAX_CHARS and len(moved) > len(doc["text"]):
                raise ProfileTooLarge(len(moved), PROFILE_MAX_CHARS)
            new_chunks = reconcile(moved, chunks)
            self._commit(db, moved, new_chunks, doc, keep_undo=True, previous_chunks=chunks)
            for position, chunk in enumerate(new_chunks):
                if chunk.start == target.start and chunk.text == value:
                    new_chunks[position] = Chunk(
                        chunk.chunk_id, chunk.text, kind or chunk.kind, "manual",
                        None, expires_at, chunk.start, chunk.end, chunk.supersedes_id,
                        "user_explicit")
                    db.execute("UPDATE profile_chunks SET kind=?,origin='manual',learned_key=NULL,"
                               "expires_at=? WHERE chunk_id=?",
                               (kind or chunk.kind, expires_at, chunk.chunk_id))
                    return new_chunks[position]
            raise MemoryPolicyError()

    def mark_superseded(self, chunk_id: str, target_id: str) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE profile_chunks SET supersedes_id=? WHERE chunk_id=?",
                       (target_id, chunk_id))

    def drop(self, chunk_id: str) -> None:
        """Remove um trecho substituído (não é esquecimento: mantém undo)."""
        with self.store.transaction() as db:
            doc = self._doc(db)
            chunks = self._chunks(db)
            target = next((chunk for chunk in chunks if chunk.chunk_id == chunk_id), None)
            if target is None:
                return
            moved = doc["text"][:target.start] + doc["text"][target.end:]
            self._commit(db, moved, reconcile(moved, chunks), doc, keep_undo=True,
                         previous_chunks=chunks)

    def forget(self, chunk_id: str) -> None:
        """Esquecer explícito: remove o trecho, limpa o undo e grava só um digest."""
        with self.store.transaction() as db:
            doc = self._doc(db)
            chunks = self._chunks(db)
            target = next((chunk for chunk in chunks if chunk.chunk_id == chunk_id), None)
            if target is None:
                raise KeyError("Memória não encontrada.")
            moved = doc["text"][:target.start] + doc["text"][target.end:]
            db.execute("INSERT OR IGNORE INTO profile_forgotten(digest) VALUES(?)",
                       (digest(target.text),))
            self._commit(db, moved, reconcile(moved, chunks), doc, keep_undo=False,
                         previous_chunks=chunks)

    def forget_by_text(self, target: str, *, allow_suffix: bool = False) -> int:
        """Esquecer pelo chat: só alvo único e exato; devolve quantos parágrafos removeu.

        Nunca usa pesquisa difusa: uma palavra solta não apaga um bloco com outras
        informações. Com ``allow_suffix`` (invalidação "não quero mais X"), aceita um
        único trecho que TERMINE em X, sem conteúdo adicional depois do alvo.
        Ambiguidade devolve 0 e nada é escrito.
        """
        wanted = normalized(target).strip(" .")
        if not wanted:
            return 0
        with self.store.transaction() as db:
            doc = self._doc(db)
            chunks = self._chunks(db)
            spans = paragraph_spans(doc["text"])
            whole = [span for span in spans if normalized(span.text).strip(" .") == wanted]
            if len(whole) == 1:
                span = whole[0]
                moved = doc["text"][:span.start] + doc["text"][span.end:]
                db.execute("INSERT OR IGNORE INTO profile_forgotten(digest) VALUES(?)",
                           (digest(span.text),))
                self._commit(db, moved, reconcile(moved, chunks), doc, keep_undo=False,
                             previous_chunks=chunks)
                return 1
            exact = [chunk for chunk in chunks if normalized(chunk.text).strip(" .") == wanted]
            if not exact and allow_suffix:
                exact = [chunk for chunk in chunks
                         if normalized(chunk.text).strip(" .").endswith(" " + wanted)]
            if len(exact) == 1:
                chunk = exact[0]
                moved = doc["text"][:chunk.start] + doc["text"][chunk.end:]
                db.execute("INSERT OR IGNORE INTO profile_forgotten(digest) VALUES(?)",
                           (digest(chunk.text),))
                self._commit(db, moved, reconcile(moved, chunks), doc, keep_undo=False,
                             previous_chunks=chunks)
                return 1
            return 0

    # ------------------------------------------------------------ aprendizagem --
    def learn(self, changes: Iterable[dict[str, Any]], *, expected_revision: int) -> int:
        """Aplica alterações aprendidas com CAS; devolve quantas foram aplicadas."""
        with self.store.transaction() as db:
            doc = self._doc(db)
            options = self._options(db)
            if not options.get("enabled") or not options.get("auto_learn"):
                return 0
            if int(doc["revision"]) != int(expected_revision):
                return 0
            text = doc["text"]
            chunks = self._chunks(db)
            original_chunks = chunks
            applied = 0
            for change in list(changes)[:3]:
                text, chunks, hit = self._apply_one(db, text, chunks, change)
                applied += 1 if hit else 0
            if not applied:
                return 0
            if len(text) > PROFILE_MAX_CHARS and len(text) > len(doc["text"]):
                return 0
            self._commit(db, text, chunks, doc, keep_undo=True,
                         previous_chunks=original_chunks)
            return applied

    def _apply_one(self, db: Any, text: str, chunks: list[Chunk],
                   change: dict[str, Any]) -> tuple[str, list[Chunk], bool]:
        kind = change.get("kind")
        if kind not in PROFILE_KINDS:
            return text, chunks, False
        try:
            value = safe_text(change.get("text"), maximum=CHUNK_MAX_CHARS)
        except MemoryPolicyError:
            return text, chunks, False
        if TEMPORAL.search(normalized(value)):
            return text, chunks, False
        key = normalized(value).strip(" .")
        if not key:
            return text, chunks, False
        if db.execute("SELECT 1 FROM profile_forgotten WHERE digest=?", (digest(value),)).fetchone():
            return text, chunks, False
        replaces = change.get("replaces")
        if isinstance(replaces, str) and replaces.strip():
            wanted = normalized(replaces).strip(" .")
            targets = [chunk for chunk in chunks
                       if chunk.origin == "learned" and normalized(chunk.text).strip(" .") == wanted]
            if len(targets) == 1:
                target = targets[0]
                moved = text[:target.start] + value + text[target.end:]
                new_chunks = reconcile(moved, chunks)
                # A correção mantém-se aprendida: senão o mesmo facto nunca mais seria
                # reconciliável e a atualização automática ficaria bloqueada.
                for position, chunk in enumerate(new_chunks):
                    if chunk.start == target.start and chunk.text == value:
                        new_chunks[position] = Chunk(chunk.chunk_id, chunk.text, kind,
                                                     "learned", key, None,
                                                     chunk.start, chunk.end, None, "system")
                        break
                return moved, new_chunks, True
            # Alvo ambíguo ou manual: não adivinha e não apaga informação adicional.
            return text, chunks, False
        for chunk in chunks:
            if normalized(chunk.text).strip(" .") == key:
                return text, chunks, False
        moved = join_paragraphs(text, value)
        new_chunks = reconcile(moved, chunks)
        for position, span in enumerate(segment(moved)):
            if position >= len(new_chunks):
                break
            if new_chunks[position].text == value:
                new_chunks[position] = Chunk(new_chunks[position].chunk_id, new_chunks[position].text,
                                             kind, "learned", key, None,
                                             span.start, span.end, None, "system")
                break
        return moved, new_chunks, True

    def clear_undo(self) -> None:
        """Import/wipe não podem desfazer para um conteúdo que deixou de ser o atual."""
        with self.store.transaction() as db:
            db.execute("UPDATE profile_document SET previous_text=NULL,previous_chunks_json=NULL "
                       "WHERE id=1")

    # --------------------------------------------------------------- wipe --
    def wipe(self, db: Any) -> None:
        # A revisão avança mesmo ao esvaziar: um rascunho antigo nunca pode voltar a casar
        # por a revisão regressar a um valor já usado (ABA).
        row = db.execute("SELECT revision FROM profile_document WHERE id=1").fetchone()
        revision = (int(row["revision"]) if row is not None else 0) + 1
        db.execute("DELETE FROM profile_chunks")
        db.execute("DELETE FROM profile_forgotten")
        db.execute("UPDATE profile_document SET text='',revision=?,previous_text=NULL,"
                   "previous_chunks_json=NULL,updated_at=? WHERE id=1", (revision, _stamp()))


def _stamp() -> str:
    return now()

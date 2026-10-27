"""One bounded public response on the existing IPC, shared with future voice consumers."""
from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any, Callable, Iterable, Mapping

# Provenance precedes speech: only these backend templates may be marked verified and
# offered to a future voice consumer. A reply that carries file content (``read``) is
# data, even when the read itself was verified; an agent summary after a real operation
# is a summary, not evidence. The rest of the text stays visible, never spoken.
CONSULTATION_SPEECH = 'Consulta concluída e verificada. Conteúdo consultado (dados):'
OPERATION_SPEECH = 'Operação verificada. Resumo do agente (não é evidência):'

# Operations whose verified evidence proves a read, not a change to the desktop.
READ_ONLY_OPERATIONS = frozenset({
    'read', 'list', 'search', 'exists', 'metadata', 'get_text', 'texts',
    'uia_observe', 'uia_find_element', 'uia_verify', 'verify_window', 'filesystem_read',
})


def verified_speech_prefix(evidence: Iterable[Any] | None, *, scope: str,
                           status: str) -> str:
    """The one template this turn is allowed to speak, decided by evidence alone.

    Conversation and memory turns keep their own answer text; automation may only speak
    a fixed backend sentence, because the attached content or the agent's comment can
    claim an action nobody performed or verified.
    """
    if status != 'SUCCESS' or scope in {'conversation', 'memory_command', 'engine_only'}:
        return ''
    verified = [item for item in (evidence or [])
                if isinstance(item, Mapping) and item.get('verified') is True]
    if not verified:
        return ''
    if all(_read_only(item) for item in verified):
        return CONSULTATION_SPEECH
    return OPERATION_SPEECH


def _read_only(item: Mapping[str, Any]) -> bool:
    """The runtime's explicit flag wins over the tool name.

    The automation layer marks real reads returned under ``allow_actions=False`` with
    ``read_only: true`` (tools such as ``find_application`` included). Launch evidence
    never carries that flag, so it can never be announced from a consultation.
    """
    flag = item.get('read_only')
    if flag is True:
        return True
    if flag is False:
        return False
    return str(item.get('operation') or '').strip() in READ_ONLY_OPERATIONS


def _public_text(prefix: str, text: str) -> str:
    return f'{prefix} {text}' if prefix and text else (prefix or text)


class PublicResponse:
    def __init__(self, emit: Callable[[str, dict[str, Any]], None], session_id: str, task_id: str) -> None:
        self.emit = emit
        self.ids = {'session_id': session_id, 'task_id': task_id,
                    'response_id': 'rsp-' + uuid.uuid4().hex[:16],
                    'message_id': 'msg-' + uuid.uuid4().hex[:16]}
        self.seq = 0
        self.text = ''
        self.pending = ''
        self.stable_end = 0
        self.closed = False
        self.started = False
        self.final_state = 'interrupted'
        self.verified_result = False
        self.timer: asyncio.TimerHandle | None = None

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        self.seq += 1
        self.emit(kind, {**self.ids, 'seq': self.seq, **data})

    def start(self, streaming: bool = False) -> None:
        if not self.started and not self.closed:
            self.started = True
            self._emit('response.started', {'streaming': streaming})

    def receive(self, kind: str, data: dict[str, Any]) -> None:
        if self.closed:
            return
        if kind == 'response.started':
            self.start(bool(data.get('streaming')))
        elif kind == 'response.text.delta':
            self.start()
            text = data.get('text')
            if not isinstance(text, str) or len(self.text) + len(self.pending) + len(text) > 8000:
                raise ValueError('Texto público inválido.')
            self.pending += text
            if self.timer is None:
                self.timer = asyncio.get_running_loop().call_later(.025, self.flush)
        elif kind in {'operation.started', 'operation.finished'}:
            self.start()
            self.flush()
            # Operation events carry status/identity plus the closed diagnostic vocabulary;
            # never arguments, control labels or raw exception text.
            self._emit(kind, {k: data[k] for k in ('operation_id', 'state', 'verified', 'tool',
                                                   'category', 'failure_reason') if k in data})

    def flush(self) -> None:
        if self.timer:
            self.timer.cancel()
            self.timer = None
        if self.closed or not self.pending:
            return
        offset = len(self.text)
        delta, self.pending = self.pending, ''
        self.text += delta
        self._emit('response.text.delta', {'offset': offset, 'text': delta})
        # Retain unfinished sentences; a decimal point is not a speech boundary.
        ends = list(re.finditer(r'[.!?](?:["”»])?\s+', self.text[self.stable_end:]))
        if ends:
            self._stable(self.stable_end + ends[-1].end())

    def _stable(self, end: int) -> None:
        if end > self.stable_end:
            start, self.stable_end = self.stable_end, end
            text = self.text[start:end]
            inside_code = self.text[:start].count('```') % 2
            if inside_code or '`' in text or text.lstrip().startswith(('{', '[')):
                return
            self._emit('response.text.stable', {'start': start, 'end': end, 'text': text,
                       'source': 'verified_result' if self.verified_result else 'answer',
                       'verified': self.verified_result})

    def finish(self, text: str, status: str, *, verified_result: bool = False,
               verified_prefix: str = '') -> None:
        """Close the response with its canonical text and its provenance.

        ``verified_prefix`` is the backend template the evidence supports. It is only
        applied when nothing has been published yet (offsets are append-only), it is the
        only range marked stable/verified, and the remaining text stays visible as data
        or as an unverified summary.
        """
        if self.closed:
            return
        self.start()
        prefix = str(verified_prefix or '').strip() if verified_result else ''
        if prefix and self.text:
            # Provenance cannot be prepended after public text without rewriting offsets.
            prefix = ''
        canonical = _public_text(prefix, text)
        if self.text and not canonical.startswith(self.text):
            self.fail('stream_mismatch')
            return
        if self.text + self.pending and not canonical.startswith(self.text + self.pending):
            self.fail('stream_mismatch')
            return
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.pending = ''
        self.verified_result = bool(verified_result and prefix)
        if len(canonical) > len(self.text):
            offset = len(self.text)
            delta = canonical[offset:]
            self.text = canonical
            self._emit('response.text.delta', {'offset': offset, 'text': delta})
        # Speech ends where the template ends: content and summaries are never spoken.
        self._stable(len(prefix) if prefix else len(self.text))
        self._emit('response.completed', {'text': self.text, 'status': status})
        self.final_state = 'completed'
        self.closed = True

    def fail(self, code: str, *, cancelled: bool = False) -> None:
        if self.closed:
            return
        self.start()
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.pending = ''
        self._emit('response.cancelled' if cancelled else 'response.error', {'code': code})
        self.final_state = 'cancelled' if cancelled else 'interrupted'
        self.closed = True

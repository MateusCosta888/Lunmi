"""Contrato IPC JSONL v1 da Lunmi: framing, validação de envelope, limites e capabilities.

Regras inegociáveis (docs/IPC-CONTRACT.md):
- Envelope obrigatório: `v: 1`, `id: string`, `type: string`, `payload: object`.
- Respostas usam o mesmo `id` do comando com `type` em `command.result` ou `command.error`.
- Eventos usam `id` próprio gerado pelo backend.
- Limites verificados antes de alocar ou desserializar payloads gigantes.
- JSON e UTF-8 estritos nos dois lados (NaN/Infinity recusados na entrada e saída; bytes UTF-8 inválidos recusados).
- Nenhum traceback, Settings bruto ou segredo sai em `command.error` ou eventos.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

PROTOCOL_VERSION = 1

# Limites formais declarados e unificados no contrato v1 (Python + TypeScript + golden + docs)
MAX_IMPORT_BYTES = 4 * 1024 * 1024          # 4 MiB (preserva teto de importação de memória de app/ui/memory_window.py)
MAX_LINE_BYTES = 5 * 1024 * 1024            # 5 MiB por frame JSONL (acomoda importação de 4 MiB + envelope JSON)
MAX_CHAT_TEXT_CHARS = 8000                  # Teto de mensagem de chat no protocolo (controller valida instrução em 4000)
MAX_MEMORY_TEXT_CHARS = 800                 # Teto de texto de memória (paridade com app/memory/policy.py)
MAX_ID_CHARS = 128                          # Teto para identificadores de correlação
MAX_TYPE_CHARS = 64                         # Teto para o nome do comando/evento
MAX_HOST_PIDS = 64                          # Teto de PIDs da árvore do host enviados pelo processo pai confiável

ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.]*$")

COMMAND_TYPES: frozenset[str] = frozenset({
    "session.hello",
    "chat.send",
    "task.cancel",
    "task.confirm",
    "task.answer",
    # T5/DoD global8: capacidade ADITIVA e opcional (ver docs/IPC-CONTRACT.md). Cliente v1 que
    # não a conhece continua funcionando: o comando só chega se for pedido, e a flag de
    # capability é uma chave a mais que clientes antigos ignoram.
    "fs.read",
    "brain.list",
    "brain.get",
    "brain.set",
    "brain.verify",
    "brain.probe",
    "approval.get",
    "approval.set",
    "memory.list",
    "memory.search",
    "memory.upsert",
    "memory.delete",
    "memory.proposals",
    "memory.propose",
    "memory.accept",
    "memory.reject",
    "memory.export",
    "memory.import",
    "memory.wipe",
    "memory.options.get",
    "memory.options.set",
    "ui.preferences.get",
    "ui.preferences.set",
    "app.shutdown",
})

RESPONSE_TYPES: frozenset[str] = frozenset({
    "command.result",
    "command.error",
})

EVENT_TYPES: frozenset[str] = frozenset({
    "session.ready",
    "chat.message",
    "task.started",
    "task.progress",
    "task.finished",
    "task.need_confirm",
    "task.need_input",
    "task.cancelled",
    "task.error",
    "brain.updated",
    "memory.changed",
    "log.append",
})


class ProtocolError(ValueError):
    """Erro público e tipado do protocolo IPC (nunca expõe traceback interno)."""

    def __init__(self, code: str, message: str, *, request_id: str = "unknown") -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.request_id = request_id or "unknown"


@dataclass(frozen=True)
class Envelope:
    v: int
    id: str
    type: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "id": self.id,
            "type": self.type,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class FrameOutcome:
    """Resultado individual por linha no framing JSONL: preserva frames seguintes mesmo se um falhar."""

    raw: bytes | None = None
    error: ProtocolError | None = None


def capabilities_for_commands(
    supported_commands: Iterable[str] = (),
    *,
    web_search: bool = False,
    browser_use: bool = False,
) -> dict[str, bool]:
    """Deriva capabilities verdadeiras a partir dos comandos realmente suportados na sessão."""
    cmds = set(supported_commands)
    has_chat = "chat.send" in cmds
    has_exec_controls = {"chat.send", "task.cancel", "task.confirm", "task.answer"} <= cmds
    has_memory = {
        "memory.list",
        "memory.search",
        "memory.upsert",
        "memory.delete",
        "memory.proposals",
        "memory.propose",
        "memory.accept",
        "memory.reject",
        "memory.export",
        "memory.import",
        "memory.wipe",
        "memory.options.get",
        "memory.options.set",
    } <= cmds
    has_brain = {
        "brain.list",
        "brain.get",
        "brain.set",
        "brain.verify",
        "brain.probe",
        "approval.get",
        "approval.set",
    } <= cmds
    return {
        "converse": has_chat,
        "execute": has_exec_controls,
        "memory": has_memory,
        "brain": has_brain,
        # Aditiva: `false` numa sessão mínima e `true` só quando o serviço realmente registra
        # `fs.read`. Um cliente v1 lê as seis chaves que conhece e ignora esta.
        "file_read": "fs.read" in cmds,
        "web_search": bool(web_search),
        "browser_use": bool(browser_use),
    }


def default_capabilities(*, web_search: bool = False, browser_use: bool = False) -> dict[str, bool]:
    """Capabilities de sessão mínima sem dispatcher de serviço (apenas handshake/shutdown)."""
    return capabilities_for_commands((), web_search=web_search, browser_use=browser_use)


def validate_host_pids(raw_pids: Any) -> list[int]:
    """Valida `host_pids` em `session.hello`: lista de até MAX_HOST_PIDS inteiros positivos."""
    if raw_pids is None:
        return []
    if not isinstance(raw_pids, list):
        raise ProtocolError("invalid_payload", "Campo 'host_pids' deve ser uma lista de inteiros.")
    if len(raw_pids) > MAX_HOST_PIDS:
        raise ProtocolError(
            "invalid_payload",
            f"Campo 'host_pids' excede o limite de {MAX_HOST_PIDS} itens.",
        )
    validated: list[int] = []
    for item in raw_pids:
        if type(item) is not int or item <= 0 or item > 2_147_483_647:
            raise ProtocolError(
                "invalid_payload",
                "Cada item de 'host_pids' deve ser um inteiro positivo válido.",
            )
        validated.append(item)
    return validated


def _reject_non_finite_constant(token: str) -> Any:
    raise ProtocolError("invalid_json", f"Constante numérica não finita não permitida em JSON estrito: {token}")


def _contains_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def _check_no_surrogates(obj: Any) -> None:
    if isinstance(obj, str):
        if _contains_surrogate(obj):
            raise ProtocolError("invalid_encoding", "Strings não podem conter code points surrogate.")
    elif isinstance(obj, list):
        for item in obj:
            _check_no_surrogates(item)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and _contains_surrogate(k):
                raise ProtocolError("invalid_encoding", "Chaves não podem conter code points surrogate.")
            _check_no_surrogates(v)


def _extract_safe_id(raw_obj: Any) -> str:
    if isinstance(raw_obj, dict):
        candidate = raw_obj.get("id")
        if isinstance(candidate, str) and ID_PATTERN.fullmatch(candidate):
            return candidate
    return "unknown"


def parse_envelope(raw: str | bytes, *, max_bytes: int = MAX_LINE_BYTES) -> Envelope:
    """Valida e converte uma linha JSONL em `Envelope` com JSON e UTF-8 estritos."""
    if isinstance(raw, bytes):
        if len(raw) > max_bytes:
            raise ProtocolError("message_too_large", "Mensagem excede o limite do protocolo.")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise ProtocolError("invalid_encoding", "Mensagem não está em UTF-8 válido.") from None
    elif isinstance(raw, str):
        if _contains_surrogate(raw):
            raise ProtocolError("invalid_encoding", "Mensagem contém code point surrogate inválido.")
        encoded = raw.encode("utf-8", errors="strict")
        if len(encoded) > max_bytes:
            raise ProtocolError("message_too_large", "Mensagem excede o limite do protocolo.")
        text = raw
    else:
        raise ProtocolError("invalid_envelope", "Formato de frame inválido.")

    try:
        data = json.loads(text, parse_constant=_reject_non_finite_constant)
    except ProtocolError:
        raise
    except ValueError:
        raise ProtocolError("invalid_json", "Frame não é um objeto JSON válido.") from None

    if not isinstance(data, dict):
        raise ProtocolError("invalid_envelope", "Envelope deve ser um objeto JSON.")

    _check_no_surrogates(data)

    req_id = _extract_safe_id(data)
    version = data.get("v")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ProtocolError(
            "unsupported_version",
            f"Versão de protocolo incompatível: esperado v={PROTOCOL_VERSION}.",
            request_id=req_id,
        )

    raw_id = data.get("id")
    if not isinstance(raw_id, str) or len(raw_id) > MAX_ID_CHARS or not ID_PATTERN.fullmatch(raw_id):
        raise ProtocolError(
            "invalid_envelope",
            "Campo 'id' deve ser uma string ASCII válida (1..128 chars, [A-Za-z0-9_.:-]).",
            request_id="unknown",
        )

    msg_type = data.get("type")
    if not isinstance(msg_type, str) or len(msg_type) > MAX_TYPE_CHARS or not TYPE_PATTERN.fullmatch(msg_type):
        raise ProtocolError(
            "invalid_envelope",
            "Campo 'type' deve ser uma string ASCII válida (1..64 chars, [a-z][a-z0-9_.]*).",
            request_id=raw_id,
        )

    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise ProtocolError(
            "invalid_payload",
            "Campo 'payload' deve ser um objeto JSON.",
            request_id=raw_id,
        )

    return Envelope(v=PROTOCOL_VERSION, id=raw_id, type=msg_type, payload=payload)


def encode_message(message: Envelope | dict[str, Any]) -> str:
    """Serializa uma mensagem como linha JSONL UTF-8 terminada por `\\n`."""
    obj = message.to_dict() if isinstance(message, Envelope) else message
    _check_no_surrogates(obj)
    try:
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        encoded = line.encode("utf-8", errors="strict")
    except (ValueError, TypeError) as exc:
        raise ProtocolError("invalid_json", "Mensagem contém valor não serializável em JSON estrito.") from exc
    if len(encoded) > MAX_LINE_BYTES:
        raise ProtocolError("message_too_large", "Resposta excede o limite do protocolo.")
    return line + "\n"


def make_result(request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "id": request_id,
        "type": "command.result",
        "payload": payload,
    }


def make_error(request_id: str, code: str, message: str, *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": code,
        "message": message,
    }
    if details:
        body["details"] = details
    return {
        "v": PROTOCOL_VERSION,
        "id": request_id if ID_PATTERN.fullmatch(request_id or "") else "unknown",
        "type": "command.error",
        "payload": body,
    }


def make_event(event_type: str, payload: dict[str, Any], *, event_id: str | None = None) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "id": event_id or f"evt-{uuid.uuid4().hex[:16]}",
        "type": event_type,
        "payload": payload,
    }


class JsonlFramer:
    """Acumulador incremental de bytes com proteção por linha sem perder frames seguintes.

    Quando uma linha excede `max_line_bytes`, emite um `FrameOutcome(error=ProtocolError(...))`
    para aquela linha e preserva integralmente todas as linhas válidas seguintes do mesmo chunk.
    `feed(chunk)` mantém compatibilidade simples (lança `ProtocolError` se houve linha gigante,
    mas guarda as linhas válidas já extraídas em `drain_pending_lines()`), enquanto
    `feed_outcomes(chunk)` entrega a sequência exata na ordem de chegada.
    """

    def __init__(self, *, max_line_bytes: int = MAX_LINE_BYTES) -> None:
        self.max_line_bytes = max_line_bytes
        self._buf = bytearray()
        self._discarding_oversized = False
        self._oversized_notified = False
        self._pending_valid_lines: list[bytes] = []

    def feed_outcomes(self, chunk: bytes) -> list[FrameOutcome]:
        if not chunk:
            return []
        outcomes: list[FrameOutcome] = []
        offset = 0
        length = len(chunk)

        while offset < length:
            nl_pos = chunk.find(b"\n", offset)
            if self._discarding_oversized:
                if nl_pos == -1:
                    return outcomes
                self._discarding_oversized = False
                self._oversized_notified = False
                offset = nl_pos + 1
                continue

            if nl_pos == -1:
                remaining = chunk[offset:]
                if len(self._buf) + len(remaining) > self.max_line_bytes:
                    self._buf.clear()
                    self._discarding_oversized = True
                    if not self._oversized_notified:
                        self._oversized_notified = True
                        outcomes.append(
                            FrameOutcome(
                                error=ProtocolError("message_too_large", "Linha JSONL excede MAX_LINE_BYTES.")
                            )
                        )
                else:
                    self._buf.extend(remaining)
                break

            segment = chunk[offset:nl_pos]
            offset = nl_pos + 1
            if len(self._buf) + len(segment) > self.max_line_bytes:
                self._buf.clear()
                outcomes.append(
                    FrameOutcome(
                        error=ProtocolError("message_too_large", "Linha JSONL excede MAX_LINE_BYTES.")
                    )
                )
                continue

            if self._buf:
                self._buf.extend(segment)
                raw_line = bytes(self._buf)
                self._buf.clear()
            else:
                raw_line = segment

            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            if raw_line.strip():
                outcomes.append(FrameOutcome(raw=raw_line))

        return outcomes

    def drain_pending_lines(self) -> list[bytes]:
        lines = list(self._pending_valid_lines)
        self._pending_valid_lines.clear()
        return lines

    def feed(self, chunk: bytes) -> list[bytes]:
        outcomes = self.feed_outcomes(chunk)
        lines: list[bytes] = self.drain_pending_lines()
        first_error: ProtocolError | None = None
        for item in outcomes:
            if item.error is not None and first_error is None:
                first_error = item.error
            elif item.raw is not None:
                lines.append(item.raw)
        if first_error is not None:
            self._pending_valid_lines.extend(lines)
            raise first_error
        return lines

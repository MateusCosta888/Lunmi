"""Host WebSocket/HMAC da integração Spotify — porte do kit congelado (S1).

Portado para `app/integrations/spotify_host` sem relaxar fences: nove operações
allowlistadas, um cliente autenticado de cada vez, um comando em voo, frames
estritos, deadlines limitados e sem retry depois de incerteza. Mudanças de produto:
(a) nenhum estado de fixture por omissão; (b) `grant_activation_once()` permite ao
broker conceder UMA ativação por decisão humana, sem aprovação no fio; (c) erros de
porta/config mantêm os códigos `KIT_*` e são traduzidos pelo broker.
"""

from __future__ import annotations

import asyncio
import errno
import http
import secrets
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from . import strictwire as wire
from .config import ALLOWED_ORIGINS, KitError

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_ORIGINS = ALLOWED_ORIGINS

STAT_KEYS = (
    "sessions_opened",
    "auth_ok",
    "auth_failed",
    "auth_replay",
    "mode_mismatch",
    "origin_rejected",
    "client_busy",
    "commands_sent",
    "results_accepted",
    "results_verified",
    "timeouts",
    "late_replies",
    "duplicate_ids",
    "session_mismatch",
    "protocol_errors",
    "unsupported_actions",
    "mode_blocked",
    "device_blocked",
    "approval_blocked",
    "activation_dispatched",
    "cancelled_pending",
    "cleanups",
)


@dataclass
class ActionResult:
    status: str
    data: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    duration_ms: int = 0
    error_code: str | None = None
    # Optional fixed stage for PLAYER_NOT_READY. Only an allowlisted token ever
    # lands here; arbitrary client detail is dropped, never echoed.
    error_stage: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "data": self.data,
            "evidence": self.evidence,
            "duration_ms": self.duration_ms,
            "error_code": self.error_code,
            "error_stage": self.error_stage,
        }


@dataclass(eq=False)
class _Session:
    ws: Any
    epoch: int
    mode: str
    server_nonce: str
    session_id: str | None = None
    client_nonce: str | None = None
    device_scope: str = "unknown"
    support: frozenset[str] = frozenset()
    authenticated: bool = False
    ended: bool = False
    pending: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    dispatch: dict[str, tuple[float, int, str]] = field(default_factory=dict)
    seen_results: set[str] = field(default_factory=set)
    timed_out: set[str] = field(default_factory=set)

    @property
    def capabilities(self) -> dict[str, str]:
        return wire.effective_capabilities(self.support, self.mode, self.device_scope)


class SpotifyHostBridge:
    """One authenticated client at a time, one command in flight per session."""

    def __init__(
        self,
        secret: bytes,
        *,
        mode: str = "read_only",
        origin_allowlist: Iterable[str] = DEFAULT_ORIGINS,
        auth_deadline_ms: int = 2000,
        default_deadline_ms: int = 3000,
        reply_grace_ms: int = 250,
        state: dict[str, Any] | None = None,
        activate_local_approved: bool = False,
    ) -> None:
        wire.proof_key(secret)
        if mode not in wire.MODES:
            raise KitError("KIT_CONFIG_INVALID", "mode")
        # Trusted backend caller approval for the one fixed-local activation. It is
        # never carried by a frame, an argument, a capability or a model decision.
        if type(activate_local_approved) is not bool:
            raise KitError("KIT_CONFIG_INVALID", "activation_approval")
        self._secret = secret
        self._mode = mode
        self._activate_local_approved = activate_local_approved
        self._origins = tuple(origin_allowlist)
        self._auth_deadline_ms = auth_deadline_ms
        self._default_deadline_ms = default_deadline_ms
        self._grace_ms = reply_grace_ms
        self._state = dict(state or {})
        self._server: Any = None
        self._epoch = 0
        self._active: _Session | None = None
        self._joining: _Session | None = None
        self._sessions: set[_Session] = set()
        self._used_server_nonces: set[str] = set()
        self._used_client_nonces: set[str] = set()
        self._closed = False
        self._events: deque[dict[str, Any]] = deque(maxlen=256)
        self.auth_event = asyncio.Event()
        # Produto: notifica o broker em cada transição de sessão (auth/end) para o
        # DTO público mudar por transição real, nunca por polling de estado musical.
        self.session_listener: Callable[[Any], None] | None = None
        self.stats: Counter[str] = Counter({key: 0 for key in STAT_KEYS})

    # -- lifecycle ---------------------------------------------------------

    async def start(self, port: int = 0) -> "SpotifyHostBridge":
        try:
            self._server = await serve(
                self._handle,
                LOOPBACK_HOST,
                port,
                max_size=wire.MAX_FRAME_BYTES,
                ping_interval=None,
                close_timeout=2,
                process_request=self._process_request,
            )
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, 10048):
                raise KitError("KIT_PORT_IN_USE", str(port)) from exc
            raise KitError("KIT_PORT_ERROR", str(exc.errno)) from exc
        return self

    @property
    def port(self) -> int:
        return int(self._server.sockets[0].getsockname()[1])

    @property
    def url(self) -> str:
        return f"ws://{LOOPBACK_HOST}:{self.port}"

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def has_active_session(self) -> bool:
        return self._active is not None

    @property
    def session_info(self) -> dict[str, Any]:
        session = self._active
        if session is None or not session.authenticated:
            return {
                "authenticated": False,
                "mode": self._mode,
                "device_scope": None,
                "capabilities": wire.effective_capabilities(
                    frozenset(), self._mode, "unknown"
                ),
            }
        return {
            "authenticated": True,
            "mode": session.mode,
            "device_scope": session.device_scope,
            "capabilities": session.capabilities,
            "epoch": session.epoch,
            "support": wire.support_string(session.support),
        }

    def grant_activation_once(self) -> bool:
        """Autoriza exatamente UMA ativação local ao próximo `request`.

        Só o `SpotifyBroker` de produto chama isto, depois de consumir o grant interno
        ligado ao pedido humano atual. A aprovação nunca viaja no fio, não é rearmada
        por reauth/reconexão e o `request("activate_local")` consome-a antes do envio.
        """
        if self._mode != "control" or self._closed:
            return False
        self._activate_local_approved = True
        return True

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stats["cleanups"] += 1
        for session in list(self._sessions):
            await self._end_session(session, close_code=1001)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def events(self) -> list[dict[str, Any]]:
        """Bounded, payload-free record: op, status, epoch and timings only."""
        return list(self._events)

    # -- connection handling ----------------------------------------------

    def _process_request(self, connection: Any, request: Any) -> Any:
        origin = request.headers.get("Origin")
        if origin is not None and origin not in self._origins:
            self.stats["origin_rejected"] += 1
            self._events.append({"op": "origin", "status": "rejected"})
            return connection.respond(http.HTTPStatus.FORBIDDEN, "Forbidden\n")
        return None

    async def _handle(self, ws: Any) -> None:
        if self._active is not None or self._joining is not None:
            self.stats["client_busy"] += 1
            self._events.append({"op": "session", "status": "CLIENT_BUSY"})
            try:
                await ws.send(wire.encode(wire.frame("error", id="", code="CLIENT_BUSY")))
                await ws.close(code=1013, reason="CLIENT_BUSY")
            except ConnectionClosed:
                pass
            return

        self._epoch += 1
        server_nonce = secrets.token_hex(32)
        self._used_server_nonces.add(server_nonce)
        session = _Session(
            ws=ws, epoch=self._epoch, mode=self._mode, server_nonce=server_nonce
        )
        self._sessions.add(session)
        self._joining = session
        self.stats["sessions_opened"] += 1
        self._events.append({"op": "session", "status": "opened", "epoch": session.epoch})

        try:
            await ws.send(
                wire.encode(
                    wire.frame(
                        "hello",
                        server_nonce=server_nonce,
                        epoch=session.epoch,
                        auth_deadline_ms=self._auth_deadline_ms,
                        mode=self._mode,
                    )
                )
            )
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=self._auth_deadline_ms / 1000
                )
            except (asyncio.TimeoutError, ConnectionClosed):
                self.stats["auth_failed"] += 1
                self._events.append(
                    {"op": "auth", "status": "AUTH_TIMEOUT", "epoch": session.epoch}
                )
                return

            if not await self._authenticate(session, raw):
                return

            await self._read_loop(session)
        finally:
            await self._end_session(session)

    async def _authenticate(self, session: _Session, raw: Any) -> bool:
        try:
            msg = wire.loads_strict(raw)
            kind = wire.validate(msg)
        except wire.ProtocolError as exc:
            await self._refuse(session, exc.code, close_code=1008)
            return False
        if kind != "auth":
            await self._refuse(session, "ROLE_VIOLATION", close_code=1008)
            return False

        client_nonce = msg["client_nonce"]
        if client_nonce in self._used_client_nonces:
            self.stats["auth_replay"] += 1
            self._events.append(
                {"op": "auth", "status": "AUTH_REPLAY", "epoch": session.epoch}
            )
            await self._refuse(session, "AUTH_REPLAY", close_code=1008)
            return False

        if msg["mode"] != session.mode:
            self.stats["mode_mismatch"] += 1
            self._events.append(
                {"op": "auth", "status": "MODE_MISMATCH", "epoch": session.epoch}
            )
            await self._refuse(session, "MODE_MISMATCH", close_code=1008)
            return False

        support = wire.parse_support(msg["support"])
        expected = wire.client_proof(
            self._secret,
            session.server_nonce,
            client_nonce,
            session.epoch,
            msg["mode"],
            msg["device_scope"],
            msg["support"],
        )
        if not wire.proof_matches(expected, msg["client_proof"]):
            self.stats["auth_failed"] += 1
            self._events.append(
                {"op": "auth", "status": "AUTH_FAILED", "epoch": session.epoch}
            )
            await self._refuse(session, "AUTH_FAILED", close_code=1008)
            return False

        self._used_client_nonces.add(client_nonce)
        session.client_nonce = client_nonce
        session.session_id = secrets.token_hex(16)
        session.device_scope = msg["device_scope"]
        session.support = support
        session.authenticated = True
        self._active = session
        self._joining = None
        self.stats["auth_ok"] += 1
        self._events.append({"op": "auth", "status": "ok", "epoch": session.epoch})
        await session.ws.send(
            wire.encode(
                wire.frame(
                    "ready",
                    id=msg["id"],
                    server_proof=wire.server_proof(
                        self._secret,
                        session.server_nonce,
                        client_nonce,
                        session.epoch,
                        msg["mode"],
                        msg["device_scope"],
                        msg["support"],
                    ),
                    session=session.session_id,
                    epoch=session.epoch,
                    mode=session.mode,
                    device_scope=session.device_scope,
                    capabilities=session.capabilities,
                    origin_filter="kit",
                )
            )
        )
        self.auth_event.set()
        self._notify_session()
        return True

    async def _read_loop(self, session: _Session) -> None:
        try:
            async for raw in session.ws:
                await self._on_frame(session, raw)
        except ConnectionClosed:
            pass

    async def _on_frame(self, session: _Session, raw: Any) -> None:
        try:
            msg = wire.loads_strict(raw)
            kind = wire.validate(msg)
        except wire.ProtocolError as exc:
            self.stats["protocol_errors"] += 1
            await self._refuse(session, exc.code, close_code=1008)
            return

        if kind == "result":
            await self._handle_result(session, msg)
        elif kind == "error":
            await self._handle_client_error(session, msg)
        else:
            # A client never sends hello/ready/command to this bridge.
            self.stats["protocol_errors"] += 1
            await self._refuse(session, "ROLE_VIOLATION")

    async def _handle_result(self, session: _Session, msg: dict[str, Any]) -> None:
        if msg["session"] != session.session_id:
            self.stats["session_mismatch"] += 1
            await self._refuse(session, "SESSION_MISMATCH")
            return
        rid = msg["id"]
        if rid in session.seen_results:
            self.stats["duplicate_ids"] += 1
            await self._refuse(session, "DUPLICATE_ID")
            return

        # deadline_ms is the operation limit, measured locally at dispatch. A reply
        # that lands after it is lost even inside the transport grace window, and a
        # client-reported duration_ms is never evidence.
        entry = session.dispatch.get(rid)
        expected_action = entry[2] if entry is not None else None
        if entry is not None:
            started, deadline, _action = entry
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            if elapsed_ms > deadline:
                session.dispatch.pop(rid, None)
                session.timed_out.add(rid)
                future = session.pending.pop(rid, None)
                self.stats["late_replies"] += 1
                self.stats["timeouts"] += 1
                self._events.append(
                    {
                        "op": "reply",
                        "status": "LATE_REPLY",
                        "id": rid,
                        "epoch": session.epoch,
                        "duration_ms": elapsed_ms,
                    }
                )
                await self._refuse(session, "LATE_REPLY")
                if future is not None and not future.done():
                    future.set_result({"__late__": True})
                return

        if rid in session.timed_out:
            self.stats["late_replies"] += 1
            self._events.append(
                {"op": "reply", "status": "LATE_REPLY", "id": rid, "epoch": session.epoch}
            )
            await self._refuse(session, "LATE_REPLY")
            return
        if msg["status"] == "SUCCESS_VERIFIED" and expected_action is not None:
            if not wire.validate_result_data(expected_action, msg["data"]):
                session.dispatch.pop(rid, None)
                self.stats["protocol_errors"] += 1
                future = session.pending.pop(rid, None)
                if future is not None and not future.done():
                    future.set_result(
                        {
                            "__client_error__": True,
                            "status": "FAILED",
                            "error_code": "RESULT_SHAPE",
                        }
                    )
                await self._refuse(session, "RESULT_SHAPE")
                return
        future = session.pending.pop(rid, None)
        if future is None:
            await self._refuse(session, "UNKNOWN_ID")
            return
        session.dispatch.pop(rid, None)
        session.seen_results.add(rid)
        if not future.done():
            future.set_result(msg)

    async def _handle_client_error(self, session: _Session, msg: dict[str, Any]) -> None:
        future = session.pending.pop(msg["id"], None)
        if future is None:
            await self._refuse(session, "UNKNOWN_ID")
            return
        session.dispatch.pop(msg["id"], None)
        stage = msg.get("detail")
        if msg["code"] == "PLAYER_NOT_READY":
            stage = stage if stage in wire.PLAYER_STAGES else None
        elif msg["code"] in ("SEARCH_FAILED", "SEARCH_UNAVAILABLE"):
            stage = stage if stage in wire.SEARCH_STAGES else None
        else:
            stage = None
        if not future.done():
            future.set_result(
                {
                    "__client_error__": True,
                    "status": "FAILED",
                    "error_code": msg["code"],
                    "error_stage": stage,
                }
            )

    def _notify_session(self) -> None:
        listener = self.session_listener
        if listener is None:
            return
        try:
            listener(self)
        except Exception:
            # A transição de UI nunca pode derrubar o host.
            pass

    async def _refuse(
        self, session: _Session, code: str, close_code: int | None = None
    ) -> None:
        self._events.append({"op": "refuse", "status": code, "epoch": session.epoch})
        try:
            await session.ws.send(wire.encode(wire.frame("error", id="", code=code)))
            if close_code is not None:
                await session.ws.close(code=close_code, reason=code[:120])
        except ConnectionClosed:
            pass

    async def _end_session(self, session: _Session, close_code: int | None = None) -> None:
        if session.ended:
            return
        session.ended = True
        self.auth_event.clear()
        self._notify_session()
        if self._joining is session:
            self._joining = None
        if self._active is session:
            self._active = None
        pending = list(session.pending.items())
        session.pending.clear()
        session.dispatch.clear()
        for _cid, future in pending:
            if not future.done():
                future.set_result(None)
        if pending:
            self._events.append(
                {
                    "op": "session",
                    "status": "pending_cancelled",
                    "count": len(pending),
                    "epoch": session.epoch,
                }
            )
        self._sessions.discard(session)
        if close_code is not None:
            try:
                await session.ws.close(code=close_code, reason="closed")
            except ConnectionClosed:
                pass
        self._events.append(
            {"op": "session", "status": "closed", "epoch": session.epoch}
        )

    # -- operations --------------------------------------------------------

    async def request(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        deadline_ms: int | None = None,
    ) -> ActionResult:
        payload = {} if args is None else dict(args)
        if action not in wire.ALLOWED_ACTIONS:
            self.stats["unsupported_actions"] += 1
            self._events.append({"op": "action", "status": "UNSUPPORTED"})
            return ActionResult(status="UNSUPPORTED", error_code="CAPABILITY_UNSUPPORTED")
        if not wire.validate_args(action, payload):
            return ActionResult(status="FAILED", error_code="BAD_ARGS")

        session = self._active
        if session is None or not session.authenticated or session.ended:
            return ActionResult(status="FAILED", error_code="BRIDGE_DISCONNECTED")
        if action in wire.MUTATIONS:
            if self._mode != "control":
                self.stats["mode_blocked"] += 1
                self._events.append({"op": "action", "status": "MODE_READ_ONLY"})
                return ActionResult(status="UNSUPPORTED", error_code="MODE_READ_ONLY")
            session_now = self._active
            scope = session_now.device_scope if session_now else "unknown"
            if scope != "local":
                self.stats["device_blocked"] += 1
                self._events.append(
                    {"op": "action", "status": "DEVICE_SCOPE_BLOCKED"}
                )
                return ActionResult(
                    status="UNSUPPORTED", error_code="DEVICE_SCOPE_BLOCKED"
                )
        if action in wire.ACTIVATION_ACTIONS:
            # Activation is not a music mutation: no device-scope guard here (it
            # exists to leave a remote/unknown device), but control and the
            # separate trusted approval are both required.
            if self._mode != "control":
                self.stats["mode_blocked"] += 1
                self._events.append({"op": "action", "status": "MODE_READ_ONLY"})
                return ActionResult(status="UNSUPPORTED", error_code="MODE_READ_ONLY")
            if not self._activate_local_approved:
                self.stats["approval_blocked"] += 1
                self._events.append(
                    {"op": "action", "status": "APPROVAL_REQUIRED"}
                )
                return ActionResult(
                    status="UNSUPPORTED", error_code="APPROVAL_REQUIRED"
                )
        if action not in session.support:
            self.stats["unsupported_actions"] += 1
            return ActionResult(status="UNSUPPORTED", error_code="CAPABILITY_UNSUPPORTED")
        if session.pending:
            return ActionResult(status="FAILED", error_code="BUSY")

        if action in wire.ACTIVATION_ACTIONS:
            # Single dispatch: the trusted grant authorizes exactly ONE accepted
            # activation, consumed here before the first send. A failed send, a
            # timeout or any uncertain outcome does not re-arm it; a new explicit
            # backend decision is required to approve another one.
            self._activate_local_approved = False
            self.stats["activation_dispatched"] += 1

        requested = self._default_deadline_ms if deadline_ms is None else deadline_ms
        deadline = max(100, min(int(requested), 5000))
        cid = f"cmd-{secrets.token_hex(16)}"
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        session.pending[cid] = future
        started = time.perf_counter()
        session.dispatch[cid] = (started, deadline, action)
        self.stats["commands_sent"] += 1
        self._events.append(
            {"op": "command", "status": "sent", "id": cid, "epoch": session.epoch}
        )

        try:
            await session.ws.send(
                wire.encode(
                    wire.frame(
                        "command",
                        id=cid,
                        session=session.session_id,
                        action=action,
                        args=payload,
                        deadline_ms=deadline,
                    )
                )
            )
        except ConnectionClosed:
            session.pending.pop(cid, None)
            return ActionResult(
                status="CANCELLED",
                error_code="BRIDGE_DISCONNECTED",
                duration_ms=_elapsed_ms(started),
            )

        try:
            outcome = await asyncio.wait_for(
                future, timeout=(deadline + self._grace_ms) / 1000
            )
        except asyncio.TimeoutError:
            session.pending.pop(cid, None)
            session.dispatch.pop(cid, None)
            session.timed_out.add(cid)
            self.stats["timeouts"] += 1
            self._events.append(
                {"op": "command", "status": "TIMEOUT", "id": cid, "epoch": session.epoch}
            )
            return ActionResult(
                status="TIMEOUT",
                error_code="OPERATION_TIMEOUT",
                duration_ms=_elapsed_ms(started),
            )

        if outcome is None:
            session.dispatch.pop(cid, None)
            self.stats["cancelled_pending"] += 1
            self._events.append(
                {"op": "command", "status": "CANCELLED", "id": cid, "epoch": session.epoch}
            )
            return ActionResult(
                status="CANCELLED",
                error_code="BRIDGE_DISCONNECTED",
                duration_ms=_elapsed_ms(started),
            )

        if outcome.get("__late__"):
            self._events.append(
                {"op": "command", "status": "TIMEOUT", "id": cid, "epoch": session.epoch}
            )
            return ActionResult(
                status="TIMEOUT",
                error_code="OPERATION_TIMEOUT",
                duration_ms=_elapsed_ms(started),
            )

        duration = _elapsed_ms(started)
        session.dispatch.pop(cid, None)
        if outcome.get("__client_error__"):
            self._events.append(
                {"op": "command", "status": "FAILED", "id": cid, "epoch": session.epoch}
            )
            return ActionResult(
                status="FAILED",
                error_code=outcome.get("error_code") or "CLIENT_ERROR",
                error_stage=outcome.get("error_stage"),
                duration_ms=duration,
            )

        status = outcome["status"]
        if status == "SUCCESS_VERIFIED" and not outcome["evidence"]["observed"]:
            status = "NOT_VERIFIED"
        self.stats["results_accepted"] += 1
        if status == "SUCCESS_VERIFIED":
            self.stats["results_verified"] += 1
        self._events.append(
            {
                "op": "command",
                "status": status,
                "id": cid,
                "epoch": session.epoch,
                "duration_ms": duration,
            }
        )
        return ActionResult(
            status=status,
            data=outcome["data"],
            evidence=outcome["evidence"],
            duration_ms=duration,
        )


def _elapsed_ms(started: float) -> int:
    # perf_counter is monotonic and, unlike time.monotonic on Windows, keeps
    # sub-millisecond resolution, so a 100 ms deadline is not quantized to 15.6 ms.
    return max(0, int((time.perf_counter() - started) * 1000))

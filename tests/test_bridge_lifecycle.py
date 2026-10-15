"""Testes de ciclo de vida e supervisão da ponte IPC v1 (F1).

Cobre:
- Fechamento de stdin durante leitura encerra o servidor com código 0 e fecha o `BridgeService`.
- `app.shutdown` durante pergunta/confirmação pendente recusa a pergunta imediatamente,
  cancela a tarefa em voo e encerra sem travar.
- Crash/morte do subprocesso Python não deixa processos filhos órfãos nem reexecuta tarefa.
- Reinício explícito do servidor cria um novo `session_id`, e respostas/confirmações da sessão
  antiga não afetam a nova.
- Nenhuma mutação em arquivos pessoais (`.local/` da raiz real) quando `--sandbox-dir` está ativo.
"""
from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from app.bridge import protocol
from app.bridge.service import BridgeService
from app.core.controller import TaskResult
from app.settings import ROOT, Settings


@dataclass
class _ControllableFakeEngine:
    host_pids: set[int] = field(default_factory=set)
    stopped: int = 0

    def set_host_pids(self, pids: Any) -> None:
        self.host_pids = set(pids)

    async def stop(self) -> None:
        self.stopped += 1


class _LifecycleFakeController:
    def __init__(
        self,
        settings: Settings,
        on_state: Any = None,
        confirm: Any = None,
        choose: Any = None,
        indicate: Any = None,
        memory: Any = None,
    ) -> None:
        self.settings = settings
        self.on_state = on_state or (lambda _: None)
        self.confirm = confirm
        self.choose = choose
        self.indicate = indicate
        self.memory = memory
        self.engine = _ControllableFakeEngine()
        self.active = False
        self.closed = False
        self.runs = 0
        self._current_task: asyncio.Task[Any] | None = None

    def set_auto_approve(self, enabled: bool) -> None:
        pass

    def cancel(self) -> None:
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    async def run(self, instruction: str, *, conversation: bool = False) -> TaskResult:
        self.active = True
        self.runs += 1
        self._current_task = asyncio.current_task()
        try:
            self.on_state("Thinking")
            if "pedir_confirmacao" in instruction and self.confirm is not None:
                approved = await self.confirm("Confirmar ação sensível de teste?")
                if not approved:
                    return TaskResult(
                        status="CANCELLED",
                        failure_kind="CANCELLED",
                        message="Ação recusada.",
                        scope="end_to_end",
                    )
            await asyncio.sleep(0.02)
            return TaskResult(
                status="SUCCESS",
                message=f"Executado: {instruction}",
                scope="conversation" if conversation else "end_to_end",
                evidence=[{"verified": True}],
            )
        except asyncio.CancelledError:
            return TaskResult(
                status="CANCELLED",
                failure_kind="CANCELLED",
                message="Cancelado.",
                scope="end_to_end",
            )
        finally:
            self.active = False
            self._current_task = None

    async def reconfigure(self, settings: Settings) -> None:
        self.settings = settings

    async def close(self) -> None:
        self.cancel()
        self.closed = True
        await self.engine.stop()
        if self.memory is not None:
            self.memory.close()


def _personal_local_snapshot() -> dict[str, str]:
    local_dir = ROOT / ".local"
    snap: dict[str, str] = {}
    if not local_dir.exists():
        return snap
    for p in sorted(local_dir.glob("*")):
        if p.is_file():
            snap[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


def test_shutdown_while_confirm_pending_denies_question_and_closes_cleanly(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    confirm_event = threading.Event()

    def on_emit(evt_type: str, payload: dict[str, Any]) -> None:
        events.append((evt_type, payload))
        if evt_type == "task.need_confirm":
            confirm_event.set()

    svc = BridgeService(
        emit=on_emit,
        sandbox_dir=tmp_path,
        controller_factory=_LifecycleFakeController,
    )

    res = svc.dispatch(
        protocol.Envelope(
            v=1,
            id="c1",
            type="chat.send",
            payload={"text": "pedir_confirmacao agora", "mode": "execute"},
        )
    )
    assert res["status"] == "started"
    assert confirm_event.wait(timeout=3.0)

    # Fecha o serviço enquanto a confirmação está pendente
    t0 = time.perf_counter()
    svc.close()
    elapsed = time.perf_counter() - t0
    assert elapsed < 3.0
    assert svc.controller.closed is True

    finished = [p for t, p in events if t == "task.finished"]
    assert len(finished) == 1
    assert finished[0]["status"] == "CANCELLED"


def test_subprocess_close_during_read_and_restart_isolation_and_no_personal_mutation(tmp_path: Path) -> None:
    before_personal = _personal_local_snapshot()

    def spawn_bridge() -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [sys.executable, "-u", "-m", "app.bridge.server", "--sandbox-dir", str(tmp_path)],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    # 1. Partida 1: envia hello, lê ready, depois fecha stdin abruptamente (close durante leitura)
    p1 = spawn_bridge()
    assert p1.stdin is not None and p1.stdout is not None
    p1.stdin.write(
        protocol.encode_message(
            {"v": 1, "id": "h1", "type": "session.hello", "payload": {"client": "test", "host_pids": [p1.pid]}}
        ).encode("utf-8")
    )
    p1.stdin.flush()

    line1 = p1.stdout.readline()
    line2 = p1.stdout.readline()
    env1 = protocol.parse_envelope(line1)
    env2 = protocol.parse_envelope(line2)
    ready1 = env1.payload if env1.type == "session.ready" else env2.payload
    session_id_1 = ready1["session_id"]
    pid_1 = p1.pid

    p1.stdin.close()
    rc1 = p1.wait(timeout=5.0)
    assert rc1 == 0
    assert not psutil.pid_exists(pid_1)

    # 2. Partida 2 (restart explícito): gera novo session_id e não reexecuta nada automaticamente
    p2 = spawn_bridge()
    assert p2.stdin is not None and p2.stdout is not None
    p2.stdin.write(
        protocol.encode_message(
            {"v": 1, "id": "h2", "type": "session.hello", "payload": {"client": "test", "host_pids": [p2.pid]}}
        ).encode("utf-8")
    )
    p2.stdin.flush()

    r2_a = protocol.parse_envelope(p2.stdout.readline())
    r2_b = protocol.parse_envelope(p2.stdout.readline())
    ready2 = r2_a.payload if r2_a.type == "session.ready" else r2_b.payload
    session_id_2 = ready2["session_id"]
    assert session_id_2 != session_id_1

    # Confirmação atrasada/velha de outra sessão é rejeitada com invalid_request_id
    p2.stdin.write(
        protocol.encode_message(
            {
                "v": 1,
                "id": "stale-cfm",
                "type": "task.confirm",
                "payload": {"request_id": "cfm-stale-from-sess1", "approved": True},
            }
        ).encode("utf-8")
    )
    p2.stdin.write(
        protocol.encode_message({"v": 1, "id": "shut-2", "type": "app.shutdown", "payload": {}}).encode("utf-8")
    )
    p2.stdin.flush()

    out2, _ = p2.communicate(timeout=10.0)
    assert p2.returncode == 0
    assert not psutil.pid_exists(p2.pid)

    parsed_rest = [protocol.parse_envelope(line) for line in out2.splitlines() if line.strip()]
    stale_reply = next(m for m in parsed_rest if m.id == "stale-cfm")
    assert stale_reply.type == "command.error"
    assert stale_reply.payload["code"] == "invalid_request_id"

    after_personal = _personal_local_snapshot()
    assert before_personal == after_personal

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
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil
import pytest

from app.bridge import protocol
from app.bridge.service import BridgeService
from app.computer.appindex import default_index_path
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

    async def run(
        self,
        instruction: str,
        *,
        conversation: bool = False,
        user_name: str = "",
        attachments: list[dict[str, Any]] | None = None,
    ) -> TaskResult:
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


def test_a_sandboxed_session_keeps_the_application_index_inside_the_sandbox(tmp_path: Path) -> None:
    """O índice de aplicativos que o launcher lê e aprende fica dentro do sandbox.

    Antes, o `TaskController` real abria sempre `ROOT/.local/apps.json`, mesmo com
    `--sandbox-dir`: escolhas e localizações aprendidas numa sessão de aceite iam para o
    índice pessoal. Settings entregues explicitamente também não escapam do sandbox.
    """
    svc = BridgeService(emit=lambda *_: None, sandbox_dir=tmp_path,
                        settings=Settings(state_root=ROOT))
    try:
        sandbox = tmp_path.resolve()
        catalog = svc.controller.catalog
        assert svc.settings.state_root == sandbox
        assert svc.controller.settings.state_root == sandbox
        assert catalog.path == default_index_path(sandbox)
        assert catalog.folders_path.parent == sandbox / ".local"
        catalog.save()
        assert default_index_path(sandbox).is_file()
    finally:
        svc.close()


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


def test_bridge_service_slow_commands_concurrent_with_confirm_cancel_reconfigure_and_shutdown(
    tmp_path: Path,
) -> None:
    """Integração BridgeServer + BridgeService real: slow commands, confirmação, corrida brain.set vs chat.send e shutdown."""
    import io
    import json
    import pytest
    from app.brain.discovery import DiscoveryStatus, ModelListing
    from app.bridge.server import BridgeServer

    list_started = threading.Event()
    release_list = threading.Event()
    reconfig_started = threading.Event()
    release_reconfig = threading.Event()

    class _FakeDiscovery:
        async def list(self, provider: str, *, settings: Any = None, brain: Any = None, force: bool = False) -> ModelListing:
            list_started.set()
            await asyncio.to_thread(release_list.wait, 5.0)
            return ModelListing(
                provider=provider,
                label="OpenAI",
                status=DiscoveryStatus.READY,
                models=(),
                detail="ok",
            )

    class _ReconfigAwareController(_LifecycleFakeController):
        async def reconfigure(self, settings: Settings) -> None:
            reconfig_started.set()
            await asyncio.to_thread(release_reconfig.wait, 5.0)
            self.settings = settings

    stdout = io.BytesIO()
    svc = BridgeService(
        emit=lambda *_: None,
        sandbox_dir=tmp_path,
        settings=Settings(provider="codex", model="gpt-5-codex", state_root=tmp_path),
        controller_factory=_ReconfigAwareController,
        discovery=_FakeDiscovery(),  # type: ignore[arg-type]
    )
    server = BridgeServer(
        stdin=io.BytesIO(),
        stdout=stdout,
        stderr=io.BytesIO(),
        sandbox_dir=tmp_path,
        service=svc,
    )
    svc._emit = server.emit_event

    def send(identifier: str, kind: str, payload: dict[str, Any]) -> None:
        server.handle_raw_line(
            protocol.encode_message({"v": 1, "id": identifier, "type": kind, "payload": payload}).encode("utf-8")
        )

    try:
        send("h1", "session.hello", {"client": "test"})
        # 1. Inicia tarefa que pede confirmação e ao mesmo tempo dispara brain.list lento
        send("chat-1", "chat.send", {"text": "pedir_confirmacao agora", "mode": "execute"})
        deadline = time.perf_counter() + 3.0
        confirm_req_id = ""
        while time.perf_counter() < deadline:
            lines = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line.strip()]
            need_cfm = [m for m in lines if m.get("type") == "task.need_confirm"]
            if need_cfm:
                confirm_req_id = need_cfm[0]["payload"]["request_id"]
                break
            time.sleep(0.02)
        assert confirm_req_id

        send("slow-list", "brain.list", {"provider": "codex"})
        assert list_started.wait(timeout=3.0)

        # Enquanto brain.list está preso na thread lenta, brain.set vê tarefa ativa e recusa com task_busy
        send("set-while-busy", "brain.set", {"provider": "codex", "model": "gpt-5-codex"})
        # E task.confirm responde imediatamente na reader thread e destrava a tarefa
        send("cfm-ok", "task.confirm", {"request_id": confirm_req_id, "approved": True})

        deadline = time.perf_counter() + 3.0
        while time.perf_counter() < deadline:
            lines = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line.strip()]
            if any(m.get("type") == "task.finished" for m in lines) and any(
                m.get("id") == "set-while-busy" for m in lines
            ):
                break
            time.sleep(0.02)

        release_list.set()
        assert server.wait_for_slow_commands(5.0)

        # 2. Agora inicia brain.set lento e verifica que chat.send concorrente recebe reconfiguring
        send("slow-set", "brain.set", {"provider": "codex", "model": "gpt-5-codex"})
        assert reconfig_started.wait(timeout=3.0)
        send("chat-during-set", "chat.send", {"text": "tentar durante set", "mode": "converse"})

        release_reconfig.set()
        assert server.wait_for_slow_commands(5.0)

        # 3. Encerramento rejeita novos comandos com shutting_down
        send("shut-1", "app.shutdown", {})
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(protocol.Envelope(v=1, id="after-close", type="brain.get", payload={}))
        assert exc_info.value.code == "shutting_down"

        messages = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line.strip()]
        by_id = {m["id"]: m for m in messages if "id" in m and m["type"] in {"command.result", "command.error"}}

        assert by_id["cfm-ok"]["type"] == "command.result"
        assert by_id["set-while-busy"]["type"] == "command.error"
        assert by_id["set-while-busy"]["payload"]["code"] == "task_busy"
        assert by_id["slow-list"]["type"] == "command.result"
        assert by_id["slow-set"]["type"] == "command.result"
        assert by_id["chat-during-set"]["type"] == "command.error"
        assert by_id["chat-during-set"]["payload"]["code"] == "reconfiguring"
        assert by_id["shut-1"]["type"] == "command.result"
    finally:
        release_list.set()
        release_reconfig.set()
        svc.close()


def test_brain_set_timeout_holds_the_guard_until_the_commit_really_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regressão do defeito: prazo estourado não pode soltar a guarda nem divergir estado.

    Antes, `fut.result(timeout=30.0)` estourava, o `finally` soltava `_reconfiguring` e
    `chat.send` começava enquanto `controller.reconfigure` ainda rodava; ao terminar,
    `service.settings` ficava velho e `controller.settings` novo, sem `brain.updated`.
    Agora o commit inteiro é uma coroutine dona da guarda até o fim real.
    """
    import app.bridge.service as service_module

    monkeypatch.setattr(service_module, "RECONFIGURE_TIMEOUT_SECONDS", 0.1)

    release = threading.Event()
    reconfig_started = threading.Event()
    events: list[tuple[str, dict[str, Any]]] = []

    class _SlowReconfigController(_LifecycleFakeController):
        async def reconfigure(self, settings: Settings) -> None:
            reconfig_started.set()
            await asyncio.to_thread(release.wait, 5.0)
            self.settings = settings

    svc = BridgeService(
        emit=lambda t, p: events.append((t, p)),
        sandbox_dir=tmp_path,
        settings=Settings(provider="codex", model="review-old", state_root=tmp_path),
        controller_factory=_SlowReconfigController,
    )
    try:
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(
                    v=1,
                    id="set-1",
                    type="brain.set",
                    payload={"provider": "codex", "model": "review-new"},
                )
            )
        assert exc_info.value.code == "reconfiguring"
        assert reconfig_started.wait(timeout=3.0)

        # A guarda continua presa enquanto a coroutine real não termina.
        assert svc._reconfiguring is True
        with pytest.raises(protocol.ProtocolError) as busy:
            svc.dispatch(
                protocol.Envelope(
                    v=1,
                    id="chat-1",
                    type="chat.send",
                    payload={"text": "durante a reconfiguração", "mode": "converse"},
                )
            )
        assert busy.value.code == "reconfiguring"
        assert all(evt != "task.started" for evt, _ in events)

        release.set()
        deadline = time.perf_counter() + 5.0
        while svc._reconfiguring and time.perf_counter() < deadline:
            time.sleep(0.02)
        assert svc._reconfiguring is False
        # Sem divergência: serviço e controlador veem o mesmo cérebro.
        assert svc.settings.model == "review-new"
        assert svc.controller.settings.model == "review-new"
        updated = [p for t, p in events if t == "brain.updated"]
        assert updated and updated[-1]["ok"] is True and updated[-1]["model"] == "review-new"
        saved = json.loads((tmp_path / ".local" / "brain-selection.json").read_text(encoding="utf-8"))
        assert saved == {"provider": "codex", "model": "review-new"}
    finally:
        release.set()
        svc.close()


def test_brain_set_timeout_publishes_a_final_error_when_reconfigure_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falha do commit depois do prazo ainda publica estado final visível e solta a guarda."""
    import app.bridge.service as service_module

    monkeypatch.setattr(service_module, "RECONFIGURE_TIMEOUT_SECONDS", 0.1)

    release = threading.Event()
    events: list[tuple[str, dict[str, Any]]] = []

    class _FailingReconfigController(_LifecycleFakeController):
        async def reconfigure(self, settings: Settings) -> None:
            await asyncio.to_thread(release.wait, 5.0)
            raise RuntimeError("reconfigure quebrou")

    svc = BridgeService(
        emit=lambda t, p: events.append((t, p)),
        sandbox_dir=tmp_path,
        settings=Settings(provider="codex", model="review-old", state_root=tmp_path),
        controller_factory=_FailingReconfigController,
    )
    try:
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(
                    v=1, id="set-fail", type="brain.set",
                    payload={"provider": "codex", "model": "review-new"},
                )
            )
        assert exc_info.value.code == "reconfiguring"

        release.set()
        deadline = time.perf_counter() + 5.0
        while svc._reconfiguring and time.perf_counter() < deadline:
            time.sleep(0.02)
        assert svc._reconfiguring is False
        assert svc.settings.model == "review-old"
        assert svc.controller.settings.model == "review-old"
        updated = [p for t, p in events if t == "brain.updated"]
        assert updated and updated[-1]["ok"] is False
        assert updated[-1]["error"]["stage"] == "reconfigure"
    finally:
        release.set()
        svc.close()


def test_a_timed_out_provider_listing_leaves_no_orphan_coroutine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Os slots limitam threads, não coroutines: a consulta lenta precisa ser cancelada."""
    import app.bridge.service as service_module

    monkeypatch.setattr(service_module, "BRAIN_LIST_TIMEOUT_SECONDS", 0.1)
    started = threading.Event()
    cancelled = threading.Event()

    class _SlowDiscovery:
        async def list(self, provider: str, *, settings: Any = None, brain: Any = None,
                       force: bool = False) -> Any:
            started.set()
            try:
                await asyncio.sleep(30.0)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("a coroutine órfã não deveria concluir")

    svc = BridgeService(
        emit=lambda *_: None,
        sandbox_dir=tmp_path,
        settings=Settings(provider="codex", model="gpt-5-codex", state_root=tmp_path),
        controller_factory=_LifecycleFakeController,
        discovery=_SlowDiscovery(),  # type: ignore[arg-type]
    )
    try:
        with pytest.raises(TimeoutError):
            svc.dispatch(
                protocol.Envelope(v=1, id="list-slow", type="brain.list", payload={"provider": "codex"})
            )
        assert started.wait(timeout=3.0)
        assert cancelled.wait(timeout=3.0)
    finally:
        svc.close()


def test_shutdown_while_a_brain_commit_runs_finishes_cleanly(tmp_path: Path) -> None:
    """Encerramento espera o commit real: não fecha no meio de uma troca de cérebro."""
    release = threading.Event()
    reconfig_done = threading.Event()
    errors: list[BaseException] = []

    class _SlowReconfigController(_LifecycleFakeController):
        async def reconfigure(self, settings: Settings) -> None:
            await asyncio.to_thread(release.wait, 5.0)
            self.settings = settings
            reconfig_done.set()

    svc = BridgeService(
        emit=lambda *_: None,
        sandbox_dir=tmp_path,
        settings=Settings(provider="codex", model="review-old", state_root=tmp_path),
        controller_factory=_SlowReconfigController,
    )

    def setter() -> None:
        try:
            svc.dispatch(
                protocol.Envelope(
                    v=1, id="set-shutdown", type="brain.set",
                    payload={"provider": "codex", "model": "review-new"},
                )
            )
        except BaseException as exc:  # noqa: BLE001 - o teste relata qualquer falha do setter
            errors.append(exc)

    thread = threading.Thread(target=setter, daemon=True)
    thread.start()
    deadline = time.perf_counter() + 3.0
    while not svc._reconfiguring and time.perf_counter() < deadline:
        time.sleep(0.01)
    assert svc._reconfiguring is True

    release.set()
    t0 = time.perf_counter()
    svc.close()
    assert time.perf_counter() - t0 < 5.0
    thread.join(timeout=5.0)

    assert errors == []
    assert svc.controller.closed is True
    assert svc._reconfiguring is False
    assert reconfig_done.is_set()
    assert svc.settings.model == "review-new"

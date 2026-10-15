"""Testes de autoridade de identidade e exclusão de PIDs próprios da UI (F1).

Cobre:
- `filter_trusted_host_pids` aceita PIDs reais da árvore do processo host confiável e rejeita
  PIDs externos forjados que não pertencem à família do processo pai.
- `BridgeServer` e `BridgeService` filtram `host_pids` no `session.hello` e propagam apenas
  PIDs confiáveis para `controller.engine.host_pids`.
- Quando um renderer reinicia e um novo `session.hello` chega do processo pai confiável,
  o novo PID do filho real entra em `host_pids` e o antigo desaparecido sai.
- `UFOEngine` propaga `UFO_ASSISTANT_PIDS` e `host_pids` por requisição ao worker.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import psutil

from app.bridge import protocol
from app.bridge.server import BridgeServer
from app.bridge.service import BridgeService, filter_trusted_host_pids
from app.computer.ufo_engine import UFOEngine
from app.settings import Settings


def _find_external_unrelated_pid() -> int:
    own = os.getpid()
    ppid = os.getppid()
    family = {own, ppid}
    try:
        proc = psutil.Process(ppid if ppid > 0 else own)
        for child in proc.children(recursive=True):
            family.add(child.pid)
        curr: psutil.Process | None = proc
        for _ in range(5):
            if curr is None:
                break
            family.add(curr.pid)
            curr = curr.parent()
    except psutil.Error:
        pass

    for p in psutil.process_iter(["pid"]):
        pid = int(p.info["pid"] or 0)
        if pid > 100 and pid not in family:
            return pid
    return 999999


def test_forged_external_pid_is_rejected_while_real_child_pid_is_accepted() -> None:
    # Cria um subprocesso filho real para representar um renderer real sob a nossa árvore
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        forged_pid = _find_external_unrelated_pid()
        trusted = filter_trusted_host_pids(
            [child.pid, forged_pid, 88888888],
            trusted_root_pid=os.getpid(),
        )
        assert os.getpid() in trusted
        assert child.pid in trusted
        assert forged_pid not in trusted
        assert 88888888 not in trusted
    finally:
        child.kill()
        child.wait(timeout=5)


def test_renderer_restart_updates_engine_host_pids_and_drops_dead_renderer(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    svc = BridgeService(
        emit=lambda t, p: events.append((t, p)),
        sandbox_dir=tmp_path,
        trusted_root_pid=os.getpid(),
    )
    child1 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    child2: subprocess.Popen[bytes] | None = None
    try:
        forged = _find_external_unrelated_pid()
        updated1 = svc.update_host_pids([os.getpid(), child1.pid, forged])
        assert child1.pid in updated1
        assert forged not in updated1
        assert child1.pid in svc.controller.engine.host_pids
        assert forged not in svc.controller.engine.host_pids

        # Simula morte do renderer 1 e nascimento do renderer 2
        child1.kill()
        child1.wait(timeout=5)
        child2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])

        updated2 = svc.update_host_pids([os.getpid(), child2.pid, forged])
        assert child2.pid in updated2
        assert child1.pid not in updated2
        assert forged not in updated2
        assert svc.controller.engine.host_pids == updated2
    finally:
        if child1.poll() is None:
            child1.kill()
            child1.wait(timeout=5)
        if child2 is not None and child2.poll() is None:
            child2.kill()
            child2.wait(timeout=5)
        svc.close()


def test_bridge_server_hello_rejects_excessive_or_non_int_host_pids() -> None:
    bad_hello = protocol.encode_message(
        {
            "v": 1,
            "id": "bad-hello",
            "type": "session.hello",
            "payload": {"host_pids": ["not-an-int"]},
        }
    )
    too_many_hello = protocol.encode_message(
        {
            "v": 1,
            "id": "many-hello",
            "type": "session.hello",
            "payload": {"host_pids": list(range(1, 70))},
        }
    )
    shutdown = protocol.encode_message({"v": 1, "id": "s1", "type": "app.shutdown", "payload": {}})

    stdin = io.BytesIO((bad_hello + too_many_hello + shutdown).encode("utf-8"))
    stdout = io.BytesIO()
    stderr = io.BytesIO()
    server = BridgeServer(stdin=stdin, stdout=stdout, stderr=stderr)
    assert server.run() == 0

    msgs = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line.strip()]
    err1 = next(m for m in msgs if m["id"] == "bad-hello")
    assert err1["type"] == "command.error"
    assert err1["payload"]["code"] == "invalid_payload"

    err2 = next(m for m in msgs if m["id"] == "many-hello")
    assert err2["type"] == "command.error"
    assert err2["payload"]["code"] == "invalid_payload"


def test_ufo_engine_stores_and_updates_host_pids(tmp_path: Path) -> None:
    settings = Settings(logs_path=tmp_path / "logs", output_path=tmp_path / "out")
    engine = UFOEngine(settings, host_pids={101, 202})
    assert engine.host_pids == {101, 202}
    engine.set_host_pids([303, 404, -1, 0])
    assert engine.host_pids == {303, 404}

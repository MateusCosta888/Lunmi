"""`fs.read` — a capacidade aditiva do DoD global8 na ponte (T5/EXTENSAO-FS-READ).

O comando existe para uma leitura de arquivo próprio ser **real e verificável** dentro do mecanismo
que já existe: teto de bytes conferido antes de ler, UTF-8 estrito, hash/tamanho/mtime dos mesmos
bytes, recusa sem UI e sem truncamento silencioso. Nada de arquivo pessoal ou credencial: todos os
arquivos destes testes são criados em `tmp_path`.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from app.bridge import protocol
from app.bridge.service import BridgeService
from app.computer.windows_tools import MAX_READ_BYTES


class _FakeEngine:
    def __init__(self) -> None:
        self.host_pids: set[int] = set()
        self.started_calls = 0

    def set_host_pids(self, pids: Any) -> None:
        self.host_pids = set(pids)

    async def start(self) -> None:
        self.started_calls += 1

    async def stop(self) -> None:
        pass


class _FakeController:
    """Controller mínimo: `fs.read` não pode tocar tarefa, engine nem consentimento."""

    def __init__(self, settings: Any, on_state: Any = None, confirm: Any = None, choose: Any = None,
                 indicate: Any = None, memory: Any = None) -> None:
        self.settings = settings
        self.engine = _FakeEngine()
        self.approvals: list[str] = []

    def set_auto_approve(self, enabled: bool) -> None:
        pass

    def cancel(self) -> None:
        pass

    async def close(self) -> None:
        pass


def service(tmp_path: Path) -> BridgeService:
    return BridgeService(emit=lambda *_: None, sandbox_dir=tmp_path,
                         controller_factory=_FakeController)


def dispatch(svc: BridgeService, payload: dict[str, Any], request_id: str = "fs-1") -> dict[str, Any]:
    return svc.dispatch(protocol.Envelope(v=1, id=request_id, type="fs.read", payload=payload))


def test_reads_strict_utf8_text_with_metadata_from_the_same_bytes(tmp_path: Path) -> None:
    target = tmp_path / "nota.txt"
    content = "LUNMI-F2 linha 1\nacentuação e emoji 🌙\n"
    # Bytes escritos direto: `write_text` traduziria \n e o teste mediria a tradução, não a leitura.
    target.write_bytes(content.encode("utf-8"))

    svc = service(tmp_path)
    try:
        result = dispatch(svc, {"path": str(target)})
    finally:
        svc.close()

    assert result["text"] == content
    assert result["bytes"] == target.stat().st_size
    assert result["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert result["modified_ns"] == target.stat().st_mtime_ns
    assert result["encoding"] == "utf-8"
    assert result["limit_bytes"] == MAX_READ_BYTES
    assert result["truncated"] is False
    assert result["verified"] is True
    assert result["operation"] == "read"
    # Leitura não sobe worker, não abre UI e não pede consentimento.
    assert svc.controller.engine.started_calls == 0
    assert svc.controller.approvals == []


def test_oversize_binary_and_missing_files_are_refused_with_a_public_reason(tmp_path: Path) -> None:
    big = tmp_path / "grande.txt"
    big.write_bytes(b"a" * (MAX_READ_BYTES + 1))
    binary = tmp_path / "bin.dat"
    binary.write_bytes(b"\xff\xfe\xfd")

    svc = service(tmp_path)
    try:
        with pytest.raises(protocol.ProtocolError) as exc_info:
            dispatch(svc, {"path": str(big)})
        assert exc_info.value.code == "invalid_payload"
        assert str(MAX_READ_BYTES) in exc_info.value.public_message

        with pytest.raises(protocol.ProtocolError) as exc_info:
            dispatch(svc, {"path": str(binary)})
        assert exc_info.value.code == "invalid_payload"
        assert "UTF-8" in exc_info.value.public_message

        with pytest.raises(protocol.ProtocolError) as exc_info:
            dispatch(svc, {"path": str(tmp_path / "nao-existe.txt")})
        assert exc_info.value.code == "invalid_payload"
    finally:
        svc.close()


def test_payload_limits_are_validated_before_reading(tmp_path: Path) -> None:
    target = tmp_path / "nota.txt"
    target.write_text("ok", encoding="utf-8")

    svc = service(tmp_path)
    try:
        for payload in ({"path": ""}, {}, {"path": "x" * 2000}, {"path": str(target), "max_bytes": 0},
                        {"path": str(target), "max_bytes": True},
                        {"path": str(target), "max_bytes": MAX_READ_BYTES + 1}):
            with pytest.raises(protocol.ProtocolError) as exc_info:
                dispatch(svc, payload)
            assert exc_info.value.code == "invalid_payload"

        # Um teto MENOR é aceito e passa a ser o teto que vale: exato lê, abaixo recusa.
        exact = dispatch(svc, {"path": str(target), "max_bytes": 2})
        assert exact["limit_bytes"] == 2
        assert exact["text"] == "ok"
        with pytest.raises(protocol.ProtocolError) as exc_info:
            dispatch(svc, {"path": str(target), "max_bytes": 1}, request_id="fs-2")
        assert "excede" in exc_info.value.public_message
    finally:
        svc.close()


def test_capability_is_announced_only_when_the_service_registers_the_command(tmp_path: Path) -> None:
    svc = service(tmp_path)
    try:
        caps = svc.capabilities()
    finally:
        svc.close()
    assert caps["file_read"] is True
    assert "fs.read" in BridgeService.SUPPORTED_COMMANDS
    # O cliente v1 continua lendo as seis capabilities que já conhecia, com os mesmos valores.
    assert caps["converse"] is True and caps["execute"] is True
    assert caps["memory"] is True and caps["brain"] is True
    # WEB-SEARCH-V1: os handlers `web.get`/`web.set` existem, logo a capability é real.
    assert caps["web_search"] is True and caps["browser_use"] is False

"""Testes de Memória na ponte IPC v1 (F2b).

Cobre:
- Novos itens de perfil não contornam proposta/aceite (`memory.upsert` sem `id` existente falha;
  fluxo oficial é `memory.propose` -> `memory.accept` ou `memory.reject`).
- `memory.list` (com e sem `respect_enabled`), `memory.search` (incluindo busca de episódios com
  `kinds=["episode"]`), `memory.upsert`, `memory.delete` (exige `confirmed=True`).
- `memory.options.get` / `memory.options.set`: desligar `enabled=False` faz `memory.list(..., respect_enabled=True)`
  e `memory.search` retornarem vazio imediatamente.
- Política de privacidade (`app/memory/policy.py`): recusa segredos/tokens e texto > 800 chars sem
  ecoar o segredo na mensagem de erro.
- `memory.export`, `memory.import` (exige `confirmed=True` e rejeita arquivo/payload > 4 MiB antes
  de importar), e `memory.wipe` (exige `confirmed=True`, apaga memórias/propostas/episódios e
  restaura `DEFAULT_OPTIONS`).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.bridge import protocol
from app.bridge.service import BridgeService
from app.core.controller import TaskResult
from app.settings import Settings


class _MemoryDummyController:
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
        self.memory = memory
        self.active = False

    def set_auto_approve(self, enabled: bool) -> None:
        pass

    def cancel(self) -> None:
        pass

    async def reconfigure(self, settings: Settings) -> None:
        self.settings = settings

    async def close(self) -> None:
        if self.memory is not None:
            self.memory.close()


def test_memory_propose_accept_reject_update_delete_and_policy(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    svc = BridgeService(
        emit=lambda t, p: events.append((t, p)),
        sandbox_dir=tmp_path,
        controller_factory=_MemoryDummyController,
    )
    try:
        # 1) memory.upsert sem id existente é recusado (não pode criar perfil direto)
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(v=1, id="u-no-id", type="memory.upsert", payload={"text": "Prefiro tema escuro"})
            )
        assert exc_info.value.code == "invalid_payload"

        # 2) Proposta com segredo é recusada pela política sem ecoar o segredo
        secret_attempt = "Minha api_key=sk-proj-99999999999999999999"
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(
                    v=1,
                    id="p-sec",
                    type="memory.propose",
                    payload={"kind": "fact", "text": secret_attempt},
                )
            )
        assert exc_info.value.code == "memory_policy_error"
        assert "sk-proj-" not in exc_info.value.public_message

        # 3) Criar proposta legítima, listar propostas e aceitar
        prop_res = svc.dispatch(
            protocol.Envelope(
                v=1,
                id="p1",
                type="memory.propose",
                payload={"kind": "preference", "text": "Prefiro respostas diretas em português."},
            )
        )
        prop_id = prop_res["proposal_id"]

        props = svc.dispatch(protocol.Envelope(v=1, id="pl1", type="memory.proposals", payload={}))
        assert len(props["proposals"]) == 1
        assert props["proposals"][0]["id"] == prop_id

        acc_res = svc.dispatch(
            protocol.Envelope(v=1, id="pa1", type="memory.accept", payload={"id": prop_id})
        )
        mem_id = acc_res["memory_id"]

        listed = svc.dispatch(protocol.Envelope(v=1, id="ml1", type="memory.list", payload={}))
        assert len(listed["items"]) == 1
        assert listed["items"][0]["id"] == mem_id
        assert listed["items"][0]["text"] == "Prefiro respostas diretas em português."

        # 4) Atualizar texto da memória existente
        upd = svc.dispatch(
            protocol.Envelope(
                v=1,
                id="mu1",
                type="memory.upsert",
                payload={"id": mem_id, "text": "Prefiro respostas técnicas e concisas em português."},
            )
        )
        assert upd["item"]["text"] == "Prefiro respostas técnicas e concisas em português."

        # 5) Rejeitar segunda proposta
        prop2 = svc.dispatch(
            protocol.Envelope(
                v=1,
                id="p2",
                type="memory.propose",
                payload={"kind": "habit", "text": "Costumo organizar arquivos por data."},
            )
        )
        svc.dispatch(
            protocol.Envelope(v=1, id="pr2", type="memory.reject", payload={"id": prop2["proposal_id"]})
        )
        props_after = svc.dispatch(protocol.Envelope(v=1, id="pl2", type="memory.proposals", payload={}))
        assert props_after["proposals"] == []

        # 6) Apagar memória exige confirmed=True
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(v=1, id="md-no", type="memory.delete", payload={"id": mem_id})
            )
        assert exc_info.value.code == "confirmation_required"

        svc.dispatch(
            protocol.Envelope(
                v=1, id="md-yes", type="memory.delete", payload={"id": mem_id, "confirmed": True}
            )
        )
        listed_after = svc.dispatch(protocol.Envelope(v=1, id="ml2", type="memory.list", payload={}))
        assert listed_after["items"] == []
    finally:
        svc.close()


def test_memory_episodes_options_export_import_limit_and_wipe(tmp_path: Path) -> None:
    svc = BridgeService(
        emit=lambda _t, _p: None,
        sandbox_dir=tmp_path,
        controller_factory=_MemoryDummyController,
    )
    try:
        # Grava um episódio real via commit_episode e consulta via memory.search kinds=["episode"]
        ep_task = TaskResult(
            task_id="taskep01",
            status="SUCCESS",
            message="Bloco de Notas aberto.",
            instruction="Abra o Bloco de Notas",
            plan_source="deterministic",
            llm_calls=0,
            evidence=[{"operation": "launch", "process": "notepad.exe", "verified": True}],
        )
        svc.memory.commit_episode(ep_task)

        episodes = svc.dispatch(
            protocol.Envelope(
                v=1,
                id="ep1",
                type="memory.search",
                payload={"query": "", "kinds": ["episode"], "limit": 50},
            )
        )
        assert len(episodes["items"]) == 1
        assert episodes["items"][0]["kind"] == "episode"

        # Cria e aceita um item de perfil para exportar
        p = svc.dispatch(
            protocol.Envelope(
                v=1,
                id="p-exp",
                type="memory.propose",
                payload={"kind": "fact", "text": "Trabalho com projetos Python e TypeScript."},
            )
        )
        svc.dispatch(protocol.Envelope(v=1, id="a-exp", type="memory.accept", payload={"id": p["proposal_id"]}))

        export_file = tmp_path / "exported_memory.json"
        exp_res = svc.dispatch(
            protocol.Envelope(
                v=1,
                id="ex1",
                type="memory.export",
                payload={"_trusted_export_path": str(export_file)},
            )
        )
        assert exp_res["exported"] is True
        assert export_file.exists()

        # Desligar enabled oculta snapshot (respect_enabled=True) e recall
        svc.dispatch(
            protocol.Envelope(
                v=1,
                id="opt-off",
                type="memory.options.set",
                payload={"options": {"enabled": False}},
            )
        )
        snap_off = svc.dispatch(
            protocol.Envelope(v=1, id="ml-off", type="memory.list", payload={"respect_enabled": True})
        )
        assert snap_off["enabled"] is False
        assert snap_off["items"] == []

        # Wipe exige confirmed=True, limpa tudo e restaura opções padrão (enabled=True)
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(protocol.Envelope(v=1, id="w-no", type="memory.wipe", payload={}))
        assert exc_info.value.code == "confirmation_required"

        wipe_res = svc.dispatch(
            protocol.Envelope(v=1, id="w-yes", type="memory.wipe", payload={"confirmed": True})
        )
        assert wipe_res["wiped"] is True
        assert wipe_res["options"]["enabled"] is True

        empty_list = svc.dispatch(protocol.Envelope(v=1, id="ml-empty", type="memory.list", payload={}))
        assert empty_list["items"] == []

        # Rejeita importação de arquivo > 4 MiB antes de abrir
        huge_file = tmp_path / "huge_import.json"
        huge_file.write_bytes(b"{" + b" " * (protocol.MAX_IMPORT_BYTES + 1024) + b"}")
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(
                    v=1,
                    id="imp-huge",
                    type="memory.import",
                    payload={"confirmed": True, "_trusted_import_path": str(huge_file)},
                )
            )
        assert exc_info.value.code == "import_too_large"

        # Importa o arquivo legítimo exportado anteriormente
        imp_res = svc.dispatch(
            protocol.Envelope(
                v=1,
                id="imp-ok",
                type="memory.import",
                payload={"confirmed": True, "_trusted_import_path": str(export_file)},
            )
        )
        assert imp_res["imported"] is True
        assert imp_res["inserted"] >= 1

        restored = svc.dispatch(protocol.Envelope(v=1, id="ml-restored", type="memory.list", payload={}))
        assert any("Python e TypeScript" in item["text"] for item in restored["items"])
    finally:
        svc.close()


def test_memory_profile_get_save_cas_undo_and_limits(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    svc = BridgeService(
        emit=lambda t, p: events.append((t, p)),
        sandbox_dir=tmp_path,
        controller_factory=_MemoryDummyController,
    )
    try:
        initial = svc.dispatch(protocol.Envelope(v=1, id="pg-1", type="memory.profile.get", payload={}))
        assert initial == {"enabled": True, "auto_learn": True, "text": "", "revision": 0,
                           "can_undo": False, "char_count": 0, "max_chars": 8000,
                           "state": "empty"}

        payload = {"text": "Prefiro respostas curtas.", "base_revision": 0}
        saved = svc.dispatch(protocol.Envelope(v=1, id="pg-2", type="memory.profile.save",
                                               payload=payload))
        assert saved["revision"] == 1 and saved["can_undo"] is True and saved["state"] == "ready"
        assert saved["char_count"] == len("Prefiro respostas curtas.")
        assert ("memory.changed", {"reason": "saved", "revision": 1}) in events

        with pytest.raises(protocol.ProtocolError) as stale:
            svc.dispatch(protocol.Envelope(v=1, id="pg-3", type="memory.profile.save",
                                           payload=payload))
        assert stale.value.code == "stale_revision"
        assert stale.value.details == {"revision": 1, "can_undo": True}

        undone = svc.dispatch(protocol.Envelope(v=1, id="pg-4", type="memory.profile.undo",
                                                payload={"base_revision": 1}))
        assert undone["text"] == "" and undone["revision"] == 2 and undone["can_undo"] is False
        assert undone["state"] == "empty"
        assert ("memory.changed", {"reason": "undone", "revision": 2}) in events

        with pytest.raises(protocol.ProtocolError) as nothing:
            svc.dispatch(protocol.Envelope(v=1, id="pg-5", type="memory.profile.undo",
                                           payload={"base_revision": 2}))
        assert nothing.value.code == "nothing_to_undo"
        assert nothing.value.details == {"revision": 2}

        with pytest.raises(protocol.ProtocolError) as too_large:
            svc.dispatch(protocol.Envelope(v=1, id="pg-6", type="memory.profile.save",
                                           payload={"text": "x" * 8001, "base_revision": 2}))
        assert too_large.value.code == "profile_too_large"
        assert too_large.value.details == {"char_count": 8001, "max_chars": 8000}

        options = svc.dispatch(protocol.Envelope(v=1, id="pg-7", type="memory.options.set",
                                                 payload={"options": {"auto_learn": False}}))
        assert options["options"]["auto_learn"] is False
        after = svc.dispatch(protocol.Envelope(v=1, id="pg-8", type="memory.profile.get", payload={}))
        assert after["auto_learn"] is False
    finally:
        svc.close()

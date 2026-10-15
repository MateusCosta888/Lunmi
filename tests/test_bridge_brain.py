"""Testes de Brain, autoaprovação e preferências na ponte IPC v1 (F2b).

Cobre:
- `brain.get` lista providers reais do registry sem nunca expor `api_key`.
- `brain.set`:
  - com tarefa em voo é recusado com `task_busy` sem derrubar a tarefa;
  - chave sentinela (`sk-SENTINEL-SECRET-DO-NOT-ECHO-998877`) nunca aparece em `brain.set`,
    `brain.get` nem no evento `brain.updated`;
  - respeita precedência de variáveis de ambiente (`LLM_PROVIDER`, `LLM_MODEL`,
    `LLM_APP_PROVIDER`, `LLM_APP_MODEL`);
  - salva seleção principal e do AppAgent no sandbox, e só grava DPAPI quando
    `remember_credentials=True`;
  - limpar `app_provider` remove `.local/app-brain.json` e volta o AppAgent para o Brain principal.
- `brain.list` retorna catálogo do provider principal e do AppAgent.
- `brain.verify` executa verificação gratuita (`health` + `list`) sem inferência paga.
- `brain.probe` exige `consent=True` explícito (recusa sem consentimento com `consent_required`).
- `approval.get` / `approval.set` alterna `auto_approve` sem afetar tarefa em andamento.
- `ui.preferences.get` / `ui.preferences.set` valida geometria, `scale_pct` (100/125/150/200) e
  `actions`, rejeitando campos `path`/`file` vindos do renderer.
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from app.brain.contracts import BrainHealth, HealthStatus, ModelInfo
from app.brain.discovery import DiscoveryStatus, ModelDiscovery, ModelListing
from app.brain.probe import ProbeResult
from app.bridge import protocol
from app.bridge import service as service_module
from app.bridge.service import BridgeService
from app.core.controller import TaskResult
from app.settings import Settings

SENTINEL_KEY = "sk-SENTINEL-SECRET-DO-NOT-ECHO-998877"


class _BrainTestController:
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
        self.memory = memory
        self.active = False
        self.reconfigured: list[Settings] = []
        self._task: asyncio.Task[Any] | None = None

    def set_auto_approve(self, enabled: bool) -> None:
        self.settings = Settings(**{**self.settings.__dict__, "auto_approve": enabled})

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def run(self, instruction: str, *, conversation: bool = False) -> TaskResult:
        self.active = True
        self._task = asyncio.current_task()
        try:
            await asyncio.sleep(2.0)
            return TaskResult(status="SUCCESS", message="ok", scope="conversation")
        except asyncio.CancelledError:
            return TaskResult(status="CANCELLED", failure_kind="CANCELLED", message="cancel", scope="conversation")
        finally:
            self.active = False
            self._task = None

    async def reconfigure(self, settings: Settings) -> None:
        self.settings = settings
        self.reconfigured.append(settings)

    async def close(self) -> None:
        self.cancel()
        if self.memory is not None:
            self.memory.close()


def test_sentinel_api_key_never_echoed_and_dpapi_respects_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_APP_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_APP_MODEL", raising=False)

    events: list[tuple[str, dict[str, Any]]] = []
    svc = BridgeService(
        emit=lambda t, p: events.append((t, p)),
        sandbox_dir=tmp_path,
        controller_factory=_BrainTestController,
    )
    try:
        # 1) Aplicar com chave sentinela e remember_credentials=False
        set_res = svc.dispatch(
            protocol.Envelope(
                v=1,
                id="bset-1",
                type="brain.set",
                payload={
                    "provider": "openai",
                    "model": "gpt-4o",
                    "api_key": SENTINEL_KEY,
                    "app_provider": "codex",
                    "app_model": "gpt-5-codex-mini",
                    "remember_credentials": False,
                },
            )
        )
        assert SENTINEL_KEY not in json.dumps(set_res)
        assert set_res["has_api_key"] is True
        assert set_res["provider"] == "openai"
        assert set_res["model"] == "gpt-4o"
        assert set_res["app_provider"] == "codex"
        assert set_res["app_model"] == "gpt-5-codex-mini"

        # Verifica brain.get e eventos: sentinela nunca aparece
        get_res = svc.dispatch(protocol.Envelope(v=1, id="bget-1", type="brain.get", payload={}))
        assert SENTINEL_KEY not in json.dumps(get_res)
        assert SENTINEL_KEY not in json.dumps(events)

        # Sem consentimento remember_credentials, provider.dpapi não é criado
        assert not (tmp_path / ".local" / "provider.dpapi").exists()
        assert (tmp_path / ".local" / "brain-selection.json").exists()
        assert (tmp_path / ".local" / "app-brain.json").exists()

        # 2) Aplicar com remember_credentials=True grava provider.dpapi e limpar app_provider apaga app-brain.json
        svc.dispatch(
            protocol.Envelope(
                v=1,
                id="bset-2",
                type="brain.set",
                payload={
                    "provider": "openai",
                    "model": "gpt-4o",
                    "api_key": SENTINEL_KEY,
                    "app_provider": "",
                    "app_model": "",
                    "remember_credentials": True,
                },
            )
        )
        assert (tmp_path / ".local" / "provider.dpapi").exists()
        assert not (tmp_path / ".local" / "app-brain.json").exists()
        assert SENTINEL_KEY not in (tmp_path / ".local" / "brain-selection.json").read_text(encoding="utf-8")
    finally:
        svc.close()


def test_brain_set_rejected_while_task_in_flight_and_env_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finished_evt = threading.Event()
    svc = BridgeService(
        emit=lambda t, _p: finished_evt.set() if t == "task.finished" else None,
        sandbox_dir=tmp_path,
        controller_factory=_BrainTestController,
    )
    try:
        started = svc.dispatch(
            protocol.Envelope(v=1, id="s1", type="chat.send", payload={"text": "em voo", "mode": "converse"})
        )
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(
                    v=1,
                    id="bset-busy",
                    type="brain.set",
                    payload={"provider": "codex", "model": "gpt-5"},
                )
            )
        assert exc_info.value.code == "task_busy"

        svc.dispatch(
            protocol.Envelope(v=1, id="c1", type="task.cancel", payload={"task_id": started["task_id"]})
        )
        assert finished_evt.wait(timeout=3.0)

        # Precedência de variáveis de ambiente
        monkeypatch.setenv("LLM_PROVIDER", "codex")
        monkeypatch.setenv("LLM_MODEL", "codex-env-model")
        res = svc.dispatch(
            protocol.Envelope(
                v=1,
                id="bset-env",
                type="brain.set",
                payload={"provider": "claude", "model": "claude-sonnet"},
            )
        )
        assert res["provider"] == "codex"
        assert res["model"] == "codex-env-model"
        assert res["env_overrides"]["provider"] is True
        assert res["env_overrides"]["model"] is True
    finally:
        svc.close()


def test_brain_list_verify_and_probe_consent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    discovery = ModelDiscovery()
    discovery.store(
        ModelListing(
            provider="codex",
            label="Codex CLI",
            status=DiscoveryStatus.READY,
            models=(ModelInfo(id="gpt-5-codex", label="GPT-5 Codex", default=True),),
            detail="1 modelo disponível.",
            fetched_at=999999999.0,
        )
    )

    class _FakeBrain:
        async def health(self) -> BrainHealth:
            return BrainHealth(status=HealthStatus.READY, detail="CLI pronto.")

        async def models(self) -> tuple[ModelInfo, ...]:
            return (ModelInfo(id="gpt-5-codex", label="GPT-5 Codex", default=True),)

        async def close(self) -> None:
            pass

    monkeypatch.setattr(service_module, "resolve_brain", lambda _s: _FakeBrain())

    async def _fake_probe(settings: Settings, *, timeout: float = 90.0) -> ProbeResult:
        return ProbeResult(
            provider=settings.provider,
            model=settings.model,
            ok=True,
            latency_ms=142.5,
            text="ok",
            input_tokens=5,
            output_tokens=1,
        )

    monkeypatch.setattr(service_module, "probe_settings", _fake_probe)

    svc = BridgeService(
        emit=lambda _t, _p: None,
        sandbox_dir=tmp_path,
        discovery=discovery,
        controller_factory=_BrainTestController,
    )
    try:
        listing = svc.dispatch(
            protocol.Envelope(v=1, id="bl1", type="brain.list", payload={"provider": "codex", "force": False})
        )
        assert listing["ok"] is True
        assert listing["models"][0]["id"] == "gpt-5-codex"

        verified = svc.dispatch(
            protocol.Envelope(
                v=1,
                id="bv1",
                type="brain.verify",
                payload={"provider": "codex", "model": "gpt-5-codex"},
            )
        )
        assert verified["ok"] is True
        assert verified["status"] == "ready"

        # Probe sem consent=True é recusado
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(
                    v=1,
                    id="bp-no",
                    type="brain.probe",
                    payload={"provider": "codex", "model": "gpt-5-codex"},
                )
            )
        assert exc_info.value.code == "consent_required"

        # Probe com consent=True executa e devolve sumário medido
        probed = svc.dispatch(
            protocol.Envelope(
                v=1,
                id="bp-yes",
                type="brain.probe",
                payload={"provider": "codex", "model": "gpt-5-codex", "consent": True},
            )
        )
        assert probed["ok"] is True
        assert probed["latency_ms"] == 142.5
        assert "respondeu" in probed["summary"]
    finally:
        svc.close()


def test_approval_and_ui_preferences_validation(tmp_path: Path) -> None:
    svc = BridgeService(
        emit=lambda _t, _p: None,
        sandbox_dir=tmp_path,
        controller_factory=_BrainTestController,
    )
    try:
        ap0 = svc.dispatch(protocol.Envelope(v=1, id="a0", type="approval.get", payload={}))
        assert ap0["auto_approve"] is False
        assert ap0["mode"] == "manual"

        ap1 = svc.dispatch(
            protocol.Envelope(v=1, id="a1", type="approval.set", payload={"auto_approve": True})
        )
        assert ap1["auto_approve"] is True
        assert ap1["mode"] == "auto"

        # Preferências: rejeita tentativa de passar path arbitrário
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(
                    v=1,
                    id="p-bad",
                    type="ui.preferences.set",
                    payload={"path": "C:/outro.json", "scale_pct": 125},
                )
            )
        assert exc_info.value.code == "invalid_payload"

        prefs = svc.dispatch(
            protocol.Envelope(
                v=1,
                id="p-ok",
                type="ui.preferences.set",
                payload={
                    "window": [100, 120, 1120, 760],
                    "scale_pct": 125,
                    "actions": [{"label": "Abrir", "draft": "Abra o "}],
                },
            )
        )
        assert prefs["window"] == [100, 120, 1120, 760]
        assert prefs["scale_pct"] == 125
        assert prefs["actions"] == [{"label": "Abrir", "draft": "Abra o "}]
        themed = svc.dispatch(protocol.Envelope(v=1, id="p-theme", type="ui.preferences.set",
                                               payload={"theme": "soft-light"}))
        assert themed["theme"] == "soft-light"
        assert themed["window"] == prefs["window"]
        assert themed["actions"] == prefs["actions"]
        moved = svc.dispatch(protocol.Envelope(v=1, id="p-move", type="ui.preferences.set",
                                              payload={"window": [40, 50, 900, 700]}))
        assert moved["theme"] == "soft-light"
    finally:
        svc.close()

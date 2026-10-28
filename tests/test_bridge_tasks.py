"""Testes de chat, tarefas, cancelamento, confirmações e respostas interativas (F2a).

Cobre:
- `chat.send` em modo `converse`: executa `controller.run(..., conversation=True)`, não inicializa
  worker UFO, emite aviso explícito de modo Conversar, emite `chat.message` do assistente e
  `task.finished` único com DTO público.
- `chat.send` sem modo ou com modo inválido é recusado com `invalid_payload`.
- `chat.send` concorrente enquanto há tarefa em voo é recusado com `task_busy`.
- `task.cancel` cancela a tarefa ativa, recusa perguntas pendentes e garante exatamente um
  `task.finished` terminal (status `CANCELLED`).
- `task.confirm`:
  - autorização (`approved=True`) permite continuar e concluir com `SUCCESS`;
  - recusa (`approved=False`) encerra a tarefa com `CANCELLED`;
  - confirmação duplicada para o mesmo `request_id` é recusada com `invalid_request_id`;
  - confirmação sem solicitação pendente ou de outra tarefa é recusada com `invalid_request_id`.
- `task.answer`:
  - `kind="choice"`: valida índices `0 <= index < len(candidates)` e aceita `None` como recusa;
    índice fora da faixa é recusado com `invalid_payload`;
  - `kind="location"`: aceita path indicado ou string vazia ("deixar o agente resolver"), e
    respeita o timeout automático (45s em produção; configurável no teste) retornando `""` sem
    travar a tarefa.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from app.bridge import protocol
from app.bridge import service as service_module
from app.bridge.service import BridgeService
from app.core.controller import TaskResult
from app.settings import Settings


class _TaskFakeEngine:
    def __init__(self) -> None:
        self.host_pids: set[int] = set()
        self.started_calls = 0

    def set_host_pids(self, pids: Any) -> None:
        self.host_pids = set(pids)

    async def start(self) -> None:
        self.started_calls += 1

    async def stop(self) -> None:
        pass


class _TaskFakeController:
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
        self.engine = _TaskFakeEngine()
        self.active = False
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
        self._current_task = asyncio.current_task()
        self.last_user_name = user_name
        self.last_attachments = attachments
        try:
            self.on_state("Thinking")
            if conversation:
                await asyncio.sleep(0.01)
                return TaskResult(
                    status="SUCCESS",
                    message=f"Resposta de conversa para: {instruction}",
                    scope="conversation",
                    plan_source="conversation",
                    llm_calls=1,
                    input_tokens=12,
                    output_tokens=8,
                    total_tokens=20,
                )

            await self.engine.start()
            self.on_state("Executing")

            if "confirmar_risco" in instruction and self.confirm is not None:
                ok = await self.confirm("Autorizar sobrescrita de arquivo?")
                if not ok:
                    return TaskResult(
                        status="CANCELLED",
                        failure_kind="CANCELLED",
                        message="Sobrescrita não autorizada.",
                        scope="end_to_end",
                    )

            if "escolher_app" in instruction and self.choose is not None:
                candidates = [
                    {"name": "Code Stable", "publisher": "Microsoft", "install_dir": "C:/VSCode", "app_id": "vscode-1"},
                    {"name": "Code Insiders", "publisher": "Microsoft", "install_dir": "C:/Insiders", "app_id": "vscode-2"},
                ]
                chosen = await self.choose("code", candidates)
                if chosen is None:
                    return TaskResult(
                        status="CANCELLED",
                        failure_kind="CANCELLED",
                        message="Nenhum aplicativo escolhido entre os candidatos encontrados; nada foi aberto.",
                        scope="end_to_end",
                    )
                return TaskResult(
                    status="SUCCESS",
                    message=f"Abriu candidato #{chosen}",
                    scope="end_to_end",
                    evidence=[{"verified": True, "chosen": chosen}],
                )

            if "localizar_app" in instruction and self.indicate is not None:
                located = await self.indicate("programa_desconhecido")
                return TaskResult(
                    status="SUCCESS",
                    message=f"Localização recebida: '{located}'",
                    scope="end_to_end",
                    evidence=[{"verified": True, "located": located}],
                )

            if "lenta" in instruction:
                await asyncio.sleep(5.0)

            return TaskResult(
                status="SUCCESS",
                message="Ação concluída e verificada.",
                scope="end_to_end",
                plan_source="deterministic",
                actions=1,
                actions_executed=1,
                steps=1,
                evidence=[{"verified": True, "secret_dump": "NAO_VAZAR_NO_DTO"}],
            )
        except asyncio.CancelledError:
            return TaskResult(
                status="CANCELLED",
                failure_kind="CANCELLED",
                message="Tarefa interrompida.",
                scope="end_to_end",
            )
        finally:
            self.active = False
            self._current_task = None

    async def reconfigure(self, settings: Settings) -> None:
        self.settings = settings

    async def close(self) -> None:
        self.cancel()
        if self.memory is not None:
            self.memory.close()


def test_converse_mode_runs_without_computer_use_and_rejects_invalid_mode(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    finished_evt = threading.Event()

    def on_emit(t: str, p: dict[str, Any]) -> None:
        events.append((t, p))
        if t == "task.finished":
            finished_evt.set()

    svc = BridgeService(emit=on_emit, sandbox_dir=tmp_path, controller_factory=_TaskFakeController)
    try:
        # Modo ausente ou inválido -> invalid_payload
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(protocol.Envelope(v=1, id="m1", type="chat.send", payload={"text": "oi"}))
        assert exc_info.value.code == "invalid_payload"

        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(protocol.Envelope(v=1, id="m2", type="chat.send", payload={"text": "oi", "mode": "invalid-mode"}))
        assert exc_info.value.code == "invalid_payload"

        res = svc.dispatch(
            protocol.Envelope(v=1, id="m3", type="chat.send", payload={"text": "Explique o modo conversar", "mode": "converse"})
        )
        assert res["status"] == "started"
        assert finished_evt.wait(timeout=3.0)

        # Não iniciou o worker de computer-use
        assert svc.controller.engine.started_calls == 0

        chat_msgs = [p for t, p in events if t == "chat.message"]
        roles = [m["role"] for m in chat_msgs]
        assert roles == ["user", "system", "assistant"]
        assert "nenhuma ação no Windows" in chat_msgs[1]["text"]

        finished = [p for t, p in events if t == "task.finished"]
        assert len(finished) == 1
        assert finished[0]["status"] == "SUCCESS"
        assert finished[0]["scope"] == "conversation"
        assert finished[0]["total_tokens"] == 20
    finally:
        svc.close()


def test_task_started_precedes_each_user_echo_and_message_ids_are_stable(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    finished_evt = threading.Event()

    def on_emit(t: str, p: dict[str, Any]) -> None:
        events.append((t, p))
        if t == "task.finished":
            finished_evt.set()

    svc = BridgeService(emit=on_emit, sandbox_dir=tmp_path, controller_factory=_TaskFakeController)
    try:
        for turn in range(3):
            finished_evt.clear()
            result = svc.dispatch(protocol.Envelope(
                v=1, id=f"turn-{turn}", type="chat.send",
                payload={"text": f"Pergunta sintética {turn}", "mode": "converse"},
            ))
            assert finished_evt.wait(timeout=3.0)
            task_id = result["task_id"]
            own = [(kind, payload) for kind, payload in events if payload.get("task_id") == task_id]
            started = next(index for index, (kind, _) in enumerate(own) if kind == "task.started")
            user = [(index, payload) for index, (kind, payload) in enumerate(own)
                    if kind == "chat.message" and payload.get("role") == "user"]
            assert len(user) == 1 and started < user[0][0]
            assert user[0][1]["message_id"] == f"{task_id}:user"
        user_ids = [payload["message_id"] for kind, payload in events
                    if kind == "chat.message" and payload.get("role") == "user"]
        assert len(set(user_ids)) == 3
    finally:
        svc.close()


def test_concurrent_task_rejected_and_cancel_emits_single_terminal(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    finished_evt = threading.Event()

    def on_emit(t: str, p: dict[str, Any]) -> None:
        events.append((t, p))
        if t == "task.finished":
            finished_evt.set()

    svc = BridgeService(emit=on_emit, sandbox_dir=tmp_path, controller_factory=_TaskFakeController)
    try:
        started = svc.dispatch(
            protocol.Envelope(v=1, id="t1", type="chat.send", payload={"text": "tarefa lenta", "mode": "execute"})
        )
        task_id = started["task_id"]

        # Segunda tarefa concorrente é recusada
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(v=1, id="t2", type="chat.send", payload={"text": "outra", "mode": "execute"})
            )
        assert exc_info.value.code == "task_busy"

        # Cancela a tarefa em voo
        cancel_res = svc.dispatch(
            protocol.Envelope(v=1, id="c1", type="task.cancel", payload={"task_id": task_id})
        )
        assert cancel_res["cancelled"] is True
        assert finished_evt.wait(timeout=3.0)

        # Idempotência do cancel após encerrar
        cancel_again = svc.dispatch(
            protocol.Envelope(v=1, id="c2", type="task.cancel", payload={"task_id": task_id})
        )
        assert cancel_again["cancelled"] is False

        finished = [p for t, p in events if t == "task.finished"]
        assert len(finished) == 1
        assert finished[0]["status"] == "CANCELLED"
        assert finished[0]["failure_kind"] == "CANCELLED"
    finally:
        svc.close()


def test_cancel_before_controller_starts_is_not_lost(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    finished = threading.Event()
    loop_blocked = threading.Event()
    release_loop = threading.Event()

    def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))
        if kind == 'task.finished':
            finished.set()

    def block_loop() -> None:
        loop_blocked.set()
        assert release_loop.wait(5)

    svc = BridgeService(emit=emit, sandbox_dir=tmp_path, controller_factory=_TaskFakeController)
    try:
        svc._loop.call_soon_threadsafe(block_loop)
        assert loop_blocked.wait(3)
        started = svc.dispatch(protocol.Envelope(
            v=1, id='send', type='chat.send', payload={'text': 'tarefa lenta', 'mode': 'execute'},
        ))
        cancelled = svc.dispatch(protocol.Envelope(
            v=1, id='cancel', type='task.cancel', payload={'task_id': started['task_id']},
        ))
        assert cancelled['cancelled'] is True
        release_loop.set()
        assert finished.wait(3)
        terminal = [p for kind, p in events if kind == 'task.finished']
        assert len(terminal) == 1
        assert terminal[0]['status'] == 'CANCELLED'
        assert svc.controller.engine.started_calls == 0
        assert not svc._is_task_running()

        finished.clear()
        svc.dispatch(protocol.Envelope(
            v=1, id='next', type='chat.send', payload={'text': 'seguinte', 'mode': 'converse'},
        ))
        assert finished.wait(3)
        assert [p for kind, p in events if kind == 'task.finished'][-1]['status'] == 'SUCCESS'
    finally:
        release_loop.set()
        svc.close()


def test_confirm_accept_decline_duplicate_and_unsolicited(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    confirm_ready = threading.Event()
    finished_evt = threading.Event()

    def on_emit(t: str, p: dict[str, Any]) -> None:
        events.append((t, p))
        if t == "task.need_confirm":
            confirm_ready.set()
        elif t == "task.finished":
            finished_evt.set()

    svc = BridgeService(emit=on_emit, sandbox_dir=tmp_path, controller_factory=_TaskFakeController)
    try:
        # Confirmação sem pedido pendente é recusada
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(v=1, id="unsol", type="task.confirm", payload={"request_id": "cfm-none", "approved": True})
            )
        assert exc_info.value.code == "invalid_request_id"

        # 1) Tarefa com confirmação recusada
        svc.dispatch(
            protocol.Envelope(v=1, id="run1", type="chat.send", payload={"text": "confirmar_risco 1", "mode": "execute"})
        )
        assert confirm_ready.wait(timeout=3.0)
        req_id_1 = next(p["request_id"] for t, p in events if t == "task.need_confirm")

        svc.dispatch(
            protocol.Envelope(v=1, id="cfm1", type="task.confirm", payload={"request_id": req_id_1, "approved": False})
        )
        # Segunda resposta para o mesmo request_id falha com invalid_request_id
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(v=1, id="cfm1-dup", type="task.confirm", payload={"request_id": req_id_1, "approved": True})
            )
        assert exc_info.value.code == "invalid_request_id"

        assert finished_evt.wait(timeout=3.0)
        fin1 = [p for t, p in events if t == "task.finished"][-1]
        assert fin1["status"] == "CANCELLED"

        # 2) Tarefa com confirmação aprovada
        events.clear()
        confirm_ready.clear()
        finished_evt.clear()

        svc.dispatch(
            protocol.Envelope(v=1, id="run2", type="chat.send", payload={"text": "confirmar_risco 2", "mode": "execute"})
        )
        assert confirm_ready.wait(timeout=3.0)
        req_id_2 = next(p["request_id"] for t, p in events if t == "task.need_confirm")

        svc.dispatch(
            protocol.Envelope(v=1, id="cfm2", type="task.confirm", payload={"request_id": req_id_2, "approved": True})
        )
        assert finished_evt.wait(timeout=3.0)
        fin2 = [p for t, p in events if t == "task.finished"][-1]
        assert fin2["status"] == "SUCCESS"
        assert "secret_dump" not in str(fin2)
    finally:
        svc.close()


def test_task_answer_choice_and_location_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    input_ready = threading.Event()
    finished_evt = threading.Event()

    def on_emit(t: str, p: dict[str, Any]) -> None:
        events.append((t, p))
        if t == "task.need_input":
            input_ready.set()
        elif t == "task.finished":
            finished_evt.set()

    svc = BridgeService(emit=on_emit, sandbox_dir=tmp_path, controller_factory=_TaskFakeController)
    try:
        # 1) Escolha ambígua: índice fora do intervalo falha, índice válido conclui
        svc.dispatch(
            protocol.Envelope(v=1, id="ch1", type="chat.send", payload={"text": "escolher_app", "mode": "execute"})
        )
        assert input_ready.wait(timeout=3.0)
        inp_evt = next(p for t, p in events if t == "task.need_input")
        req_id = inp_evt["request_id"]
        assert inp_evt["kind"] == "choice"
        assert len(inp_evt["candidates"]) == 2

        # Índice inválido (5) não consome a pergunta e retorna invalid_payload
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(
                    v=1,
                    id="ans-bad",
                    type="task.answer",
                    payload={"request_id": req_id, "kind": "choice", "index": 5},
                )
            )
        assert exc_info.value.code == "invalid_payload"

        # Índice válido (1) resolve
        svc.dispatch(
            protocol.Envelope(
                v=1,
                id="ans-ok",
                type="task.answer",
                payload={"request_id": req_id, "kind": "choice", "index": 1},
            )
        )
        assert finished_evt.wait(timeout=3.0)
        fin = [p for t, p in events if t == "task.finished"][-1]
        assert fin["status"] == "SUCCESS"
        assert fin["message"] == "Abriu candidato #1"

        # 2) Timeout automático de localização (configurado para 0.15s no teste; default é 45.0s)
        assert service_module.LOCATION_TIMEOUT_SECONDS == 45.0
        monkeypatch.setattr(service_module, "LOCATION_TIMEOUT_SECONDS", 0.15)
        events.clear()
        input_ready.clear()
        finished_evt.clear()

        svc.dispatch(
            protocol.Envelope(v=1, id="loc1", type="chat.send", payload={"text": "localizar_app", "mode": "execute"})
        )
        assert input_ready.wait(timeout=3.0)
        assert finished_evt.wait(timeout=3.0)
        fin_loc = [p for t, p in events if t == "task.finished"][-1]
        assert fin_loc["status"] == "SUCCESS"
        assert fin_loc["message"] == "Localização recebida: ''"
    finally:
        svc.close()


def test_internal_type_error_in_controller_is_never_retried(tmp_path: Path) -> None:
    """Regressão de segurança: `TypeError` lançado dentro de `controller.run` não pode causar retry."""
    events: list[tuple[str, dict[str, Any]]] = []
    finished_evt = threading.Event()
    calls = 0

    class _TypeErrorController(_TaskFakeController):
        async def run(
            self,
            instruction: str,
            *,
            conversation: bool = False,
            user_name: str = "",
            attachments: list[dict[str, Any]] | None = None,
        ) -> TaskResult:
            nonlocal calls
            calls += 1
            raise TypeError("erro interno depois de despachar efeito")

    def on_emit(t: str, p: dict[str, Any]) -> None:
        events.append((t, p))
        if t == "task.finished":
            finished_evt.set()

    svc = BridgeService(emit=on_emit, sandbox_dir=tmp_path, controller_factory=_TypeErrorController)
    try:
        svc.dispatch(
            protocol.Envelope(v=1, id="te-1", type="chat.send", payload={"text": "oi", "mode": "converse"})
        )
        assert finished_evt.wait(timeout=3.0)
        assert calls == 1, "controller.run deve ser chamado exatamente uma vez"
        errors = [p for t, p in events if t == "task.error"]
        finished = [p for t, p in events if t == "task.finished"]
        assert len(errors) == 1
        assert len(finished) == 1
        assert finished[0]["status"] == "FAILED"
    finally:
        svc.close()


def test_ui_preferences_profile_and_chat_attachments_contract(tmp_path: Path) -> None:
    import base64
    import io
    from PIL import Image

    events: list[tuple[str, dict[str, Any]]] = []
    finished_evt = threading.Event()

    def on_emit(t: str, p: dict[str, Any]) -> None:
        events.append((t, p))
        if t == "task.finished":
            finished_evt.set()

    svc = BridgeService(emit=on_emit, sandbox_dir=tmp_path, controller_factory=_TaskFakeController)
    try:
        # 1) Perfil inicial vazio
        initial = svc.dispatch(protocol.Envelope(v=1, id="p0", type="ui.preferences.get", payload={}))
        assert initial["profile"] == {"name": "", "avatar_data_url": ""}

        # 2) Salva nome e avatar PNG real; merge parcial preserva o outro campo
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), (10, 120, 220)).save(buf, format="PNG")
        png_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

        updated = svc.dispatch(
            protocol.Envelope(
                v=1,
                id="p1",
                type="ui.preferences.set",
                payload={"profile": {"name": "  Lucas  ", "avatar_data_url": png_url}},
            )
        )
        assert updated["profile"]["name"] == "Lucas"
        assert updated["profile"]["avatar_data_url"] == png_url

        # Merge parcial: atualizar só o tema ou só o nome preserva o avatar
        merged = svc.dispatch(
            protocol.Envelope(
                v=1,
                id="p2",
                type="ui.preferences.set",
                payload={"profile": {"name": "Marina"}},
            )
        )
        assert merged["profile"]["name"] == "Marina"
        assert merged["profile"]["avatar_data_url"] == png_url

        # Avatar inválido/corrompido ou path são recusados com invalid_payload
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(
                    v=1,
                    id="p-bad",
                    type="ui.preferences.set",
                    payload={"profile": {"avatar_data_url": "data:image/png;base64,aW52YWxpZG8="}},
                )
            )
        assert exc_info.value.code == "invalid_payload"

        # 3) Anexos em modo Execute são recusados claramente
        with pytest.raises(protocol.ProtocolError) as exc_info:
            svc.dispatch(
                protocol.Envelope(
                    v=1,
                    id="att-exec",
                    type="chat.send",
                    payload={
                        "mode": "execute",
                        "text": "Analise este arquivo",
                        "attachments": [{"name": "notas.txt", "text": "conteúdo"}],
                    },
                )
            )
        assert exc_info.value.code == "invalid_payload"
        assert "Conversar" in exc_info.value.public_message

        # 4) Anexos com path, extensão de imagem/PDF ou NUL são recusados
        for bad_att in (
            [{"name": "notas.txt", "text": "oi", "path": "C:/secreto.txt"}],
            [{"name": "../notas.txt", "text": "oi"}],
            [{"name": "notas..txt", "text": "oi"}],
            [{"name": "foto.png", "text": "oi"}],
            [{"name": "relatorio.pdf", "text": "oi"}],
            [{"name": "binario.txt", "text": "a\x00b"}],
        ):
            with pytest.raises(protocol.ProtocolError) as exc_info:
                svc.dispatch(
                    protocol.Envelope(
                        v=1,
                        id="att-bad",
                        type="chat.send",
                        payload={"mode": "converse", "text": "Veja", "attachments": bad_att},
                    )
                )
            assert exc_info.value.code == "invalid_payload"

        # 5) Anexo válido em modo Conversar repassa user_name (sem avatar) e attachments separados
        events.clear()
        finished_evt.clear()
        svc.dispatch(
            protocol.Envelope(
                v=1,
                id="att-ok",
                type="chat.send",
                payload={
                    "mode": "converse",
                    "text": "Resuma o arquivo",
                    "attachments": [{"name": "resumo.md", "text": "# Título\nLinha 1"}],
                },
            )
        )
        assert finished_evt.wait(timeout=3.0)
        assert svc.controller.last_user_name == "Marina"
        assert svc.controller.last_attachments == [
            {
                "name": "resumo.md",
                "size_bytes": len("# Título\nLinha 1".encode("utf-8")),
                "text": "# Título\nLinha 1",
            }
        ]
        user_evt = next(p for t, p in events if t == "chat.message" and p.get("role") == "user")
        assert user_evt["text"] == "Resuma o arquivo"
        assert user_evt["attachments"] == [
            {"name": "resumo.md", "size_bytes": len("# Título\nLinha 1".encode("utf-8"))}
        ]
    finally:
        svc.close()


def test_controller_converse_with_profile_and_attachments_never_persists_attachment_to_memory(
    tmp_path: Path,
) -> None:
    """Verifica no TaskController real (com brain stub local, sem rede) que:
    - `user_name` entra como contexto JSON isolado mesmo com `memory.enabled=False`;
    - `attachments` entram em mensagem separada marcada como dado não confiável;
    - quando há anexo, nem o texto do anexo nem uma resposta que o cite geram propostas ou
      histórico automático de memória, e `logs/tasks.jsonl` não contém o anexo;
    - `size_bytes` malformado não envenena `controller.active`.
    """
    import json
    from app.core.controller import TaskController
    from app.llm import Completion
    from app.memory import MemoryManager

    captured_messages: list[list[dict[str, str]]] = []

    class _LocalStubBrain:
        async def complete(self, messages: list[dict[str, str]]) -> Completion:
            captured_messages.append(list(messages))
            # Tenta propor memória citando texto do anexo e da instrução
            reply = json.dumps(
                {
                    "observation": "",
                    "thought": "",
                    "status": "FINISH",
                    "message": ["Olá Marina! O arquivo diz SEGREDO_DO_ANEXO_123."],
                    "action": None,
                    "memory_proposals": [
                        {"kind": "fact", "text": "Eu gosto de café"},
                        {"kind": "fact", "text": "SEGREDO_DO_ANEXO_123"},
                    ],
                }
            )
            return Completion(text=reply, input_tokens=10, output_tokens=10)

        async def close(self) -> None:
            pass

    mem = MemoryManager(path=tmp_path / "memory.sqlite3")
    mem.set_options(enabled=True, proposals=True, history=True, episodes=True)
    settings = Settings(
        provider="openai",
        model="gpt-4.1-mini",
        api_key="sk-test-local-only",
        logs_path=tmp_path / "logs",
        output_path=tmp_path / "output",
        collect_metrics=False,
    )
    ctrl = TaskController(settings, memory=mem)
    ctrl.brain = _LocalStubBrain()  # type: ignore[assignment]

    try:
        # 1) Mesmo com memória desligada, user_name vai ao prompt como JSON
        mem.set_options(enabled=False)
        res1 = asyncio.run(
            ctrl.run(
                "Oi, tudo bem?",
                conversation=True,
                user_name='Marina "Admin"',
            )
        )
        assert res1.status == "SUCCESS"
        assert ctrl.active is False
        sent1 = captured_messages[-1]
        assert any('"user_name": "Marina \\"Admin\\""' in m["content"] for m in sent1 if m["role"] == "system")

        # 2) Com memória ligada e anexo presente (incluindo size_bytes malformado), nenhuma proposta
        # ou histórico de conversa é gravado, e tasks.jsonl não contém o texto do anexo.
        mem.set_options(enabled=True, proposals=True, history=True, episodes=True)
        res2 = asyncio.run(
            ctrl.run(
                "Eu gosto de café. Analise o anexo.",
                conversation=True,
                user_name="Marina",
                attachments=[
                    {
                        "name": "dados.txt",
                        "size_bytes": "nao-inteiro",  # não pode envenenar controller.active
                        "text": "SEGREDO_DO_ANEXO_123",
                    }
                ],
            )
        )
        assert res2.status == "SUCCESS"
        assert ctrl.active is False
        assert mem.proposals() == []
        assert mem.list() == []
        exported = mem.export()
        assert exported["messages"] == []
        assert "SEGREDO_DO_ANEXO_123" not in json.dumps(exported)

        tasks_log = (tmp_path / "logs" / "tasks.jsonl").read_text(encoding="utf-8")
        assert "SEGREDO_DO_ANEXO_123" not in tasks_log
    finally:
        asyncio.run(ctrl.close())

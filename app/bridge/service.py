"""Serviço de domínio da ponte IPC v1 da Lunmi (`app/bridge/service.py`).

Encapsula uma única instância de `TaskController`, `MemoryManager` e preferências de UI
em um loop asyncio dedicado em thread em segundo plano, mantendo o contrato público:
- `session.hello` / `app.shutdown`
- `chat.send`, `task.cancel`, `task.confirm`, `task.answer`
- `brain.list`, `brain.get`, `brain.set`, `brain.verify`, `brain.probe`
- `approval.get`, `approval.set`
- `memory.list`, `memory.search`, `memory.upsert`, `memory.delete`,
  `memory.proposals`, `memory.propose`, `memory.accept`, `memory.reject`,
  `memory.export`, `memory.import`, `memory.wipe`,
  `memory.options.get`, `memory.options.set`
- `ui.preferences.get`, `ui.preferences.set`

Regras críticas:
- Em modo sandbox (`sandbox_dir` definido), todo o estado (`.local/memory.sqlite3`,
  `.local/lunmi-ui.json`, `.local/brain-selection.json`, `.local/app-brain.json`,
  `.local/approval.json`, `.local/provider.dpapi`, `logs/`, `output/`) vive dentro de
  `sandbox_dir`, sem nunca ler ou mutar o estado pessoal da máquina.
- Nenhuma chave API (`api_key`) é retornada em `brain.get`, `brain.set` ou eventos.
- `host_pids` é filtrado para aceitar apenas o próprio processo, seus ancestrais/pais
  e descendentes reais da árvore do processo pai (Electron main), rejeitando PIDs
  externos forjados.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import psutil

from app import settings as settings_module
from app.brain import registry
from app.brain.contracts import BrainError, Capability
from app.brain.discovery import DiscoveryStatus, ModelDiscovery, ModelListing, get_discovery
from app.brain.probe import PROBE_TIMEOUT_SECONDS, probe_settings
from app.brain.registry import resolve as resolve_brain
from app.bridge import protocol
from app.core.controller import TaskController, TaskResult
from app.memory import MemoryItem, MemoryManager, MemoryPolicyError
from app.memory.contracts import PROFILE_KINDS
from app.settings import (
    API_KEY_PROVIDERS,
    CLI_PROVIDERS,
    ConfigurationError,
    Settings,
)
from app.ui import ui_preferences

LOCATION_TIMEOUT_SECONDS = 45.0


def filter_trusted_host_pids(candidate_pids: list[int], *, trusted_root_pid: int | None = None) -> set[int]:
    """Filtra `candidate_pids` para aceitar somente PIDs pertencentes à árvore do host confiável.

    Aceita:
    - `os.getpid()` e `os.getppid()`
    - `trusted_root_pid` (por padrão `os.getppid()`) e seus ancestrais/descendentes vivos
    Qualquer PID externo forjado que não pertença à família de processos do host é ignorado.
    """
    own_pid = os.getpid()
    parent_pid = os.getppid()
    root_pid = trusted_root_pid if (trusted_root_pid and trusted_root_pid > 0) else parent_pid

    allowed_family: set[int] = {own_pid}
    if parent_pid > 0:
        allowed_family.add(parent_pid)
    if root_pid > 0:
        allowed_family.add(root_pid)

    for anchor in list(allowed_family):
        try:
            proc = psutil.Process(anchor)
            for child in proc.children(recursive=True):
                allowed_family.add(child.pid)
            curr: psutil.Process | None = proc
            depth = 0
            while curr is not None and depth < 4:
                allowed_family.add(curr.pid)
                curr = curr.parent()
                depth += 1
        except (psutil.Error, OSError):
            continue

    verified: set[int] = {own_pid}
    if parent_pid > 0:
        verified.add(parent_pid)
    for pid in candidate_pids:
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            if pid in allowed_family:
                verified.add(pid)
    return verified


def public_task_result_dto(result: TaskResult) -> dict[str, Any]:
    """DTO público de `TaskResult` para `task.finished`, sem dumps UIA brutos, prompts ou segredos."""
    evidence = result.evidence or []
    verified_count = sum(1 for item in evidence if isinstance(item, dict) and item.get("verified"))
    asked_approvals = sum(1 for item in (result.approvals or []) if isinstance(item, dict) and item.get("asked"))
    total_approvals = len(result.approvals or [])
    return {
        "task_id": result.task_id,
        "status": result.status,
        "message": result.message,
        "provider": result.provider,
        "model": result.model,
        "app_provider": result.app_provider,
        "app_model": result.app_model,
        "scope": result.scope,
        "plan_source": result.plan_source,
        "fast_path_label": result.fast_path_label,
        "failure_kind": result.failure_kind,
        "false_success": bool(result.false_success),
        "cold_start": bool(result.cold_start),
        "duration_ms": round(float(result.duration_ms or 0.0), 2),
        "llm_time_ms": round(float(result.llm_time_ms or 0.0), 2),
        "planning_time_ms": round(float(result.planning_time_ms or 0.0), 2),
        "observation_time_ms": round(float(result.observation_time_ms or 0.0), 2),
        "execution_time_ms": round(float(result.execution_time_ms or 0.0), 2),
        "verification_time_ms": round(float(result.verification_time_ms or 0.0), 2),
        "startup_time_ms": round(float(result.startup_time_ms or 0.0), 2),
        "waits_ms": round(float(result.waits_ms or 0.0), 2),
        "recall_ms": round(float(result.recall_ms or 0.0), 2),
        "llm_calls": int(result.llm_calls or 0),
        "actions": int(result.actions or 0),
        "actions_executed": int(result.actions_executed or 0),
        "steps": int(result.steps or 0),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "total_tokens": result.total_tokens,
        "evidence_count": len(evidence),
        "evidence_verified_count": verified_count,
        "approval_mode": result.approval_mode,
        "approvals_total": total_approvals,
        "approvals_asked": asked_approvals,
        "app_index_hit": bool(result.app_index_hit),
        "app_source": result.app_source,
        "app_launch_kind": result.app_launch_kind,
        "app_resolve_ms": round(float(result.app_resolve_ms or 0.0), 2),
        "app_search_ms": round(float(result.app_search_ms or 0.0), 2),
        "app_candidates": int(result.app_candidates or 0),
        "app_index_cold": bool(result.app_index_cold),
        "memory_context_items": int(result.memory_context_items or 0),
        "memory_error": bool(result.memory_error),
    }


def public_memory_item_dto(item: MemoryItem) -> dict[str, Any]:
    """DTO público de `MemoryItem`: nunca expõe `metadata` bruto ou segredos."""
    return {
        "id": item.id,
        "kind": item.kind,
        "text": item.text,
        "status": item.status,
        "source": item.source,
        "updated_at": item.updated_at,
        "created_at": item.created_at,
    }


class BridgeService:
    """Serviço completo da ponte IPC v1."""

    SUPPORTED_COMMANDS: frozenset[str] = protocol.COMMAND_TYPES

    def __init__(
        self,
        *,
        emit: Callable[[str, dict[str, Any]], None],
        sandbox_dir: Path | None = None,
        settings: Settings | None = None,
        controller_factory: Callable[..., Any] | None = None,
        memory: MemoryManager | None = None,
        discovery: ModelDiscovery | None = None,
        trusted_root_pid: int | None = None,
    ) -> None:
        self._emit = emit
        self.sandbox_dir = Path(sandbox_dir).resolve() if sandbox_dir is not None else None
        self.root_dir = self.sandbox_dir if self.sandbox_dir is not None else Path(settings_module.ROOT)
        self.trusted_root_pid = trusted_root_pid if trusted_root_pid is not None else os.getppid()
        self.host_pids: set[int] = filter_trusted_host_pids([], trusted_root_pid=self.trusted_root_pid)

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="bridge-service-loop", daemon=True)
        self._thread.start()

        self.prefs_path = ui_preferences.path_for(self.root_dir)
        self.prefs = ui_preferences.load(self.prefs_path)

        if settings is not None:
            self.settings = settings
        elif self.sandbox_dir is not None:
            self.settings = self._load_sandbox_settings(self.sandbox_dir)
        else:
            try:
                self.settings = Settings.load()
            except ConfigurationError:
                self.settings = Settings()

        self._memory_owned = memory is None
        if memory is not None:
            self.memory = memory
        else:
            mem_path = self.root_dir / ".local" / "memory.sqlite3"
            mem_path.parent.mkdir(parents=True, exist_ok=True)
            self.memory = MemoryManager(path=mem_path)

        self.discovery = discovery if discovery is not None else (
            ModelDiscovery() if self.sandbox_dir is not None else get_discovery()
        )

        factory = controller_factory or TaskController
        self.controller = factory(
            self.settings,
            on_state=self._on_controller_state,
            confirm=self._confirm_callback,
            choose=self._choose_callback,
            indicate=self._indicate_callback,
            memory=self.memory,
        )
        self._sync_engine_host_pids()

        self._active_task_id: str | None = None
        self._active_task_future: concurrent.futures.Future[TaskResult] | None = None
        self._reconfiguring = False
        self._closed = False

        # Solicitações interativas pendentes (confirm, choice, location)
        self._pending_confirms: dict[str, tuple[str, asyncio.Future[bool]]] = {}
        self._pending_inputs: dict[str, tuple[str, str, int, asyncio.Future[Any]]] = {}
        self._lock = threading.RLock()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def _load_sandbox_settings(self, sandbox: Path) -> Settings:
        local_dir = sandbox / ".local"
        local_dir.mkdir(parents=True, exist_ok=True)
        logs_dir = sandbox / "logs"
        out_dir = sandbox / "output"
        logs_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        approval = False
        app_path = local_dir / "approval.json"
        if app_path.exists():
            try:
                raw = json.loads(app_path.read_text(encoding="utf-8"))
                approval = isinstance(raw, dict) and raw.get("auto_approve") is True
            except (OSError, ValueError):
                approval = False

        provider = "openai"
        model = ""
        sel_path = local_dir / "brain-selection.json"
        if sel_path.exists():
            try:
                sel = json.loads(sel_path.read_text(encoding="utf-8"))
                if isinstance(sel, dict) and isinstance(sel.get("provider"), str):
                    provider = sel["provider"]
                    model = str(sel.get("model") or "")
            except (OSError, ValueError):
                pass

        return Settings(
            provider=provider,
            model=model,
            auto_approve=approval,
            prewarm=False,
            logs_path=logs_dir,
            output_path=out_dir,
        )

    def _sync_engine_host_pids(self) -> None:
        engine = getattr(self.controller, "engine", None)
        if engine is not None and hasattr(engine, "set_host_pids"):
            engine.set_host_pids(self.host_pids)

    def update_host_pids(self, raw_pids: Any) -> set[int]:
        validated = protocol.validate_host_pids(raw_pids)
        with self._lock:
            self.host_pids = filter_trusted_host_pids(validated, trusted_root_pid=self.trusted_root_pid)
            self._sync_engine_host_pids()
            return set(self.host_pids)

    def capabilities(self) -> dict[str, bool]:
        return protocol.capabilities_for_commands(
            self.SUPPORTED_COMMANDS,
            web_search=False,
            browser_use=False,
        )

    # ------------------------------------------------- callbacks do TaskController --
    def _on_controller_state(self, state: str) -> None:
        if self._closed:
            return
        with self._lock:
            task_id = self._active_task_id or ""
        self._emit("task.progress", {"task_id": task_id, "state": state})

    async def _confirm_callback(self, message: str) -> bool:
        request_id = f"cfm-{uuid.uuid4().hex[:12]}"
        fut: asyncio.Future[bool] = self._loop.create_future()
        with self._lock:
            task_id = self._active_task_id or ""
            self._pending_confirms[request_id] = (task_id, fut)
        self._emit(
            "task.need_confirm",
            {
                "task_id": task_id,
                "request_id": request_id,
                "message": str(message or ""),
            },
        )
        try:
            return bool(await fut)
        finally:
            with self._lock:
                self._pending_confirms.pop(request_id, None)

    async def _choose_callback(self, label: str, candidates: list[dict[str, Any]]) -> int | None:
        request_id = f"inp-{uuid.uuid4().hex[:12]}"
        fut: asyncio.Future[int | None] = self._loop.create_future()
        safe_candidates = [
            {
                "name": str(c.get("name") or ""),
                "publisher": str(c.get("publisher") or ""),
                "install_dir": str(c.get("install_dir") or c.get("target") or ""),
                "app_id": str(c.get("app_id") or ""),
                "source": str(c.get("source") or ""),
            }
            for c in candidates
            if isinstance(c, dict)
        ]
        with self._lock:
            task_id = self._active_task_id or ""
            self._pending_inputs[request_id] = (task_id, "choice", len(safe_candidates), fut)
        self._emit(
            "task.need_input",
            {
                "task_id": task_id,
                "request_id": request_id,
                "kind": "choice",
                "label": str(label or ""),
                "candidates": safe_candidates,
            },
        )
        try:
            return await fut
        finally:
            with self._lock:
                self._pending_inputs.pop(request_id, None)

    async def _indicate_callback(self, query: str) -> str:
        request_id = f"inp-{uuid.uuid4().hex[:12]}"
        fut: asyncio.Future[str] = self._loop.create_future()
        with self._lock:
            task_id = self._active_task_id or ""
            self._pending_inputs[request_id] = (task_id, "location", 0, fut)
        self._emit(
            "task.need_input",
            {
                "task_id": task_id,
                "request_id": request_id,
                "kind": "location",
                "query": str(query or ""),
                "timeout_seconds": LOCATION_TIMEOUT_SECONDS,
            },
        )
        try:
            return str(await asyncio.wait_for(fut, timeout=LOCATION_TIMEOUT_SECONDS))
        except TimeoutError:
            return ""
        finally:
            with self._lock:
                self._pending_inputs.pop(request_id, None)

    def _deny_all_pending_questions(self) -> None:
        with self._lock:
            confirms = list(self._pending_confirms.values())
            inputs = list(self._pending_inputs.values())
            self._pending_confirms.clear()
            self._pending_inputs.clear()

        def _resolve_in_loop() -> None:
            for _tid, cfut in confirms:
                if not cfut.done():
                    cfut.set_result(False)
            for _tid, kind, _count, ifut in inputs:
                if not ifut.done():
                    ifut.set_result(None if kind == "choice" else "")

        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(_resolve_in_loop)

    # --------------------------------------------------------------- fs.read (T5) --
    def _handle_fs_read(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Capacidade aditiva do DoD global8: leitura de arquivo próprio, com teto e sem UI.

        O comando é opcional: uma sessão que não o anuncie continua válida, e um cliente v1 que
        nunca o envie não muda de comportamento. O texto do arquivo é DADO do usuário; ele vai na
        resposta do comando (que não entra em `logs/tasks.jsonl`) e nunca é interpretado aqui.
        """
        # Import tardio: a ponte não carrega a superfície de ferramentas do worker (psutil/fastmcp
        # e os módulos de computer) só para servir um comando opcional.
        from app.computer.windows_tools import MAX_READ_BYTES, FileReadError, read_text_file

        raw_path = payload.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip() or len(raw_path) > 1024:
            raise protocol.ProtocolError("invalid_payload", "Campo 'path' deve ser um caminho válido.")
        raw_limit = payload.get("max_bytes")
        if raw_limit is None:
            limit = MAX_READ_BYTES
        elif type(raw_limit) is bool or not isinstance(raw_limit, int) or not 1 <= raw_limit <= MAX_READ_BYTES:
            raise protocol.ProtocolError(
                "invalid_payload",
                f"Campo 'max_bytes' deve ser inteiro entre 1 e {MAX_READ_BYTES}.",
            )
        else:
            limit = raw_limit
        try:
            return read_text_file(raw_path.strip(), limit_bytes=limit)
        except FileReadError as exc:
            # Motivo público e tipado: nunca um traceback, nunca o conteúdo do arquivo.
            raise protocol.ProtocolError("invalid_payload", str(exc)) from exc
        except ValueError as exc:
            raise protocol.ProtocolError("invalid_payload", str(exc)) from exc

    # ------------------------------------------------------------- dispatch geral --
    def dispatch(
        self,
        env: protocol.Envelope,
        _send: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        cmd = env.type
        payload = env.payload

        if cmd == "chat.send":
            return self._handle_chat_send(payload)
        if cmd == "task.cancel":
            return self._handle_task_cancel(payload)
        if cmd == "task.confirm":
            return self._handle_task_confirm(payload)
        if cmd == "task.answer":
            return self._handle_task_answer(payload)
        if cmd == "fs.read":
            return self._handle_fs_read(payload)
        if cmd == "approval.get":
            return self._handle_approval_get()
        if cmd == "approval.set":
            return self._handle_approval_set(payload)
        if cmd == "brain.list":
            return self._handle_brain_list(payload)
        if cmd == "brain.get":
            return self._handle_brain_get()
        if cmd == "brain.set":
            return self._handle_brain_set(payload)
        if cmd == "brain.verify":
            return self._handle_brain_verify(payload)
        if cmd == "brain.probe":
            return self._handle_brain_probe(payload)
        if cmd == "memory.list":
            return self._handle_memory_list(payload)
        if cmd == "memory.search":
            return self._handle_memory_search(payload)
        if cmd == "memory.upsert":
            return self._handle_memory_upsert(payload)
        if cmd == "memory.delete":
            return self._handle_memory_delete(payload)
        if cmd == "memory.proposals":
            return self._handle_memory_proposals()
        if cmd == "memory.propose":
            return self._handle_memory_propose(payload)
        if cmd == "memory.accept":
            return self._handle_memory_accept(payload)
        if cmd == "memory.reject":
            return self._handle_memory_reject(payload)
        if cmd == "memory.options.get":
            return self._handle_memory_options_get()
        if cmd == "memory.options.set":
            return self._handle_memory_options_set(payload)
        if cmd == "memory.export":
            return self._handle_memory_export(payload)
        if cmd == "memory.import":
            return self._handle_memory_import(payload)
        if cmd == "memory.wipe":
            return self._handle_memory_wipe(payload)
        if cmd == "ui.preferences.get":
            return self._handle_ui_preferences_get()
        if cmd == "ui.preferences.set":
            return self._handle_ui_preferences_set(payload)
        if cmd == "app.shutdown":
            self.close()
            return {"status": "shutting_down"}

        raise protocol.ProtocolError("unknown_command", f"Comando desconhecido: {cmd}", request_id=env.id)

    # --------------------------------------------------------- chat & tarefas (F2a) --
    def _is_task_running(self) -> bool:
        with self._lock:
            return self._active_task_id is not None

    def _handle_chat_send(self, payload: dict[str, Any]) -> dict[str, Any]:
        mode = payload.get("mode")
        if mode not in {"converse", "execute"}:
            raise protocol.ProtocolError(
                "invalid_payload",
                "Campo 'mode' obrigatório: use 'converse' ou 'execute'.",
            )
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise protocol.ProtocolError("invalid_payload", "Campo 'text' deve ser uma instrução não vazia.")
        stripped = text.strip()
        if len(stripped) > protocol.MAX_CHAT_TEXT_CHARS or len(stripped) > 4000:
            raise protocol.ProtocolError(
                "invalid_payload",
                "Escreva uma instrução com 1 a 4000 caracteres.",
            )

        attachments = protocol.validate_chat_attachments(payload.get("attachments"))
        if attachments and mode != "converse":
            raise protocol.ProtocolError(
                "invalid_payload",
                "Arquivos anexados são suportados no modo Conversar. "
                "Troque para Conversar ou remova o anexo para executar ações no Windows.",
            )

        with self._lock:
            if self._reconfiguring:
                raise protocol.ProtocolError(
                    "reconfiguring",
                    "Troca de cérebro em andamento; aguarde concluir antes de enviar.",
                )
            if self._is_task_running():
                raise protocol.ProtocolError(
                    "task_busy",
                    "Já existe uma tarefa em execução.",
                )
            task_id = f"tsk-{uuid.uuid4().hex[:16]}"
            self._active_task_id = task_id

            conversation = mode == "converse"
            user_msg: dict[str, Any] = {
                "role": "user",
                "text": stripped,
                "mode": mode,
                "task_id": task_id,
            }
            if attachments:
                user_msg["attachments"] = [
                    {"name": item["name"], "size_bytes": item["size_bytes"]}
                    for item in attachments
                ]
            self._emit("chat.message", user_msg)
            if conversation:
                self._emit(
                    "chat.message",
                    {
                        "role": "system",
                        "text": "Modo Conversar: nenhuma ação no Windows será executada nesta resposta.",
                        "task_id": task_id,
                    },
                )
            self._emit("task.started", {"task_id": task_id, "mode": mode, "instruction": stripped})

            user_name = self.prefs.profile.name
            fut = asyncio.run_coroutine_threadsafe(
                self._execute_task(
                    task_id,
                    stripped,
                    conversation=conversation,
                    user_name=user_name,
                    attachments=attachments,
                ),
                self._loop,
            )
            self._active_task_future = fut

        return {"task_id": task_id, "mode": mode, "status": "started"}

    async def _execute_task(
        self,
        task_id: str,
        instruction: str,
        *,
        conversation: bool,
        user_name: str = "",
        attachments: list[dict[str, Any]] | None = None,
    ) -> TaskResult:
        res: TaskResult | None = None
        err: Exception | None = None
        try:
            run_kwargs: dict[str, Any] = {"conversation": conversation}
            if user_name:
                run_kwargs["user_name"] = user_name
            if attachments:
                run_kwargs["attachments"] = attachments
            res = await self.controller.run(instruction, **run_kwargs)
            res = replace(res, task_id=task_id)
        except Exception as exc:
            err = exc
        finally:
            self._deny_all_pending_questions()
            with self._lock:
                if self._active_task_id == task_id:
                    self._active_task_id = None
                    self._active_task_future = None

        if err is not None or res is None:
            msg = "Falha interna ao executar tarefa."
            self._emit(
                "task.error",
                {"task_id": task_id, "code": type(err).__name__ if err else "Error", "message": msg},
            )
            fallback = TaskResult(
                task_id=task_id,
                status="FAILED",
                message=msg,
                failure_kind="INTERNAL_ERROR",
            )
            self._emit("task.finished", public_task_result_dto(fallback))
            return fallback

        dto = public_task_result_dto(res)
        if conversation and res.status == "SUCCESS":
            self._emit(
                "chat.message",
                {"role": "assistant", "text": res.message, "task_id": task_id, "scope": res.scope},
            )
        if res.status == "CANCELLED":
            self._emit("task.cancelled", {"task_id": task_id, "message": res.message, "result": dto})
        self._emit("task.finished", dto)
        if res.scope != "conversation":
            self._emit("memory.changed", {"reason": "task_finished"})
        return res

    def _handle_task_cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        req_task_id = payload.get("task_id")
        with self._lock:
            active_id = self._active_task_id
            if req_task_id is not None and active_id is not None and req_task_id != active_id:
                return {"cancelled": False, "reason": "task_mismatch", "active_task_id": active_id}
            if not self._is_task_running():
                return {"cancelled": False, "reason": "no_active_task"}
            self._deny_all_pending_questions()
            self._loop.call_soon_threadsafe(self.controller.cancel)
            return {"cancelled": True, "task_id": active_id or ""}

    def _handle_task_confirm(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("request_id")
        approved = payload.get("approved")
        if not isinstance(request_id, str) or not request_id.strip():
            raise protocol.ProtocolError("invalid_payload", "Campo 'request_id' obrigatório.")
        if type(approved) is not bool:
            raise protocol.ProtocolError("invalid_payload", "Campo 'approved' deve ser booleano.")

        req_task_id = payload.get("task_id")
        with self._lock:
            entry = self._pending_confirms.get(request_id)
            if entry is None:
                raise protocol.ProtocolError(
                    "invalid_request_id",
                    "Confirmação inexistente, expirada ou já respondida.",
                )
            owner_task_id, fut = entry
            if req_task_id is not None and owner_task_id and req_task_id != owner_task_id:
                raise protocol.ProtocolError(
                    "invalid_request_id",
                    "Confirmação pertence a outra tarefa.",
                )
            if fut.done():
                raise protocol.ProtocolError(
                    "invalid_request_id",
                    "Confirmação já foi decidida.",
                )
            self._pending_confirms.pop(request_id, None)

        self._loop.call_soon_threadsafe(lambda: fut.set_result(approved) if not fut.done() else None)
        return {"request_id": request_id, "approved": approved}

    def _handle_task_answer(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("request_id")
        kind = payload.get("kind")
        if not isinstance(request_id, str) or not request_id.strip():
            raise protocol.ProtocolError("invalid_payload", "Campo 'request_id' obrigatório.")
        if kind not in {"choice", "location"}:
            raise protocol.ProtocolError("invalid_payload", "Campo 'kind' deve ser 'choice' ou 'location'.")

        req_task_id = payload.get("task_id")
        with self._lock:
            entry = self._pending_inputs.get(request_id)
            if entry is None:
                raise protocol.ProtocolError(
                    "invalid_request_id",
                    "Pergunta inexistente, expirada ou já respondida.",
                )
            owner_task_id, expected_kind, candidate_count, fut = entry
            if req_task_id is not None and owner_task_id and req_task_id != owner_task_id:
                raise protocol.ProtocolError(
                    "invalid_request_id",
                    "Pergunta pertence a outra tarefa.",
                )
            if kind != expected_kind:
                raise protocol.ProtocolError(
                    "invalid_payload",
                    f"Tipo de resposta '{kind}' não corresponde ao esperado ('{expected_kind}').",
                )
            if fut.done():
                raise protocol.ProtocolError("invalid_request_id", "Pergunta já foi respondida.")

            if kind == "choice":
                index = payload.get("index")
                if index is not None:
                    if type(index) is not int or not (0 <= index < candidate_count):
                        raise protocol.ProtocolError(
                            "invalid_payload",
                            f"Índice de escolha fora do intervalo (0..{max(0, candidate_count - 1)}).",
                        )
                resolved_val: Any = index
            else:
                path_val = payload.get("path", "")
                if path_val is None:
                    path_val = ""
                if not isinstance(path_val, str) or len(path_val) > 1024:
                    raise protocol.ProtocolError("invalid_payload", "Caminho inválido para localização.")
                resolved_val = path_val.strip()

            self._pending_inputs.pop(request_id, None)

        self._loop.call_soon_threadsafe(lambda: fut.set_result(resolved_val) if not fut.done() else None)
        return {"request_id": request_id, "kind": kind, "accepted": True}

    # ---------------------------------------------------- aprovação & Brain (F2b) --
    def _handle_approval_get(self) -> dict[str, Any]:
        auto = bool(self.settings.auto_approve)
        return {
            "auto_approve": auto,
            "mode": "auto" if auto else "manual",
            "label": "Modo: Autoaprovação" if auto else "Modo: Manual",
        }

    def _handle_approval_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        enabled = payload.get("auto_approve")
        if type(enabled) is not bool:
            raise protocol.ProtocolError("invalid_payload", "Campo 'auto_approve' deve ser booleano.")
        self.settings = replace(self.settings, auto_approve=enabled)
        if self.sandbox_dir is not None:
            app_path = self.sandbox_dir / ".local" / "approval.json"
            app_path.parent.mkdir(parents=True, exist_ok=True)
            app_path.write_text(json.dumps({"auto_approve": enabled}), encoding="utf-8")
            self.controller.settings = replace(self.controller.settings, auto_approve=enabled)
        else:
            self.controller.set_auto_approve(enabled)
        return self._handle_approval_get()

    def _public_brain_state(self) -> dict[str, Any]:
        desc = registry.get(self.settings.provider)
        env_provider = os.getenv("LLM_PROVIDER", "").strip().lower()
        env_model = os.getenv("LLM_MODEL", "").strip()
        env_app_provider = os.getenv("LLM_APP_PROVIDER", "").strip().lower()
        env_app_model = os.getenv("LLM_APP_MODEL", "").strip()
        return {
            "provider": self.settings.provider,
            "provider_label": desc.label,
            "model": self.settings.model,
            "base_url": self.settings.base_url if desc.location is registry.BrainLocation.LOCAL_ENDPOINT else "",
            "has_api_key": bool(self.settings.api_key) if desc.requires_api_key else False,
            "app_provider": self.settings.app_provider,
            "app_model": self.settings.app_model,
            "effort": self.settings.effort,
            "env_overrides": {
                "provider": bool(env_provider),
                "model": bool(env_model),
                "app_provider": bool(env_app_provider),
                "app_model": bool(env_app_model),
            },
        }

    def _handle_brain_get(self) -> dict[str, Any]:
        providers_list: list[dict[str, Any]] = []
        for desc in registry.descriptors():
            providers_list.append(
                {
                    "id": desc.id,
                    "label": desc.label,
                    "kind": desc.kind.value,
                    "location": desc.location.value,
                    "requires_api_key": desc.requires_api_key,
                    "supports_model_discovery": desc.capabilities.supports(Capability.MODEL_DISCOVERY),
                    "supports_interactive_login": desc.capabilities.supports(Capability.INTERACTIVE_LOGIN),
                }
            )
        return {
            "current": self._public_brain_state(),
            "providers": providers_list,
        }

    def _candidate_settings_from_payload(self, payload: dict[str, Any]) -> Settings:
        provider = str(payload.get("provider") or self.settings.provider).strip().lower()
        try:
            desc = registry.get(provider)
        except (BrainError, KeyError) as exc:
            raise protocol.ProtocolError("invalid_payload", f"Provider desconhecido: {provider}.") from exc

        model = str(payload.get("model") if "model" in payload else self.settings.model).strip()
        raw_key = payload.get("api_key")
        if isinstance(raw_key, str) and raw_key.strip():
            api_key = raw_key.strip()
        elif provider == self.settings.provider:
            api_key = self.settings.api_key
        else:
            key_name = {
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
                "gemini": "GEMINI_API_KEY",
                "openrouter": "OPENROUTER_API_KEY",
                "nvidia": "NVIDIA_API_KEY",
            }.get(provider, "LLM_API_KEY")
            api_key = os.getenv(key_name, "")

        if not desc.requires_api_key or provider in CLI_PROVIDERS:
            api_key = ""

        base_url = str(payload.get("base_url") if "base_url" in payload else (
            self.settings.base_url if provider == self.settings.provider else ""
        )).strip()
        if desc.location is not registry.BrainLocation.LOCAL_ENDPOINT:
            base_url = ""

        app_provider = str(payload.get("app_provider") if "app_provider" in payload else self.settings.app_provider).strip().lower()
        app_model = str(payload.get("app_model") if "app_model" in payload else self.settings.app_model).strip()
        if not app_provider:
            app_model = ""

        return replace(
            self.settings,
            provider=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            app_provider=app_provider,
            app_model=app_model,
        )

    def _handle_brain_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = str(payload.get("provider") or self.settings.provider).strip().lower()
        force = bool(payload.get("force", False))
        role = str(payload.get("role") or "main").strip().lower()
        try:
            registry.get(provider)
        except (BrainError, KeyError) as exc:
            raise protocol.ProtocolError("invalid_payload", f"Provider desconhecido: {provider}.") from exc

        if role == "app":
            if provider == self.settings.provider:
                target = replace(self.settings, provider=provider)
            else:
                key_name = provider.upper() + "_API_KEY" if provider in API_KEY_PROVIDERS else "LLM_APP_API_KEY"
                target = replace(
                    self.settings,
                    provider=provider,
                    model="",
                    api_key=os.getenv(key_name, ""),
                    base_url=os.getenv("LLM_APP_BASE_URL", ""),
                )
        else:
            target = self._candidate_settings_from_payload({**payload, "provider": provider})

        try:
            target.validate(model=False)
        except ConfigurationError as exc:
            return {
                "provider": provider,
                "status": DiscoveryStatus.UNAVAILABLE.value,
                "ok": False,
                "detail": str(exc),
                "models": [],
            }

        fut = asyncio.run_coroutine_threadsafe(
            self.discovery.list(provider, settings=target, force=force),
            self._loop,
        )
        listing: ModelListing = fut.result(timeout=30.0)
        return self._listing_dto(listing, saved_model=target.model)

    @staticmethod
    def _listing_dto(listing: ModelListing, *, saved_model: str = "") -> dict[str, Any]:
        return {
            "provider": listing.provider,
            "label": listing.label,
            "status": listing.status.value,
            "ok": listing.ok,
            "detail": listing.detail,
            "selection_problem": listing.selection_problem(saved_model),
            "cached": listing.cached,
            "models": [
                {
                    "id": m.id,
                    "label": m.label or m.id,
                    "default": bool(m.default),
                }
                for m in listing.models
            ],
        }

    def _handle_brain_verify(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = self._candidate_settings_from_payload(payload)
        try:
            target.validate()
        except ConfigurationError as exc:
            raise protocol.ProtocolError("invalid_configuration", str(exc)) from exc

        async def _verify() -> dict[str, Any]:
            brain = resolve_brain(target)
            try:
                health = await brain.health()
                listing = await self.discovery.list(target.provider, settings=target, brain=brain, force=True)
                return {
                    "provider": target.provider,
                    "model": target.model,
                    "status": health.status.value,
                    "detail": health.detail,
                    "ok": health.status.value == "ready",
                    "listing": self._listing_dto(listing, saved_model=target.model),
                }
            finally:
                await brain.close()

        fut = asyncio.run_coroutine_threadsafe(_verify(), self._loop)
        return fut.result(timeout=45.0)

    def _handle_brain_probe(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("consent") is not True:
            raise protocol.ProtocolError(
                "consent_required",
                "O teste de modelo consome uma chamada real de inferência; confirme consent=true.",
            )
        target = self._candidate_settings_from_payload(payload)
        try:
            target.validate()
        except ConfigurationError as exc:
            raise protocol.ProtocolError("invalid_configuration", str(exc)) from exc

        fut = asyncio.run_coroutine_threadsafe(
            probe_settings(target, timeout=min(PROBE_TIMEOUT_SECONDS, target.timeout_seconds)),
            self._loop,
        )
        res = fut.result(timeout=PROBE_TIMEOUT_SECONDS + 5.0)
        return {
            "provider": res.provider,
            "model": res.model,
            "ok": res.ok,
            "latency_ms": round(float(res.latency_ms or 0.0), 2),
            "summary": res.summary(),
            "error_kind": res.error_kind.value if res.error_kind else None,
            "input_tokens": res.input_tokens,
            "output_tokens": res.output_tokens,
        }

    def _handle_brain_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._is_task_running():
                raise protocol.ProtocolError(
                    "task_busy",
                    "Há uma tarefa em voo. Pare a tarefa antes de aplicar outro cérebro.",
                )
            if self._reconfiguring:
                raise protocol.ProtocolError("reconfiguring", "Já existe uma reconfiguração em andamento.")
            self._reconfiguring = True

        try:
            # Respeita precedência de variáveis de ambiente quando presentes
            target = self._candidate_settings_from_payload(payload)
            env_provider = os.getenv("LLM_PROVIDER", "").strip().lower()
            env_model = os.getenv("LLM_MODEL", "").strip()
            env_app_provider = os.getenv("LLM_APP_PROVIDER", "").strip().lower()
            env_app_model = os.getenv("LLM_APP_MODEL", "").strip()
            if env_provider:
                target = replace(target, provider=env_provider)
            if env_model:
                target = replace(target, model=env_model)
            if env_app_provider:
                target = replace(target, app_provider=env_app_provider)
            if env_app_model:
                target = replace(target, app_model=env_app_model)

            target.validate()
            remember = payload.get("remember_credentials") is True

            fut = asyncio.run_coroutine_threadsafe(self.controller.reconfigure(target), self._loop)
            fut.result(timeout=30.0)
            self.settings = target
            self._sync_engine_host_pids()

            self._persist_brain_selection(target, remember=remember)
            state = self._public_brain_state()
            self._emit("brain.updated", state)
            return state
        except ConfigurationError as exc:
            raise protocol.ProtocolError("invalid_configuration", str(exc)) from exc
        finally:
            with self._lock:
                self._reconfiguring = False

    def _persist_brain_selection(self, target: Settings, *, remember: bool) -> None:
        if self.sandbox_dir is not None:
            local_dir = self.sandbox_dir / ".local"
            local_dir.mkdir(parents=True, exist_ok=True)
            (local_dir / "brain-selection.json").write_text(
                json.dumps({"provider": target.provider, "model": target.model}, ensure_ascii=False),
                encoding="utf-8",
            )
            app_file = local_dir / "app-brain.json"
            if target.app_provider:
                app_file.write_text(
                    json.dumps({"provider": target.app_provider, "model": target.app_model}, ensure_ascii=False),
                    encoding="utf-8",
                )
            elif app_file.exists():
                app_file.unlink()
            if remember:
                # Em sandbox, grava indicador protegido via DPAPI dentro do próprio sandbox
                dpapi_file = local_dir / "provider.dpapi"
                raw = json.dumps(
                    {
                        "provider": target.provider,
                        "model": target.model,
                        "api_key": "" if target.provider in CLI_PROVIDERS else target.api_key,
                        "base_url": "" if target.provider in CLI_PROVIDERS else target.base_url,
                    }
                ).encode("utf-8")
                dpapi_file.write_bytes(settings_module._protect(raw))
        else:
            settings_module.save_selection(target.provider, target.model)
            settings_module.save_app_selection(target.app_provider, target.app_model)
            if remember:
                settings_module.save_provider(
                    target.provider,
                    target.model,
                    target.api_key,
                    target.base_url,
                )

    # -------------------------------------------------------------- memória (F2b) --
    def _handle_memory_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query") or "").strip()
        limit = payload.get("limit", 100)
        respect_enabled = bool(payload.get("respect_enabled", False))
        if type(limit) is not int or not (1 <= limit <= 100):
            raise protocol.ProtocolError("invalid_payload", "Limite deve ser inteiro entre 1 e 100.")
        flags = self.memory.options()
        if respect_enabled and not flags.get("enabled"):
            return {"enabled": False, "items": []}
        try:
            items = self.memory.list(query=query, limit=limit)
        except MemoryPolicyError as exc:
            raise protocol.ProtocolError("memory_policy_error", str(exc)) from exc
        return {
            "enabled": bool(flags.get("enabled")),
            "items": [public_memory_item_dto(item) for item in items],
        }

    def _handle_memory_search(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query") or "").strip()
        limit = payload.get("limit", 100)
        raw_kinds = payload.get("kinds")
        kinds: tuple[str, ...] | None = None
        if raw_kinds is not None:
            if not isinstance(raw_kinds, list) or not all(isinstance(k, str) for k in raw_kinds):
                raise protocol.ProtocolError("invalid_payload", "Campo 'kinds' inválido.")
            kinds = tuple(raw_kinds)
        try:
            items = self.memory.recall(query, kinds=kinds, limit=limit)
        except MemoryPolicyError as exc:
            raise protocol.ProtocolError("memory_policy_error", str(exc)) from exc
        return {
            "enabled": bool(self.memory.options().get("enabled")),
            "items": [public_memory_item_dto(item) for item in items],
        }

    def _handle_memory_upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        # "Novos perfis não contornam a proposta/aceite": memory.upsert altera apenas item ativo existente.
        item_id = payload.get("id")
        text = payload.get("text")
        if not isinstance(item_id, str) or not item_id.strip():
            raise protocol.ProtocolError(
                "invalid_payload",
                "Campo 'id' obrigatório para atualizar memória existente; novos itens devem usar memory.propose.",
            )
        if not isinstance(text, str) or not text.strip():
            raise protocol.ProtocolError("invalid_payload", "O texto não pode ficar vazio.")
        if len(text) > protocol.MAX_MEMORY_TEXT_CHARS:
            raise protocol.ProtocolError(
                "memory_policy_error",
                f"Texto excede o limite de {protocol.MAX_MEMORY_TEXT_CHARS} caracteres.",
            )
        try:
            updated = self.memory.update(item_id.strip(), text)
        except KeyError:
            raise protocol.ProtocolError("not_found", "Memória não encontrada.") from None
        except MemoryPolicyError as exc:
            raise protocol.ProtocolError("memory_policy_error", str(exc)) from None
        self._emit("memory.changed", {"reason": "updated", "id": updated.id})
        return {"item": public_memory_item_dto(updated)}

    def _handle_memory_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        item_id = payload.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise protocol.ProtocolError("invalid_payload", "Campo 'id' obrigatório.")
        if payload.get("confirmed") is not True:
            raise protocol.ProtocolError("confirmation_required", "Confirmação explícita necessária para apagar.")
        try:
            self.memory.delete(item_id.strip())
        except KeyError:
            raise protocol.ProtocolError("not_found", "Memória não encontrada.") from None
        except MemoryPolicyError as exc:
            raise protocol.ProtocolError("memory_policy_error", str(exc)) from None
        self._emit("memory.changed", {"reason": "deleted", "id": item_id.strip()})
        return {"deleted": True, "id": item_id.strip()}

    def _handle_memory_proposals(self) -> dict[str, Any]:
        items = self.memory.proposals()
        return {"proposals": [public_memory_item_dto(item) for item in items]}

    def _handle_memory_propose(self, payload: dict[str, Any]) -> dict[str, Any]:
        kind = payload.get("kind")
        text = payload.get("text")
        if not isinstance(kind, str) or kind not in PROFILE_KINDS:
            raise protocol.ProtocolError(
                "invalid_payload",
                f"Tipo de proposta inválido. Use um de: {', '.join(PROFILE_KINDS)}.",
            )
        if not isinstance(text, str) or not text.strip():
            raise protocol.ProtocolError("invalid_payload", "Escreva o texto da proposta.")
        if len(text) > protocol.MAX_MEMORY_TEXT_CHARS:
            raise protocol.ProtocolError(
                "memory_policy_error",
                f"Texto excede o limite de {protocol.MAX_MEMORY_TEXT_CHARS} caracteres.",
            )
        try:
            prop_id = self.memory.propose(MemoryItem(kind=kind, text=text.strip(), source="user_explicit"))
        except MemoryPolicyError as exc:
            raise protocol.ProtocolError("memory_policy_error", str(exc)) from None
        self._emit("memory.changed", {"reason": "proposed", "id": prop_id})
        return {"proposal_id": prop_id}

    def _handle_memory_accept(self, payload: dict[str, Any]) -> dict[str, Any]:
        prop_id = payload.get("id")
        if not isinstance(prop_id, str) or not prop_id.strip():
            raise protocol.ProtocolError("invalid_payload", "Campo 'id' obrigatório.")
        try:
            memory_id = self.memory.accept_proposal(prop_id.strip())
        except KeyError:
            raise protocol.ProtocolError("not_found", "Proposta pendente não encontrada.") from None
        except MemoryPolicyError as exc:
            raise protocol.ProtocolError("memory_policy_error", str(exc)) from None
        self._emit("memory.changed", {"reason": "accepted", "id": memory_id})
        return {"memory_id": memory_id}

    def _handle_memory_reject(self, payload: dict[str, Any]) -> dict[str, Any]:
        prop_id = payload.get("id")
        if not isinstance(prop_id, str) or not prop_id.strip():
            raise protocol.ProtocolError("invalid_payload", "Campo 'id' obrigatório.")
        try:
            self.memory.reject_proposal(prop_id.strip())
        except MemoryPolicyError as exc:
            raise protocol.ProtocolError("memory_policy_error", str(exc)) from None
        self._emit("memory.changed", {"reason": "rejected", "id": prop_id.strip()})
        return {"rejected": True, "id": prop_id.strip()}

    def _handle_memory_options_get(self) -> dict[str, Any]:
        return {"options": self.memory.options()}

    def _handle_memory_options_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        options = payload.get("options")
        if not isinstance(options, dict) or not options:
            raise protocol.ProtocolError("invalid_payload", "Campo 'options' deve ser um objeto não vazio.")
        if any(type(v) is not bool for v in options.values()):
            raise protocol.ProtocolError("invalid_payload", "Valores de 'options' devem ser booleanos.")
        try:
            self.memory.set_options(**options)
        except MemoryPolicyError as exc:
            raise protocol.ProtocolError("memory_policy_error", str(exc)) from None
        updated = self.memory.options()
        self._emit("memory.changed", {"reason": "options", "options": updated})
        return {"options": updated}

    def _handle_memory_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Camada confiável (Electron main) injeta `_trusted_export_path` após file dialog nativo,
        # ou solicita `data` para gravar no path escolhido pelo diálogo nativo do main.
        trusted_path = payload.get("_trusted_export_path")
        exported = self.memory.export()
        if isinstance(trusted_path, str) and trusted_path.strip():
            target = Path(trusted_path)
            text = json.dumps(exported, ensure_ascii=False, indent=2)
            target.write_text(text, encoding="utf-8")
            return {"exported": True, "characters": len(text)}
        return {"exported": True, "data": exported}

    def _handle_memory_import(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise protocol.ProtocolError("confirmation_required", "Confirmação explícita necessária para importar.")
        trusted_path = payload.get("_trusted_import_path")
        import_data = payload.get("data")
        if isinstance(trusted_path, str) and trusted_path.strip():
            target = Path(trusted_path)
            try:
                size = target.stat().st_size
            except OSError:
                raise protocol.ProtocolError("io_error", "Não foi possível ler as informações do arquivo.") from None
            if size > protocol.MAX_IMPORT_BYTES:
                raise protocol.ProtocolError(
                    "import_too_large",
                    f"O arquivo excede o limite de importação de {protocol.MAX_IMPORT_BYTES // (1024 * 1024)} MiB.",
                )
            try:
                import_data = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise protocol.ProtocolError("invalid_import", "Arquivo JSON de importação inválido.") from None

        if not isinstance(import_data, dict):
            raise protocol.ProtocolError("invalid_import", "Dados de importação devem ser um objeto JSON.")
        try:
            raw_bytes = len(json.dumps(import_data, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (ValueError, TypeError):
            raise protocol.ProtocolError("invalid_import", "Dados de importação inválidos.") from None
        if raw_bytes > protocol.MAX_IMPORT_BYTES:
            raise protocol.ProtocolError(
                "import_too_large",
                f"O payload excede o limite de importação de {protocol.MAX_IMPORT_BYTES // (1024 * 1024)} MiB.",
            )
        try:
            inserted = self.memory.import_data(import_data)
        except MemoryPolicyError as exc:
            raise protocol.ProtocolError("memory_policy_error", str(exc)) from None
        self._emit("memory.changed", {"reason": "imported", "inserted": inserted})
        return {"imported": True, "inserted": inserted}

    def _handle_memory_wipe(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise protocol.ProtocolError("confirmation_required", "Confirmação explícita necessária para limpar tudo.")
        self.memory.wipe()
        options = self.memory.options()
        self._emit("memory.changed", {"reason": "wiped", "options": options})
        return {"wiped": True, "options": options}

    # ---------------------------------------------------- preferências UI (F2b) --
    def _prefs_dto(self) -> dict[str, Any]:
        return {
            "theme": self.prefs.theme,
            "window": list(self.prefs.window) if self.prefs.window else None,
            "scale_pct": self.prefs.scale_pct,
            "actions": [{"label": a.label, "draft": a.draft} for a in self.prefs.actions],
            "profile": {
                "name": self.prefs.profile.name,
                "avatar_data_url": self.prefs.profile.avatar_data_url,
            },
        }

    def _handle_ui_preferences_get(self) -> dict[str, Any]:
        self.prefs = ui_preferences.load(self.prefs_path)
        return self._prefs_dto()

    def _handle_ui_preferences_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "path" in payload or "file" in payload:
            raise protocol.ProtocolError(
                "invalid_payload",
                "Renderer não pode definir caminho de arquivo em ui.preferences.set.",
            )

        current_profile = self.prefs.profile
        if "profile" in payload:
            raw_profile = payload.get("profile")
            if raw_profile is None or not isinstance(raw_profile, dict):
                raise protocol.ProtocolError(
                    "invalid_payload",
                    "Campo 'profile' deve ser um objeto com 'name' e/ou 'avatar_data_url'.",
                )
            if "path" in raw_profile or "file" in raw_profile:
                raise protocol.ProtocolError(
                    "invalid_payload",
                    "Perfil não pode informar caminho de arquivo ('path'/'file').",
                )
            unknown_keys = set(raw_profile) - {"name", "avatar_data_url"}
            if unknown_keys:
                raise protocol.ProtocolError(
                    "invalid_payload",
                    f"Campos desconhecidos em 'profile': {', '.join(sorted(unknown_keys))}.",
                )
            try:
                next_name = (
                    ui_preferences.validate_profile_name(raw_profile.get("name"), strict=True)
                    if "name" in raw_profile
                    else current_profile.name
                )
                next_avatar = (
                    ui_preferences.validate_avatar_data_url(raw_profile.get("avatar_data_url"), strict=True)
                    if "avatar_data_url" in raw_profile
                    else current_profile.avatar_data_url
                )
            except ValueError as exc:
                raise protocol.ProtocolError("invalid_payload", str(exc)) from exc
            next_profile = ui_preferences.UserProfile(name=next_name, avatar_data_url=next_avatar)
        else:
            next_profile = current_profile

        merged = {
            "theme": payload.get("theme", self.prefs.theme),
            "window": payload.get("window", list(self.prefs.window) if self.prefs.window else None),
            "scale_pct": payload.get("scale_pct", self.prefs.scale_pct),
            "actions": payload.get(
                "actions",
                [{"label": a.label, "draft": a.draft} for a in self.prefs.actions],
            ),
            "profile": {
                "name": next_profile.name,
                "avatar_data_url": next_profile.avatar_data_url,
            },
        }
        parsed = ui_preferences.parse(merged)
        parsed = replace(parsed, profile=next_profile)
        saved = ui_preferences.save(self.prefs_path, parsed)
        if not saved:
            raise protocol.ProtocolError("io_error", "Não foi possível gravar as preferências de tela.")
        self.prefs = parsed
        return self._prefs_dto()

    # ----------------------------------------------------------- encerramento --
    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._deny_all_pending_questions()
        if not self._loop.is_closed():
            try:
                fut = asyncio.run_coroutine_threadsafe(self.controller.close(), self._loop)
                fut.result(timeout=10.0)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=3.0)

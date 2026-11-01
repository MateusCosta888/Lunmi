"""Public shapes the bridge sends to the UI and the trusted host-pid filter (moved out of service.py, 01/11)."""
from __future__ import annotations

from app.core.result import TaskResult
from app.memory import MemoryItem
from typing import Any
import os
import psutil


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
        "first_provider_event_ms": result.first_provider_event_ms,
        "first_provider_text_ms": result.first_provider_text_ms,
        "first_public_text_ms": result.first_public_text_ms,
        "operation_states": result.operation_states,
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
        "token_usage": result.token_usage,
        "provider_calls": result.provider_calls,
        "provider_startup_ms": result.provider_startup_ms,
        "provider_wait_ms": result.provider_wait_ms,
        **({"operation_counts": {
            "total": len(result.operation_states),
            "read_only": sum(op.get("category") == "read_only" for op in result.operation_states),
            "mutation": sum(op.get("category") == "mutation" for op in result.operation_states),
            "verified_mutations": sum(op.get("category") == "mutation" and op.get("verified") is True
                                      for op in result.operation_states),
            "uncertain": sum(op.get("state") == "uncertain" for op in result.operation_states),
        }} if all(op.get("category") in {"read_only", "mutation"}
                  for op in result.operation_states) else {}),
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
        # WEB-SEARCH-V1: sempre presentes; vazios/None fora de um turno web.
        "web_sources": [dict(item) for item in (result.web_sources or [])
                        if isinstance(item, dict)],
        "web_outcome": result.web_outcome,
        # WEB-SEARCH-ORIGIN-V1: proveniência e evidência simples, sem query/conteúdo.
        "search_source": result.search_source,
        "provider_id": result.provider_id or None,
        "native_tool_names": [str(name) for name in (result.native_tool_names or [])
                              if isinstance(name, str) and name],
        "source_count": len(result.web_sources or []),
        "search_outcome": result.web_outcome,
        "web_preference": result.web_preference,
        "web_fallback": result.web_fallback,
        # WEB-SEARCH-AUTO-V1: desfechos por tentativa. A tentativa anterior nunca se perde
        # quando a recuperação `auto` substitui a resposta; `fallback_triggered` só é
        # verdadeiro quando a segunda tentativa do plano começou.
        "fallback_triggered": bool(result.fallback_triggered),
        "provider_outcome": result.provider_outcome,
        "lunmi_outcome": result.lunmi_outcome,
        "web_provider_failure": result.web_provider_failure,
        "evidence_status": result.evidence_status,
        "request_status": result.request_status,
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

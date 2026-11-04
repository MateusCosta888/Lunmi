"""What a task run returns to the bridge and the UI (moved out of controller.py, 01/11)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import uuid


@dataclass
class TaskResult:
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "FAILED"
    message: str = ""
    provider: str = ""
    model: str = ""
    duration_ms: float = 0
    llm_time_ms: float = 0
    planning_time_ms: float = 0
    observation_time_ms: float = 0
    execution_time_ms: float = 0
    verification_time_ms: float = 0
    startup_time_ms: float = 0
    llm_calls: int = 0
    actions: int = 0
    actions_executed: int = 0  # successful mutation/navigation tool returns; excludes verification reads
    controls_inspected: int = 0  # collected control records, including both views and repeats
    controls_sent_to_brain: int = 0  # summed AppAgent controls across inference rounds
    steps: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    average_ram_mb: float | None = None
    peak_ram_mb: float | None = None
    cpu_seconds: float | None = None
    cpu_percent: float | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    scope: str = "end_to_end"
    # FASE 1 instrumentation: where the milliseconds and the tokens actually went.
    cold_start: bool = True            # this task started the UFO worker itself
    brain_resolve_ms: float = 0
    prompt_chars: int = 0              # characters handed to the Brain, all calls
    prompt_calls: list[dict[str, Any]] = field(default_factory=list)  # per call: agent/chars/ms/tokens
    # For CLI brains only, and a subset of llm_time_ms: time creating the process vs
    # everything the CLI did afterwards (its own init + model). Never added to a total.
    provider_startup_ms: float | None = None
    provider_wait_ms: float | None = None
    provider_calls: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=lambda: {
        "status": "unavailable", "calls_total": 0, "calls_with_input": 0,
        "calls_with_output": 0, "input_reported": None, "output_reported": None,
    })
    # FASE 4: CLI calls that reused the task-scoped session instead of spawning a process
    # of their own. A subset of llm_calls, never an extra phase.
    session_reused: int = 0
    # AppAgent UI controls offered in the prompt (0 when no AppAgent ran). No filtering
    # stage exists yet, so ``sent`` equals ``collected`` by construction rather than by
    # measurement; the two only diverge once FASE 5 adds filtering.
    uia_controls_collected: int = 0
    uia_controls_sent: int = 0
    uia_controls_omitted: int = 0
    prompt_budget: dict[str, Any] = field(default_factory=dict)
    # FASE 2.1 measurement: time inside UIA collection (walk + reading the window's
    # elements), how many collections ran, and how many requests were answered from the
    # batch-scoped snapshot instead of rewalking. A subset of observation/execution time,
    # never an extra phase.
    uia_collect_ms: float = 0
    uia_collect_calls: int = 0
    uia_collect_reused: int = 0
    # FASE 3 measurement: how much work one LLM round delegated (multi-action), what the
    # one-off MCP context discovery cost, and what creating an AppAgent per ASSIGN cost.
    # Subsets of the phase totals above, never extra phases to be added to a total.
    batches: int = 0                   # rounds that dispatched at least one command
    multi_action_batches: int = 0      # rounds that dispatched more than one action
    max_batch_actions: int = 0         # largest number of actions in a single round
    actions_per_llm_call: float = 0    # executed actions / actual inference attempts
    context_provision_ms: float = 0    # host MCP context discovery, first round only
    agent_create_ms: float = 0         # total time creating AppAgents for ASSIGN
    app_agents: int = 0                # AppAgents created for this task
    # Latency plan P2: the fast brain that drove the AppAgent, when one was
    # configured (`app_provider`); empty means the main brain drove both roles.
    app_provider: str = ""
    app_model: str = ""
    # Latency plan P4: rounds 2+ carry the short policy reminder instead of the full
    # contract. A subset of `llm_calls`, never an extra phase.
    short_policy_rounds: int = 0
    # FASE 3: the round stopped because the same action kept failing with no progress,
    # and the time the `wait` shadow did not spend sleeping. Both are measurements of
    # the round, not extra phases.
    no_progress: bool = False
    shadow_wait_saved_ms: float = 0
    # FASE 5: rounds that went back to the complete control list after a failure, because
    # the narrowed list did not carry the action.
    uia_controls_escalated: int = 0
    # FASE 4: time the launcher did not spend waiting for a window that could not come,
    # because the process it started had already exited in error. Also a subset of the
    # launch wait, not a phase.
    launch_failfast_saved_ms: float = 0
    # FASE 6: time a state-directed verification did not spend waiting for a state that had
    # already stopped changing. A subset of the 3 s deadline that used to be constant, not
    # a phase; `verify_window`'s re-read never had a deadline and never counts here.
    verify_failfast_saved_ms: float = 0
    # Readiness polling inside the deterministic tools. A SUBSET of execution_time_ms /
    # observation_time_ms, never an extra phase: it exists to answer "how long did we
    # wait for a window to appear", not to be added to a total.
    waits_ms: float = 0
    failure_kind: str = ""
    decision_error: str = ""
    false_success: bool = False        # SUCCESS reported with no evidence attached
    # FASE 8: how this task was planned — "" for engine-only/test runs, "deterministic"
    # when a table resolved it and not one inference happened, "llm" for the agent path,
    # "deterministic+llm" when the table resolved it, declined, and the agent took over.
    plan_source: str = ""
    fast_path_label: str = ""
    # Finding 6: a declined deterministic plan hands the original request to the
    # agent with a sanitized cause, the effects already verified and the pending goal.
    fast_path_declined: dict[str, Any] = field(default_factory=dict)
    first_provider_event_ms: float | None = None
    first_provider_text_ms: float | None = None
    first_public_text_ms: float | None = None
    operation_states: list[dict[str, Any]] = field(default_factory=list)
    # The authorisation mode this task ran under, and every risk decision the runtime made
    # (tool, level, reason, whether the question was asked). An approval is not evidence
    # that anything happened: this is a record of the policy, not of the result.
    approval_mode: str = ""
    approvals: list[dict[str, Any]] = field(default_factory=list)
    # F-AL7: the application resolution, measured separately from the launch it triggers.
    # `app_index_hit` is the question "did the machine's index answer, or the curated
    # table?"; `app_resolve_ms` the name-to-entry cost (milliseconds, not estimates);
    # `app_launch_kind` is `exe`/`lnk`/`appid`/`folder`, or `focus` when the application
    # was already open; `app_index_cold` says whether this session built the index.
    app_index_hit: bool = False
    app_source: str = ""
    app_intent_ms: float = 0
    app_resolve_ms: float = 0
    app_search_ms: float = 0
    app_candidates: int = 0
    app_launch_kind: str = ""
    app_index_cold: bool = False
    recall_ms: float = 0
    memory_context_chars: int = 0
    memory_context_items: int = 0
    prompt_base_chars: int = 0
    memory_error: str = ""
    # Interno: o modelo recebeu contexto sensível. Decide o filtro do arquivo; nunca sai no DTO.
    private_context: bool = False
    # Interno: problema da evidência enviada à síntese, avaliada com o pedido original.
    web_delivered_problem: str = ""
    # WEB-SEARCH-V1: fontes públicas do turno web (vazio fora dele) e desfecho da consulta.
    # Nunca é evidência de uma ação no desktop.
    web_sources: list[dict[str, Any]] = field(default_factory=list)
    web_outcome: str | None = None
    # WEB-SEARCH-ORIGIN-V1: a origem é congelada no início da Task. `search_source`
    # descreve a consulta efetiva; `web_preference` é a escolha guardada, que pode ter
    # sofrido fallback antes de começar. `provider_id` só é preenchido em modo nativo.
    search_source: str = "none"
    provider_id: str = ""
    native_tool_names: list[str] = field(default_factory=list)
    web_preference: str = "auto"
    web_fallback: str = ""
    # WEB-SEARCH-AUTO-V1: rasto honesto das tentativas. `provider_outcome`/
    # `lunmi_outcome` descrevem cada tentativa separadamente e não se apagam quando a
    # recuperação substitui a resposta. `fallback_triggered` só é verdadeiro quando a
    # segunda tentativa do plano `auto` começou de facto.
    provider_outcome: str | None = None
    lunmi_outcome: str | None = None
    web_provider_failure: str = ""
    fallback_triggered: bool = False
    # Snapshot interno da profundidade web desta Task (nunca publicado no fio).
    web_depth: str = ""
    evidence_status: str | None = None
    request_status: str | None = None
    instruction: str = field(default="", repr=False)

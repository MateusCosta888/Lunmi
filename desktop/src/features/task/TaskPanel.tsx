/**
 * Painel de estado/resultado da tarefa (`desktop/src/features/task/TaskPanel.tsx`, F2a).
 *
 * Mostra o veredito e as métricas que o `TaskResult` já publica — nada é estimado pelo renderer.
 * B2: métrica ausente do DTO não aparece; `null` medido diz "não reportado"; zero real mostra 0.
 * Tokens usam a proveniência de `token_usage` (complete/partial/unavailable); chamadas de
 * transporte por papel ficam em disclosure própria com denominadores coerentes; a fração
 * `actions_executed/actions` saiu do painel (conjuntos diferentes).
 * Um resultado com `false_success` ou `failure_kind` preenchido é apresentado como falha explícita.
 * SUCCESS descreve o desfecho do pedido; por si só não prova a verificação de uma ação.
 */
import React from "react";
import type { TaskResultDto } from "../../shared/types";
import type { ChatTaskState } from "../chat/useChatTask";
import "./task.css";

export interface TaskPanelProps {
  chat: ChatTaskState;
}

const PHASE_LABEL: Record<string, string> = {
  running: "A executar…",
  awaiting_confirm: "A aguardar confirmação",
  awaiting_input: "A aguardar resposta",
  finished: "Resultado não recebido",
  failed: "Falhou",
  cancelled: "Cancelada",
};

function formatMs(value: number): string {
  if (value >= 1000) {
    return `${(value / 1000).toFixed(2)} s`;
  }
  return `${value.toFixed(0)} ms`;
}

/** Métrica ausente do DTO (peer antigo) ≠ null medido como não reportado ≠ 0 real. */
function fmtMetric(value: number | null | undefined): string | null {
  if (value === undefined) return null;
  if (value === null) return "não reportado";
  return formatMs(value);
}
function fmtCount(value: number | null | undefined): string | null {
  if (value === undefined) return null;
  if (value === null) return "não reportado";
  return String(value);
}
function fmtUnknown(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : String(value);
}

function usageRow(result: TaskResultDto): [string, string] | null {
  const usage = result.token_usage;
  if (usage) {
    if (usage.status === "unavailable") return ["Tokens", "não reportado"];
    if (usage.status === "complete") {
      return ["Tokens", `entrada ${fmtUnknown(usage.input_reported)} · saída ${fmtUnknown(usage.output_reported)} (todas as ${usage.calls_total} chamadas reportaram)`];
    }
    return ["Tokens", `parcial: entrada ${fmtUnknown(usage.input_reported)} (${usage.calls_with_input}/${usage.calls_total} chamadas) · saída ${fmtUnknown(usage.output_reported)} (${usage.calls_with_output}/${usage.calls_total})`];
  }
  if (result.total_tokens !== undefined || result.input_tokens !== undefined || result.output_tokens !== undefined) {
    const none = (v: number | null | undefined) => v === null || v === undefined ? "não reportado" : String(v);
    return ["Tokens", `entrada ${none(result.input_tokens)} · saída ${none(result.output_tokens)} · total ${none(result.total_tokens)}`];
  }
  return null;
}

export function verdictOf(result: TaskResultDto): "success" | "failure" | "cancelled" {
  if (result.status === "CANCELLED") {
    return "cancelled";
  }
  const failed =
    result.status !== "SUCCESS" ||
    Boolean(result.false_success) ||
    (typeof result.failure_kind === "string" && result.failure_kind.length > 0);
  return failed ? "failure" : "success";
}

function metricRows(result: TaskResultDto): Array<[string, string]> {
  const rows: Array<[string, string]> = [];
  const push = (label: string, value: string | null) => { if (value !== null) rows.push([label, value]); };
  push("Duração total", fmtMetric(result.duration_ms));
  push("Inferência (LLM)", fmtMetric(result.llm_time_ms));
  push("Planeamento", fmtMetric(result.planning_time_ms));
  push("Observação", fmtMetric(result.observation_time_ms));
  push("Execução", fmtMetric(result.execution_time_ms));
  push("Verificação", fmtMetric(result.verification_time_ms));
  push("Arranque", fmtMetric(result.startup_time_ms));
  push("Espera", fmtMetric(result.waits_ms));
  push("Consulta da memória", fmtMetric(result.recall_ms));
  push("Arranque do provedor", fmtMetric(result.provider_startup_ms));
  push("Espera do provedor", fmtMetric(result.provider_wait_ms));
  push("Primeiro evento do provedor", fmtMetric(result.first_provider_event_ms));
  push("Primeiro texto do provedor", fmtMetric(result.first_provider_text_ms));
  push("Primeiro texto público", fmtMetric(result.first_public_text_ms));
  push("Passos", fmtCount(result.steps));
  push("Chamadas ao modelo", fmtCount(result.llm_calls));
  // `actions`/`actions_executed` contavam conjuntos diferentes; a fração foi removida do
  // painel. `operation_counts` é o conjunto coerente quando o backend o reporta.
  if (result.operation_counts) {
    const counts = result.operation_counts;
    rows.push(["Operações", `total ${counts.total} · leitura ${counts.read_only} · mutação ${counts.mutation} · verificadas ${counts.verified_mutations} · incertas ${counts.uncertain}`]);
  }
  push("Evidências verificadas",
    result.evidence_count === undefined ? null : `${fmtUnknown(result.evidence_verified_count)}/${result.evidence_count}`);
  const usage = usageRow(result);
  if (usage) rows.push(usage);
  push("Confirmações",
    result.approvals_total === undefined ? null : `${fmtUnknown(result.approvals_asked)}/${result.approvals_total}`);
  push("Itens de memória", fmtCount(result.memory_context_items));
  return rows;
}

export function TaskPanel({ chat }: TaskPanelProps): React.ReactElement {
  const task = chat.task;
  const result = task?.result ?? null;

  if (!task) {
    return <aside className="lunmi-task-panel" aria-label="Estado da tarefa" data-testid="task-panel" data-phase="idle">
      <h2>Resultado da tarefa</h2>
      <p className="lunmi-task-empty">Sem tarefas nesta conversa.</p>
    </aside>;
  }

  const verdict = result ? verdictOf(result) : null;
  const conversation = result?.scope === "conversation" || (!result?.scope && task.mode === "converse");
  const memoryCommand = result?.scope === "memory_command";
  const windowsAction = result?.scope === "end_to_end" || result?.scope === "engine_only" ||
    (!result?.scope && task.mode === "execute");
  const scopeLabel = conversation ? "Conversa" : memoryCommand ? "Memória" : windowsAction ? "Ação no Windows" : "Pedido";
  const verdictLabel = verdict === "success"
    ? conversation ? "Conversa concluída" : memoryCommand ? "Pedido de memória concluído" : "Pedido concluído"
    : verdict === "cancelled" ? "Pedido cancelado"
      : verdict === "failure" ? result?.status === "NOT_VERIFIED" ? "Não verificado" : "Falha declarada"
        : PHASE_LABEL[task.phase] ?? task.phase;
  const resultLabel = "Texto devolvido (dados ou resumo do agente; não é evidência)";
  const returnedText = !conversation && !memoryCommand ? result?.message ?? "" : "";
  const repeated = Boolean(returnedText && chat.messages.some(message => message.role === "assistant" &&
    message.task_id === task.task_id && message.text.trim() === returnedText.trim()));
  // A short, distinct summary earns space; full returned data stays available in the disclosure.
  const showSummary = Boolean(returnedText.trim() && !repeated && returnedText.length <= 240);
  const summary = returnedText.trim() ? <div role="group" aria-label={resultLabel} className="lunmi-task-summary">
    <p className="lunmi-task-summary-label">Resumo do agente · não é evidência de execução</p>
    <p>{returnedText}</p>
  </div> : null;

  return (
    <aside
      aria-label="Estado da tarefa"
      data-testid="task-panel"
      data-phase={task.phase}
      className="lunmi-task-panel"
      data-verdict={verdict ?? undefined}
    >
      <header className="lunmi-task-heading">
        <h2>Resultado da tarefa</h2>
        <span data-testid="task-mode" className="lunmi-task-scope">{scopeLabel}</span>
      </header>
      <p className="lunmi-task-verdict" data-testid="task-verdict" data-verdict={verdict ?? undefined}>
        {verdictLabel}
      </p>
      {result && (
        <>
          {Boolean(result.false_success) && (
            <p className="lunmi-task-warning">
              A verificação falhou depois de a ação parecer bem-sucedida.
            </p>
          )}

          {showSummary && <div data-testid="task-summary">{summary}</div>}
          {(result.duration_ms !== undefined || result.steps !== undefined) && <dl className="lunmi-task-quick" data-testid="task-quick-metrics">
            {result.duration_ms !== undefined && <div><dt>Duração total</dt><dd>{fmtMetric(result.duration_ms)}</dd></div>}
            {result.steps !== undefined && <div><dt>Passos</dt><dd>{fmtCount(result.steps)}</dd></div>}
          </dl>}
          <details key={task.task_id} className="lunmi-result-details lunmi-task-details">
            <summary>Detalhes da execução</summary>
            {!showSummary && summary}
            <dl className="lunmi-task-metrics" data-testid="task-metrics">
            {metricRows(result).map(([label, value]) => (
              <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
            ))}
            </dl>
            <dl className="lunmi-task-metrics lunmi-task-provenance" data-testid="task-provenance">
              <div><dt>Estado reportado</dt><dd>{result.status}</dd></div>
              <div><dt>Fase</dt><dd data-testid="task-phase">{task.phase}</dd></div>
              {result.token_usage && <div><dt>Estado dos tokens</dt><dd>{result.token_usage.status}</dd></div>}
              {([
                ["Causa", result.failure_kind], ["Âmbito", result.scope], ["Modo do pedido", task.mode],
                ["Origem do plano", result.plan_source], ["Provedor", result.provider], ["Modelo", result.model],
                ["Provedor AppAgent", result.app_provider], ["Modelo AppAgent", result.app_model],
              ] as const).map(([label, value]) => typeof value === "string" && value.length > 0
                ? <div key={label}><dt>{label}</dt><dd>{value}</dd></div> : null)}
            </dl>
          {result.provider_calls && result.provider_calls.length > 0 && (
            <ul data-testid="provider-calls" className="lunmi-task-calls">
              {result.provider_calls.map((call, index) => (
                <li key={index} data-testid={`provider-call-${index}`}>
                  {call.brain === "host" ? "HostAgent" : "AppAgent"} · {call.provider} · {call.transport} ·{" "}
                  {call.ok ? "ok" : "falha"} ·{" "}
                  {call.total_ms === null ? "tempo não reportado" : formatMs(call.total_ms)} ·{" "}
                  tentativas {fmtUnknown(call.requests)} · retries {fmtUnknown(call.retries)}
                </li>
              ))}
              <li className="lunmi-task-note">
                Tempos de chamada são partes do tempo de inferência, não uma soma ao total.
              </li>
            </ul>
          )}
          {result.provider_calls && result.provider_calls.length === 0 && (
            <p data-testid="provider-calls-empty" className="lunmi-task-note">
              Nenhuma chamada de transporte reportada para esta tarefa.
            </p>
          )}
        </details>
        </>
      )}
    </aside>
  );
}

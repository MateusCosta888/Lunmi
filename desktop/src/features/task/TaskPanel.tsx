/**
 * Painel de estado/resultado da tarefa (`desktop/src/features/task/TaskPanel.tsx`, F2a).
 *
 * Mostra o veredito e as métricas que o `TaskResult` já publica — nada é estimado pelo renderer.
 * B2: métrica ausente do DTO não aparece; `null` medido diz "não reportado"; zero real mostra 0.
 * Tokens usam a proveniência de `token_usage` (complete/partial/unavailable); chamadas de
 * transporte por papel ficam em disclosure própria com denominadores coerentes; a fração
 * `actions_executed/actions` saiu do painel (conjuntos diferentes).
 * Um resultado com `false_success` ou `failure_kind` preenchido é apresentado como falha explícita,
 * nunca como sucesso silencioso.
 */
import React from "react";
import { TOKENS } from "../../shared/theme";
import type { TaskResultDto } from "../../shared/types";
import type { ChatTaskState } from "../chat/useChatTask";

export interface TaskPanelProps {
  chat: ChatTaskState;
}

const PHASE_LABEL: Record<string, string> = {
  running: "Executando…",
  awaiting_confirm: "Aguardando confirmação",
  awaiting_input: "Aguardando resposta",
  finished: "Concluída",
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
  push("Planejamento", fmtMetric(result.planning_time_ms));
  push("Execução", fmtMetric(result.execution_time_ms));
  push("Verificação", fmtMetric(result.verification_time_ms));
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
    return (
      <aside
        aria-label="Estado da tarefa"
        data-testid="task-panel"
        data-phase="idle"
        style={panelStyle}
      >
        <h2 style={headingStyle}>Resultado da tarefa</h2>
        <p style={{ color: TOKENS.textMuted, fontSize: "13px", margin: 0 }}>
          Nenhuma tarefa em andamento. Envie uma instrução para começar.
        </p>
      </aside>
    );
  }

  const verdict = result ? verdictOf(result) : null;
  const conversation = result?.scope === "conversation" || (!result?.scope && task.mode === "converse");
  const memoryCommand = result?.scope === "memory_command";
  const resultLabel = "Texto devolvido (dados ou resumo do agente; não é evidência)";

  return (
    <aside
      aria-label="Estado da tarefa"
      data-testid="task-panel"
      data-phase={task.phase}
      style={panelStyle}
    >
      <h2 style={headingStyle}>Resultado da tarefa</h2>
      <div style={{ display: "flex", gap: "8px", flexWrap: "wrap", alignItems: "center" }}>
        <span style={badgeStyle(TOKENS.accent)} data-testid="task-mode">
          {task.mode === "auto" ? "Tarefa" : task.mode === "execute" ? "Executar" : "Conversar"}
        </span>
        <span
          style={badgeStyle(
            task.phase === "failed"
              ? TOKENS.error
              : task.phase === "finished"
                ? TOKENS.success
                : task.phase === "cancelled"
                  ? TOKENS.warning
                  : TOKENS.textSecondary
          )}
          data-testid="task-phase"
        >
          {PHASE_LABEL[task.phase] ?? task.phase}
        </span>
      </div>



      {result && (
        <>
          <p
            data-testid="task-verdict"
            data-verdict={verdict}
            style={{
              margin: "12px 0 0",
              fontSize: "13px",
              color:
                verdict === "success"
                  ? TOKENS.success
                  : verdict === "cancelled"
                    ? TOKENS.warning
                    : TOKENS.error,
            }}
          >
            {verdict === "success"
              ? conversation ? "Conversa concluída" : memoryCommand ? "Pedido de memória concluído"
                : task.mode === "auto" ? "Tarefa concluída com verificação" : "Concluída com verificação"
              : verdict === "cancelled"
                ? "Cancelada pelo usuário"
                : result.status === "NOT_VERIFIED" ? "Não verificado" : "Falha declarada"}
          </p>

          {verdict === "failure" && typeof result.failure_kind === "string" && (
            <p style={{ margin: "4px 0 0", fontSize: "12px", color: TOKENS.textMuted }}>
              causa: {result.failure_kind}
            </p>
          )}
          {Boolean(result.false_success) && (
            <p style={{ margin: "4px 0 0", fontSize: "12px", color: TOKENS.error }}>
              A verificação falhou depois de a ação parecer bem-sucedida.
            </p>
          )}

          {!conversation && !memoryCommand && result.message && (
            <div role="group" aria-label={resultLabel} style={{ fontSize: "13px", margin: "10px 0 0" }}>
              <p style={{ color: TOKENS.textSecondary, margin: 0 }}>{resultLabel}:</p>
              <p style={{ color: TOKENS.textPrimary, margin: "4px 0 0" }}>{result.message}</p>
            </div>
          )}

          <details className="lunmi-result-details"><summary>Detalhes da execução</summary><dl
            data-testid="task-metrics"
            style={{
              margin: "12px 0 0",
              display: "grid",
              gridTemplateColumns: "1fr auto",
              rowGap: "4px",
              columnGap: "12px",
              fontSize: "12px",
            }}
          >
            {metricRows(result).map(([label, value]) => (
              <React.Fragment key={label}>
                <dt style={{ color: TOKENS.textMuted, margin: 0 }}>{label}</dt>
                <dd style={{ color: TOKENS.textSecondary, margin: 0, textAlign: "right" }}>
                  {value}
                </dd>
              </React.Fragment>
            ))}
          </dl>
          {result.provider_calls && result.provider_calls.length > 0 && (
            <ul data-testid="provider-calls" style={{ margin: "10px 0 0", padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: "4px", fontSize: "12px" }}>
              {result.provider_calls.map((call, index) => (
                <li key={index} data-testid={`provider-call-${index}`} style={{ color: TOKENS.textSecondary }}>
                  {call.brain === "host" ? "HostAgent" : "AppAgent"} · {call.provider} · {call.transport} ·{" "}
                  {call.ok ? "ok" : "falha"} ·{" "}
                  {call.total_ms === null ? "tempo não reportado" : formatMs(call.total_ms)} ·{" "}
                  tentativas {fmtUnknown(call.requests)} · retries {fmtUnknown(call.retries)}
                </li>
              ))}
              <li style={{ color: TOKENS.textMuted }}>
                Tempos de chamada são partes do tempo de inferência, não uma soma ao total.
              </li>
            </ul>
          )}
          {result.provider_calls && result.provider_calls.length === 0 && (
            <p data-testid="provider-calls-empty" style={{ margin: "10px 0 0", fontSize: "12px", color: TOKENS.textMuted }}>
              Nenhuma chamada de transporte reportada para esta tarefa.
            </p>
          )}
        </details>
        </>
      )}
    </aside>
  );
}

const panelStyle: React.CSSProperties = {
  maxWidth: "min(680px, 94%)",
  flex: "0 0 auto",
  padding: "16px 20px",
  border: `1px solid ${TOKENS.borderSubtle}`,
  borderRadius: "16px",
  backgroundColor: TOKENS.bgCardSubtle,
  overflowWrap: "anywhere",
};

const headingStyle: React.CSSProperties = {
  margin: "0 0 10px",
  fontSize: "13px",
  letterSpacing: "0.04em",
  textTransform: "none",
  color: TOKENS.textSecondary,
};

function badgeStyle(color: string): React.CSSProperties {
  return {
    padding: "2px 8px",
    borderRadius: "999px",
    border: `1px solid ${color}`,
    color,
    fontSize: "11px",
  };
}

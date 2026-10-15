/**
 * Painel de estado/resultado da tarefa (`desktop/src/features/task/TaskPanel.tsx`, F2a).
 *
 * Mostra o veredito e as métricas que o `TaskResult` já publica — nada é estimado pelo renderer.
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

function formatMs(value: unknown): string {
  const ms = typeof value === "number" && Number.isFinite(value) ? value : 0;
  if (ms >= 1000) {
    return `${(ms / 1000).toFixed(2)} s`;
  }
  return `${ms.toFixed(0)} ms`;
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
  const rows: Array<[string, string]> = [
    ["Duração total", formatMs(result.duration_ms)],
    ["Inferência (LLM)", formatMs(result.llm_time_ms)],
    ["Planejamento", formatMs(result.planning_time_ms)],
    ["Execução", formatMs(result.execution_time_ms)],
    ["Verificação", formatMs(result.verification_time_ms)],
    ["Passos", String(result.steps ?? 0)],
    ["Ações executadas", `${result.actions_executed ?? 0}/${result.actions ?? 0}`],
    ["Chamadas ao modelo", String(result.llm_calls ?? 0)],
    [
      "Evidências verificadas",
      `${result.evidence_verified_count ?? 0}/${result.evidence_count ?? 0}`,
    ],
  ];
  if (result.total_tokens !== null && result.total_tokens !== undefined) {
    rows.push(["Tokens", String(result.total_tokens)]);
  }
  rows.push(["Confirmações", `${result.approvals_asked ?? 0}/${result.approvals_total ?? 0}`]);
  if (result.memory_context_items !== undefined) {
    rows.push(["Itens de memória", String(result.memory_context_items)]);
  }
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
          {task.mode === "execute" ? "Executar" : "Conversar"}
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
              ? task.mode === "converse" ? "Conversa concluída" : "Concluída com verificação"
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

          {task.mode !== "converse" && <p style={{ color: TOKENS.textPrimary, fontSize: "13px", margin: "10px 0 0" }}>{result.message}</p>}

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
          </dl></details>
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

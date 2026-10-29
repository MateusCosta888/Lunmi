import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TaskPanel } from "../src/features/task/TaskPanel";
import type { ChatTaskState } from "../src/features/chat/useChatTask";
import type { TaskResultDto } from "../src/shared/types";
import { contrastRatio, THEMES } from "../src/shared/theme";

function state(result: Partial<TaskResultDto> = {}, overrides: Partial<ChatTaskState> = {}): ChatTaskState {
  return {
    task: { task_id: "task-1", mode: "auto", instruction: "pedido", phase: "finished",
      progress: "", result: { task_id: "task-1", status: "SUCCESS", message: "", ...result }, error: null },
    messages: [], ...overrides,
  } as unknown as ChatTaskState;
}

describe("compact task result", () => {
  it("does not claim verification from SUCCESS alone", () => {
    render(<TaskPanel chat={state({ scope: "end_to_end" })} />);
    expect(screen.getByTestId("task-verdict").textContent).toBe("Pedido concluído");
    expect(screen.getByTestId("task-mode").textContent).toBe("Ação no Windows");
    expect(screen.getByTestId("task-panel").textContent).not.toContain("com verificação");
    expect(screen.getByText("Detalhes da execução").closest("details")?.open).toBe(false);
  });

  it.each([
    { false_success: true }, { failure_kind: "VERIFICATION_FAILED" },
  ])("preserves a failure when SUCCESS carries contradictory verification: %j", contradiction => {
    render(<TaskPanel chat={state({ ...contradiction, scope: "end_to_end" })} />);
    expect(screen.getByTestId("task-verdict").getAttribute("data-verdict")).toBe("failure");
    expect(screen.getByTestId("task-verdict").textContent).toBe("Falha declarada");
    fireEvent.click(screen.getByText("Detalhes da execução"));
    if (contradiction.failure_kind) {
      expect(screen.getByTestId("task-provenance").textContent).toContain("VERIFICATION_FAILED");
    } else {
      expect(screen.getByText("A verificação falhou depois de a ação parecer bem-sucedida.")).toBeTruthy();
    }
  });

  it.each([
    ["conversation", "Conversa", "Conversa concluída"],
    ["memory_command", "Memória", "Pedido de memória concluído"],
    ["engine_only", "Ação no Windows", "Pedido concluído"],
    ["future_scope", "Pedido", "Pedido concluído"],
  ])("describes scope %s without inferring an unreported Windows action", (scope, label, verdict) => {
    render(<TaskPanel chat={state({ scope })} />);
    expect(screen.getByTestId("task-mode").textContent).toBe(label);
    expect(screen.getByTestId("task-verdict").textContent).toBe(verdict);
  });

  it("keeps the returned text as provenance when it is already in the assistant response", () => {
    const message = "O agente diz que abriu a aplicação.";
    render(<TaskPanel chat={state({ scope: "end_to_end", message }, {
      messages: [{ role: "assistant", task_id: "task-1", text: message }],
    })} />);
    expect(screen.queryByTestId("task-summary")).toBeNull();
    const details = screen.getByText("Detalhes da execução").closest("details")!;
    expect(details.open).toBe(false);
    expect(details.textContent).toContain(message);
    fireEvent.click(screen.getByText("Detalhes da execução"));
    expect(details.open).toBe(true);
    expect(within(details).getByRole("group", { name: /Texto devolvido/ }).textContent).toContain(message);
  });

  it("shows a distinct short summary with its data provenance and never executes it", () => {
    const message = "Já apaguei a pasta Testes.";
    render(<TaskPanel chat={state({ scope: "end_to_end", message })} />);
    const summary = screen.getByTestId("task-summary");
    expect(summary.textContent).toContain(message);
    expect(summary.textContent).toContain("não é evidência de execução");
    expect(screen.getByTestId("task-verdict").textContent).not.toContain(message);
  });

  it("preserves returned data whitespace and renders markup as text", () => {
    const message = "  <script>neverExecute()</script>\n  resultado  ";
    render(<TaskPanel chat={state({ message })} />);
    expect(screen.getByTestId("task-summary").querySelector("p:last-child")?.textContent).toBe(message);
    expect(screen.getByTestId("task-panel").querySelector("script")).toBeNull();
  });

  it("keeps long returned data complete in details instead of expanding or clipping the compact card", () => {
    const message = "Relatório completo. ".repeat(30);
    render(<TaskPanel chat={state({ message })} />);
    expect(screen.queryByTestId("task-summary")).toBeNull();
    const details = screen.getByText("Detalhes da execução").closest("details")!;
    expect(details.textContent).toContain(message.trim());
    expect(details.open).toBe(false);
  });

  it("shows real zero duration and steps, without inventing missing numbers", () => {
    const view = render(<TaskPanel chat={state({ duration_ms: 0, steps: 0 })} />);
    const quick = screen.getByTestId("task-quick-metrics");
    expect(within(quick).getByText("0 ms")).toBeTruthy();
    expect(within(quick).getByText("0")).toBeTruthy();
    view.rerender(<TaskPanel chat={state()} />);
    expect(screen.queryByTestId("task-quick-metrics")).toBeNull();
    expect(screen.getByTestId("task-metrics").textContent).not.toContain("Duração total");
    expect(screen.getByTestId("task-metrics").textContent).not.toContain("Passos");
  });

  it("distinguishes missing provider metrics, null values and real zeroes", () => {
    const view = render(<TaskPanel chat={state()} />);
    expect(screen.getByTestId("task-metrics").textContent).not.toContain("Arranque do provedor");
    view.rerender(<TaskPanel chat={state({ provider_startup_ms: null, provider_wait_ms: 0 })} />);
    const metrics = screen.getByTestId("task-metrics");
    expect(metrics.textContent).toContain("Arranque do provedornão reportado");
    expect(metrics.textContent).toContain("Espera do provedor0 ms");
  });

  it("keeps raw provenance and unavailable token coverage accessible in details", () => {
    render(<TaskPanel chat={state({ provider: "gemini", model: "model-x", app_provider: "cli-x", app_model: "model-y",
      scope: "end_to_end", plan_source: "host_brain", provider_calls: [],
      token_usage: { status: "unavailable", calls_total: 0, calls_with_input: 0, calls_with_output: 0,
        input_reported: null, output_reported: null },
    })} />);
    fireEvent.click(screen.getByText("Detalhes da execução"));
    const provenance = screen.getByTestId("task-provenance").textContent;
    for (const value of ["SUCCESS", "gemini", "model-x", "cli-x", "model-y", "end_to_end", "host_brain", "auto", "unavailable"]) {
      expect(provenance).toContain(value);
    }
    expect(screen.getByTestId("provider-calls-empty")).toBeTruthy();
  });

  it("starts the next task with its details closed again", () => {
    const view = render(<TaskPanel chat={state()} />);
    fireEvent.click(screen.getByText("Detalhes da execução"));
    expect(screen.getByText("Detalhes da execução").closest("details")?.open).toBe(true);
    const next = state();
    next.task = { ...next.task!, task_id: "task-2", result: { ...next.task!.result!, task_id: "task-2" } };
    view.rerender(<TaskPanel chat={next} />);
    expect(screen.getByText("Detalhes da execução").closest("details")?.open).toBe(false);
  });

  it("does not infer a successful outcome for a terminal phase without its result", () => {
    const chat = state();
    chat.task = { ...chat.task!, result: null };
    render(<TaskPanel chat={chat} />);
    expect(screen.getByTestId("task-verdict").textContent).toBe("Resultado não recebido");
    expect(screen.getByTestId("task-verdict").getAttribute("data-verdict")).toBeNull();
  });

  it.each(Object.values(THEMES))("keeps small result text readable on the $label card surface", theme => {
    for (const color of [theme.colors.textPrimary, theme.colors.textSecondary, theme.colors.textMuted,
      theme.colors.success, theme.colors.warning, theme.colors.error]) {
      expect(contrastRatio(color, theme.colors.bgCardSubtle)).toBeGreaterThanOrEqual(4.5);
    }
  });
});

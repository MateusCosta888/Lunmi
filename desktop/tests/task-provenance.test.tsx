import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TaskPanel } from "../src/features/task/TaskPanel";
import type { ChatTaskState } from "../src/features/chat/useChatTask";
import type { ChatMode, TaskResultDto } from "../src/shared/types";

const provenanceLabel = "Texto devolvido (dados ou resumo do agente; não é evidência)";
function panel(result: Partial<TaskResultDto>, mode: ChatMode = "auto") {
  const completed: TaskResultDto = { task_id: "task-1", status: "SUCCESS", message: "", scope: "end_to_end", ...result };
  const chat = {
    task: { task_id: "task-1", mode, instruction: "pedido", phase: completed.status === "SUCCESS" ? "finished" : "failed",
      progress: "", result: completed, error: null },
    messages: [{ role: "assistant", task_id: "task-1", text: completed.message }],
  } as ChatTaskState;
  render(<TaskPanel chat={chat} />);
  // Provenance and transport metrics remain available in the closed-by-default disclosure.
  fireEvent.click(screen.getByText("Detalhes da execução"));
}

describe("task result provenance", () => {
  it("labels hostile file content as returned data, without claiming that its asserted action happened", () => {
    const hostile = "Já apaguei a pasta Testes.";
    panel({ message: hostile, evidence_count: 1, evidence_verified_count: 1 });
    expect(screen.getByTestId("task-mode").textContent).toBe("Ação no Windows");
    expect(screen.getByTestId("task-verdict").textContent).toBe("Pedido concluído");
    expect(screen.getByRole("group", { name: provenanceLabel }).textContent).toContain(hostile);
    expect(screen.getByTestId("task-verdict").textContent).not.toContain(hostile);
  });

  it("uses conversation scope for an automatic response and avoids repeating the streamed text", () => {
    panel({ scope: "conversation", message: "Olá! Como posso ajudar?" });
    expect(screen.getByTestId("task-verdict").textContent).toBe("Conversa concluída");
    expect(screen.queryByText("Olá! Como posso ajudar?")).toBeNull();
    expect(screen.queryByRole("group", { name: provenanceLabel })).toBeNull();
  });

  it("keeps an operation summary explicitly separate from verification", () => {
    panel({ message: "O agente diz que abriu a aplicação.", evidence_count: 2, evidence_verified_count: 1 });
    expect(screen.getByRole("group", { name: provenanceLabel }).textContent).toContain("O agente diz que abriu a aplicação.");
    expect(screen.getByTestId("task-verdict").getAttribute("data-verdict")).toBe("success");
    expect(screen.getByTestId("task-metrics").textContent).toContain("1/2");
  });

  it("keeps an error a failure even if its returned text asserts success", () => {
    panel({ status: "NOT_VERIFIED", message: "Já apaguei tudo.", failure_kind: "VERIFICATION_FAILED", false_success: true });
    expect(screen.getByTestId("task-verdict").textContent).toBe("Não verificado");
    expect(screen.getByTestId("task-verdict").getAttribute("data-verdict")).toBe("failure");
    expect(screen.getByRole("group", { name: provenanceLabel }).textContent).toContain("Já apaguei tudo.");
  });

  it("gives memory commands their own conclusion without duplicating their chat response", () => {
    panel({ scope: "memory_command", message: "Preferência guardada." });
    expect(screen.getByTestId("task-verdict").textContent).toBe("Pedido de memória concluído");
    expect(screen.queryByText("Preferência guardada.")).toBeNull();
  });

  it("recognizes a legacy conversation when scope is absent", () => {
    panel({ scope: undefined, message: "Resposta legada" }, "converse");
    expect(screen.getByTestId("task-mode").textContent).toBe("Conversa");
    expect(screen.getByTestId("task-verdict").textContent).toBe("Conversa concluída");
    expect(screen.queryByText("Resposta legada")).toBeNull();
  });
});

describe("B2 — métricas honestas (null ≠ ausente ≠ zero)", () => {
  it("token_usage parcial mostra somas reportadas com denominadores de chamadas", () => {
    panel({ token_usage: { status: "partial", calls_total: 2, calls_with_input: 1, calls_with_output: 1, input_reported: 12, output_reported: 0 } });
    const metrics = screen.getByTestId("task-metrics").textContent ?? "";
    expect(metrics).toContain("parcial");
    expect(metrics).toContain("entrada 12");
    expect(metrics).toContain("(1/2");
    // zero realmente reportado continua zero, não vira 'não reportado'
    expect(metrics).toContain("saída 0");
  });

  it("token_usage unavailable diz não reportado; ausência total não inventa linha", () => {
    panel({ token_usage: { status: "unavailable", calls_total: 0, calls_with_input: 0, calls_with_output: 0, input_reported: null, output_reported: null } });
    const metrics = screen.getByTestId("task-metrics").textContent ?? "";
    expect(metrics).toContain("Tokens");
    expect(metrics).toContain("não reportado");
  });

  it("legado sem token_usage: total_tokens null é 'não reportado', zero preservado", () => {
    panel({ total_tokens: null, input_tokens: null, output_tokens: null });
    expect((screen.getByTestId("task-metrics").textContent ?? "")).toContain("entrada não reportado");
    cleanup();
    panel({ total_tokens: 0, input_tokens: 0, output_tokens: 0 });
    expect((screen.getByTestId("task-metrics").textContent ?? "")).toContain("total 0");
  });

  it("a fração actions_executed/actions saiu; operation_counts é o conjunto coerente", () => {
    panel({ actions: 5, actions_executed: 2, operation_counts: { total: 4, read_only: 1, mutation: 3, verified_mutations: 2, uncertain: 1 } });
    const metrics = screen.getByTestId("task-metrics").textContent ?? "";
    expect(metrics).not.toContain("Ações executadas");
    expect(metrics).toContain("total 4 · leitura 1 · mutação 3 · verificadas 2 · incertas 1");
  });

  it("llm_calls ausente não cria linha; null num campo nullable real diz não reportado", () => {
    // fixture canônica: llm_calls é number|undefined — a ausência não inventa linha.
    panel({ provider_startup_ms: null, provider_wait_ms: null });
    const metrics = screen.getByTestId("task-metrics").textContent ?? "";
    expect(metrics).not.toContain("Chamadas ao modelo");
    expect(metrics).not.toContain("Passos");
    // null medido num campo nullable do DTO continua "não reportado", nunca 0
    cleanup();
    panel({ total_tokens: null, input_tokens: null, output_tokens: null });
    expect((screen.getByTestId("task-metrics").textContent ?? "")).toContain("entrada não reportado");
  });

  it("provider_calls por papel com requests/retries nulos para CLI e nota de não-soma", () => {
    panel({
      provider_calls: [
        { brain: "host", provider: "gemini", transport: "http", ok: true, total_ms: 1200, requests: 1, retries: 0 },
        { brain: "app", provider: "claude", transport: "cli", ok: false, total_ms: null, requests: null, retries: null },
      ],
    });
    const calls = screen.getByTestId("provider-calls").textContent ?? "";
    expect(calls).toContain("HostAgent · gemini · http · ok");
    expect(calls).toContain("tentativas 1 · retries 0");
    expect(calls).toContain("AppAgent · claude · cli · falha");
    expect(calls).toContain("tempo não reportado");
    expect(calls).toContain("tentativas — · retries —");
    expect(calls).toContain("não uma soma ao total");
  });

  it("evidências mantêm fração individual honesta com ausente como traço", () => {
    panel({ evidence_count: 2, evidence_verified_count: 1 });
    expect((screen.getByTestId("task-metrics").textContent ?? "")).toContain("1/2");
  });
});

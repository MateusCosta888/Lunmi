/**
 * e2e do shell do renderer (`desktop/tests/session.test.tsx`).
 *
 * Cobre o que a revisão F1 exigiu como prova visível e o que o T2 do plano pede:
 * - falha de backend já pronto troca o estado para "Sessão com falha" e expõe o código real;
 * - "Reiniciar backend" passa pelo main (`restartBackend`), abre sessão nova e **não** repete a
 *   instrução anterior (nenhum `chat.send` extra, histórico limpo);
 * - Conversar e Executar enviam o modo certo;
 * - confirmação/pergunta respondem pelo `request_id` e não por evento solto;
 * - `Parar` pede `task.cancel` da tarefa em voo;
 * - capacidade não anunciada desabilita o modo e impede envio (nada de promessa falsa).
 *
 * Nenhum processo real: `window.lunmi` é um fake controlado pelo teste.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App } from "../src/App";
import type { LunmiBridgeApi } from "../src/shared/ipc/client";
import type { Envelope, SessionReadyPayload } from "../src/shared/protocol";

function sessionPayload(sessionId: string, capabilities?: Partial<Record<string, boolean>>): SessionReadyPayload {
  return {
    v: 1,
    session_id: sessionId,
    pid: 4321,
    capabilities: {
      converse: true,
      execute: true,
      memory: true,
      brain: true,
      web_search: false,
      browser_use: false,
      ...capabilities,
    },
    limits: {
      max_line_bytes: 5 * 1024 * 1024,
      max_import_bytes: 4 * 1024 * 1024,
      max_chat_text_chars: 8000,
    },
  };
}

it("preserva o rascunho ao abrir Memória e voltar à conversa", async () => {
  const bridge = makeFakeBridge(sessionPayload("draft-navigation"));
  await renderReadyApp(bridge);
  fireEvent.change(screen.getByTestId("composer"), { target: { value: "Meu rascunho intacto" } });
  fireEvent.click(screen.getByTestId("nav-memory"));
  await screen.findByTestId("memory-panel");
  fireEvent.click(screen.getByTestId("nav-chat"));
  expect((screen.getByTestId("composer") as HTMLTextAreaElement).value).toBe("Meu rascunho intacto");
  expect(bridge.calls.filter(call => call.type === "chat.send")).toHaveLength(0);
});

interface FakeBridge {
  api: LunmiBridgeApi;
  emit: (envelope: Envelope) => void;
  calls: Array<{ type: string; payload: Record<string, unknown> }>;
  restartCount: () => number;
  /** Segura os próximos `chat.send` para o teste controlar quando (e como) eles terminam. */
  setDeferSend: (value: boolean) => void;
  deferredSendCount: () => number;
  settleSend: (index: number, error?: Error) => void;
}

function makeFakeBridge(initialSession: SessionReadyPayload): FakeBridge {
  const listeners = new Set<(event: Envelope) => void>();
  const calls: Array<{ type: string; payload: Record<string, unknown> }> = [];
  let restartCount = 0;
  let currentSession = initialSession;
  let deferSend = false;
  const deferredSends: Array<{
    resolve: (value: Record<string, unknown>) => void;
    reject: (error: Error) => void;
  }> = [];

  // O espião devolve respostas por comando; o tipo genérico de `sendCommand` é satisfeito pelo
  // cast abaixo, já que cada resposta concreta é verificada pelos testes de cada painel.
  const sendCommandMock = vi.fn(
    async (type: string, payload: Record<string, unknown> = {}): Promise<Record<string, unknown>> => {
      calls.push({ type, payload });
      if (type === "chat.send" && deferSend) {
        return await new Promise<Record<string, unknown>>((resolve, reject) => {
          deferredSends.push({ resolve, reject });
        });
      }
      if (type === "brain.get") {
        return { current: {}, providers: [] };
      }
      if (type === "approval.get") {
        return { auto_approve: false, mode: "manual", label: "Modo: Manual" };
      }
      if (type === "ui.preferences.get") {
        return { window: null, scale_pct: 100, actions: [] };
      }
      if (type === "memory.list") {
        return { enabled: true, items: [] };
      }
      if (type === "memory.proposals") {
        return { proposals: [] };
      }
      if (type === "memory.options.get") {
        return { options: { enabled: true } };
      }
      return { ok: true };
    }
  );

  const api: LunmiBridgeApi = {
    hello: vi.fn(async () => currentSession),
    notifyRendererReady: vi.fn(async () => ({
      ok: true,
      rendererPaintedReadyMs: 1,
      windowShownMs: 1,
      windowReadyToShowMs: 1,
    })),
    sendCommand: sendCommandMock as unknown as LunmiBridgeApi["sendCommand"],
    pickLocation: vi.fn(async () => ({ path: "C:/tmp/app.exe" })),
    restartBackend: vi.fn(async () => {
      restartCount += 1;
      currentSession = sessionPayload(`sess-restarted-${restartCount}`);
      return currentSession;
    }),
    shutdown: vi.fn(async () => ({ status: "shutting_down", session_id: currentSession.session_id })),
    onEvent: (callback: (event: Envelope) => void) => {
      listeners.add(callback);
      return () => {
        listeners.delete(callback);
      };
    },
  };

  return {
    api,
    emit: (envelope) => {
      for (const listener of [...listeners]) {
        listener(envelope);
      }
    },
    calls,
    restartCount: () => restartCount,
    setDeferSend: (value: boolean) => {
      deferSend = value;
    },
    deferredSendCount: () => deferredSends.length,
    settleSend: (index: number, error?: Error) => {
      const entry = deferredSends[index];
      if (!entry) {
        throw new Error(`sem envio adiado no índice ${index}`);
      }
      if (error) {
        entry.reject(error);
      } else {
        entry.resolve({ ok: true });
      }
    },
  };
}

function event(type: string, payload: Record<string, unknown>): Envelope {
  return { v: 1, id: `evt-${type}-${Math.random().toString(16).slice(2)}`, type, payload };
}

async function renderReadyApp(bridge: FakeBridge): Promise<void> {
  (window as unknown as { lunmi: LunmiBridgeApi }).lunmi = bridge.api;
  render(<App />);
  await waitFor(() => expect(screen.getByTestId("session-status").textContent).toBe("Ready"));
  // O `Ready` nasce na mesma atualização que sobe `sessionVersion`, cujo efeito limpa o estado da
  // conversa. Sem esta volta de macrotarefa, um evento emitido logo depois podia ser apagado por
  // esse efeito e o teste mediria a corrida, não o comportamento.
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("Shell do renderer — recuperação de sessão (F1 reparo 3)", () => {
  it("troca para 'Sessão com falha' e reinicia o backend sem repetir a instrução", async () => {
    const bridge = makeFakeBridge(sessionPayload("sess-1"));
    await renderReadyApp(bridge);

    // Envia uma tarefa real e deixa o backend morrer com a tarefa ainda em voo.
    fireEvent.change(screen.getByTestId("composer"), { target: { value: "abra o Bloco de Notas" } });
    fireEvent.click(screen.getByTestId("send-task"));
    await waitFor(() =>
      expect(bridge.calls.filter((call) => call.type === "chat.send")).toHaveLength(1)
    );
    bridge.emit(event("task.started", { task_id: "tsk-1", mode: "execute", instruction: "abra o Bloco de Notas" }));

    bridge.emit(
      event("task.error", {
        code: "backend_exited",
        message: "O processo Python do backend encerrou inesperadamente; nenhuma tarefa foi reexecutada.",
        session_id: "sess-1",
        exit_code: 1,
        signal: null,
      })
    );

    await waitFor(() => expect(screen.getByTestId("session-status").textContent).toBe("Sessão com falha"));
    expect(screen.getByTestId("session-failure-code").textContent).toBe("backend_exited");
    expect(screen.queryByTestId("session-summary")).toBeNull();

    fireEvent.click(screen.getByTestId("restart-backend"));

    await waitFor(() => expect(screen.getByTestId("session-status").textContent).toBe("Ready"));
    expect(bridge.restartCount()).toBe(1);
    // A sessão nova não carrega o histórico nem reenvia a instrução antiga.
    expect(bridge.calls.filter((call) => call.type === "chat.send")).toHaveLength(1);
    expect(screen.queryByTestId("session-failure-code")).toBeNull();
    expect(screen.getByTestId("chat-messages").textContent).not.toContain("abra o Bloco de Notas");
  });

  it("mostra falha quando a primeira hello não completa", async () => {
    const bridge = makeFakeBridge(sessionPayload("sess-0"));
    (bridge.api.hello as unknown as { mockRejectedValueOnce: (err: Error) => void }).mockRejectedValueOnce(
      new Error("backend_spawn_failed: Não foi possível iniciar o processo Python do backend.")
    );
    (window as unknown as { lunmi: LunmiBridgeApi }).lunmi = bridge.api;

    render(<App />);

    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("Não foi possível iniciar"));
    expect(screen.getByTestId("startup-screen")).toBeTruthy();
    expect(screen.queryByTestId("composer")).toBeNull();
  });
});

describe("Chat e tarefa (F2a)", () => {
  it("envia o modo escolhido e renderiza a resposta sem executar HTML do modelo", async () => {
    const bridge = makeFakeBridge(sessionPayload("sess-1"));
    await renderReadyApp(bridge);

    fireEvent.click(screen.getByTestId("mode-converse"));
    fireEvent.change(screen.getByTestId("composer"), { target: { value: "explique o que é UIA" } });
    fireEvent.click(screen.getByTestId("send-task"));

    await waitFor(() =>
      expect(bridge.calls.find((call) => call.type === "chat.send")?.payload).toEqual({
        mode: "converse",
        text: "explique o que é UIA",
      })
    );

    bridge.emit(event("chat.message", { role: "user", text: "explique o que é UIA", mode: "converse", task_id: "tsk-1" }));
    bridge.emit(
      event("chat.message", {
        role: "assistant",
        text: "**Resposta** <script>alert(1)</script> <img src=x onerror=alert(2)>",
        task_id: "tsk-1",
      })
    );

    await waitFor(() => expect(screen.getByTestId("chat-message-assistant")).toBeTruthy());
    // O HTML do modelo é texto escapado, nunca DOM executável.
    expect(document.querySelector("script")).toBeNull();
    expect(screen.getByTestId("chat-message-assistant").querySelector("img")).toBeNull();
    expect(screen.getByTestId("chat-message-assistant").textContent).toContain("<script>");
    expect(screen.getByTestId("chat-message-assistant").querySelector("strong")?.textContent).toBe(
      "Resposta"
    );
  });

  it("responde confirmação e pergunta pelo request_id e não por evento solto", async () => {
    const bridge = makeFakeBridge(sessionPayload("sess-1"));
    await renderReadyApp(bridge);

    fireEvent.change(screen.getByTestId("composer"), { target: { value: "apague o arquivo" } });
    fireEvent.click(screen.getByTestId("send-task"));
    await waitFor(() => expect(bridge.calls.some((call) => call.type === "chat.send")).toBe(true));

    bridge.emit(event("task.started", { task_id: "tsk-9", mode: "execute", instruction: "apague o arquivo" }));
    bridge.emit(event("task.need_confirm", { task_id: "tsk-9", request_id: "cfm-1", message: "Apagar o arquivo?" }));

    await waitFor(() => expect(screen.getByTestId("confirm-request")).toBeTruthy());
    fireEvent.click(screen.getByTestId("confirm-accept"));

    await waitFor(() => {
      const confirm = bridge.calls.find((call) => call.type === "task.confirm");
      expect(confirm?.payload).toEqual({ request_id: "cfm-1", approved: true, task_id: "tsk-9" });
    });
    // O diálogo some depois de respondido; uma segunda resposta exigiria nova solicitação.
    await waitFor(() => expect(screen.queryByTestId("confirm-request")).toBeNull());

    bridge.emit(
      event("task.need_input", {
        task_id: "tsk-9",
        request_id: "inp-2",
        kind: "choice",
        label: "Qual aplicativo?",
        candidates: [
          { name: "Notepad", publisher: "Microsoft", install_dir: "", app_id: "notepad", source: "start_menu" },
        ],
      })
    );
    await waitFor(() => expect(screen.getByTestId("input-request")).toBeTruthy());
    fireEvent.click(screen.getByTestId("input-candidate-0"));

    await waitFor(() => {
      const answer = bridge.calls.find((call) => call.type === "task.answer");
      expect(answer?.payload).toEqual({
        request_id: "inp-2",
        kind: "choice",
        index: 0,
        task_id: "tsk-9",
      });
    });
  });

  it("preserva o rascunho quando o envio é recusado e mostra o motivo", async () => {
    const bridge = makeFakeBridge(sessionPayload("sess-1"));
    await renderReadyApp(bridge);

    bridge.setDeferSend(true);
    fireEvent.change(screen.getByTestId("composer"), {
      target: { value: "LUNMI-E2E: preservar rascunho se envio recusado" },
    });
    fireEvent.click(screen.getByTestId("send-task"));
    await waitFor(() => expect(bridge.deferredSendCount()).toBe(1));

    // O backend recusa antes de qualquer aceite (troca de cérebro em andamento).
    bridge.settleSend(0, new Error("reconfiguring: Troca de cérebro em andamento; aguarde."));

    await waitFor(() => expect(screen.getByTestId("chat-error")).toBeTruthy());
    expect((screen.getByTestId("composer") as HTMLTextAreaElement).value).toBe(
      "LUNMI-E2E: preservar rascunho se envio recusado"
    );
    expect(screen.getByTestId("chat-error").textContent).toContain("em andamento");
    // Uma recusa não pode deixar o app achando que há tarefa em voo.
    expect(screen.queryByTestId("stop-task")).toBeNull();
  });

  it("só limpa o rascunho depois do aceite, e não apaga texto novo digitado durante a espera", async () => {
    const bridge = makeFakeBridge(sessionPayload("sess-1"));
    await renderReadyApp(bridge);

    // (A) O texto NÃO é apagado antes da resposta do backend...
    bridge.setDeferSend(true);
    fireEvent.change(screen.getByTestId("composer"), { target: { value: "primeira instrução" } });
    fireEvent.click(screen.getByTestId("send-task"));
    await waitFor(() => expect(bridge.deferredSendCount()).toBe(1));
    expect((screen.getByTestId("composer") as HTMLTextAreaElement).value).toBe("primeira instrução");

    // ...e só sai do campo quando o comando é aceito.
    bridge.settleSend(0);
    await waitFor(() =>
      expect((screen.getByTestId("composer") as HTMLTextAreaElement).value).toBe("")
    );
    // A tarefa termina, liberando a interface para o próximo envio.
    bridge.emit(event("task.finished", { task_id: "tsk-a", status: "SUCCESS", message: "ok" }));

    // (B) Rascunho NOVO digitado enquanto o envio está pendente pertence ao usuário.
    fireEvent.change(screen.getByTestId("composer"), { target: { value: "segunda instrução" } });
    fireEvent.click(screen.getByTestId("send-task"));
    await waitFor(() => expect(bridge.deferredSendCount()).toBe(2));
    fireEvent.change(screen.getByTestId("composer"), {
      target: { value: "rascunho novo durante a espera" },
    });

    bridge.settleSend(1);
    await waitFor(() => expect(bridge.calls.filter((c) => c.type === "chat.send")).toHaveLength(2));
    expect((screen.getByTestId("composer") as HTMLTextAreaElement).value).toBe(
      "rascunho novo durante a espera"
    );
  });

  it("Parar pede cancelamento da tarefa em voo", async () => {
    const bridge = makeFakeBridge(sessionPayload("sess-1"));
    await renderReadyApp(bridge);

    fireEvent.change(screen.getByTestId("composer"), { target: { value: "abra a calculadora" } });
    fireEvent.click(screen.getByTestId("send-task"));
    bridge.emit(event("task.started", { task_id: "tsk-7", mode: "execute", instruction: "abra a calculadora" }));

    fireEvent.click(await screen.findByTestId("stop-task"));

    await waitFor(() => {
      const cancel = bridge.calls.find((call) => call.type === "task.cancel");
      expect(cancel?.payload).toEqual({ task_id: "tsk-7" });
    });
  });

  it("mantém Parar acessível numa página secundária e preserva o rascunho", async () => {
    const bridge = makeFakeBridge(sessionPayload("secondary-stop"));
    await renderReadyApp(bridge);
    bridge.emit(event("task.started", { task_id: "active", mode: "execute", instruction: "tarefa própria" }));
    fireEvent.change(screen.getByTestId("composer"), { target: { value: "próximo pedido" } });
    fireEvent.click(screen.getByTestId("nav-memory"));
    await screen.findByTestId("memory-panel");
    fireEvent.click(screen.getByRole("button", { name: "Parar" }));
    await waitFor(() => expect(bridge.calls.find(call => call.type === "task.cancel")?.payload).toEqual({ task_id: "active" }));
    fireEvent.click(screen.getByTestId("nav-chat"));
    expect((screen.getByTestId("composer") as HTMLTextAreaElement).value).toBe("próximo pedido");
  });

  it("não promete modo que a sessão não anunciou", async () => {
    const bridge = makeFakeBridge(sessionPayload("sess-2", { converse: false }));
    await renderReadyApp(bridge);

    const converseRadio = screen.getByTestId("mode-converse") as HTMLInputElement;
    expect(converseRadio.disabled).toBe(true);

    // Mesmo forçando o estado do rádio, o envio em Conversar é recusado antes de sair.
    fireEvent.click(converseRadio);
    fireEvent.change(screen.getByTestId("composer"), { target: { value: "só conversar" } });
    fireEvent.click(screen.getByTestId("send-task"));

    await waitFor(() => expect(screen.getByTestId("chat-error")).toBeTruthy());
    expect(bridge.calls.some((call) => call.type === "chat.send")).toBe(false);
  });

  it("mostra o veredito real e a falha declarada do resultado", async () => {
    const bridge = makeFakeBridge(sessionPayload("sess-1"));
    await renderReadyApp(bridge);

    bridge.emit(event("task.started", { task_id: "tsk-3", mode: "execute", instruction: "abra o app" }));
    bridge.emit(
      event("task.finished", {
        task_id: "tsk-3",
        status: "FAILED",
        message: "A verificação não confirmou o estado final.",
        failure_kind: "VERIFICATION_FAILED",
        false_success: true,
        duration_ms: 1234.5,
        steps: 3,
        actions: 4,
        actions_executed: 3,
        evidence_count: 2,
        evidence_verified_count: 0,
      })
    );

    await waitFor(() => expect(screen.getByTestId("task-verdict").textContent).toBe("Falha declarada"));
    expect(screen.getByTestId("task-verdict").getAttribute("data-verdict")).toBe("failure");
    expect(screen.getByTestId("task-metrics").textContent).toContain("1.23 s");
  });
});

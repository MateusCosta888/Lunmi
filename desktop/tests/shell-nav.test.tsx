/**
 * Sidebar recolhível do shell (`desktop/tests/shell-nav.test.tsx`, lote B).
 *
 * Contratos: botão real com `aria-expanded`; o rail de ícones nunca desaparece — cada rota
 * mantém nome acessível e continua navegável recolhida; "Parar" permanece alcançável em página
 * secundária com a barra recolhida; janela pequena começa recolhida e pode ser expandida
 * manualmente. Nenhum backend real.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App } from "../src/App";
import type { LunmiBridgeApi } from "../src/shared/ipc/client";
import type { Envelope, SessionReadyPayload } from "../src/shared/protocol";

function sessionPayload(sessionId: string): SessionReadyPayload {
  return {
    v: 1,
    session_id: sessionId,
    pid: 4321,
    capabilities: { converse: true, execute: true, memory: true, brain: true, web_search: false, browser_use: false },
    limits: { max_line_bytes: 5 * 1024 * 1024, max_import_bytes: 4 * 1024 * 1024, max_chat_text_chars: 8000 },
  };
}

function makeFakeBridge() {
  const listeners = new Set<(event: Envelope) => void>();
  const calls: Array<{ type: string; payload: Record<string, unknown> }> = [];
  const api: LunmiBridgeApi = {
    hello: vi.fn(async () => sessionPayload("sess-nav")),
    notifyRendererReady: vi.fn(async () => ({ ok: true, rendererPaintedReadyMs: 1, windowShownMs: 1, windowReadyToShowMs: 1 })),
    sendCommand: vi.fn(async (type: string, payload: Record<string, unknown> = {}) => {
      calls.push({ type, payload });
      if (type === "brain.get") return { current: {}, providers: [] };
      if (type === "approval.get") return { auto_approve: false, mode: "manual", label: "Modo: Manual" };
      if (type === "ui.preferences.get") return { window: null, scale_pct: 100, actions: [] };
      if (type === "memory.list") return { enabled: true, items: [] };
      if (type === "memory.proposals") return { proposals: [] };
      if (type === "memory.options.get") return { options: { enabled: true } };
      return { ok: true };
    }) as unknown as LunmiBridgeApi["sendCommand"],
    pickLocation: vi.fn(async () => ({ path: "" })),
    restartBackend: vi.fn(async () => sessionPayload("sess-nav-2")),
    shutdown: vi.fn(async () => ({ status: "shutting_down", session_id: "sess-nav" })),
    onEvent: (callback: (event: Envelope) => void) => {
      listeners.add(callback);
      return () => listeners.delete(callback);
    },
  };
  return { api, calls, emit: (envelope: Envelope) => { for (const listener of [...listeners]) listener(envelope); } };
}

async function renderReady(bridge: ReturnType<typeof makeFakeBridge>) {
  (window as unknown as { lunmi: LunmiBridgeApi }).lunmi = bridge.api;
  render(<App />);
  await waitFor(() => expect(screen.getByTestId("session-status").textContent).toBe("Ready"));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

function sidebar(): HTMLElement {
  return document.querySelector(".lunmi-sidebar") as HTMLElement;
}

beforeEach(() => {
  // O stub global dos testes sempre devolve `matches: true`; aqui a largura precisa importar:
  // movimento reduzido continua dispensando a intro, mas a janela padrão não é estreita.
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: query.includes("prefers-reduced-motion"),
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }) as unknown as MediaQueryList);
});

describe("Sidebar recolhível (lote B)", () => {
  it("recolhe a um rail de ícones sem perder nomes acessíveis e expande de novo", async () => {
    const bridge = makeFakeBridge();
    await renderReady(bridge);

    const toggle = screen.getByTestId("nav-collapse-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(toggle.getAttribute("aria-label")).toBe("Recolher navegação");

    fireEvent.click(toggle);
    expect(sidebar().dataset.collapsed).toBe("true");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.getAttribute("aria-label")).toBe("Expandir navegação");

    // O rail nunca desaparece: cada rota continua um botão real com o mesmo nome acessível.
    expect(screen.getByRole("button", { name: "Memória" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Configurações" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Brain" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Memória" }));
    await screen.findByTestId("memory-panel");

    fireEvent.click(toggle);
    expect(sidebar().dataset.collapsed).toBe("false");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
  });

  it("mantém Parar alcançável em página secundária com a barra recolhida", async () => {
    const bridge = makeFakeBridge();
    await renderReady(bridge);

    fireEvent.click(screen.getByTestId("nav-collapse-toggle"));
    bridge.emit({ v: 1, id: "t", type: "task.started", payload: { task_id: "tsk-nav", mode: "execute", instruction: "tarefa" } });

    fireEvent.click(screen.getByRole("button", { name: "Memória" }));
    await screen.findByTestId("memory-panel");
    fireEvent.click(screen.getByRole("button", { name: "Parar" }));

    await waitFor(() =>
      expect(bridge.calls.find((call) => call.type === "task.cancel")?.payload).toEqual({ task_id: "tsk-nav" })
    );
  });

  it("janela estreita começa recolhida e o usuário pode expandir manualmente", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: query.includes("prefers-reduced-motion") || query === "(max-width: 900px)",
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList);

    const bridge = makeFakeBridge();
    await renderReady(bridge);
    expect(sidebar().dataset.collapsed).toBe("true");

    fireEvent.click(screen.getByTestId("nav-collapse-toggle"));
    expect(sidebar().dataset.collapsed).toBe("false");
    expect(screen.getByRole("button", { name: "Conversa" })).toBeTruthy();
  });

  it("não duplica a navegação: um único controle e um único conjunto de rotas", async () => {
    const bridge = makeFakeBridge();
    await renderReady(bridge);
    expect(screen.getAllByRole("navigation")).toHaveLength(1);
    expect(screen.getAllByTestId("nav-collapse-toggle")).toHaveLength(1);
  });
});

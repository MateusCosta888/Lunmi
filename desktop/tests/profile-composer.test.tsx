/**
 * Topo, rail, anexos e perfil (`desktop/tests/profile-composer.test.tsx`, 2026-10-15).
 *
 * Cobre os contratos do lote: cápsula LUNMI + modelo real (vazio nunca inventa nome), rail
 * recolhível sem tocar Parar/rascunho, Anexar antes do seletor com limites do contrato
 * (4/32KiB/64KiB, só no modo Conversar, seleção não envia, limpeza só após aceite, recusa
 * preserva tudo) e o modal de perfil (nome/foto, seam `ui.preferences.set` com `profile`,
 * Escape sem salvar, validação visível sem eco de conteúdo). Nenhum backend real.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

interface Options {
  model?: string;
  pickAttachments?: () => Promise<unknown>;
  deferSend?: boolean;
}

function makeFakeBridge(options: Options = {}) {
  const listeners = new Set<(event: Envelope) => void>();
  const calls: Array<{ type: string; payload: Record<string, unknown> }> = [];
  const deferred: Array<{ resolve: (v: unknown) => void; reject: (e: Error) => void }> = [];
  const model = options.model ?? "lunmi-balanced";
  const api = {
    hello: vi.fn(async () => sessionPayload("sess-pc")),
    notifyRendererReady: vi.fn(async () => ({ ok: true, rendererPaintedReadyMs: 1, windowShownMs: 1, windowReadyToShowMs: 1 })),
    sendCommand: vi.fn(async (type: string, payload: Record<string, unknown> = {}) => {
      calls.push({ type, payload });
      if (type === "chat.send" && options.deferSend) {
        return await new Promise((resolve, reject) => deferred.push({ resolve, reject }));
      }
      if (type === "brain.get") {
        return { current: { provider: "custom", provider_label: "Conexão local", model, env_overrides: { model: false } }, providers: [] };
      }
      if (type === "approval.get") return { auto_approve: false, mode: "manual", label: "Modo: Manual" };
      if (type === "ui.preferences.get") return { window: null, scale_pct: 100, actions: [], profile: { name: "Ana", avatar_data_url: "" } };
      if (type === "ui.preferences.set") return { window: null, scale_pct: 100, actions: [], profile: payload.profile ?? { name: "Ana", avatar_data_url: "" } };
      if (type === "memory.list") return { enabled: true, items: [] };
      if (type === "memory.proposals") return { proposals: [] };
      if (type === "memory.options.get") return { options: { enabled: true } };
      return { ok: true };
    }),
    pickLocation: vi.fn(async () => ({ path: "" })),
    restartBackend: vi.fn(async () => sessionPayload("sess-pc-2")),
    shutdown: vi.fn(async () => ({ status: "shutting_down", session_id: "sess-pc" })),
    onEvent: (callback: (event: Envelope) => void) => {
      listeners.add(callback);
      return () => listeners.delete(callback);
    },
    ...(options.pickAttachments ? { pickAttachments: options.pickAttachments } : {}),
  };
  return {
    api: api as unknown as LunmiBridgeApi,
    calls,
    emit: (event: Envelope) => { for (const listener of [...listeners]) listener(event); },
    settle: (index: number, error?: Error) => {
      const entry = deferred[index];
      if (!entry) throw new Error("sem envio adiado");
      if (error) entry.reject(error); else entry.resolve({ ok: true });
    },
  };
}

async function renderReady(bridge: ReturnType<typeof makeFakeBridge>) {
  (window as unknown as { lunmi: LunmiBridgeApi }).lunmi = bridge.api;
  render(<App />);
  await waitFor(() => expect(screen.getByTestId("session-status").textContent).toBe("Ready"));
  await new Promise((resolve) => setTimeout(resolve, 0));
}

beforeEach(() => {
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

describe("Topo — cápsula LUNMI + modelo real", () => {
  it("mostra o modelo real da sessão e nunca o rótulo do provedor", async () => {
    const bridge = makeFakeBridge({ model: "llama-3.1-lunmi" });
    await renderReady(bridge);
    expect(screen.getByTestId("topbar-model").textContent).toBe("llama-3.1-lunmi");
    expect(screen.getByTestId("brain-pill").textContent).not.toContain("Conexão local");
    expect(screen.getByTestId("brain-pill").textContent).toContain("LUNMI");
  });

  it("modelo vazio é honesto: 'Modelo padrão', sem nome inventado", async () => {
    const bridge = makeFakeBridge({ model: "" });
    await renderReady(bridge);
    expect(screen.getByTestId("topbar-model").textContent).toBe("Modelo padrão");
    expect(screen.getByTestId("brain-pill").getAttribute("title")).toContain("padrão");
  });
});

describe("Rail direito recolhível", () => {
  it("recolhe a uma faixa com botão de reabrir e mantém o compositor intacto", async () => {
    const bridge = makeFakeBridge();
    await renderReady(bridge);
    const collapse = screen.getByTestId("rail-collapse-toggle");
    expect(collapse.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(collapse);
    const expand = screen.getByTestId("rail-collapse-toggle");
    expect(expand.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Ações rápidas")).toBeNull();
    fireEvent.click(expand);
    expect(await screen.findByText("Ações rápidas")).toBeTruthy();
    expect(screen.getByTestId("composer")).toBeTruthy();
  });
});

describe("Anexos no compositor", () => {
  const picked = {
    cancelled: false,
    attachments: [
      { id: "a1", name: "notas.txt", size_bytes: 1200, sha256: "x", text: "conteúdo do arquivo" },
      { id: "a2", name: "rotina.py", size_bytes: 800, sha256: "y", text: "print(1)" },
    ],
    errors: ["relatorio.pdf: formato não suportado (somente texto/código UTF-8)."],
  };

  it("sem a API da ponte, Anexar fica desabilitado com motivo", async () => {
    const bridge = makeFakeBridge();
    await renderReady(bridge);
    const button = screen.getByTestId("attach-button") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.getAttribute("title")).toContain("não disponíveis");
  });

  it("escolher anexa sem enviar, mostra chips removíveis e o motivo do PDF recusado", async () => {
    const bridge = makeFakeBridge({ pickAttachments: vi.fn(async () => picked) });
    await renderReady(bridge);
    const button = screen.getByTestId("attach-button") as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    fireEvent.click(button);
    await waitFor(() => expect(screen.getByTestId("attachment-a1")).toBeTruthy());
    expect(screen.getByTestId("attachment-a2")).toBeTruthy();
    expect(screen.getByTestId("attachment-notice").textContent).toContain("relatorio.pdf");
    expect(screen.getByTestId("attachment-notice").textContent).toContain("formato não suportado");
    expect(bridge.calls.some((call) => call.type === "chat.send")).toBe(false);
    fireEvent.click(screen.getByTestId("attachment-remove-a2"));
    expect(screen.queryByTestId("attachment-a2")).toBeNull();
  });

  it("em Executar o envio fica bloqueado com aviso, sem trocar modo nem perder dados", async () => {
    const bridge = makeFakeBridge({ pickAttachments: vi.fn(async () => picked) });
    await renderReady(bridge);
    fireEvent.click(screen.getByTestId("attach-button"));
    await screen.findByTestId("attachment-a1");
    fireEvent.change(screen.getByTestId("composer"), { target: { value: "abra o app" } });
    expect((screen.getByTestId("send-task") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(screen.getByTestId("composer"), { key: "Enter" });
    expect(screen.getByTestId("attach-mode-notice")).toBeTruthy();
    expect(screen.getByTestId("attach-mode-notice").textContent).toContain("modo Conversar");
    expect(bridge.calls.some((call) => call.type === "chat.send")).toBe(false);
    expect((screen.getByTestId("composer") as HTMLTextAreaElement).value).toBe("abra o app");
    expect(screen.getByTestId("attachment-a1")).toBeTruthy();
  });

  it("no Conversar o payload leva anexos; limpeza só após aceite, recusa preserva tudo", async () => {
    const bridge = makeFakeBridge({ pickAttachments: vi.fn(async () => picked), deferSend: true });
    await renderReady(bridge);
    fireEvent.click(screen.getByTestId("attach-button"));
    await screen.findByTestId("attachment-a1");
    fireEvent.click(screen.getByTestId("mode-converse"));
    fireEvent.change(screen.getByTestId("composer"), { target: { value: "resuma" } });
    fireEvent.click(screen.getByTestId("send-task"));
    await waitFor(() => expect(bridge.calls.some((call) => call.type === "chat.send")).toBe(true));
    const send = bridge.calls.find((call) => call.type === "chat.send")!;
    expect(send.payload.mode).toBe("converse");
    expect(send.payload.text).toBe("resuma");
    expect(send.payload.attachments).toEqual([
      { name: "notas.txt", text: "conteúdo do arquivo", size_bytes: 1200 },
      { name: "rotina.py", text: "print(1)", size_bytes: 800 },
    ]);
    // Recusa: nada é perdido.
    bridge.settle(0, new Error("task_busy: Já existe uma tarefa em andamento."));
    await waitFor(() => expect(screen.getByTestId("chat-error")).toBeTruthy());
    expect((screen.getByTestId("composer") as HTMLTextAreaElement).value).toBe("resuma");
    expect(screen.getByTestId("attachment-a1")).toBeTruthy();
    // Aceite: rascunho e anexos saem juntos.
    fireEvent.click(screen.getByTestId("send-task"));
    await waitFor(() => expect(bridge.calls.filter((call) => call.type === "chat.send")).toHaveLength(2));
    bridge.settle(1);
    await waitFor(() => expect((screen.getByTestId("composer") as HTMLTextAreaElement).value).toBe(""));
    expect(screen.queryByTestId("attachment-a1")).toBeNull();
    bridge.emit({ v: 1, id: "fin", type: "task.finished", payload: { task_id: "t1", status: "SUCCESS", message: "ok" } });
  });

  it("limite de 4 é validado sobre o conjunto, com aviso explícito e sem descarte silencioso", async () => {
    const many = {
      cancelled: false,
      attachments: Array.from({ length: 6 }, (_, i) => ({ id: `m${i}`, name: `f${i}.txt`, size_bytes: 100, sha256: "z", text: "oi" })),
      errors: [],
    };
    const bridge = makeFakeBridge({ pickAttachments: vi.fn(async () => many) });
    await renderReady(bridge);
    fireEvent.click(screen.getByTestId("attach-button"));
    await waitFor(() => expect(screen.getByTestId("attachment-m3")).toBeTruthy());
    const chips = screen.getAllByTestId(/^attachment-m/);
    expect(chips).toHaveLength(4);
    expect(screen.getByTestId("attachment-notice").textContent).toContain("limite de 4");
    expect(screen.getByTestId("attachment-notice").textContent).toContain("f4.txt");
    expect((screen.getByTestId("attach-button") as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("Perfil — miniperfil e modal", () => {
  it("abre pelo rodapé, mostra o nome salvo e salva via ui.preferences com o DTO canônico", async () => {
    const bridge = makeFakeBridge();
    await renderReady(bridge);
    expect(screen.getByTestId("profile-button").textContent).toContain("Ana");
    fireEvent.click(screen.getByTestId("profile-button"));
    await screen.findByTestId("profile-modal");
    const nameInput = screen.getByTestId("profile-name-input") as HTMLInputElement;
    expect(document.activeElement).toBe(nameInput);
    fireEvent.change(nameInput, { target: { value: "  Ana Beatriz  " } });
    fireEvent.click(screen.getByTestId("profile-save"));
    await waitFor(() => {
      const call = [...bridge.calls].reverse().find((c) => c.type === "ui.preferences.set" && "profile" in c.payload);
      expect(call?.payload.profile).toEqual({ name: "Ana Beatriz", avatar_data_url: "" });
    });
    await waitFor(() => expect(screen.queryByTestId("profile-modal")).toBeNull());
    expect(screen.getByTestId("profile-button").textContent).toContain("Ana Beatriz");
  });

  it("Escape fecha sem salvar; nome longo demais vira erro visível sem enviar", async () => {
    const bridge = makeFakeBridge();
    await renderReady(bridge);
    fireEvent.click(screen.getByTestId("profile-button"));
    await screen.findByTestId("profile-modal");
    fireEvent.change(screen.getByTestId("profile-name-input"), { target: { value: "x".repeat(60) } });
    fireEvent.click(screen.getByTestId("profile-save"));
    expect(screen.getByTestId("profile-error").textContent).toContain("40");
    expect(bridge.calls.some((call) => call.type === "ui.preferences.set" && "profile" in call.payload)).toBe(false);
    fireEvent.keyDown(screen.getByTestId("profile-modal"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("profile-modal")).toBeNull());
  });

  it("foto inválida é recusada sem eco de conteúdo e sem fallback bruto", async () => {
    const bridge = makeFakeBridge();
    await renderReady(bridge);
    fireEvent.click(screen.getByTestId("profile-button"));
    await screen.findByTestId("profile-modal");
    const input = screen.getByTestId("profile-photo-input") as HTMLInputElement;
    const file = new File([new Uint8Array([1, 2, 3])], "corrompido.png", { type: "image/png" });
    await act(async () => {
      fireEvent.change(input, { target: { files: [file] } });
    });
    await waitFor(() => expect(screen.getByTestId("profile-error")).toBeTruthy(), { timeout: 4000 });
    expect(screen.getByTestId("profile-error").textContent).toContain("PNG/JPEG");
    expect(screen.getByTestId("profile-error").textContent).not.toContain("corrompido");
  });

  it("resposta sem perfil canônico é erro, não persistência fingida", async () => {
    const bridge = makeFakeBridge();
    const original = (bridge.api.sendCommand as unknown as { getMockImplementation: () => (t: string, p?: Record<string, unknown>) => Promise<unknown> }).getMockImplementation()!;
    (bridge.api.sendCommand as unknown as { mockImplementation: (fn: unknown) => void }).mockImplementation(
      async (type: string, payload: Record<string, unknown> = {}) => {
        const result = await original(type, payload);
        if (type === "ui.preferences.set" && "profile" in payload) return { window: null, scale_pct: 100, actions: [] };
        return result;
      }
    );
    await renderReady(bridge);
    fireEvent.click(screen.getByTestId("profile-button"));
    await screen.findByTestId("profile-modal");
    fireEvent.change(screen.getByTestId("profile-name-input"), { target: { value: "Bia" } });
    fireEvent.click(screen.getByTestId("profile-save"));
    await waitFor(() => expect(screen.getByTestId("profile-error").textContent).toContain("não confirmou"));
    expect(screen.getByTestId("profile-modal")).toBeTruthy();
    expect(screen.getByTestId("profile-button").textContent).toContain("Ana");
  });

  it("durante a gravação despachada, fechar/Escape/Cancelar ficam travados", async () => {
    const bridge = makeFakeBridge();
    let releaseSave!: () => void;
    const original = (bridge.api.sendCommand as unknown as { getMockImplementation: () => (t: string, p?: Record<string, unknown>) => Promise<unknown> }).getMockImplementation()!;
    (bridge.api.sendCommand as unknown as { mockImplementation: (fn: unknown) => void }).mockImplementation(
      async (type: string, payload: Record<string, unknown> = {}) => {
        if (type === "ui.preferences.set" && "profile" in payload) {
          await new Promise<void>((resolve) => { releaseSave = resolve; });
        }
        return original(type, payload);
      }
    );
    await renderReady(bridge);
    fireEvent.click(screen.getByTestId("profile-button"));
    await screen.findByTestId("profile-modal");
    fireEvent.change(screen.getByTestId("profile-name-input"), { target: { value: "Lia" } });
    fireEvent.click(screen.getByTestId("profile-save"));
    await waitFor(() => expect((screen.getByTestId("profile-close") as HTMLButtonElement).disabled).toBe(true));
    expect((screen.getByTestId("profile-cancel") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(screen.getByTestId("profile-modal"), { key: "Escape" });
    expect(screen.getByTestId("profile-modal")).toBeTruthy();
    await act(async () => { releaseSave(); });
    await waitFor(() => expect(screen.queryByTestId("profile-modal")).toBeNull());
    expect(screen.getByTestId("profile-button").textContent).toContain("Lia");
  });

  it("falha de persistência mantém o modal aberto com erro, sem fingir salvamento", async () => {
    const bridge = makeFakeBridge();
    const failing = async (type: string, payload: Record<string, unknown> = {}): Promise<unknown> => {
      bridge.calls.push({ type, payload });
      if (type === "ui.preferences.set" && "profile" in payload) throw new Error("io_error: Sem acesso ao arquivo");
      if (type === "brain.get") return { current: { provider: "custom", provider_label: "Conexão local", model: "m", env_overrides: {} }, providers: [] };
      if (type === "approval.get") return { auto_approve: false, mode: "manual", label: "Modo: Manual" };
      if (type === "ui.preferences.get") return { window: null, scale_pct: 100, actions: [], profile: { name: "", avatar_data_url: "" } };
      if (type === "memory.list") return { enabled: true, items: [] };
      return { ok: true };
    };
    (bridge.api.sendCommand as unknown as { mockImplementation: (fn: unknown) => void }).mockImplementation(failing);
    await renderReady(bridge);
    fireEvent.click(screen.getByTestId("profile-button"));
    await screen.findByTestId("profile-modal");
    fireEvent.change(screen.getByTestId("profile-name-input"), { target: { value: "Rafa" } });
    fireEvent.click(screen.getByTestId("profile-save"));
    await waitFor(() => expect(screen.getByTestId("profile-error").textContent).toContain("Não foi possível"));
    expect(screen.getByTestId("profile-modal")).toBeTruthy();
  });
});

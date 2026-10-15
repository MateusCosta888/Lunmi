import { expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App } from "../src/App";
import type { LunmiBridgeApi } from "../src/shared/ipc/client";
import type { BrainStateDto } from "../src/shared/types";
import type { Envelope } from "../src/shared/protocol";

function setup({ fixed = false, unavailable = false, defer = false } = {}) {
  let current = { provider: "custom", provider_label: "Conexão local", model: "balanced", app_provider: "separate", app_model: "executor", has_api_key: true, env_overrides: { model: fixed } } as BrainStateDto;
  let settle!: () => void, reject!: () => void;
  const listeners = new Set<(event: Envelope) => void>();
  const api = {
    hello: vi.fn(async () => ({ session_id: "composer-test", capabilities: { brain: true, memory: true, execute: true, converse: true } })),
    onEvent: (callback: (event: Envelope) => void) => { listeners.add(callback); return () => { listeners.delete(callback); }; },
    restartBackend: vi.fn(async () => ({ session_id: "composer-restarted", capabilities: { brain: true, memory: true, execute: true, converse: true } })),
    notifyRendererReady: vi.fn(async () => ({})),
    sendCommand: vi.fn(async (type: string, payload: Record<string, unknown>): Promise<unknown> => {
      if (type === "brain.get") return { current, providers: [{ id: "custom", label: "Conexão local" }] };
      if (type === "brain.list") return { provider: "custom", ok: !unavailable, models: [{ id: "balanced", label: "Balanced" }, { id: "fast", label: "Fast" }] };
      if (type === "brain.set") {
        if (defer) await new Promise<void>((resolve, fail) => { settle = resolve; reject = () => fail(new Error("private driver details")); });
        current = { ...current, model: payload.model as string }; return current;
      }
      if (type === "memory.list") return { enabled: true, items: [] };
      if (type === "ui.preferences.get") return { scale_pct: 200, actions: [] };
      if (type === "approval.get") return { auto_approve: false };
      return {};
    }),
  };
  window.lunmi = api as unknown as LunmiBridgeApi;
  render(<App />);
  return { api, settle: () => settle(), reject: () => reject(), emit: (event: Envelope) => { for (const listener of listeners) listener(event); } };
}

it("lista só os modelos do provedor atual e troca sem apagar rascunho ou enviar tarefa", async () => {
  const { api } = setup();
  await screen.findByTestId("composer");
  fireEvent.change(screen.getByTestId("composer"), { target: { value: "Meu rascunho" } });
  fireEvent.click(screen.getByTestId("composer-model"));
  await screen.findByRole("option", { name: "Fast" });
  expect(screen.queryByTestId("brain-panel")).toBeNull();
  expect(api.sendCommand).toHaveBeenCalledWith("brain.list", { provider: "custom", role: "main", force: false });
  fireEvent.click(screen.getByRole("option", { name: "Fast" }));
  await waitFor(() => expect(screen.getByTestId("composer-model").textContent).toContain("fast"));
  expect(api.sendCommand).toHaveBeenCalledWith("brain.set", { model: "fast" });
  expect((screen.getByTestId("composer") as HTMLTextAreaElement).value).toBe("Meu rascunho");
  expect(api.sendCommand.mock.calls.some(([type]) => type === "chat.send")).toBe(false);
});

it("contador usa o limite real e o rótulo do envio acompanha o modo sem enviar sozinho", async () => {
  const { api } = setup(); await screen.findByTestId("composer");
  expect(screen.getByTestId("composer-counter").textContent).toBe("0/4000");
  fireEvent.change(screen.getByTestId("composer"), { target: { value: "Uma ideia" } });
  expect(screen.getByTestId("composer-counter").textContent).toBe("9/4000");
  expect(screen.getByTestId("send-task").textContent).toBe("Executar");
  fireEvent.click(screen.getByTestId("mode-converse"));
  expect(screen.getByTestId("send-task").textContent).toBe("Enviar");
  expect((screen.getByTestId("composer") as HTMLTextAreaElement).value).toBe("Uma ideia");
  expect(api.sendCommand.mock.calls.some(([type]) => type === "chat.send")).toBe(false);
});

it("bloqueia envio durante troca, permite rascunho e mantém modelo após falha", async () => {
  const fake = setup({ defer: true });
  await screen.findByTestId("composer");
  fireEvent.click(screen.getByTestId("composer-model"));
  fireEvent.click(await screen.findByRole("option", { name: "Fast" }));
  expect((screen.getByTestId("send-task") as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByTestId("header-auto-approve") as HTMLInputElement).disabled).toBe(true);
  fireEvent.change(screen.getByTestId("composer"), { target: { value: "Novo rascunho" } });
  fireEvent.keyDown(screen.getByTestId("composer"), { key: "Enter" });
  expect(fake.api.sendCommand.mock.calls.some(([type]) => type === "chat.send")).toBe(false);
  fake.reject();
  await screen.findByRole("alert");
  expect(document.body.textContent).not.toContain("private driver details");
  expect(screen.getByTestId("composer-model").textContent).toContain("balanced");
  expect((screen.getByTestId("composer") as HTMLTextAreaElement).value).toBe("Novo rascunho");
});

it("busca/teclado operam no seletor sem Enter enviar a conversa", async () => {
  const { api } = setup(); await screen.findByTestId("composer");
  fireEvent.click(screen.getByTestId("composer-model"));
  await screen.findByRole("option", { name: "Fast" });
  const search = screen.getByRole("textbox", { name: "Buscar modelos" });
  fireEvent.keyDown(search, { key: "ArrowUp" });
  expect(document.activeElement).toBe(screen.getByRole("option", { name: "Fast" }));
  search.focus();
  fireEvent.change(search, { target: { value: "fas" } });
  expect(screen.queryByRole("option", { name: /Balanced/ })).toBeNull();
  expect(fireEvent.keyDown(search, { key: "Enter" })).toBe(false);
  fireEvent.keyDown(search, { key: "ArrowDown" });
  expect(document.activeElement).toBe(screen.getByRole("option", { name: "Fast" }));
  fireEvent.keyDown(document.activeElement!, { key: "Escape" });
  expect(screen.queryByTestId("model-menu")).toBeNull();
  expect(document.activeElement).toBe(screen.getByTestId("composer-model"));
  expect(api.sendCommand.mock.calls.some(([type]) => type === "chat.send")).toBe(false);
});

it("só o atalho de provedor abre o Brain, com rascunho preservado", async () => {
  const { api } = setup(); await screen.findByTestId("composer");
  fireEvent.change(screen.getByTestId("composer"), { target: { value: "Continuar depois" } });
  fireEvent.click(screen.getByTestId("composer-model"));
  await screen.findByRole("option", { name: "Fast" });
  fireEvent.click(screen.getByRole("button", { name: /Configurar ou trocar provedor/ }));
  await screen.findByTestId("brain-panel");
  fireEvent.click(screen.getByTestId("nav-chat"));
  expect((screen.getByTestId("composer") as HTMLTextAreaElement).value).toBe("Continuar depois");
  expect(api.sendCommand.mock.calls.some(([type]) => type === "brain.set")).toBe(false);
});

it("não troca modelo fixado por ambiente", async () => {
  const { api } = setup({ fixed: true }); await screen.findByTestId("composer");
  fireEvent.click(screen.getByTestId("composer-model"));
  const option = await screen.findByRole("option", { name: "Fast" });
  expect((option as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(option);
  expect(api.sendCommand.mock.calls.some(([type]) => type === "brain.set")).toBe(false);
  expect(screen.getByText(/definido pela variável/)).toBeTruthy();
});

it("não apresenta catálogo recusado como modelos disponíveis", async () => {
  setup({ unavailable: true }); await screen.findByTestId("composer");
  fireEvent.click(screen.getByTestId("composer-model"));
  await screen.findByText(/não confirmou a lista/);
  expect(screen.queryByRole("option")).toBeNull();
});

it("fecha o catálogo antigo e aguarda controles da sessão nova após reiniciar", async () => {
  const fake = setup(); await screen.findByTestId("composer");
  fireEvent.click(screen.getByTestId("composer-model"));
  await screen.findByRole("option", { name: "Fast" });
  await act(async () => { fake.emit({ v: 1, id: "exited", type: "task.error", payload: { code: "backend_exited", message: "Stopped" } }); });
  expect(screen.queryByTestId("model-menu")).toBeNull();
  let resolve!: (value: unknown) => void;
  const original = fake.api.sendCommand.getMockImplementation()!;
  fake.api.sendCommand.mockImplementation(async (type, payload) => type === "brain.get" ? new Promise(r => { resolve = r; }) : original(type, payload));
  fireEvent.click(screen.getByTestId("restart-backend"));
  await waitFor(() => expect(resolve).toBeTypeOf("function"));
  expect((screen.getByTestId("composer-model") as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByTestId("composer-model"));
  expect(screen.queryByTestId("model-menu")).toBeNull();
  await act(async () => { resolve({ current: { provider: "other", provider_label: "Outro", model: "new-model", env_overrides: {} }, providers: [] }); });
  await waitFor(() => expect((screen.getByTestId("composer-model") as HTMLButtonElement).disabled).toBe(false));
  expect(screen.getByTestId("composer-model").textContent).toContain("new-model");
  expect(fake.api.sendCommand.mock.calls.filter(([type]) => type === "brain.list")).toHaveLength(1);
  expect(fake.api.sendCommand.mock.calls.some(([type]) => type === "brain.set")).toBe(false);
});

it("bloqueia a seleção durante a saída visível e só desmonta o menu no fim do exit", async () => {
  // Movimento reduzido é o padrão dos testes; aqui desligo ao vivo para exercitar a saída animada.
  let reduced = true;
  const handlers = new Set<(event: { matches: boolean }) => void>();
  vi.stubGlobal("matchMedia", (query: string) => ({
    get matches() { return query.includes("prefers-reduced-motion") ? reduced : false; },
    media: query,
    onchange: null,
    addEventListener: (_: string, cb: (event: { matches: boolean }) => void) => { if (query.includes("prefers-reduced-motion")) handlers.add(cb); },
    removeEventListener: (_: string, cb: (event: { matches: boolean }) => void) => { handlers.delete(cb); },
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }) as unknown as MediaQueryList);

  const fake = setup();
  await screen.findByTestId("composer");
  await act(async () => { reduced = false; for (const cb of [...handlers]) cb({ matches: false }); });

  let finishExit!: () => void;
  const prototype = Element.prototype as unknown as { animate?: unknown };
  const originalAnimate = prototype.animate;
  prototype.animate = () => ({
    finished: new Promise<void>((resolve) => { finishExit = resolve; }),
    cancel: vi.fn(),
  }) as unknown as Animation;

  fireEvent.click(screen.getByTestId("composer-model"));
  await screen.findByRole("option", { name: "Fast" });
  fireEvent.keyDown(screen.getByTestId("model-menu"), { key: "Escape" });

  expect(screen.getByTestId("model-menu").getAttribute("data-state")).toBe("closing");
  const option = screen.getByRole("option", { name: "Fast" }) as HTMLButtonElement;
  expect(option.disabled).toBe(true);
  fireEvent.click(option);
  expect(fake.api.sendCommand.mock.calls.some(([type]) => type === "brain.set")).toBe(false);
  expect(document.activeElement).toBe(screen.getByTestId("composer-model"));

  await act(async () => { finishExit(); });
  await waitFor(() => expect(screen.queryByTestId("model-menu")).toBeNull());
  prototype.animate = originalAnimate;
});

it("reabrir durante a saída cancela o exit sem menu órfão", async () => {
  let reduced = true;
  const handlers = new Set<(event: { matches: boolean }) => void>();
  vi.stubGlobal("matchMedia", (query: string) => ({
    get matches() { return query.includes("prefers-reduced-motion") ? reduced : false; },
    media: query,
    onchange: null,
    addEventListener: (_: string, cb: (event: { matches: boolean }) => void) => { if (query.includes("prefers-reduced-motion")) handlers.add(cb); },
    removeEventListener: (_: string, cb: (event: { matches: boolean }) => void) => { handlers.delete(cb); },
    addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
  }) as unknown as MediaQueryList);
  const fake = setup();
  await screen.findByTestId("composer");
  await act(async () => { reduced = false; for (const cb of [...handlers]) cb({ matches: false }); });

  let finishExit!: () => void;
  const cancel = vi.fn(() => finishExit());
  const prototype = Element.prototype as unknown as { animate?: unknown };
  const originalAnimate = prototype.animate;
  prototype.animate = () => ({
    finished: new Promise<void>((resolve) => { finishExit = resolve; }), cancel,
  }) as unknown as Animation;

  fireEvent.click(screen.getByTestId("composer-model"));
  await screen.findByRole("option", { name: "Fast" });
  fireEvent.keyDown(screen.getByTestId("model-menu"), { key: "Escape" });
  expect(screen.getByTestId("model-menu").getAttribute("data-state")).toBe("closing");

  fireEvent.click(screen.getByTestId("composer-model"));
  expect(cancel).toHaveBeenCalled();
  expect(screen.getByTestId("model-menu").getAttribute("data-state")).toBe("open");
  await waitFor(() => expect(fake.api.sendCommand.mock.calls.filter(([type]) => type === "brain.list").length).toBe(2));
  fireEvent.keyDown(screen.getByTestId("model-menu"), { key: "Escape" });
  await waitFor(() => expect(screen.getByTestId("model-menu").getAttribute("data-state")).toBe("closing"));
  await act(async () => { finishExit(); });
  await waitFor(() => expect(screen.queryByTestId("model-menu")).toBeNull());
  prototype.animate = originalAnimate;
});

/**
 * Regressão: uma troca de cérebro que termina depois da resposta do comando (ou que falha na
 * gravação) precisa ficar visível ao testador.
 *
 * O backend publica o estado final em `brain.updated` com `ok`/`persisted`/`error`. Antes,
 * nenhum componente consumia esse evento e o seletor de modelo afirmava "a seleção anterior foi
 * mantida" mesmo quando o commit continuava a correr e aplicava o modelo novo.
 */
import { expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App } from "../src/App";
import type { LunmiBridgeApi } from "../src/shared/ipc/client";
import type { BrainStateDto } from "../src/shared/types";
import type { Envelope } from "../src/shared/protocol";

interface SetupOptions {
  /** Resposta de `brain.set`, ou o erro a lançar. */
  setResult?: BrainStateDto | Error;
}

function setup({ setResult }: SetupOptions = {}) {
  const base = {
    provider: "custom",
    provider_label: "Conexão local",
    model: "balanced",
    base_url: "",
    has_api_key: true,
    app_provider: "",
    app_model: "",
    effort: "",
    env_overrides: { provider: false, model: false, app_provider: false, app_model: false },
  } as BrainStateDto;
  let current: BrainStateDto = { ...base };
  const listeners = new Set<(event: Envelope) => void>();
  const api = {
    hello: vi.fn(async () => ({
      session_id: "brain-reconfigure",
      capabilities: { brain: true, memory: true, execute: true, converse: true },
    })),
    onEvent: (callback: (event: Envelope) => void) => {
      listeners.add(callback);
      return () => { listeners.delete(callback); };
    },
    restartBackend: vi.fn(async () => ({
      session_id: "brain-reconfigure-2",
      capabilities: { brain: true, memory: true, execute: true, converse: true },
    })),
    notifyRendererReady: vi.fn(async () => ({})),
    sendCommand: vi.fn(async (type: string, payload: Record<string, unknown>): Promise<unknown> => {
      if (type === "brain.get") {
        return { current, providers: [{ id: "custom", label: "Conexão local", kind: "model_api", location: "local_endpoint", requires_api_key: false, supports_model_discovery: true, supports_interactive_login: false }] };
      }
      if (type === "brain.list") {
        current = { ...current, model: (payload.model as string) ?? current.model };
        return { provider: "custom", ok: true, models: [{ id: "balanced", label: "Balanced" }, { id: "fast", label: "Fast" }] };
      }
      if (type === "brain.set") {
        if (setResult instanceof Error) throw setResult;
        current = setResult ?? { ...current, model: payload.model as string };
        return current;
      }
      if (type === "memory.list") return { enabled: true, items: [] };
      if (type === "memory.profile.get") return { enabled: true, auto_learn: true, text: "", revision: 0, can_undo: false, char_count: 0, max_chars: 8000, state: "empty" };
      if (type === "ui.preferences.get") return { scale_pct: 100, actions: [] };
      if (type === "approval.get") return { auto_approve: false };
      return {};
    }),
  };
  window.lunmi = api as unknown as LunmiBridgeApi;
  render(<App />);
  return {
    api,
    // O backend é a fonte da verdade: um evento publica o estado e a releitura seguinte
    // (`brain.get`) enxerga o mesmo valor, como no produto real.
    setCurrent: (next: BrainStateDto) => { current = next; },
    emit: (event: Envelope) => { for (const listener of listeners) listener(event); },
  };
}

function stateEvent(payload: Partial<BrainStateDto>): Envelope {
  return { v: 1, id: "evt-brain", type: "brain.updated", payload: payload as Record<string, unknown> };
}

const FULL_OVERRIDES = { provider: false, model: false, app_provider: false, app_model: false };

it("não afirma que a seleção anterior foi mantida quando a troca ainda está em andamento", async () => {
  setup({ setResult: new Error("reconfiguring: A troca continua em andamento.") });
  await screen.findByTestId("composer");
  fireEvent.click(screen.getByTestId("composer-model"));
  fireEvent.click(await screen.findByRole("option", { name: "Fast" }));

  const alert = await screen.findByRole("alert");
  expect(alert.textContent).toMatch(/continua em andamento/);
  expect(document.body.textContent).not.toContain("A seleção anterior foi mantida");
  // O rótulo continua no modelo atual: o estado real chega pelo evento, não por um palpite.
  expect(screen.getByTestId("composer-model").textContent).toContain("balanced");
});

it("uma falha de persistência publicada no evento vira aviso visível e aplica o modelo", async () => {
  const fake = setup();
  await screen.findByTestId("composer");
  const published = {
    provider: "custom", provider_label: "Conexão local", model: "fast", base_url: "",
    has_api_key: true, app_provider: "", app_model: "", effort: "", env_overrides: FULL_OVERRIDES,
    ok: true, persisted: false,
    error: { stage: "persist", code: "PermissionError", message: "Não foi possível gravar a seleção." },
  } as BrainStateDto;
  fake.setCurrent(published);
  await act(async () => { fake.emit(stateEvent(published)); });
  const notice = await screen.findByTestId("brain-notice");
  expect(notice.textContent).toMatch(/não foi gravada/);
  await waitFor(() => expect(screen.getByTestId("composer-model").textContent).toContain("fast"));
});

it("uma falha do commit publicada no evento não muda o modelo exibido", async () => {
  const fake = setup();
  await screen.findByTestId("composer");
  await act(async () => {
    fake.emit(stateEvent({
      provider: "custom", provider_label: "Conexão local", model: "old", base_url: "",
      has_api_key: true, app_provider: "", app_model: "", effort: "", env_overrides: FULL_OVERRIDES,
      ok: false, persisted: false,
      error: { stage: "reconfigure", code: "RuntimeError", message: "Falha ao aplicar o novo cérebro." },
    }));
  });
  const notice = await screen.findByTestId("brain-notice");
  expect(notice.textContent).toMatch(/O cérebro não mudou/);
  expect(notice.textContent).toMatch(/reconfigure/);
});

it("o painel do Brain diz que a seleção não foi gravada em vez de 'Cérebro aplicado'", async () => {
  const fake = setup({
    setResult: {
      provider: "custom", provider_label: "Conexão local", model: "balanced", base_url: "",
      has_api_key: true, app_provider: "", app_model: "", effort: "",
      env_overrides: { provider: false, model: false, app_provider: false, app_model: false },
      ok: true, persisted: false,
      error: { stage: "persist", code: "PermissionError", message: "Não foi possível gravar a seleção." },
    },
  });
  await screen.findByTestId("composer");
  fireEvent.click(screen.getByTestId("nav-brain"));
  await screen.findByTestId("brain-panel");
  await waitFor(() => expect(screen.getByTestId("provider-select-btn-custom").getAttribute("aria-pressed")).toBe("true"));
  fireEvent.click(screen.getByTestId("apply-brain"));
  const status = await screen.findByTestId("brain-status");
  expect(status.textContent).toMatch(/não foi gravada/);
  expect(status.textContent).not.toContain("Cérebro aplicado.");
  expect(fake.api.sendCommand.mock.calls.some(([type]) => type === "brain.set")).toBe(true);
});

it("uma troca lenta no painel do Brain anuncia andamento, sem erro falso", async () => {
  setup({ setResult: new Error("reconfiguring: A troca continua em andamento.") });
  await screen.findByTestId("composer");
  fireEvent.click(screen.getByTestId("nav-brain"));
  await screen.findByTestId("brain-panel");
  await waitFor(() => expect(screen.getByTestId("provider-select-btn-custom").getAttribute("aria-pressed")).toBe("true"));
  fireEvent.click(screen.getByTestId("apply-brain"));
  const status = await screen.findByTestId("brain-status");
  expect(status.getAttribute("role")).toBe("status");
  expect(status.textContent).toMatch(/continua em andamento/);
});

function finalState(model: string, extra: Partial<BrainStateDto>): BrainStateDto {
  return {
    provider: "custom", provider_label: "Conexão local", model, base_url: "",
    has_api_key: true, app_provider: "", app_model: "", effort: "",
    env_overrides: FULL_OVERRIDES, ...extra,
  } as BrainStateDto;
}

it("a sequência completa (timeout e depois evento) limpa o aviso no seletor e aplica o modelo final", async () => {
  const fake = setup({ setResult: new Error("reconfiguring: A troca continua em andamento.") });
  await screen.findByTestId("composer");
  fireEvent.click(screen.getByTestId("composer-model"));
  fireEvent.click(await screen.findByRole("option", { name: "Fast" }));
  expect((await screen.findByRole("alert")).textContent).toMatch(/continua em andamento/);

  const published = finalState("fast", { ok: true, persisted: true });
  fake.setCurrent(published);
  await act(async () => { fake.emit(stateEvent(published)); });
  // O aviso de andamento sai sem exigir nova consulta nem navegação, e o rótulo acompanha.
  await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  expect(screen.getByTestId("composer-model").textContent).toContain("fast");
});

it("o painel do Brain acompanha o evento final com o painel aberto, sem exigir navegar", async () => {
  const fake = setup({ setResult: new Error("reconfiguring: A troca continua em andamento.") });
  await screen.findByTestId("composer");
  fireEvent.click(screen.getByTestId("nav-brain"));
  await screen.findByTestId("brain-panel");
  await waitFor(() => expect(screen.getByTestId("provider-select-btn-custom").getAttribute("aria-pressed")).toBe("true"));
  fireEvent.click(screen.getByTestId("apply-brain"));
  expect((await screen.findByTestId("brain-status")).textContent).toMatch(/continua em andamento/);

  // Evento final de gravação falha: o "em andamento" sai e o motivo real aparece.
  const failedPersist = finalState("fast", {
    ok: true, persisted: false,
    error: { stage: "persist", code: "PermissionError", message: "Não foi possível gravar a seleção." },
  });
  fake.setCurrent(failedPersist);
  await act(async () => { fake.emit(stateEvent(failedPersist)); });
  const status = await screen.findByTestId("brain-status");
  expect(status.textContent).toMatch(/não foi gravada/);
  expect(status.textContent).not.toMatch(/continua em andamento/);
  await waitFor(() => expect(screen.getByTestId("current-brain").textContent).toContain("fast"));
});

it("o painel do Brain mostra a falha do commit publicada no evento, sem ficar em andamento", async () => {
  const fake = setup({ setResult: new Error("reconfiguring: A troca continua em andamento.") });
  await screen.findByTestId("composer");
  fireEvent.click(screen.getByTestId("nav-brain"));
  await screen.findByTestId("brain-panel");
  await waitFor(() => expect(screen.getByTestId("provider-select-btn-custom").getAttribute("aria-pressed")).toBe("true"));
  fireEvent.click(screen.getByTestId("apply-brain"));
  expect((await screen.findByTestId("brain-status")).textContent).toMatch(/continua em andamento/);

  const failedCommit = finalState("balanced", {
    ok: false, persisted: false,
    error: { stage: "reconfigure", code: "RuntimeError", message: "Falha ao aplicar o novo cérebro." },
  });
  await act(async () => { fake.emit(stateEvent(failedCommit)); });
  const status = await screen.findByTestId("brain-status");
  expect(status.getAttribute("role")).toBe("alert");
  expect(status.textContent).toMatch(/Falha ao aplicar o novo cérebro/);
  expect(status.textContent).toMatch(/reconfigure/);
  expect(status.textContent).not.toMatch(/continua em andamento/);
  // O painel não pode continuar mostrando um cérebro que não foi aplicado.
  expect(screen.getByTestId("current-brain").textContent).toContain("balanced");
});

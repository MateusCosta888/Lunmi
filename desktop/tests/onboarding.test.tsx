/**
 * Introdução da primeira abertura (`src/features/onboarding/Onboarding.tsx`).
 *
 * Sem Electron: `window.lunmi` é uma ponte espiã. O que importa provar é o plug and play — a
 * chave Groq copiada no browser liga o cérebro sem a pessoa colar nada —, que adiar nunca prende
 * ninguém e que a introdução só aparece a quem ainda não tem cérebro.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { needsOnboarding, Onboarding } from "../src/features/onboarding/Onboarding";
import { groqKeyFrom, groqKeyWatch } from "../electron/main";
import type { BrainGetDto, UiPreferencesDto } from "../src/shared/types";

const KEY = "gsk_" + "a".repeat(52);

function brain(provider: string, hasKey: boolean, providers: Partial<BrainGetDto["providers"][number]>[]): BrainGetDto {
  return {
    current: { provider, has_api_key: hasKey } as BrainGetDto["current"],
    providers: providers.map(p => ({ id: "", label: "", kind: "", location: "", requires_api_key: false,
      supports_model_discovery: false, supports_interactive_login: false, ...p })),
  };
}

const prefs = (done = false) => ({ window: null, scale_pct: 100, actions: [], onboarding_done: done }) as UiPreferencesDto;

describe("needsOnboarding", () => {
  it("aparece só a quem ainda não tem cérebro", () => {
    const openai = [{ id: "openai", requires_api_key: true }];
    expect(needsOnboarding(prefs(), brain("openai", false, openai))).toBe(true);
    expect(needsOnboarding(prefs(), brain("openai", true, openai))).toBe(false);
    expect(needsOnboarding(prefs(true), brain("openai", false, openai))).toBe(false);
    expect(needsOnboarding(prefs(), brain("kiro", false, [{ id: "kiro", installed: true }]))).toBe(false);
    expect(needsOnboarding(prefs(), brain("kiro", false, [{ id: "kiro", installed: false }]))).toBe(true);
    expect(needsOnboarding(null, brain("openai", false, openai))).toBe(false);
    // Backend antigo, sem o campo: nunca abre a introdução.
    expect(needsOnboarding({ window: null, scale_pct: 100, actions: [] }, brain("openai", false, openai))).toBe(false);
  });
});

describe("groqKeyFrom", () => {
  it("só deixa sair da área de transferência uma chave Groq", () => {
    expect(groqKeyFrom(`  ${KEY}\n`)).toBe(KEY);
    expect(groqKeyFrom("a minha palavra-passe secreta")).toBeNull();
    expect(groqKeyFrom(`${KEY} e mais texto`)).toBeNull();
    expect(groqKeyFrom("gsk_curta")).toBeNull();
  });
});

describe("groqKeyWatch", () => {
  it("só lê depois de Abrir Groq, durante 10 min, e para na primeira chave", () => {
    groqKeyWatch.until = 0;
    expect(groqKeyWatch.take(KEY, 1000)).toBeNull();
    groqKeyWatch.open(1000);
    expect(groqKeyWatch.take("texto qualquer", 2000)).toBeNull();
    expect(groqKeyWatch.active(2000)).toBe(true);
    expect(groqKeyWatch.take(KEY, 3000)).toBe(KEY);
    expect(groqKeyWatch.take(KEY, 4000)).toBeNull();
    groqKeyWatch.open(0);
    expect(groqKeyWatch.take(KEY, 10 * 60 * 1000 + 1)).toBeNull();
  });
});

describe("Onboarding", () => {
  afterEach(() => { delete (window as { lunmi?: unknown }).lunmi; vi.useRealTimers(); });

  function bridge(clipboard: () => string | null) {
    const open = vi.fn(async () => ({ opened: true }));
    (window as { lunmi?: unknown }).lunmi = {
      onboarding: { open, groqKey: async () => ({ key: clipboard() }) },
      onEvent: () => () => undefined,
    };
    return { open };
  }

  it("Groq: a chave copiada no browser liga o cérebro sem colar nada", async () => {
    let copied: string | null = null;
    const { open } = bridge(() => copied);
    const calls: Array<[string, Record<string, unknown>]> = [];
    const sendCommand = vi.fn(async (type: string, payload: Record<string, unknown> = {}) => {
      calls.push([type, payload]);
      if (type === "brain.list") return { ok: true, detail: "", models: [
        { id: "llama-3.1-8b-instant", label: "", default: false },
        { id: "openai/gpt-oss-120b", label: "", default: true }] };
      if (type === "brain.set") return { ok: true };
      return {};
    });
    const onDone = vi.fn();
    render(<Onboarding sendCommand={sendCommand as never} onDone={onDone} />);
    fireEvent.click(screen.getByTestId("onboarding-start"));
    fireEvent.click(screen.getByTestId("onboarding-groq"));
    fireEvent.click(screen.getByTestId("onboarding-groq-open"));
    expect(open).toHaveBeenCalledWith("groq-keys");

    copied = KEY;
    fireEvent.focus(window);
    await screen.findByTestId("onboarding-finish");
    expect(calls.find(([type]) => type === "brain.list")?.[1]).toMatchObject({ provider: "groq", api_key: KEY, force: true });
    const set = calls.find(([type]) => type === "brain.set")?.[1];
    expect(set).toMatchObject({ provider: "groq", model: "openai/gpt-oss-120b", api_key: KEY, remember_credentials: true });

    fireEvent.click(screen.getByTestId("onboarding-finish"));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("chat"));
    expect(calls).toContainEqual(["ui.preferences.set", { onboarding_done: true }]);
  });

  it("Groq: uma chave recusada mostra o motivo e não aplica nada", async () => {
    bridge(() => null);
    const sendCommand = vi.fn(async (type: string) => type === "brain.list"
      ? { ok: false, detail: "Chave inválida.", models: [] } : {});
    render(<Onboarding sendCommand={sendCommand as never} onDone={vi.fn()} />);
    fireEvent.click(screen.getByTestId("onboarding-start"));
    fireEvent.click(screen.getByTestId("onboarding-groq"));
    fireEvent.change(screen.getByTestId("onboarding-groq-key"), { target: { value: KEY } });
    fireEvent.submit(screen.getByTestId("onboarding-groq-key").closest("form")!);
    expect((await screen.findByTestId("onboarding-error")).textContent).toContain("Chave inválida.");
    expect(sendCommand.mock.calls.map(([type]) => type)).not.toContain("brain.set");
  });

  it("Configurar depois grava a conclusão e leva ao painel Brain", async () => {
    bridge(() => null);
    const sendCommand = vi.fn(async () => ({}));
    const onDone = vi.fn();
    render(<Onboarding sendCommand={sendCommand as never} onDone={onDone} />);
    fireEvent.click(screen.getByTestId("onboarding-skip"));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("brain"));
    expect(sendCommand).toHaveBeenCalledWith("ui.preferences.set", { onboarding_done: true });
  });

  it("Kiro instalado e com sessão liga o cérebro sozinho", async () => {
    bridge(() => null);
    const sendCommand = vi.fn(async (type: string) => {
      if (type === "brain.kiro.status") return { phase: "idle", progress: null, error: null, url: null,
        supported: true, installed: true, logged_in: true, account: "Google" };
      if (type === "brain.set") return { ok: true };
      return {};
    });
    render(<Onboarding sendCommand={sendCommand as never} onDone={vi.fn()} />);
    fireEvent.click(screen.getByTestId("onboarding-start"));
    fireEvent.click(screen.getByTestId("onboarding-kiro"));
    expect((await screen.findByText(/Ligada a Kiro \(Google\)/)).textContent).toBeTruthy();
    expect(screen.queryByText("A ligar o cérebro…")).toBeNull();
    expect(sendCommand).toHaveBeenCalledWith("brain.set", expect.objectContaining({ provider: "kiro", model: "auto" }));
  });
});

describe("toBridgeError", () => {
  it("tira o embrulho do Electron e devolve o código e o texto do backend", async () => {
    const { toBridgeError } = await import("../src/shared/ipc/client");
    const error = toBridgeError(new Error(
      "Error invoking remote method 'lunmi:command': Error: invalid_configuration: Configure sua chave em Configurar API."));
    expect([error.code, error.message]).toEqual(["invalid_configuration", "Configure sua chave em Configurar API."]);
  });
});

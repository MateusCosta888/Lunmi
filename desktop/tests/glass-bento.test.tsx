/**
 * Autoaprovação no compositor e mecanismo bento (`desktop/tests/glass-bento.test.tsx`).
 *
 * Cobre a mudança de lote: o controle de autoaprovação saiu do header e vive na linha utilitária
 * do compositor como `<button type="button" aria-pressed>` (sem virar switch), só muda depois da
 * resposta real do `approval.set`, bloqueia clique duplicado enquanto ocupado e mantém os locks
 * durante troca de Brain. Cobre também o motor do mecanismo bento: escreve variáveis com o
 * ponteiro, para o rAF quando tudo assenta, limpa e nunca deixa laço permanente.
 */
import React, { useRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App } from "../src/App";
import { __bentoInternals, useBentoSurface } from "../src/shared/bento";
import type { LunmiBridgeApi } from "../src/shared/ipc/client";
import type { Envelope } from "../src/shared/protocol";

function setup() {
  let resolveSet: (() => void) | null = null;
  let autoApprove = false;
  const calls: Array<{ type: string; payload: Record<string, unknown> }> = [];
  const listeners = new Set<(event: Envelope) => void>();
  const api = {
    hello: vi.fn(async () => ({ session_id: "bento-test", capabilities: { brain: true, memory: true, execute: true, converse: true } })),
    onEvent: (callback: (event: Envelope) => void) => { listeners.add(callback); return () => { listeners.delete(callback); }; },
    restartBackend: vi.fn(async () => ({ session_id: "bento-test", capabilities: { brain: true, memory: true, execute: true, converse: true } })),
    notifyRendererReady: vi.fn(async () => ({})),
    sendCommand: vi.fn(async (type: string, payload: Record<string, unknown>): Promise<unknown> => {
      calls.push({ type, payload });
      if (type === "brain.get") return { current: { provider: "custom", provider_label: "Conexão local", model: "balanced", app_provider: "separate", app_model: "executor", has_api_key: true, env_overrides: {} }, providers: [{ id: "custom", label: "Conexão local" }] };
      if (type === "memory.list") return { enabled: true, items: [] };
      if (type === "memory.profile.get") return { enabled: true, auto_learn: true, text: "", revision: 0, can_undo: false, char_count: 0, max_chars: 8000, state: "empty" };
      if (type === "ui.preferences.get") return { scale_pct: 200, actions: [] };
      if (type === "approval.get") return { auto_approve: autoApprove, mode: "manual", label: "Modo: Manual" };
      if (type === "approval.set") {
        await new Promise<void>((resolve) => {
          resolveSet = () => { autoApprove = Boolean(payload.auto_approve); resolve(); };
        });
        return { auto_approve: autoApprove, mode: "manual", label: "Modo: Manual" };
      }
      return {};
    }),
  };
  window.lunmi = api as unknown as LunmiBridgeApi;
  render(<App />);
  return {
    api, calls,
    sets: () => calls.filter(call => call.type === "approval.set"),
    settle: () => resolveSet?.(),
  };
}

describe("autoaprovação no compositor", () => {
  it("vive no compositor como botão aria-pressed — nunca um switch ou checkbox", async () => {
    setup();
    await screen.findByTestId("composer");
    const button = screen.getByTestId("header-auto-approve") as HTMLButtonElement;
    expect(button.closest(".lunmi-composer-surface")).not.toBeNull();
    expect(button.tagName).toBe("BUTTON");
    expect(button.type).toBe("button");
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(button.getAttribute("aria-label")).toBe("Autoaprovação: desligada");
    expect(button.textContent).toContain("desligada");
    expect(screen.queryByRole("switch")).toBeNull();
    expect(document.querySelector('input[type="checkbox"][data-testid="header-auto-approve"]')).toBeNull();
  });

  it("só muda depois da resposta real, envia o booleano e bloqueia clique duplicado", async () => {
    const { api, sets, settle } = setup();
    await screen.findByTestId("composer");
    const button = screen.getByTestId("header-auto-approve") as HTMLButtonElement;

    fireEvent.click(button);
    await waitFor(() => expect(sets()).toHaveLength(1));
    expect(sets()[0].payload).toEqual({ auto_approve: true });
    expect(button.getAttribute("aria-pressed")).toBe("false");
    expect(button.disabled).toBe(true);
    fireEvent.click(button);
    expect(sets()).toHaveLength(1);

    settle();
    await waitFor(() => expect(button.getAttribute("aria-pressed")).toBe("true"));
    expect(button.getAttribute("aria-label")).toBe("Autoaprovação: ligada");
    expect(button.textContent).toContain("ligada");
    expect(button.disabled).toBe(false);

    fireEvent.click(button);
    await waitFor(() => expect(sets()).toHaveLength(2));
    settle();
    await waitFor(() => expect(button.getAttribute("aria-pressed")).toBe("false"));
    expect(sets()[1].payload).toEqual({ auto_approve: false });
    expect(api.sendCommand.mock.calls.some(([type]) => type === "chat.send")).toBe(false);
  });
});

describe("motor bento", () => {
  function Probe({ intro = false }: { intro?: boolean }): React.ReactElement {
    const ref = useRef<HTMLDivElement>(null);
    useBentoSurface(ref, { wave: true, intro });
    return <div ref={ref} data-testid="bento-probe" />;
  }
  const rectAt = (left: number, top: number): DOMRect => ({ left, top, width: 200, height: 100, right: left + 200, bottom: top + 100, x: left, y: top, toJSON: () => ({}) }) as DOMRect;
  const fakeRect = (): DOMRect => rectAt(60, 40);

  beforeEach(() => {
    // O jsdom responde `prefers-reduced-motion: reduce` com matches=true e o motor, corretamente,
    // desliga tudo. Para exercitar o caminho com movimento, o sinal é controlado aqui — o teste de
    // reduced-motion logo abaixo prova os dois lados (liga → desliga → volta).
    (window as unknown as { matchMedia: unknown }).matchMedia = vi.fn((query: string) => ({
      matches: false, media: query,
      addEventListener: () => {}, removeEventListener: () => {},
    }));
    __bentoInternals.reset();
  });

  afterEach(() => {
    delete (window as unknown as { matchMedia?: unknown }).matchMedia;
    __bentoInternals.reset();
  });

  it("segue o ponteiro e para o rAF quando tudo assenta — sem laço ocioso", () => {
    const view = render(<Probe />);
    const el = screen.getByTestId("bento-probe");
    el.getBoundingClientRect = fakeRect;
    window.dispatchEvent(new Event("resize"));
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 160, clientY: 90 }));
    expect(__bentoInternals.hasPendingFrame()).toBe(true);
    __bentoInternals.settle();
    expect(el.style.getPropertyValue("--bento-hot")).toBe("1.000");
    expect(el.style.getPropertyValue("--bento-near")).toBe("1.000");
    expect(Number.parseFloat(el.style.getPropertyValue("--bento-x"))).toBeCloseTo(100, 0);
    expect(Number.parseFloat(el.style.getPropertyValue("--bento-y"))).toBeCloseTo(50, 0);
    expect(__bentoInternals.hasPendingFrame()).toBe(false);

    document.dispatchEvent(new MouseEvent("pointerout", { relatedTarget: null }));
    __bentoInternals.settle();
    expect(el.style.getPropertyValue("--bento-hot")).toBe("0.000");
    expect(el.style.getPropertyValue("--bento-near")).toBe("0.000");
    expect(__bentoInternals.hasPendingFrame()).toBe(false);

    view.unmount();
    expect(__bentoInternals.surfaceCount()).toBe(0);
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 160, clientY: 90 }));
    expect(__bentoInternals.hasPendingFrame()).toBe(false);
  });

  it("relê a geometria no início de cada geração — layout que muda com o motor ocioso não fica preso", () => {
    const view = render(<Probe />);
    const el = screen.getByTestId("bento-probe");
    el.getBoundingClientRect = (): DOMRect => rectAt(60, 40);
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 160, clientY: 90 }));
    __bentoInternals.settle();
    expect(el.style.getPropertyValue("--bento-hot")).toBe("1.000");
    document.dispatchEvent(new MouseEvent("pointerout", { relatedTarget: null }));
    __bentoInternals.settle();
    expect(el.style.getPropertyValue("--bento-hot")).toBe("0.000");

    // Como a lateral que colapsa/expande sem resize da janela: a caixa muda, nenhum
    // evento de resize chega, mas o primeiro quadro da nova geração relê a geometria.
    el.getBoundingClientRect = (): DOMRect => rectAt(600, 300);
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 700, clientY: 350 }));
    __bentoInternals.settle();
    expect(el.style.getPropertyValue("--bento-hot")).toBe("1.000");
    expect(Number.parseFloat(el.style.getPropertyValue("--bento-x"))).toBeCloseTo(100, 0);
    expect(Number.parseFloat(el.style.getPropertyValue("--bento-y"))).toBeCloseTo(50, 0);
    expect(__bentoInternals.hasPendingFrame()).toBe(false);
    view.unmount();
  });

  it("relê a geometria a cada movimento — geração viva não fica presa ao retângulo velho", async () => {
    const view = render(<Probe />);
    const el = screen.getByTestId("bento-probe");
    el.getBoundingClientRect = (): DOMRect => rectAt(60, 40);
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 160, clientY: 90 }));
    await new Promise((resolve) => setTimeout(resolve, 90)); // geração viva: o rAF do jsdom roda
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 165, clientY: 95 }));
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(Number.parseFloat(el.style.getPropertyValue("--bento-hot"))).toBeGreaterThan(0.3);

    // A caixa muda com o ponteiro se movendo e sem nenhum evento de resize — como a
    // troca de vista/tema com o motor no meio de uma geração. Sem a releitura por
    // movimento, o quadro continuaria mirando (60,40) e a luz apagaria.
    el.getBoundingClientRect = (): DOMRect => rectAt(600, 300);
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 700, clientY: 350 }));
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(Number.parseFloat(el.style.getPropertyValue("--bento-hot"))).toBeGreaterThan(0.9);
    const followX = Number.parseFloat(el.style.getPropertyValue("--bento-x"));
    const followY = Number.parseFloat(el.style.getPropertyValue("--bento-y"));
    expect(followX).toBeGreaterThan(90);
    expect(followX).toBeLessThan(110);
    expect(followY).toBeGreaterThan(40);
    expect(followY).toBeLessThan(60);
    view.unmount();
  });

  it("onda de clique cresce, morre sozinha em ~1,1s e não deixa frame pendente", () => {
    const view = render(<Probe />);
    const el = screen.getByTestId("bento-probe");
    el.getBoundingClientRect = fakeRect;
    window.dispatchEvent(new Event("resize"));
    window.dispatchEvent(new MouseEvent("pointerdown", { clientX: 100, clientY: 70 }));
    __bentoInternals.advance(0.2);
    expect(Number.parseFloat(el.style.getPropertyValue("--bento-wa"))).toBeGreaterThan(0);
    const first = Number.parseFloat(el.style.getPropertyValue("--bento-wr"));
    __bentoInternals.advance(0.3);
    const second = Number.parseFloat(el.style.getPropertyValue("--bento-wr"));
    expect(second).toBeGreaterThan(first);
    __bentoInternals.advance(1.2);
    expect(Number.parseFloat(el.style.getPropertyValue("--bento-wa"))).toBe(0);
    expect(__bentoInternals.hasPendingFrame()).toBe(false);
    view.unmount();
  });

  it("reduced-motion em runtime desliga, limpa as variáveis e volta a aceitar eventos", () => {
    const listeners = new Set<(event: MediaQueryListEvent) => void>();
    let matches = false;
    const media = {
      get matches() { return matches; },
      media: "(prefers-reduced-motion: reduce)",
      addEventListener: (_type: string, callback: (event: MediaQueryListEvent) => void) => { listeners.add(callback); },
      removeEventListener: (_type: string, callback: (event: MediaQueryListEvent) => void) => { listeners.delete(callback); },
    };
    (window as unknown as { matchMedia: unknown }).matchMedia = vi.fn(() => media);
    const view = render(<Probe />);
    const el = screen.getByTestId("bento-probe");
    el.getBoundingClientRect = fakeRect;
    window.dispatchEvent(new Event("resize"));
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 160, clientY: 90 }));
    __bentoInternals.settle();
    expect(el.style.getPropertyValue("--bento-hot")).toBe("1.000");

    matches = true;
    for (const callback of [...listeners]) callback({ matches } as MediaQueryListEvent);
    expect(el.style.getPropertyValue("--bento-hot")).toBe("");
    expect(__bentoInternals.hasPendingFrame()).toBe(false);
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 160, clientY: 90 }));
    __bentoInternals.settle();
    expect(el.style.getPropertyValue("--bento-hot")).toBe("");

    matches = false;
    for (const callback of [...listeners]) callback({ matches } as MediaQueryListEvent);
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 160, clientY: 90 }));
    __bentoInternals.settle();
    expect(el.style.getPropertyValue("--bento-hot")).toBe("1.000");
    view.unmount();
  });

  it("documento oculto cancela o frame pendente sem apagar nada do produto", () => {
    const view = render(<Probe />);
    const el = screen.getByTestId("bento-probe");
    el.getBoundingClientRect = fakeRect;
    window.dispatchEvent(new Event("resize"));
    window.dispatchEvent(new MouseEvent("pointermove", { clientX: 160, clientY: 90 }));
    expect(__bentoInternals.hasPendingFrame()).toBe(true);
    Object.defineProperty(document, "hidden", { configurable: true, get: () => true });
    document.dispatchEvent(new Event("visibilitychange"));
    expect(__bentoInternals.hasPendingFrame()).toBe(false);
    delete (document as unknown as { hidden?: unknown }).hidden;
    view.unmount();
    expect(__bentoInternals.surfaceCount()).toBe(0);
  });
});

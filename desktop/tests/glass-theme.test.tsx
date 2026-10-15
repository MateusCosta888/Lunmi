/**
 * Vidro, controles e Red Velvet (`desktop/tests/glass-theme.test.tsx`).
 *
 * Cobre o renderer dos controles próprios (enum, estado maximizado vindo do evento nativo,
 * degradação sem ponte) e o tema Red Velvet: rótulo exato, paridade de tokens, materiais de
 * profundidade publicados e contraste nos três temas pela matriz existente.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App } from "../src/App";
import { WindowControls } from "../src/features/shell/WindowControls";
import { GlassSurface } from "../src/shared/GlassSurface";
import { contrastPairs, contrastRatio, getTheme, GRAPHIC_FLOOR, THEMES } from "../src/shared/theme";
import { applyTheme } from "../src/shared/themeDom";
import type { LunmiBridgeApi } from "../src/shared/ipc/client";
import type { Envelope, SessionReadyPayload } from "../src/shared/protocol";

function sessionPayload(): SessionReadyPayload {
  return {
    v: 1,
    session_id: "sess-glass",
    pid: 1,
    capabilities: { converse: true, execute: true, memory: true, brain: true, web_search: false, browser_use: false },
    limits: { max_line_bytes: 5 * 1024 * 1024, max_import_bytes: 4 * 1024 * 1024, max_chat_text_chars: 8000 },
  };
}

describe("WindowControls (renderer)", () => {
  it("usa só o enum e sincroniza o estado maximizado pela resposta e pelo evento nativo", async () => {
    const listeners = new Set<(event: Envelope) => void>();
    let maximized = false;
    const windowControl = vi.fn(async (action: string) => {
      if (action === "toggle-maximize") maximized = !maximized;
      return { minimized: false, maximized };
    });
    const api = {
      hello: vi.fn(async () => sessionPayload()),
      sendCommand: vi.fn(async () => ({})),
      shutdown: vi.fn(async () => ({ status: "stopping", session_id: "sess-glass" })),
      onEvent: (cb: (event: Envelope) => void) => { listeners.add(cb); return () => listeners.delete(cb); },
      windowControl,
    };
    (window as unknown as { lunmi: LunmiBridgeApi }).lunmi = api as unknown as LunmiBridgeApi;
    render(<WindowControls />);
    await waitFor(() => expect(windowControl).toHaveBeenCalledWith("state"));
    fireEvent.click(screen.getByTestId("win-minimize"));
    expect(windowControl).toHaveBeenCalledWith("minimize");
    fireEvent.click(screen.getByTestId("win-maximize"));
    expect(windowControl).toHaveBeenCalledWith("toggle-maximize");
    await waitFor(() => expect(screen.getByTestId("win-maximize").getAttribute("aria-label")).toBe("Restaurar"));
    // O evento nativo é a autoridade: restauração via broadcast atualiza sem novo clique.
    for (const listener of [...listeners]) {
      listener({ v: 1, id: "w1", type: "window.state", payload: { minimized: false, maximized: false } });
    }
    await waitFor(() => expect(screen.getByTestId("win-maximize").getAttribute("aria-label")).toBe("Maximizar"));
    fireEvent.click(screen.getByTestId("win-close"));
    expect(windowControl).toHaveBeenCalledWith("close");
  });

  it("sem a ponte, controles ficam desabilitados com motivo — nunca clicáveis falsos", () => {
    (window as unknown as { lunmi?: undefined }).lunmi = undefined;
    render(<WindowControls />);
    for (const id of ["win-minimize", "win-maximize", "win-close"]) {
      expect((screen.getByTestId(id) as HTMLButtonElement).disabled).toBe(true);
    }
  });
});

describe("GlassSurface (R1 — vidro fora do fluxo)", () => {
  it("conteúdo é irmão da camada decorativa, nunca filho do pacote", () => {
    const view = render(<GlassSurface radius={18}><button type="button" data-testid="inner-control">Ação</button></GlassSurface>);
    const layer = view.container.querySelector(".lunmi-glass-layer");
    const content = view.container.querySelector(".lunmi-glass-content");
    expect(layer).not.toBeNull();
    expect(layer!.getAttribute("aria-hidden")).toBe("true");
    expect(content).not.toBeNull();
    expect(content!.querySelector('[data-testid="inner-control"]')).not.toBeNull();
    expect(layer!.contains(content)).toBe(false);
    // conteúdo do app não pode morar dentro da árvore interna do pacote
    const glass = layer!.querySelector(".glass");
    expect(glass?.contains(content)).toBe(false);
    expect(glass?.textContent).toBe("");
  });

  it("propaga className/estilo do painel na raiz, sem depender de utilitários do pacote", () => {
    const view = render(<GlassSurface className="lunmi-rail-glass" style={{ height: 100 }}><span>x</span></GlassSurface>);
    const root = view.container.firstElementChild as HTMLElement;
    expect(root.className).toContain("lunmi-glass");
    expect(root.className).toContain("lunmi-rail-glass");
    expect(root).toBeTruthy();
    expect(root.style.height).toBe("100px");
  });
});

describe("Red Velvet (lote C — só cores)", () => {
  it("existe com rótulo exato, modo claro e os mesmos tokens dos outros temas", () => {
    expect(THEMES["red-velvet"].label).toBe("Red Velvet");
    expect(THEMES["red-velvet"].mode).toBe("light");
    expect(Object.keys(THEMES["red-velvet"].colors)).toEqual(Object.keys(THEMES.celestial.colors));
    expect(Object.keys(THEMES["red-velvet"].depth)).toEqual(Object.keys(THEMES.celestial.depth));
  });

  it("passa a matriz de contraste e publica variáveis de cor e profundidade", () => {
    for (const pair of contrastPairs(THEMES["red-velvet"].colors)) {
      expect(contrastRatio(pair.fg, pair.bg), `${pair.fgLabel}/${pair.bgLabel}`).toBeGreaterThanOrEqual(pair.floor);
    }
    applyTheme("red-velvet");
    expect(document.documentElement.dataset.theme).toBe("red-velvet");
    expect(document.documentElement.style.colorScheme).toBe("light");
    expect(document.documentElement.style.getPropertyValue("--lunmi-bg-window")).toBe("#FFF8F6");
    expect(document.documentElement.style.getPropertyValue("--lunmi-accent")).toBe("#800000");
    expect(document.documentElement.style.getPropertyValue("--lunmi-depth-raised")).toContain("rgba(130, 80, 75");
    applyTheme("celestial");
    expect(getTheme("red-velvet").id).toBe("red-velvet");
  });

  it("o par REAL do hover de fechar passa em 3:1 nos três temas", () => {
    for (const theme of Object.values(THEMES)) {
      const pair = contrastPairs(theme.colors).find(p => p.fgLabel === "ícone de fechar (hover)");
      expect(pair, `${theme.id}: par do fechar ausente`).toBeDefined();
      expect(contrastRatio(pair!.fg, pair!.bg), `${theme.id}: ${pair!.fg}/${pair!.bg}`).toBeGreaterThanOrEqual(GRAPHIC_FLOOR);
    }
    // O material claro do Celestial exige primeiro plano ESCURO — branco sobre #E58D99 era a quebra.
    expect(contrastRatio("#FFFFFF", THEMES.celestial.colors.error)).toBeLessThan(GRAPHIC_FLOOR);
    expect(contrastRatio(THEMES.celestial.colors.onError, THEMES.celestial.colors.error)).toBeGreaterThanOrEqual(GRAPHIC_FLOOR);
  });

  it("aparece na grade de temas e troca sem perder sessão", async () => {    const calls: Array<{ type: string; payload: Record<string, unknown> }> = [];
    let theme = "celestial";
    const api = {
      hello: vi.fn(async () => sessionPayload()),
      sendCommand: vi.fn(async (type: string, payload: Record<string, unknown> = {}) => {
        calls.push({ type, payload });
        if (type === "ui.preferences.set") theme = (payload.theme as string) ?? theme;
        if (type === "ui.preferences.get") return { window: null, scale_pct: 100, actions: [], theme };
        if (type === "brain.get") return { current: { provider: "custom", provider_label: "Local", model: "m", env_overrides: {} }, providers: [] };
        if (type === "approval.get") return { auto_approve: false, mode: "manual", label: "Manual" };
        if (type === "memory.list") return { enabled: true, items: [] };
        return { ok: true };
      }),
      shutdown: vi.fn(async () => ({ status: "stopping", session_id: "sess-glass" })),
      onEvent: () => () => {},
    };
    (window as unknown as { lunmi: LunmiBridgeApi }).lunmi = api as unknown as LunmiBridgeApi;
    render(<App />);
    await waitFor(() => expect(screen.getByTestId("session-status").textContent).toBe("Ready"));
    fireEvent.click(screen.getByTestId("nav-preferences"));
    const choice = await screen.findByTestId("theme-red-velvet");
    expect(choice.textContent).toContain("Red Velvet");
    fireEvent.click(choice);
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("red-velvet"));
    expect(calls.some(call => call.type === "ui.preferences.set" && call.payload.theme === "red-velvet")).toBe(true);
    expect(screen.getByTestId("session-status").textContent).toBe("Ready");
  });
});

/**
 * Helper dos controles de janela (`desktop/tests/window-controls.test.ts`).
 *
 * Prova o contrato do IPC exclusivo: só o enum é aceito; maximizar/restaurar alterna pelo estado
 * real da janela; `close` devolve null (janela fechada pela rotina nativa, sem atalho genérico).
 */
import { describe, expect, it, vi } from "vitest";
import {
  applyWindowControl,
  isWindowControlAction,
  readWindowState,
  WINDOW_CONTROL_ACTIONS,
  type ControllableWindow,
} from "../electron/window-controls";

function fakeWindow(overrides: Partial<ControllableWindow> = {}): ControllableWindow {
  return {
    isDestroyed: () => false,
    isMinimized: () => false,
    isMaximized: () => false,
    minimize: vi.fn(),
    maximize: vi.fn(),
    unmaximize: vi.fn(),
    close: vi.fn(),
    ...overrides,
  };
}

describe("window-controls (lote B)", () => {
  it("só aceita o enum publicado", () => {
    expect(WINDOW_CONTROL_ACTIONS).toEqual(["minimize", "toggle-maximize", "close", "state"]);
    for (const action of WINDOW_CONTROL_ACTIONS) expect(isWindowControlAction(action)).toBe(true);
    for (const bad of ["destroy", "hide", "reload", "", "MINIMIZE", 42, null, { action: "close" }]) {
      expect(isWindowControlAction(bad)).toBe(false);
    }
  });

  it("minimizar chama a rotina nativa e lê o estado resultante", () => {
    const win = fakeWindow({ isMinimized: () => true });
    expect(applyWindowControl(win, "minimize")).toEqual({ minimized: true, maximized: false });
    expect(win.minimize).toHaveBeenCalledTimes(1);
    expect(win.close).not.toHaveBeenCalled();
  });

  it("toggle-maximize alterna conforme o estado atual, não conforme o clique", () => {
    let maximized = false;
    const win = fakeWindow({
      isMaximized: () => maximized,
      maximize: vi.fn(() => { maximized = true; }),
      unmaximize: vi.fn(() => { maximized = false; }),
    });
    expect(applyWindowControl(win, "toggle-maximize")).toEqual({ minimized: false, maximized: true });
    expect(applyWindowControl(win, "toggle-maximize")).toEqual({ minimized: false, maximized: false });
    expect(win.maximize).toHaveBeenCalledTimes(1);
    expect(win.unmaximize).toHaveBeenCalledTimes(1);
  });

  it("close devolve null e usa o close nativo; janela destruída não gera ação", () => {
    const win = fakeWindow();
    expect(applyWindowControl(win, "close")).toBeNull();
    expect(win.close).toHaveBeenCalledTimes(1);
    const dead = fakeWindow({ isDestroyed: () => true });
    expect(applyWindowControl(dead, "minimize")).toBeNull();
    expect(readWindowState(dead)).toEqual({ minimized: false, maximized: false });
  });

  it("state lê sem mutar nada", () => {
    const win = fakeWindow({ isMaximized: () => true, isMinimized: () => true });
    expect(applyWindowControl(win, "state")).toEqual({ minimized: true, maximized: true });
    expect(win.maximize).not.toHaveBeenCalled();
    expect(win.minimize).not.toHaveBeenCalled();
    expect(win.close).not.toHaveBeenCalled();
  });
});

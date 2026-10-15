/**
 * Helper mínimo dos controles de janela (`desktop/electron/window-controls.ts`).
 *
 * O IPC exclusivo aceita SOMENTE o enum abaixo — nada de `BrowserWindow` genérico nem
 * `ipcRenderer` exposto. `close` usa a rotina nativa de fechar (o `close`/`window-all-closed`
 * do main persiste geometria e derruba o backend sem órfãos). Maximizar/restaurar sincroniza a
 * UI pelos eventos nativos da janela, não por callback do clique.
 */

export const WINDOW_CONTROL_ACTIONS = ["minimize", "toggle-maximize", "close", "state"] as const;
export type WindowControlAction = (typeof WINDOW_CONTROL_ACTIONS)[number];

export interface WindowStateDto {
  minimized: boolean;
  maximized: boolean;
}

/** Janela mínima que o helper conhece — `BrowserWindow` real satisfaz; teste usa um fake. */
export interface ControllableWindow {
  isDestroyed(): boolean;
  isMinimized(): boolean;
  isMaximized(): boolean;
  minimize(): void;
  maximize(): void;
  unmaximize(): void;
  close(): void;
}

export function isWindowControlAction(value: unknown): value is WindowControlAction {
  return typeof value === "string" && (WINDOW_CONTROL_ACTIONS as readonly string[]).includes(value);
}

export function readWindowState(win: ControllableWindow): WindowStateDto {
  if (win.isDestroyed()) {
    return { minimized: false, maximized: false };
  }
  return { minimized: win.isMinimized(), maximized: win.isMaximized() };
}

/** Aplica a ação e devolve o estado resultante; `null` quando a janela foi fechada. */
export function applyWindowControl(win: ControllableWindow, action: WindowControlAction): WindowStateDto | null {
  if (win.isDestroyed()) {
    return null;
  }
  if (action === "minimize") {
    win.minimize();
  } else if (action === "toggle-maximize") {
    if (win.isMaximized()) {
      win.unmaximize();
    } else {
      win.maximize();
    }
  } else if (action === "close") {
    win.close();
    return null;
  }
  return readWindowState(win);
}

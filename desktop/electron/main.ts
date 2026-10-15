/**
 * Processo principal Electron da Lunmi (F0/F1).
 * - Cria BrowserWindow com `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`.
 * - Valida sender/frame de todas as chamadas IPC contra o documento exato carregado (`expectedDocumentUrl`).
 * - Bloqueia navegação externa e abertura de popups (`setWindowOpenHandler` / `will-navigate`).
 * - Usa `PythonBackendSupervisor` (`desktop/electron/backend.ts`) para supervisionar o processo
 *   Python, coletar os PIDs reais da árvore Electron (`app.getAppMetrics()`) e impedir que o
 *   renderer envie `session.hello` ou falsifique `host_pids`.
 */
import { app, BrowserWindow, dialog, ipcMain, IpcMainInvokeEvent, nativeTheme, screen, session } from "electron";
import { CELESTIAL_COLORS, getTheme } from "../src/shared/theme";
import * as fs from "fs";
import * as path from "path";
import { pathToFileURL } from "url";
import { isPlainObject, SessionReadyPayload } from "../src/shared/protocol";
import { PythonBackendSupervisor, resolvePythonExecutable } from "./backend";
import {
  DEFAULT_WINDOW_HEIGHT,
  DEFAULT_WINDOW_WIDTH,
  readPreferencesFile,
  usableBounds,
  type Rect,
  type UiPreferences,
} from "./preferences";

export { resolvePythonExecutable };

export function normalizeDocumentUrl(rawUrl: string): string {
  try {
    const parsed = new URL(rawUrl);
    parsed.hash = "";
    parsed.search = "";
    return parsed.toString();
  } catch {
    return "";
  }
}

export function isTrustedSenderFrame(
  event: IpcMainInvokeEvent,
  expectedWindow: BrowserWindow | null,
  expectedDocumentUrl?: string
): boolean {
  if (!expectedWindow || expectedWindow.isDestroyed()) {
    return false;
  }
  if (event.sender !== expectedWindow.webContents) {
    return false;
  }
  const frame = event.senderFrame;
  if (!frame || frame !== expectedWindow.webContents.mainFrame) {
    return false;
  }
  const frameUrl = normalizeDocumentUrl(frame.url || "");
  if (!frameUrl) {
    return false;
  }
  if (expectedDocumentUrl) {
    return frameUrl === normalizeDocumentUrl(expectedDocumentUrl);
  }
  const currentWinUrl = normalizeDocumentUrl(expectedWindow.webContents.getURL() || "");
  return Boolean(currentWinUrl) && frameUrl === currentWinUrl;
}

let mainWindow: BrowserWindow | null = null;
let backendSupervisor: PythonBackendSupervisor | null = null;
let expectedIndexFileUrl = "";
let prefsLocalDir = "";
let appliedPrefs: UiPreferences = { scalePct: 100, window: null, theme: "celestial" };
const bootStartEpochMs = Date.now();
const bootStartHr = process.hrtime.bigint();
let windowReadyToShowMs: number | null = null;
let windowShownMs: number | null = null;
let handshakeCompletedMs: number | null = null;
let rendererPaintedReadyMs: number | null = null;

function elapsedMs(): number {
  return Number((Number(process.hrtime.bigint() - bootStartHr) / 1_000_000).toFixed(2));
}

function getRepoRoot(): string {
  if (process.env.LUNMI_REPO_ROOT) {
    return path.resolve(process.env.LUNMI_REPO_ROOT);
  }
  return path.resolve(__dirname, "..", "..", "..");
}

function collectLiveElectronHostPids(): number[] {
  const pids = new Set<number>([process.pid]);
  try {
    for (const metric of app.getAppMetrics()) {
      if (typeof metric.pid === "number" && metric.pid > 0) {
        pids.add(metric.pid);
      }
    }
  } catch {
    // fallback caso appMetrics ainda não esteja disponível
  }
  if (mainWindow && !mainWindow.isDestroyed()) {
    try {
      const rendererPid = mainWindow.webContents.getOSProcessId();
      if (rendererPid > 0) {
        pids.add(rendererPid);
      }
    } catch {
      // ignore
    }
  }
  return Array.from(pids);
}

function resolveWindowBounds(): Rect | null {
  // A geometria salva é do usuário, não do renderer: quem valida é a camada confiável, contra a
  // área útil da tela correspondente. Retângulo de monitor removido volta `null` (lugar padrão).
  const saved = appliedPrefs.window;
  if (!saved) {
    return null;
  }
  try {
    const display = screen.getDisplayMatching({ x: saved.x, y: saved.y, width: saved.width, height: saved.height });
    return usableBounds(saved, display.workArea);
  } catch {
    return null;
  }
}

/** Chromium já acompanha o DPI do monitor; não multiplicar por um zoom salvo antigo. */
function applyScaleToWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  try {
    mainWindow.webContents.setZoomFactor(1);
  } catch {
    // Escala é apresentação: falhar aqui não pode derrubar a janela.
  }
}

/** Relê o arquivo de preferências (fonte da verdade) e aplica escala à janela viva. */
function reloadAndApplyPreferences(): void {
  if (!prefsLocalDir) {
    return;
  }
  appliedPrefs = readPreferencesFile(prefsLocalDir);
  applyScaleToWindow();
  applyNativeTheme();
}

function applyNativeTheme(): void {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  const theme = getTheme(appliedPrefs.theme);
  nativeTheme.themeSource = theme.mode;
  mainWindow.setBackgroundColor(theme.colors.bgWindow);
  mainWindow.setTitleBarOverlay({ color: theme.colors.bgWindow, symbolColor: theme.colors.textPrimary, height: 40 });
}

function persistWindowBounds(): void {
  // Grava só a geometria (nunca `path`/`file`, nunca escala): o backend faz o merge com o resto.
  if (!mainWindow || mainWindow.isDestroyed() || !backendSupervisor?.isRunning) {
    return;
  }
  try {
    const bounds = mainWindow.getBounds();
    void backendSupervisor
      ._sendInternalCommand("ui.preferences.set", {
        window: [bounds.x, bounds.y, bounds.width, bounds.height],
      })
      .catch(() => undefined);
  } catch {
    // Encerrar a janela não pode falhar por causa de preferência de tela.
  }
}

function createWindow(): BrowserWindow {
  const preloadPath = path.join(__dirname, "preload.js");
  const isHeadlessBench = process.env.LUNMI_BENCH_HEADLESS === "1";
  const bounds = resolveWindowBounds();
  const workArea = screen.getDisplayMatching(bounds ?? screen.getPrimaryDisplay().workArea).workArea;
  const win = new BrowserWindow({
    width: Math.min(bounds ? bounds.width : DEFAULT_WINDOW_WIDTH, workArea.width),
    height: Math.min(bounds ? bounds.height : DEFAULT_WINDOW_HEIGHT, workArea.height),
    ...(bounds ? { x: bounds.x, y: bounds.y } : {}),
    minWidth: Math.min(700, workArea.width),
    minHeight: Math.min(560, workArea.height),
    show: !isHeadlessBench,
    backgroundColor: CELESTIAL_COLORS.bgWindow,
    titleBarStyle: "hidden",
    titleBarOverlay: { color: CELESTIAL_COLORS.bgWindow, symbolColor: CELESTIAL_COLORS.textPrimary, height: 40 },
    title: "Lunmi",
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      allowRunningInsecureContent: false,
      spellcheck: false,
    },
  });

  win.removeMenu();

  win.once("ready-to-show", () => {
    windowReadyToShowMs = elapsedMs();
    if (!isHeadlessBench && win.isVisible() && windowShownMs === null) {
      windowShownMs = windowReadyToShowMs;
    }
  });
  win.once("show", () => {
    windowShownMs = elapsedMs();
  });
  win.webContents.on("did-finish-load", () => {
    applyScaleToWindow();
  });
  win.on("close", () => {
    persistWindowBounds();
  });
  if (!isHeadlessBench && win.isVisible()) {
    windowShownMs = elapsedMs();
  }

  win.webContents.on("preload-error", (_event, preloadFile, error) => {
    process.stderr.write(`[preload-error] ${preloadFile}: ${error.message}\n`);
  });
  win.webContents.on("did-fail-load", (_event, code, desc, url) => {
    process.stderr.write(`[did-fail-load] code=${code} desc=${desc} url=${url}\n`);
  });
  win.webContents.on("render-process-gone", () => {
    // Se o renderer reiniciar, atualiza os PIDs vivos no backend
    if (backendSupervisor && backendSupervisor.isRunning) {
      void backendSupervisor.syncHostPids().catch(() => undefined);
    }
  });
  win.webContents.on("did-finish-load", () => {
    if (backendSupervisor && backendSupervisor.isRunning) {
      void backendSupervisor.syncHostPids().catch(() => undefined);
    }
  });

  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (event, targetUrl) => {
    if (normalizeDocumentUrl(targetUrl) !== normalizeDocumentUrl(expectedIndexFileUrl)) {
      event.preventDefault();
    }
  });

  const indexHtmlPath = path.resolve(__dirname, "..", "..", "dist", "index.html");
  expectedIndexFileUrl = pathToFileURL(indexHtmlPath).toString();
  void win.loadFile(indexHtmlPath);
  return win;
}

function setupIpc(): void {
  ipcMain.handle("lunmi:hello", async (event) => {
    if (!isTrustedSenderFrame(event, mainWindow, expectedIndexFileUrl)) {
      throw new Error("Sender não autorizado.");
    }
    if (!backendSupervisor) {
      throw new Error("Bridge não inicializada.");
    }
    const ready = await backendSupervisor.startAndHandshake();
    handshakeCompletedMs = elapsedMs();
    if (
      process.env.LUNMI_BENCH_OUT &&
      process.env.LUNMI_BENCH_WAIT_PAINT !== "1"
    ) {
      void emitBenchAndClose(ready, "handshake_only").catch((err: unknown) => {
        process.stderr.write(`[bench-error] ${(err as Error).message}\n`);
      });
    }
    return ready;
  });

  ipcMain.handle("lunmi:renderer-ready", async (event) => {
    if (!isTrustedSenderFrame(event, mainWindow, expectedIndexFileUrl)) {
      throw new Error("Sender não autorizado.");
    }
    rendererPaintedReadyMs = elapsedMs();
    applyNativeTheme();
    if (
      process.env.LUNMI_BENCH_OUT &&
      process.env.LUNMI_BENCH_WAIT_PAINT === "1" &&
      backendSupervisor?.currentSession
    ) {
      const ready = backendSupervisor.currentSession;
      void emitBenchAndClose(ready, "visible_painted_ready").catch((err: unknown) => {
        process.stderr.write(`[bench-error] ${(err as Error).message}\n`);
      });
    }
    return {
      ok: true,
      rendererPaintedReadyMs,
      windowShownMs,
      windowReadyToShowMs,
    };
  });

  ipcMain.handle(
    "lunmi:command",
    async (event, args: { type?: unknown; payload?: unknown }) => {
      if (!isTrustedSenderFrame(event, mainWindow, expectedIndexFileUrl)) {
        throw new Error("Sender não autorizado.");
      }
      if (!backendSupervisor) {
        throw new Error("Bridge não inicializada.");
      }
      if (!args || typeof args.type !== "string") {
        throw new Error("Comando inválido.");
      }
      const payload = isPlainObject(args.payload) ? args.payload : {};

      // Seletores nativos de arquivo administrados pela camada confiável (main):
      // o renderer nunca envia path arbitrário para leitura/escrita em disco.
      if (args.type === "memory.export" && payload.use_native_dialog === true && mainWindow) {
        const saveRes = await dialog.showSaveDialog(mainWindow, {
          title: "Exportar memória",
          defaultPath: "lunmi-memory.json",
          filters: [{ name: "JSON", extensions: ["json"] }],
        });
        if (saveRes.canceled || !saveRes.filePath) {
          return { exported: false, cancelled: true };
        }
        return backendSupervisor._sendInternalCommand("memory.export", {
          _trusted_export_path: saveRes.filePath,
        });
      }

      if (args.type === "ui.preferences.set") {
        const saved = await backendSupervisor.sendRendererCommand(args.type, payload);
        // Read the persisted result so native chrome and renderer share the same theme.
        reloadAndApplyPreferences();
        return saved;
      }

      if (args.type === "memory.import" && payload.use_native_dialog === true && mainWindow) {
        const openRes = await dialog.showOpenDialog(mainWindow, {
          title: "Importar memória",
          properties: ["openFile"],
          filters: [{ name: "JSON", extensions: ["json"] }],
        });
        if (openRes.canceled || openRes.filePaths.length === 0) {
          return { imported: false, cancelled: true };
        }
        return backendSupervisor._sendInternalCommand("memory.import", {
          confirmed: payload.confirmed === true,
          _trusted_import_path: openRes.filePaths[0],
        });
      }

      return backendSupervisor.sendRendererCommand(args.type, payload);
    }
  );

  ipcMain.handle(
    "lunmi:pick-location",
    async (event, args: { kind?: unknown; query?: unknown }) => {
      if (!isTrustedSenderFrame(event, mainWindow, expectedIndexFileUrl)) {
        throw new Error("Sender não autorizado.");
      }
      if (!mainWindow || mainWindow.isDestroyed()) {
        return { path: "" };
      }
      const kind = args?.kind === "folder" ? "folder" : "file";
      const query = typeof args?.query === "string" ? args.query.slice(0, 80) : "aplicativo";
      if (kind === "folder") {
        const res = await dialog.showOpenDialog(mainWindow, {
          title: `Pasta de ${query}`,
          properties: ["openDirectory"],
        });
        return { path: res.canceled || res.filePaths.length === 0 ? "" : res.filePaths[0] };
      }
      const res = await dialog.showOpenDialog(mainWindow, {
        title: `Executável de ${query}`,
        properties: ["openFile"],
        filters: [{ name: "Aplicativo", extensions: ["exe"] }],
      });
      return { path: res.canceled || res.filePaths.length === 0 ? "" : res.filePaths[0] };
    }
  );

  ipcMain.handle("lunmi:restart-backend", async (event) => {
    if (!isTrustedSenderFrame(event, mainWindow, expectedIndexFileUrl)) {
      throw new Error("Sender não autorizado.");
    }
    if (!backendSupervisor) {
      throw new Error("Bridge não inicializada.");
    }
    // Reinício explícito do backend: refaz apenas o handshake numa sessão nova.
    // Não reexecuta nenhuma instrução anterior — repetir a tarefa é decisão do usuário.
    const ready = await backendSupervisor.restart();
    handshakeCompletedMs = elapsedMs();
    return ready;
  });

  ipcMain.handle("lunmi:shutdown", async (event) => {
    if (!isTrustedSenderFrame(event, mainWindow, expectedIndexFileUrl)) {
      throw new Error("Sender não autorizado.");
    }
    if (!backendSupervisor) {
      return { status: "stopped", session_id: "" };
    }
    return backendSupervisor.shutdown();
  });
}

async function emitBenchAndClose(
  ready: SessionReadyPayload,
  mode: "handshake_only" | "visible_painted_ready"
): Promise<void> {
  const readyMs = elapsedMs();
  const benchFile = process.env.LUNMI_BENCH_OUT!;
  const appMetrics = app.getAppMetrics();
  const electronPids = appMetrics.map((m) => ({
    pid: m.pid,
    type: m.type,
    workingSetSizeKb: m.memory.workingSetSize,
    privateBytesKb: m.memory.privateBytes,
  }));
  const isVisible =
    mainWindow && !mainWindow.isDestroyed() ? mainWindow.isVisible() : false;
  const record = {
    mode,
    bootStartEpochMs,
    handshakeCompletedMs: handshakeCompletedMs ?? readyMs,
    windowReadyToShowMs,
    windowShownMs,
    rendererPaintedReadyMs,
    readyMs,
    windowVisible: isVisible,
    electronVersion: process.versions.electron,
    electronMainPid: process.pid,
    rendererOsPid:
      mainWindow && !mainWindow.isDestroyed() ? mainWindow.webContents.getOSProcessId() : null,
    pythonPid: ready.pid,
    sessionId: ready.session_id,
    capabilities: ready.capabilities,
    electronProcesses: electronPids,
  };
  fs.writeFileSync(benchFile, JSON.stringify(record, null, 2), "utf8");
  const holdMs = Number(process.env.LUNMI_BENCH_HOLD_MS || "0");
  if (holdMs > 0) {
    await new Promise<void>((resolve) => setTimeout(resolve, holdMs));
  }
  if (backendSupervisor) {
    await backendSupervisor.shutdown();
  }
  app.quit();
}

function isElectronMainRuntime(): boolean {
  return Boolean(process.versions?.electron) && Boolean(app) && typeof app.whenReady === "function";
}

if (isElectronMainRuntime()) {
  void app.whenReady().then(() => {
    nativeTheme.themeSource = "dark";
    session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => {
      callback(false);
    });

    const repoRoot = getRepoRoot();
    const sandboxDir = process.env.LUNMI_SANDBOX_DIR || null;
    const handshakeOnly = process.env.LUNMI_HANDSHAKE_ONLY === "1";

    // Preferências de tela do escopo em uso: sandbox quando houver, senão o estado do usuário.
    prefsLocalDir = path.join(sandboxDir ?? repoRoot, ".local");
    appliedPrefs = readPreferencesFile(prefsLocalDir);

    backendSupervisor = new PythonBackendSupervisor({
      repoRoot,
      clientVersion: app.getVersion(),
      sandboxDir,
      handshakeOnly,
      // Aceite/sandbox: usa a configuração real (provider/modelo/credencial) só em RAM, com a
      // persistência confinada ao sandbox. Sem esta variável o comportamento é o de produção.
      settingsInRam: process.env.LUNMI_SETTINGS_IN_RAM === "1",
      getHostPids: collectLiveElectronHostPids,
      onEvent: (envelope) => {
        if (mainWindow && !mainWindow.isDestroyed()) {
          mainWindow.webContents.send("lunmi:event", envelope);
        }
      },
    });

    setupIpc();
    mainWindow = createWindow();

    mainWindow.on("closed", () => {
      mainWindow = null;
    });
  });

  app.on("window-all-closed", () => {
    void (async () => {
      if (backendSupervisor) {
        await backendSupervisor.shutdown();
      }
      app.quit();
    })();
  });
}

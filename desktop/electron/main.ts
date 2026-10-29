/**
 * Processo principal Electron da Lunmi (F0/F1, BACKGROUND V1).
 * - Cria BrowserWindow com `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`.
 * - Valida sender/frame de todas as chamadas IPC contra o documento exato carregado (`expectedDocumentUrl`).
 * - Bloqueia navegação externa e abertura de popups (`setWindowOpenHandler` / `will-navigate`).
 * - Usa `PythonBackendSupervisor` (`desktop/electron/backend.ts`) para supervisionar o processo
 *   Python, coletar os PIDs reais da árvore Electron (`app.getAppMetrics()`) e impedir que o
 *   renderer envie `session.hello` ou falsifique `host_pids`.
 * - BACKGROUND V1: mantém uma projeção pública da conversa em RAM, uma janela auxiliar
 *   (orb/mini chat), bandeja, single-instance e um único caminho de encerramento. X oculta;
 *   «Sair» encerra. Nada aqui sintetiza voz nem reinicia a sessão ao trocar de janela.
 */
import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  IpcMainInvokeEvent,
  globalShortcut,
  Menu,
  nativeImage,
  nativeTheme,
  powerMonitor,
  screen,
  session,
  shell,
  clipboard,
  Tray,
} from "electron";
import { CELESTIAL_COLORS, getTheme } from "../src/shared/theme";
import { BackgroundVoiceTurn, nodeVoicePhase } from "./voice-node";
import { WindowsMediaSession } from "./media-session";
import { registerNodeMediaIpc } from "./media-ipc";
import { NodeTerminalNotice, type NodeTerminalNoticeSnapshot } from "./node-terminal-notice";
import * as fs from "fs";
import * as path from "path";
import { pathToFileURL } from "url";
import {
  isPlainObject,
  MAX_ATTACHMENT_BYTES,
  MAX_ATTACHMENT_NAME_CHARS,
  MAX_CHAT_ATTACHMENTS,
  MAX_TOTAL_ATTACHMENTS_BYTES,
  PROTOCOL_VERSION,
  SessionReadyPayload,
  UNSUPPORTED_ATTACHMENT_EXTENSIONS,
} from "../src/shared/protocol";
import { createChatProjection, mergeChatDelta, projectChat } from "../src/shared/chatProjection";
import { MAX_DRAFT_CHARS } from "../src/shared/presence";
import {
  applyNodeDoneExpiry,
  applyNodePreferencesPatch,
  COMPANION_PRESENCE_ACTIONS,
  deriveNodeStatus,
  nodeDoneExpiryMatches,
  type NodeDoneExpiry,
  effectiveNodeStatus,
  isCompanionPresenceAction,
  isNodeAttentionStatus,
  isPresenceAction,
  NODE_ACTIONS,
  NODE_PASSIVE_REASON,
  NODE_SUCCESS_VISIBLE_MS,
  nodePassiveBlocksAction,
  nodePassiveFromTaskPhase,
  nodeStatusInputFromProjection,
  nodeSurfaceForStatus,
  nodeToneFor,
  PRESENCE_ACTIONS,
  type NodeVisibleSurface,
} from "../src/shared/node";
import type {
  ChatProjectionDeltaDto,
  ChatProjectionDto,
  NodePreferencesDto,
  NodePreferencesResultDto,
  NodeStateDto,
  NodeStatus,
  PresenceAction,
  PresenceActionResultDto,
  PresenceEventDto,
  PresenceMode,
  PresenceSnapshotDto,
  VoiceInputHoldPhaseDto,
  VoiceInputShortcutResultDto,
  VoiceStateEventDto,
} from "../src/shared/types";
import { PythonBackendSupervisor, resolvePythonExecutable } from "./backend";
import {
  applyWindowControl,
  isWindowControlAction,
  readWindowState,
  WINDOW_CONTROL_ACTIONS,
} from "./window-controls";
import {
  DEFAULT_PREFERENCES,
  DEFAULT_WINDOW_HEIGHT,
  DEFAULT_WINDOW_WIDTH,
  readPreferencesFile,
  usableBounds,
  type Rect,
  type UiPreferences,
} from "./preferences";
import {
  DEFAULT_PRESENCE,
  readPresenceFile,
  toPresencePreferencesDto,
  writePresenceFile,
  type PresencePreferences,
} from "./presence";
import { applyProjectionCommand, ChatProjectionStore } from "./chat-projection";
import { CompanionManager, type CompanionSnapshot } from "./companion";
import {
  parseVoiceShortcutSetRequest,
  sameVoiceShortcut,
  VoiceInputShortcutStore,
} from "./voice-shortcut";
import { VoiceHoldCoordinator, VoiceHotkeyObserver } from "./voice-hotkey";
import { createWebSourceOpenHandler, WebSourceOpener } from "./web-source-open";
import { LunmiUpdates } from "./updates";
import updateTrust from "./update-public-key.json";

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
let companionManager: CompanionManager | null = null;
let tray: Tray | null = null;
let chatProjection: ChatProjectionStore | null = null;
let expectedIndexFileUrl = "";
let prefsLocalDir = "";
let presenceLocalDir = "";
let appliedPrefs: UiPreferences = { ...DEFAULT_PREFERENCES };
let presencePrefs: PresencePreferences = { ...DEFAULT_PRESENCE };
/** Último estado publicado pelo CompanionManager; o main compõe o DTO público a partir daqui. */
let companionSnapshot: CompanionSnapshot = {
  surface: "hidden",
  phase: "settled",
  transition_id: 0,
  transition: null,
};
/**
 * N4: modo passivo derivado da fase RAW da projeção (`task.phase === "running"`). Nunca de
 * `nodeStatus`: com `show_activity:false` o status efetivo é `null` e o passivo desaparecia.
 */
let nodePassive = false;

/** N2: estado de negócio reduzido no main a partir da projeção pública. */
let nodeStatus: NodeStatus = "idle";
/** Sucesso e avisos terminais são breves; confirmações/respostas continuam persistentes. */
let nodeSuccessTimer: NodeJS.Timeout | null = null;
const nodeTerminalNotice = new NodeTerminalNotice(expireNodeTerminalNotice);
let nodeMedia: WindowsMediaSession | null = null;
let nodeMediaIdleTimer: NodeJS.Timeout | null = null;
/** Cápsula foi aberta pela política de status (não por gesto): pode recolher sozinha. */
let nodePillAutomatic = false;
/** Gesto explícito de recolha vence a política até o status mudar. */
let nodeSurfaceSuppressedFor: NodeStatus | null = null;
/** Cada gesto humano invalida um timer de sucesso que ainda não disparou. */
let nodeGestureSeq = 0;
/** `done` já expirado por sessão+tarefa: um resync da mesma sessão não o ressuscita. */
let nodeExpiredDone: NodeDoneExpiry | null = null;
let lastSession: SessionReadyPayload | null = null;
let lastVoiceState: VoiceStateEventDto | null = null;
/** Atalho próprio do ditado; prefs e registo separados do Node. */
let voiceShortcutStore: VoiceInputShortcutStore | null = null;
/** Supervisor do helper WH_KEYBOARD_LL; prefs e registo separados do Node. */
let voiceHotkey: VoiceHotkeyObserver | null = null;
/** Fases hold sanitizadas; o renderer único decide start/finish/chat.send. */
let voiceHoldCoordinator: VoiceHoldCoordinator | null = null;
const backgroundVoice = new BackgroundVoiceTurn();
let draft = "";
const conversationDrafts = new Map<string, string>();
let draftConversationId = '';
let pendingChatDelta: ChatProjectionDeltaDto | null = null;
let chatDeltaTimer: NodeJS.Timeout | null = null;
let mainRendererReady = false;
let pendingShellRoute: "chat" | "preferences" | null = null;
let quitting = false;
let updates: LunmiUpdates | null = null;
let integrationConnecting = false;
let quitPromise: Promise<void> | null = null;
const bootStartEpochMs = Date.now();
const bootStartHr = process.hrtime.bigint();
let windowReadyToShowMs: number | null = null;
let windowShownMs: number | null = null;
let handshakeCompletedMs: number | null = null;
let rendererPaintedReadyMs: number | null = null;

const BACKEND_FAILURE_CODES = new Set<string>([
  "backend_exited",
  "backend_spawn_failed",
  "backend_pipe_error",
]);

/** O companheiro nunca encerra a aplicação nem abre rotas arbitrárias. */
export const COMPANION_ACTIONS: readonly PresenceAction[] = COMPANION_PRESENCE_ACTIONS;

/** Allowlist de comandos do mini chat: sem confirmação, sem credenciais, sem restart. */
export const COMPANION_COMMANDS = new Set<string>([
  "chat.send",
  "task.cancel",
  "voice.stop",
  "voice.get",
]);

const EMPTY_CHAT_PROJECTION: ChatProjectionDto = projectChat(createChatProjection(""));

/** WEB-SEARCH-V1: abertura de fontes por gesto; dedupe in-flight vive nesta instância. */
const webSourceOpener = new WebSourceOpener({
  openExternal: (url) => shell.openExternal(url),
});

function elapsedMs(): number {
  return Number((Number(process.hrtime.bigint() - bootStartHr) / 1_000_000).toFixed(2));
}

/** Páginas que a introdução pode abrir; o renderer escolhe a chave, nunca o endereço. */
const ONBOARDING_PAGES: Record<string, string> = {
  "groq-keys": "https://console.groq.com/keys",
  "kiro-docs": "https://kiro.dev/docs/cli/installation/",
};

/**
 * A introdução apanha a chave Groq que a pessoa acabou de copiar. A área de transferência só sai
 * do main se for uma chave Groq; qualquer outro conteúdo fica aqui.
 */
/**
 * A área de transferência só é lida depois do gesto "Abrir Groq", durante 10 min, e até à primeira
 * chave entregue. Fora disso, nem um renderer comprometido consegue pedir o que lá estiver.
 */
export const groqKeyWatch = {
  until: 0,
  open(now: number): void { this.until = now + 10 * 60 * 1000; },
  active(now: number): boolean { return now <= this.until; },
  take(text: string, now: number): string | null {
    if (!this.active(now)) return null;
    const key = groqKeyFrom(text);
    if (key) this.until = 0;
    return key;
  },
};

export function groqKeyFrom(text: string): string | null {
  const candidate = text.trim();
  return /^gsk_[A-Za-z0-9]{20,120}$/.test(candidate) ? candidate : null;
}

export function getRepoRoot(): string {
  if (process.env.LUNMI_REPO_ROOT) {
    return path.resolve(process.env.LUNMI_REPO_ROOT);
  }
  // Renamed Electron reports isPackaged in dev; defaultApp still identifies a source launch.
  if (app.isPackaged && !process.defaultApp) {
    // Recursos somente-leitura do pacote: backend Python e `vendor/UFO` ficam fora do asar.
    return path.join(process.resourcesPath, "app");
  }
  return path.resolve(__dirname, "..", "..", "..");
}

/** Raiz dos recursos do pacote (onde vive o runtime Python privado), ou `null` em dev. */
export function getResourcesRoot(): string | null {
  return app.isPackaged && !process.defaultApp ? process.resourcesPath : null;
}

/**
 * Raiz de dados graváveis por utilizador. No pacote fica em `LOCALAPPDATA\Lunmi`, fora dos
 * recursos somente-leitura instalados: reinstalar ou desinstalar não apaga o que o testador
 * criou, e nada escreve dentro de `resources/`.
 */
export function getUserDataRoot(): string {
  if (process.env.LUNMI_USER_DATA) {
    return path.resolve(process.env.LUNMI_USER_DATA);
  }
  if (app.isPackaged && !process.defaultApp) {
    const base = process.env.LOCALAPPDATA || app.getPath("userData");
    return path.join(base, "Lunmi");
  }
  return getRepoRoot();
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
  for (const win of [mainWindow, companionManager?.window ?? null]) {
    if (win && !win.isDestroyed()) {
      try {
        const rendererPid = win.webContents.getOSProcessId();
        if (rendererPid > 0) {
          pids.add(rendererPid);
        }
      } catch {
        // ignore
      }
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
}

/** Estados nativos da janela (maximizar/restaurar/minimizar) sincronizam a UI por evento. */
function broadcastWindowState(): void {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  const state = readWindowState(mainWindow);
  const id = `win-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  mainWindow.webContents.send("lunmi:event", { v: 1, id, type: "window.state", payload: state });
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

function appIconPath(): string {
  const icon = process.platform === "win32" ? "lunmi.ico" : "lunmi.png";
  return path.resolve(__dirname, "..", "..", "src", "assets", "branding", icon);
}

// ---------------------------------------------------------------------------
// Presença residente: projeção da conversa, janela auxiliar, bandeja e encerramento
// ---------------------------------------------------------------------------

function presenceMode(): PresenceMode {
  return companionManager?.currentMode ?? "hidden";
}

function nodeStateDto(): NodeStateDto {
  const status = effectiveNodeStatus(nodeStatus, presencePrefs.node.show_activity);
  const voicePhase = nodeVoicePhase(backgroundVoice.phase, status);
  return {
    v: 1,
    enabled: presencePrefs.node.enabled,
    side: presencePrefs.node.side,
    surface: companionSnapshot.surface,
    phase: companionSnapshot.phase,
    transition_id: companionSnapshot.transition_id,
    transition: companionSnapshot.transition,
    status,
    tone: voicePhase === "error" ? "error" : voicePhase ? "working" : nodeToneFor(status),
    session_id: lastSession?.session_id ?? null,
    voice_phase: voicePhase,
  };
}

function publishNode(): void {
  broadcastPresence({ type: "node", state: nodeStateDto() });
}

/**
 * Redução N2 chamada depois de cada alteração real da projeção. Não há timer em repouso e
 * nenhum texto da conversa entra no Node: só o enum de estado.
 */
function refreshNodeStatus(): void {
  const chat = chatProjection?.snapshot() ?? EMPTY_CHAT_PROJECTION;
  const sessionId = lastSession?.session_id ?? null;
  const taskId = chat.task?.task_id ?? null;
  // Tarefa/sessão nova limpam a marca; a mesma sessão+tarefa mantém a conclusão expirada.
  if (nodeExpiredDone && !nodeDoneExpiryMatches(nodeExpiredDone, sessionId, taskId)) {
    nodeExpiredDone = null;
  }
  const next = applyNodeDoneExpiry(
    deriveNodeStatus(nodeStatusInputFromProjection(chat, Boolean(lastSession))),
    nodeExpiredDone,
    sessionId,
    taskId
  );
  const noticeChanged = nodeTerminalNotice.observe({
    session_id: sessionId, task_id: taskId, status: next, gesture: nodeGestureSeq,
  });
  if (next === nodeStatus) {
    // Outra tarefa pode acabar com o mesmo estado antes de o coalescer mostrar running.
    if (noticeChanged) {
      nodeSurfaceSuppressedFor = null;
      // Outra tarefa concluída: o prazo de 4 s passa a ser o dela, não o da anterior.
      if (next === "done") {
        if (nodeSuccessTimer) clearTimeout(nodeSuccessTimer);
        nodeSuccessTimer = null;
        startNodeSuccessTimer(taskId);
      }
      applyNodeSurfacePolicy();
      publishNode();
    }
    return;
  }
  nodeStatus = next;
  // Status novo: o gesto de recolha anterior deixa de valer.
  nodeSurfaceSuppressedFor = null;
  if (nodeSuccessTimer) {
    clearTimeout(nodeSuccessTimer);
    nodeSuccessTimer = null;
  }
  if (next === "done") startNodeSuccessTimer(chat.task?.task_id ?? null);
  applyNodeSurfacePolicy();
  publishNode();
}

function terminalNodeNoticeExpired(): boolean {
  return nodeTerminalNotice.expiredFor({
    session_id: lastSession?.session_id ?? null,
    task_id: chatProjection?.snapshot().task?.task_id ?? null,
    status: nodeStatus,
  });
}

/** Recolhe só a apresentação automática; o erro e o resultado continuam na projeção. */
function expireNodeTerminalNotice(notice: NodeTerminalNoticeSnapshot): void {
  const chat = chatProjection?.snapshot() ?? EMPTY_CHAT_PROJECTION;
  if (lastSession?.session_id !== notice.session_id || chat.task?.task_id !== notice.task_id ||
      nodeStatus !== notice.status || chat.confirm || chat.input) return;
  // A política preserva voz/chat ativos e recolhe o aviso quando estes devolvem o controlo.
  applyNodeSurfacePolicy();
  publishNode();
}

/** Sucesso recolhe após 4 s, sem fechar o chat humano nem apagar pendências. */
function startNodeSuccessTimer(taskId: string | null): void {
  const gesture = nodeGestureSeq;
  const sessionId = lastSession?.session_id ?? null;
  nodeSuccessTimer = setTimeout(() => {
    nodeSuccessTimer = null;
    if (nodeStatus !== "done") return;
    // Só expira a conclusão que armou o timer; outra sessão/tarefa tem o seu próprio prazo.
    const currentTask = chatProjection?.snapshot().task?.task_id ?? null;
    if (currentTask !== taskId || (lastSession?.session_id ?? null) !== sessionId) return;
    nodeStatus = "idle";
    // Conclusão expirada: só sai deste par sessão+tarefa. Um resync do companheiro não a revive.
    nodeExpiredDone = { session_id: sessionId, task_id: taskId };
    if (
      gesture === nodeGestureSeq &&
      nodePillAutomatic &&
      companionSnapshot.surface === "pill"
    ) {
      nodePillAutomatic = false;
      companionManager?.setSurface("docked");
    }
    applyNodeSurfacePolicy();
    publishNode();
  }, NODE_SUCCESS_VISIBLE_MS);
}

/**
 * Política fechada status→superfície no MAIN. Só age com a janela do Node visível; o chat humano
 * é preservado e nenhuma superfície automática pede foco.
 */
function applyNodeSurfacePolicy(): void {
  const manager = companionManager;
  const win = manager?.window;
  if (!manager || !win || win.isDestroyed() || !win.isVisible() || !presencePrefs.node.enabled) {
    return;
  }
  const voicePhase = backgroundVoice.phase;
  if (voicePhase && nodeStatus !== "needs_confirm" && nodeStatus !== "needs_input") {
    manager.setSurface(voicePhase === "error" ? "attention" : "pill", { focus: false });
    nodePillAutomatic = voicePhase !== "error";
    return;
  }
  const target = nodeSurfaceForStatus({
    status: nodeStatus,
    show_activity: presencePrefs.node.show_activity,
    current: companionSnapshot.surface,
    auto_pill: nodePillAutomatic,
    suppressed: nodeSurfaceSuppressedFor === nodeStatus,
    terminal_expired: terminalNodeNoticeExpired(),
  });
  if (target === "none") return;
  if (target === "docked") {
    nodePillAutomatic = false;
    manager.setSurface("docked");
    return;
  }
  if (manager.setSurface(target)) {
    nodePillAutomatic = target === "pill";
  }
}

function presenceSnapshot(): PresenceSnapshotDto {
  return {
    mode: presenceMode(),
    resident: presencePrefs.resident,
    session: lastSession,
    chat: chatProjection?.snapshot() ?? EMPTY_CHAT_PROJECTION,
    draft,
    theme: getTheme(appliedPrefs.theme).id,
    voice: lastVoiceState,
    node: nodeStateDto(),
    node_preferences: { ...presencePrefs.node },
  };
}

function sendPresenceTo(win: BrowserWindow | null, event: PresenceEventDto): void {
  if (!win || win.isDestroyed()) {
    return;
  }
  try {
    win.webContents.send("lunmi:presence-event", event);
  } catch {
    // janela em destruição: o evento seguinte volta a ser tentado
  }
}

function broadcastPresence(event: PresenceEventDto): void {
  sendPresenceTo(mainWindow, event);
  sendPresenceTo(companionManager?.window ?? null, event);
}

/**
 * Coalescer curto: agrupa deltas por frame sem depender de timer de janela oculta. Um delta
 * novo NUNCA substitui o anterior do mesmo intervalo — senão o primeiro turno de uma sessão
 * nova (reset + mensagem) desaparecia. `mergeChatDelta` mantém o cursor mais recente e as
 * mensagens por ordem.
 */
function publishChatDelta(delta: ChatProjectionDeltaDto): void {
  if (delta.reset && !delta.conversation_id) {
    conversationDrafts.clear();
    draftConversationId = '';
    setDraft('');
  } else if (delta.conversation_id && delta.conversation_id !== draftConversationId) {
    if (draftConversationId) conversationDrafts.set(draftConversationId, draft);
    draftConversationId = delta.conversation_id;
    setDraft(conversationDrafts.get(draftConversationId) ?? '');
  }
  // Toda a mudança da projeção passa aqui: é o único sítio onde o passivo N4 é sincronizado,
  // antes mesmo do coalescer, para a janela nunca aceitar um tick depois de a tarefa arrancar.
  syncNodePassive();
  pendingChatDelta = pendingChatDelta ? mergeChatDelta(pendingChatDelta, delta) : delta;
  if (chatDeltaTimer) {
    return;
  }
  chatDeltaTimer = setTimeout(flushChatDelta, 16);
}

/** Ligar/desligar o passivo no manager a partir da fase RAW; idempotente e sem foco. */
function syncNodePassive(): void {
  const running = nodePassiveFromTaskPhase(chatProjection?.taskPhase);
  if (running === nodePassive) return;
  nodePassive = running;
  companionManager?.setPassive(running);
}

function flushChatDelta(): void {
  if (chatDeltaTimer) {
    clearTimeout(chatDeltaTimer);
    chatDeltaTimer = null;
  }
  const delta = pendingChatDelta;
  pendingChatDelta = null;
  if (delta) {
    broadcastPresence({ type: "chat", delta });
  }
  // A projeção mudou: o estado do Node é reduzido aqui, no main, e não no renderer.
  syncNodePassive();
  refreshNodeStatus();
}

function persistPresence(): void {
  if (!presenceLocalDir) return;
  writePresenceFile(presenceLocalDir, presencePrefs);
}

function setDraft(text: string): void {
  draft = text.slice(0, MAX_DRAFT_CHARS);
  broadcastPresence({ type: "draft", text: draft });
}

function deliverShellRoute(view: "chat" | "preferences"): void {
  if (!mainRendererReady || !mainWindow || mainWindow.isDestroyed()) {
    pendingShellRoute = view;
    return;
  }
  pendingShellRoute = null;
  mainWindow.webContents.send("lunmi:event", {
    v: PROTOCOL_VERSION,
    id: `route-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
    type: "shell.route",
    payload: { view },
  });
}

function showMainWindow(route?: "chat" | "preferences"): void {
  if (quitting) return;
  backgroundVoice.dismiss();
  companionManager?.hide();
  if (!mainWindow || mainWindow.isDestroyed()) {
    mainWindow = createWindow();
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.show();
  mainWindow.focus();
  if (route) {
    deliverShellRoute(route);
  }
}

function restoreMainAfterCompanionFailure(reason: string): void {
  process.stderr.write(`[companion] ${reason}\n`);
  if (quitting) return;
  disposeNodeMedia();
  companionManager?.destroy();
  companionManager = null;
  nodePillAutomatic = false;
  nodeSurfaceSuppressedFor = null;
  // Sem janela não há superfície: o snapshot também não pode continuar a dizer «chat».
  companionSnapshot = {
    surface: "hidden",
    phase: "settled",
    transition_id: companionSnapshot.transition_id + 1,
    transition: null,
  };
  publishNode();
  // Enquanto a tarefa corre, uma falha do Node é fail-closed: não abrir a principal com foco.
  // A recuperação é manual (bandeja/gesto) depois do terminal, como no passivo normal.
  if (nodePassiveFromTaskPhase(chatProjection?.taskPhase)) return;
  showMainWindow();
}

function ensureCompanion(surface: NodeVisibleSurface = "docked"): boolean {
  // `resident` governa a existência do recurso; `node.enabled` só governa esta janela.
  if (!presencePrefs.resident || !presencePrefs.node.enabled || quitting) {
    return false;
  }
  if (!companionManager) {
    companionManager = new CompanionManager({
      preloadPath: path.join(__dirname, "preload.js"),
      indexHtmlPath: path.resolve(__dirname, "..", "..", "dist", "index.html"),
      iconPath: appIconPath(),
      preferences: {
        enabled: presencePrefs.node.enabled,
        side: presencePrefs.node.side,
        autoDock: presencePrefs.node.auto_dock,
      },
      onStateChange: (snapshot) => {
        companionSnapshot = snapshot;
        broadcastPresence({ type: "mode", mode: presenceMode() });
        publishNode();
      },
      onFailure: (reason) => {
        restoreMainAfterCompanionFailure(reason);
      },
      onRendererReady: () => {
        void backendSupervisor?.syncHostPids().catch(() => undefined);
      },
      onRendererGone: () => {
        void backendSupervisor?.syncHostPids().catch(() => undefined);
        if (companionManager) {
          restoreMainAfterCompanionFailure("companion_renderer_gone");
        }
      },
    });
  }
  // Janela criada já com a tarefa a correr: entra passiva (sem foco, input atrás) antes de
  // aparecer. Nenhum caminho abre o Node com input ativo em `running`.
  companionManager.setPassive(nodePassive);
  const shown = companionManager.show(surface);
  if (!shown) {
    companionManager?.destroy();
    companionManager = null;
    restoreMainAfterCompanionFailure("companion_unavailable");
    return false;
  }
  // Um Node aberto com tarefa a correr já entra na superfície certa (policy do main).
  applyNodeSurfacePolicy();
  // O renderer auxiliar entra no inventário de PIDs próprios antes de qualquer automação.
  void backendSupervisor?.syncHostPids().catch(() => undefined);
  return true;
}

function hideMainToCompanion(): void {
  if (!presencePrefs.resident || quitting) {
    void requestQuit();
    return;
  }
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  mainWindow.hide();
  // Node desligado + residente ligado: X esconde para a bandeja, backend vivo, sem janela nova.
  if (!presencePrefs.node.enabled) {
    return;
  }
  if (!ensureCompanion("docked") && !quitting) {
    showMainWindow();
  }
}

async function stopVoice(): Promise<void> {
  if (!backendSupervisor?.isRunning) return;
  try {
    await backendSupervisor._sendInternalCommand("voice.stop", {}, 5000);
  } catch {
    // Voz é opcional: parar uma voz já parada não é erro visível.
  }
}

/**
 * Bloqueio de ecrã e suspensão param a fala pelo mesmo `voice.stop` da bandeja/companheiro:
 * o worker invalida a geração e os buffers pendentes. Não há retoma automática — voltar do
 * bloqueio/suspensão não abre microfone, não reinicia TTS nem repete a tarefa.
 */
type PowerMonitorLike = {
  on(event: "lock-screen", listener: () => void): unknown;
  on(event: "suspend", listener: () => void): unknown;
};

/**
 * Bloqueio/suspensão cancelam primeiro a captura e só depois a fala. Nenhum evento de regresso
 * é ligado: voltar do bloqueio não abre microfone nem retoma o TTS.
 */
export function registerPowerVoiceGuards(
  monitor: PowerMonitorLike,
  stop: () => void,
  cancelInput?: () => void
): void {
  const onSuspend = () => {
    cancelInput?.();
    stop();
  };
  monitor.on("lock-screen", onSuspend);
  monitor.on("suspend", onSuspend);
}

// ---------------------------------------------------------------------------
// VOICE REFINEMENTS V2: fases hold main-only (sem start/finish/chat.send)
// ---------------------------------------------------------------------------

function publishVoiceHoldPhase(phase: VoiceInputHoldPhaseDto): void {
  const background = !mainWindow?.isVisible() && presencePrefs.resident && presencePrefs.node.enabled;
  if (background && phase.phase === "pressed") nodeGestureSeq += 1;
  backgroundVoice.hold(phase, background);
  refreshBackgroundVoice();
  if (!mainWindow || mainWindow.isDestroyed()) {
    return;
  }
  try {
    mainWindow.webContents.send("lunmi:voice-input-hold", phase);
  } catch {
    // janela em destruição: a fase seguinte volta a ser tentada
  }
}

/**
 * Cancelamento de ciclo de vida (lock/suspend/quit/restart/shutdown): finito e sem retoma.
 * No maximo uma chamada `voice.input.cancel`; nunca sintetiza release, finish ou envio.
 */
async function cancelVoiceInputCapture(): Promise<void> {
  if (!backendSupervisor?.isRunning) {
    return;
  }
  try {
    await backendSupervisor._sendInternalCommand("voice.input.cancel", {}, 4000);
  } catch {
    // captura já inativa, árvore morta ou sessão em encerramento: nada a retomar
  }
}

/**
 * Um único `voice.input.cancel` por ciclo de vida: se havia gesto hold, o coordenador já o
 * envia; se não havia, ainda pode existir ditado manual para cancelar. Nunca sintetiza release.
 */
function cancelVoiceInputForLifecycle(): void | Promise<void> {
  backgroundVoice.reset();
  refreshBackgroundVoice();
  const holdActive = voiceHoldCoordinator?.isActive() === true;
  const pending = voiceHoldCoordinator?.cancel("lifecycle");
  return holdActive ? pending : cancelVoiceInputCapture();
}

/** A completed, acknowledged voice task reveals only its already projected conversation. */
function refreshBackgroundVoice(): void {
  if (backgroundVoice.takeCompleted() && !quitting && !mainWindow?.isVisible() &&
      presencePrefs.resident && presencePrefs.node.enabled) {
    syncNodePassive();
    if (!nodePassive) companionManager?.setSurface("chat", { focus: false });
  }
  if (backgroundVoice.phase && !mainWindow?.isVisible() && presencePrefs.resident &&
      presencePrefs.node.enabled && !companionManager) ensureCompanion("pill");
  applyNodeSurfacePolicy();
  publishNode();
}

function setupVoiceInputShortcutIpc(): void {
  ipcMain.handle("lunmi:voice-input-shortcut-get", async (event) => {
    if (!isTrustedMain(event)) {
      throw new Error("Sender não autorizado.");
    }
    return voiceShortcutStore
      ? await voiceShortcutStore.get()
      : { accelerator: null, error: "" };
  });

  ipcMain.handle("lunmi:voice-input-shortcut-set", async (event, args: unknown) => {
    if (!isTrustedMain(event)) {
      throw new Error("Sender não autorizado.");
    }
    if (!voiceShortcutStore) {
      return { accelerator: null, error: "io_error" } satisfies VoiceInputShortcutResultDto;
    }
    const request = parseVoiceShortcutSetRequest(args);
    if (!request.ok) {
      return {
        accelerator: (await voiceShortcutStore.get()).accelerator,
        error: "invalid_shortcut",
      } satisfies VoiceInputShortcutResultDto;
    }
    // Alterar o atalho nunca pode autorizar um release pendente do gesto antigo.
    voiceHoldCoordinator?.cancel("reconfigured");
    return voiceShortcutStore.set(request.accelerator);
  });

  ipcMain.handle("lunmi:voice-input-shortcut-pause", async (event) => {
    if (!isTrustedMain(event)) {
      throw new Error("Sender não autorizado.");
    }
    voiceHoldCoordinator?.cancel("pause");
    return voiceShortcutStore
      ? await voiceShortcutStore.pause()
      : { ok: false, error: "hold_unavailable" };
  });

  ipcMain.handle("lunmi:voice-input-shortcut-resume", async (event) => {
    if (!isTrustedMain(event)) {
      throw new Error("Sender não autorizado.");
    }
    return voiceShortcutStore
      ? await voiceShortcutStore.resume()
      : { ok: false, error: "hold_unavailable" };
  });
}

/**
 * Comandos que alteram a projeção: o backend é a autoridade. Só depois de a promessa resolver
 * (comando realmente aceite) o main retira a pendência consumida ou bloqueia o stream cancelado,
 * uma única vez para as duas vistas. Recusa/erro/terminal concorrente não tocam em nada.
 */
async function dispatchRendererCommand(
  type: string,
  payload: Record<string, unknown>,
  fromMain = false
): Promise<Record<string, unknown>> {
  if (updates?.installing) throw new Error("update_installing: Reinício para atualização em curso.");
  if (!backendSupervisor) {
    throw new Error("Bridge não inicializada.");
  }
  // Sessão original do pedido: um ACK que chega depois de um reinício não toca na sessão nova.
  const sessionId = chatProjection?.sessionId;
  const ticket = backgroundVoice.begin(type, payload, fromMain);
  let result: Record<string, unknown>;
  try {
    result = await backendSupervisor.sendRendererCommand(type, payload);
    backgroundVoice.end(type, result, ticket);
  } catch (error) {
    backgroundVoice.failed(ticket);
    refreshBackgroundVoice();
    throw error;
  }
  refreshBackgroundVoice();
  if (chatProjection) {
    applyProjectionCommand(chatProjection, type, payload, result ?? null, sessionId);
  }
  return result;
}

function isCompanionAction(value: unknown): value is PresenceAction {
  return isCompanionPresenceAction(value);
}

async function applyPresenceAction(
  action: PresenceAction,
  fromCompanion = false
): Promise<PresenceActionResultDto> {
  // N4: com a tarefa a correr o companheiro não abre nada nem rouba foco. Lido da fase RAW no
  // momento da ação (nunca de um estado memorizado). A principal e a bandeja não passam por aqui
  // para recuperar: cancelar/abrir continuam a ser gestos humanos válidos.
  if (
    fromCompanion &&
    nodePassiveFromTaskPhase(chatProjection?.taskPhase) &&
    nodePassiveBlocksAction(action)
  ) {
    return {
      ok: false,
      mode: presenceMode(),
      resident: presencePrefs.resident,
      error: NODE_PASSIVE_REASON,
    };
  }
  if (["show-main", "show-settings", "hide-mini", "toggle-mini", "node-open", "node-close", "node-dock", "quit"].includes(action)) {
    backgroundVoice.dismiss();
    publishNode();
  }
  let error: string | undefined;
  switch (action) {
    case "show-main": {
      const chat = chatProjection?.snapshot();
      showMainWindow(chat && (chat.confirm || chat.input) ? "chat" : undefined);
      break;
    }
    case "show-settings":
      showMainWindow("preferences");
      break;
    case "toggle-mini": {
      // Legado: abrir/fechar o chat a partir do orb antigo.
      nodeGestureSeq += 1;
      nodePillAutomatic = false;
      if (!presencePrefs.resident || !presencePrefs.node.enabled) {
        showMainWindow();
      } else if (companionSnapshot.surface === "chat") {
        companionManager?.setSurface("docked");
      } else if (!ensureCompanion("chat")) {
        error = "companion_unavailable";
      }
      if (companionSnapshot.surface === "docked") nodeSurfaceSuppressedFor = nodeStatus;
      break;
    }
    case "hide-mini":
      // Fechar o chat recolhe o Node; não destrói nem esconde a janela.
      nodeGestureSeq += 1;
      nodePillAutomatic = false;
      if (presencePrefs.resident && presencePrefs.node.enabled) {
        companionManager?.setSurface("docked");
      }
      if (companionSnapshot.surface === "docked") nodeSurfaceSuppressedFor = nodeStatus;
      break;
    case "node-reveal":
    case "node-open":
    case "node-close":
    case "node-dock": {
      if (!presencePrefs.resident || !presencePrefs.node.enabled) {
        error = "node_disabled";
        break;
      }
      if (!companionManager) {
        // Um clique no arco só existe com a janela viva; sem ela, nada a fazer.
        error = companionSnapshot.surface === "hidden" ? "node_hidden" : "companion_unavailable";
        break;
      }
      const wasChat = companionSnapshot.surface === "chat";
      nodeGestureSeq += 1;
      nodePillAutomatic = false;
      if (action === "node-close" && wasChat) {
        // Fechar o chat com pendência volta a `attention`: o aviso não se perde.
        companionManager.setSurface(
          isNodeAttentionStatus(nodeStatus) && !terminalNodeNoticeExpired() ? "attention" : "revealed"
        );
      } else if (!companionManager.handleAction(action)) {
        error = "invalid_action";
      }
      if (companionSnapshot.surface === "docked") nodeSurfaceSuppressedFor = nodeStatus;
      break;
    }
    case "stop-voice":
      void stopVoice();
      break;
    case "cancel-dictation":
      void cancelVoiceInputForLifecycle();
      break;
    case "quit":
      setImmediate(() => void requestQuit());
      break;
    default:
      error = "invalid_action";
      break;
  }
  return {
    ok: !error,
    mode: presenceMode(),
    resident: presencePrefs.resident,
    ...(error ? { error } : {}),
  };
}

// ---------------------------------------------------------------------------
// NODE N1: atalho e preferências (escrita só pela janela principal)
// ---------------------------------------------------------------------------

/**
 * Atalho = um gesto humano: abre o chat com o compositor (o bloqueio durante `running` é N4).
 * Segundo toque recolhe o Node. Nunca abre só a cápsula nem rouba foco fora do chat.
 */
function toggleNodeFromShortcut(): void {
  if (quitting || !presencePrefs.resident || !presencePrefs.node.enabled) return;
  // N4: o atalho também é recusado enquanto a tarefa corre — não abre, não recolhe, não foca.
  // Mesma fase RAW do passivo; um atalho não pode furar a guarda que o companheiro respeita.
  if (nodePassiveFromTaskPhase(chatProjection?.taskPhase)) return;
  nodeGestureSeq += 1;
  nodePillAutomatic = false;
  if (companionSnapshot.surface === "chat") {
    companionManager?.setSurface("docked");
    nodeSurfaceSuppressedFor = nodeStatus;
    return;
  }
  ensureCompanion("chat");
}

function registerNodeShortcut(accelerator: string | null): boolean {
  if (!accelerator) return true;
  try {
    return globalShortcut.register(accelerator, toggleNodeFromShortcut);
  } catch {
    return false;
  }
}

/**
 * Troca de atalho com rollback: desregista o antigo, tenta o novo e, se o sistema recusar
 * (conflito com outra aplicação), volta a registar o anterior. O ficheiro só muda depois disto.
 */
function applyNodeShortcutChange(
  next: string | null,
  previous: string | null
): "ok" | "shortcut_conflict" | "shortcut_unavailable" {
  if (next === previous) return "ok";
  if (!next) {
    // Desregistar só acontece quando não há candidato a registar.
    if (previous) {
      try {
        globalShortcut.unregister(previous);
      } catch {
        // já livre
      }
    }
    return "ok";
  }
  // Regista o candidato PRIMEIRO: se outro processo o tiver, o binding antigo fica intacto.
  if (!registerNodeShortcut(next)) return "shortcut_conflict";
  if (previous) {
    try {
      globalShortcut.unregister(previous);
    } catch {
      // antigo já livre; o novo está garantido
    }
  }
  return "ok";
}

/**
 * Aplica ao runtime o que mudou no ficheiro, sem anunciar estado que não foi gravado.
 * `enabled:false` destrói apenas a janela do Node; a sessão, a conversa e a principal ficam vivas.
 */
function applyNodeRuntimeChange(previous: NodePreferencesDto, next: NodePreferencesDto): void {
  if (!next.enabled) {
    disposeNodeMedia();
    backgroundVoice.dismiss();
    companionManager?.destroy();
    companionManager = null;
    nodePillAutomatic = false;
    nodeSurfaceSuppressedFor = null;
    companionSnapshot = {
      surface: "hidden",
      phase: "settled",
      transition_id: companionSnapshot.transition_id + 1,
      transition: null,
    };
    // Nunca deixar a aplicação sem janela visível: com residente ligado, abre a principal.
    if (!quitting && (!mainWindow || !mainWindow.isVisible())) {
      showMainWindow();
    }
    return;
  }
  if (companionManager) {
    // `applyPreferences` já recalcula o envelope quando o lado muda; um segundo retarget
    // só duplicaria ids de transição e trabalho de shape.
    companionManager.applyPreferences({
      enabled: next.enabled,
      side: next.side,
      autoDock: next.auto_dock,
    });
    return;
  }
  // Node ligado de novo com a principal escondida (bandeja): devolve o Node ao ecrã.
  if (previous.enabled === false && presencePrefs.resident && !quitting &&
      (!mainWindow || !mainWindow.isVisible())) {
    ensureCompanion("docked");
  }
}

function isTrustedMain(event: IpcMainInvokeEvent): boolean {
  return isTrustedSenderFrame(event, mainWindow, expectedIndexFileUrl);
}

function isTrustedCompanion(event: IpcMainInvokeEvent): boolean {
  return isTrustedSenderFrame(event, companionManager?.window ?? null, expectedIndexFileUrl);
}

/** Janela confiável para uma operação de presença; `null` para um sender hostil. */
function trustedPresenceWindow(event: IpcMainInvokeEvent): BrowserWindow | null {
  if (isTrustedMain(event)) {
    return mainWindow;
  }
  if (isTrustedCompanion(event)) {
    return companionManager?.window ?? null;
  }
  return null;
}

function disposeNodeMedia(): void {
  if (nodeMediaIdleTimer) clearTimeout(nodeMediaIdleTimer);
  nodeMediaIdleTimer = null;
  const previous = nodeMedia;
  nodeMedia = null;
  if (previous) void previous.dispose();
}

function currentNodeMedia(): WindowsMediaSession {
  if (!nodeMedia) nodeMedia = new WindowsMediaSession({ scriptPath: path.join(app.getAppPath(), "electron", "media-session.ps1") });
  if (nodeMediaIdleTimer) clearTimeout(nodeMediaIdleTimer);
  // Polling stops with the player; an inactive Node must not retain a helper process.
  nodeMediaIdleTimer = setTimeout(disposeNodeMedia, 30_000);
  return nodeMedia;
}

function setupPresenceIpc(): void {
  registerNodeMediaIpc({
    ipc: ipcMain,
    trusted: isTrustedCompanion,
    active: () => presencePrefs.resident && presencePrefs.node.enabled &&
      Boolean(companionManager?.window?.isVisible()) && companionSnapshot.surface === "chat" && companionSnapshot.phase === "settled",
    passive: () => nodePassiveFromTaskPhase(chatProjection?.taskPhase) ||
      Boolean(chatProjection?.snapshot().confirm || chatProjection?.snapshot().input),
    media: currentNodeMedia,
  });
  // WEB-SEARCH-V1: o renderer envia só IDs; a URL sai da projeção canónica em RAM.
  ipcMain.handle(
    "lunmi:web-open-source",
    createWebSourceOpenHandler({
      trustedWindow: (event) => trustedPresenceWindow(event as IpcMainInvokeEvent),
      snapshot: () => chatProjection?.snapshot() ?? null,
      opener: webSourceOpener,
    })
  );

  ipcMain.handle("lunmi:presence-snapshot", (event) => {
    if (!isTrustedMain(event) && !isTrustedCompanion(event)) {
      throw new Error("Sender não autorizado.");
    }
    return presenceSnapshot();
  });

  ipcMain.handle("lunmi:presence-action", async (event, args: { action?: unknown }) => {
    const fromCompanion = isTrustedCompanion(event);
    const fromMain = isTrustedMain(event);
    if (!fromMain && !fromCompanion) {
      throw new Error("Sender não autorizado.");
    }
    const action = args?.action;
    if (!isPresenceAction(action)) {
      throw new Error(`Ação de presença inválida: use apenas ${PRESENCE_ACTIONS.join(", ")}.`);
    }
    if (fromCompanion && !isCompanionAction(action)) {
      return {
        ok: false,
        mode: presenceMode(),
        resident: presencePrefs.resident,
        error: "forbidden_action",
      } satisfies PresenceActionResultDto;
    }
    // Preferências do Node e o próprio Node pertencem ao companheiro; a principal só escreve
    // preferências pelo canal próprio.
    if (fromMain && (NODE_ACTIONS as readonly string[]).includes(action)) {
      return {
        ok: false,
        mode: presenceMode(),
        resident: presencePrefs.resident,
        error: "forbidden_action",
      } satisfies PresenceActionResultDto;
    }
    // `quit` responde antes de encerrar, para o renderer não ficar com uma promessa pendente.
    if (action === "quit") {
      setImmediate(() => void requestQuit());
      return { ok: true, mode: presenceMode(), resident: presencePrefs.resident };
    }
    return applyPresenceAction(action, fromCompanion);
  });

  ipcMain.handle("lunmi:presence-draft", (event, args: { text?: unknown; conversation_id?: unknown }) => {
    if (!isTrustedMain(event) && !isTrustedCompanion(event)) {
      throw new Error("Sender não autorizado.");
    }
    if (typeof args?.text !== "string") {
      return { ok: false };
    }
    if (args.conversation_id !== undefined && args.conversation_id !== draftConversationId) return { ok: false };
    setDraft(args.text);
    return { ok: true };
  });

  ipcMain.handle("lunmi:presence-resident", (event, args: { resident?: unknown }) => {
    if (!isTrustedMain(event)) {
      throw new Error("Sender não autorizado.");
    }
    if (typeof args?.resident !== "boolean") {
      throw new Error("invalid_payload: resident deve ser booleano.");
    }
    // Estado só muda depois do write confirmado: uma falha de disco não anuncia uma opção
    // que não volta no próximo arranque.
    const next = { ...presencePrefs, resident: args.resident };
    if (!writePresenceFile(presenceLocalDir, next)) {
      return toPresencePreferencesDto(presencePrefs);
    }
    presencePrefs = next;
    broadcastPresence({ type: "resident", resident: presencePrefs.resident });
    if (!presencePrefs.resident) {
      disposeNodeMedia();
      companionManager?.destroy();
      companionManager = null;
      companionSnapshot = {
        surface: "hidden",
        phase: "settled",
        transition_id: companionSnapshot.transition_id + 1,
        transition: null,
      };
      publishNode();
      showMainWindow();
    }
    return toPresencePreferencesDto(presencePrefs);
  });

  // Deprecated em N1: não há arrasto livre nem hit-test manual; a shape nativa é a autoridade.
  // Mantêm-se para um renderer antigo não ficar com promessas pendentes.
  ipcMain.handle("lunmi:presence-drag", (event) => {
    if (!isTrustedCompanion(event)) {
      throw new Error("Sender não autorizado.");
    }
    return { ok: false };
  });

  ipcMain.handle("lunmi:presence-drag-end", (event) => {
    if (!isTrustedCompanion(event)) {
      throw new Error("Sender não autorizado.");
    }
    return { ok: false };
  });

  ipcMain.handle("lunmi:presence-hit-test", (event) => {
    if (!isTrustedCompanion(event)) {
      throw new Error("Sender não autorizado.");
    }
    return { ok: true };
  });

  ipcMain.handle("lunmi:node-preferences", (event, args: { patch?: unknown }) => {
    if (!isTrustedMain(event)) {
      throw new Error("Sender não autorizado.");
    }
    const previous = presencePrefs.node;
    const applied = applyNodePreferencesPatch(previous, args?.patch);
    if (!applied.ok) {
      return { ok: false, preferences: previous, error: "invalid_payload" } satisfies NodePreferencesResultDto;
    }
    const next = applied.next;
    if (next.shortcut !== previous.shortcut) {
      const outcome = applyNodeShortcutChange(next.shortcut, previous.shortcut);
      if (outcome !== "ok") {
        return { ok: false, preferences: previous, error: outcome } satisfies NodePreferencesResultDto;
      }
    }
    const persisted: PresencePreferences = { ...presencePrefs, node: next };
    if (!writePresenceFile(presenceLocalDir, persisted)) {
      // Disco recusou: desfaz também o atalho para o runtime não divergir do ficheiro.
      if (next.shortcut !== previous.shortcut) {
        applyNodeShortcutChange(previous.shortcut, next.shortcut);
      }
      return { ok: false, preferences: previous, error: "io_error" } satisfies NodePreferencesResultDto;
    }
    presencePrefs = persisted;
    applyNodeRuntimeChange(previous, next);
    broadcastPresence({ type: "node-prefs", preferences: next });
    // `show_activity` mudou: a cápsula automática pode aparecer/recolher agora.
    applyNodeSurfacePolicy();
    publishNode();
    return { ok: true, preferences: next } satisfies NodePreferencesResultDto;
  });

  ipcMain.handle(
    "lunmi:presence-command",
    async (event, args: { type?: unknown; payload?: unknown }) => {
      if (!isTrustedCompanion(event)) {
        throw new Error("Sender não autorizado.");
      }
      if (!backendSupervisor) {
        throw new Error("Bridge não inicializada.");
      }
      const type = args?.type;
      if (typeof type !== "string") {
        throw new Error("Comando inválido.");
      }
      if (!COMPANION_COMMANDS.has(type)) {
        throw new Error(`forbidden_command: O companheiro não pode enviar ${type}.`);
      }
      const payload = isPlainObject(args?.payload) ? args.payload : {};
      return dispatchRendererCommand(type, payload);
    }
  );
}

function setupTray(): void {
  if (tray) return;
  try {
    const icon = nativeImage.createFromPath(appIconPath());
    tray = new Tray(icon.isEmpty() ? appIconPath() : icon);
    tray.setToolTip("Lunmi");
    tray.setContextMenu(
      Menu.buildFromTemplate([
        { label: "Abrir Lunmi", click: () => showMainWindow() },
        { label: "Configurações", click: () => showMainWindow("preferences") },
        { label: "Parar voz", click: () => void stopVoice() },
        { type: "separator" },
        { label: "Sair", click: () => void requestQuit() },
      ])
    );
    tray.on("double-click", () => showMainWindow());
  } catch (err) {
    // Bandeja é conveniência: sem ícone a aplicação continua a funcionar pela janela.
    process.stderr.write(`[tray] ${(err as Error).message}\n`);
    tray = null;
  }
}

/** Caminho único e idempotente de encerramento: voz, bridge, workers, janelas e bandeja. */
function updateRestartBlocked(): boolean {
  const projection = chatProjection?.snapshot();
  return quitting || backendSupervisor?.hasPendingCommands === true ||
    projection?.task?.phase === "running" || Boolean(projection?.confirm || projection?.input) ||
    voiceHoldCoordinator?.isActive() === true || Boolean(backgroundVoice.phase) || integrationConnecting ||
    ["loading", "synthesizing", "playing"].includes(lastVoiceState?.state ?? "");
}

function setupUpdates(): void {
  const installed = process.platform === "win32" && process.arch === "x64" && app.isPackaged && !process.defaultApp;
  // Lazy load: development and IPC fixtures never initialize electron-updater or access a feed.
  const updater = installed ? new (require("electron-updater").NsisUpdater)({
    provider: "github", owner: "MateusCosta888", repo: "Lunmi",
  }) as import("electron-updater").NsisUpdater : null;
  // Local diagnosis for failed updates; the renderer still only receives phase/error codes.
  // shortcut: no rotation, a few lines per six-hour check; rotate if it ever grows.
  const updateLog = path.join(getUserDataRoot(), "logs", "updater.log");
  const log = (level: string) => (message?: unknown) => {
    try {
      fs.mkdirSync(path.dirname(updateLog), { recursive: true });
      fs.appendFileSync(updateLog, `${new Date().toISOString()} ${level} ${String(message)}\n`);
    } catch { /* diagnóstico opcional; nunca bloqueia a atualização */ }
  };
  if (updater) { updater.logger = { info: log("info"), warn: log("warn"), error: log("error") }; updater.disableWebInstaller = true; }
  updates = new LunmiUpdates({
    version: app.getVersion(), updater, publicKey: updateTrust.publicKey,
    publish: state => {
      if (updater) log("state")(JSON.stringify(state));
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("lunmi:updates-state", state);
    },
    blocked: updateRestartBlocked,
    confirmRestart: async () => {
      if (!mainWindow || mainWindow.isDestroyed()) return false;
      const result = await dialog.showMessageBox(mainWindow, {
        type: "question", title: "Atualizar a Lunmi", buttons: ["Mais tarde", "Reiniciar e atualizar"],
        defaultId: 0, cancelId: 0, noLink: true,
        message: "Reiniciar para instalar a atualização?",
        detail: "Guarda primeiro as alterações nos formulários. Rascunhos de conversa e alterações por guardar serão perdidos. O histórico e as configurações guardadas serão mantidos.\n\nA Lunmi fecha e o instalador mostra o progresso. A instalação pode demorar alguns minutos; no fim, a Lunmi abre sozinha.",
      });
      return result.response === 1;
    },
    finish: () => requestQuit(() => {
      updater!.once("error", () => {
        dialog.showErrorBox("Atualização não iniciada", "Não foi possível iniciar o instalador. Abre novamente a Lunmi para continuar na versão atual e tentar de novo.");
        app.quit();
      });
      updater!.quitAndInstall(true, true);
    }),
  });
  for (const action of ["get", "check", "install"] as const) {
    ipcMain.handle(`lunmi:updates-${action}`, event => {
      if (!isTrustedMain(event)) throw new Error("Sender não autorizado.");
      return updates![action]();
    });
  }
  updates.start();
}

function requestQuit(afterCleanup?: () => void): Promise<void> {
  if (quitPromise) return quitPromise;
  quitting = true;
  disposeNodeMedia();
  updates?.dispose();
  if (chatDeltaTimer) {
    clearTimeout(chatDeltaTimer);
    chatDeltaTimer = null;
  }
  pendingChatDelta = null;
  nodeExpiredDone = null;
  nodeTerminalNotice.dispose();
  if (nodeSuccessTimer) {
    clearTimeout(nodeSuccessTimer);
    nodeSuccessTimer = null;
  }
  if (presencePrefs.node.shortcut) {
    try {
      globalShortcut.unregister(presencePrefs.node.shortcut);
    } catch {
      // atalho já livre
    }
  }
  // Atalho próprio do ditado: cancelar a fase e libertar sem tocar nas preferências (Node intacto).
  const voiceCancel = cancelVoiceInputForLifecycle();
  voiceShortcutStore?.dispose();
  companionManager?.destroy();
  companionManager = null;
  try {
    tray?.destroy();
  } catch {
    // bandeja já removida
  }
  tray = null;
  quitPromise = (async () => {
    try {
      // Cancelar a captura antes de fechar a bridge; nada é retomado ao voltar a abrir.
      await voiceCancel;
      if (afterCleanup) await backendSupervisor?.shutdownForUpdate();
      else await backendSupervisor?.shutdown();
    } catch {
      if (afterCleanup) {
        dialog.showErrorBox("Atualização adiada", "Não foi possível concluir o encerramento seguro. A atualização não será instalada; abre novamente a Lunmi para continuar.");
        app.quit();
        return;
      }
      // o supervisor já tem fallback de kill; sair não pode ficar preso
    }
    if (afterCleanup) afterCleanup();
    else app.quit();
  })();
  return quitPromise;
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
    title: "Lunmi",
    icon: appIconPath(),
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
  mainRendererReady = false;

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
  win.on("close", (event) => {
    persistWindowBounds();
    // X com o recurso residente ativo oculta e mostra o orb; «Sair» é o único encerramento.
    if (quitting || !presencePrefs.resident) {
      return;
    }
    event.preventDefault();
    hideMainToCompanion();
  });
  win.on("maximize", broadcastWindowState);
  win.on("unmaximize", broadcastWindowState);
  win.on("minimize", broadcastWindowState);
  win.on("restore", broadcastWindowState);
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
  ipcMain.handle("lunmi:onboarding-open", async (event, args: { target?: unknown }) => {
    if (!isTrustedMain(event)) {
      throw new Error("Sender não autorizado.");
    }
    const url = typeof args?.target === "string" ? ONBOARDING_PAGES[args.target] : undefined;
    if (!url) return { opened: false };
    if (args.target === "groq-keys") groqKeyWatch.open(Date.now());
    await shell.openExternal(url);
    return { opened: true };
  });

  ipcMain.handle("lunmi:onboarding-groq-key", async (event) => {
    if (!isTrustedMain(event)) {
      throw new Error("Sender não autorizado.");
    }
    if (!groqKeyWatch.active(Date.now())) return { key: null };
    return { key: groqKeyWatch.take(await clipboard.readText(), Date.now()) };
  });

  ipcMain.handle("lunmi:hello", async (event) => {
    if (!isTrustedSenderFrame(event, mainWindow, expectedIndexFileUrl)) {
      throw new Error("Sender não autorizado.");
    }
    if (!backendSupervisor) {
      throw new Error("Bridge não inicializada.");
    }
    const ready = await backendSupervisor.startAndHandshake();
    handshakeCompletedMs = elapsedMs();
    lastSession = ready;
    chatProjection?.syncSession(ready);
    if (ready.capabilities.chat_history) await backendSupervisor.sendRendererCommand('chat.history.get', {});
    broadcastPresence({ type: "session", session: ready });
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
    mainRendererReady = true;
    applyNativeTheme();
    if (pendingShellRoute) {
      deliverShellRoute(pendingShellRoute);
    }
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
      if (updates?.installing) throw new Error("update_installing: Reinício para atualização em curso.");
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
        const previousTheme = appliedPrefs.theme;
        const saved = await backendSupervisor.sendRendererCommand(args.type, payload);
        // Read the persisted result so native chrome and renderer share the same theme.
        reloadAndApplyPreferences();
        if (appliedPrefs.theme !== previousTheme) {
          broadcastPresence({ type: "theme", theme: appliedPrefs.theme });
        }
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

      return dispatchRendererCommand(args.type, payload, true);
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

  ipcMain.handle("lunmi:pick-attachments", async (event) => {
    if (!isTrustedSenderFrame(event, mainWindow, expectedIndexFileUrl)) {
      throw new Error("Sender não autorizado.");
    }
    if (!mainWindow || mainWindow.isDestroyed() || !backendSupervisor) {
      return { cancelled: true, attachments: [], errors: [] };
    }
    const res = await dialog.showOpenDialog(mainWindow, {
      title: "Anexar arquivo de texto ou código (UTF-8)",
      properties: ["openFile", "multiSelections"],
      filters: [
        {
          name: "Texto e Código (UTF-8)",
          extensions: [
            "txt", "md", "json", "yaml", "yml", "toml", "ini", "cfg", "csv", "tsv", "log",
            "py", "ts", "tsx", "js", "jsx", "mjs", "cjs", "html", "css", "sql", "xml",
            "sh", "ps1", "c", "cpp", "h", "hpp", "rs", "go", "java", "cs", "rb", "php",
          ],
        },
        { name: "Todos os arquivos", extensions: ["*"] },
      ],
    });
    if (res.canceled || res.filePaths.length === 0) {
      return { cancelled: true, attachments: [], errors: [] };
    }

    const unsupportedSet = new Set<string>(UNSUPPORTED_ATTACHMENT_EXTENSIONS);
    const attachments: Array<{
      id: string;
      name: string;
      size_bytes: number;
      sha256: string;
      text: string;
    }> = [];
    const errors: string[] = [];
    let totalBytes = 0;

    const selectedPaths = res.filePaths.slice(0, MAX_CHAT_ATTACHMENTS);
    if (res.filePaths.length > MAX_CHAT_ATTACHMENTS) {
      errors.push(`Máximo de ${MAX_CHAT_ATTACHMENTS} arquivos por envio; os excedentes foram ignorados.`);
    }

    for (let i = 0; i < selectedPaths.length; i++) {
      const filePath = selectedPaths[i];
      const baseName = path.basename(filePath).trim();
      const ext = path.extname(baseName).toLowerCase();

      if (!baseName || baseName.length > MAX_ATTACHMENT_NAME_CHARS) {
        errors.push(`Nome de arquivo muito longo ou inválido: '${baseName.slice(0, 40)}'.`);
        continue;
      }
      if (unsupportedSet.has(ext)) {
        errors.push(
          `'${baseName}': formato (${ext || "binário"}) sem leitura real nesta versão; anexe apenas texto ou código UTF-8.`
        );
        continue;
      }

      try {
        const readRes = (await backendSupervisor._sendInternalCommand("fs.read", {
          path: filePath,
          max_bytes: MAX_ATTACHMENT_BYTES,
        })) as {
          bytes?: number;
          sha256?: string;
          text?: string;
        };
        const text = typeof readRes.text === "string" ? readRes.text : "";
        const sizeBytes =
          typeof readRes.bytes === "number" ? readRes.bytes : Buffer.byteLength(text, "utf8");
        if (sizeBytes <= 0) {
          errors.push(`'${baseName}': o arquivo está vazio.`);
          continue;
        }
        if (totalBytes + sizeBytes > MAX_TOTAL_ATTACHMENTS_BYTES) {
          errors.push(
            `'${baseName}': excede o limite total de ${MAX_TOTAL_ATTACHMENTS_BYTES} bytes somados nos anexos.`
          );
          continue;
        }
        totalBytes += sizeBytes;
        attachments.push({
          id: `att-${Date.now()}-${i}-${(readRes.sha256 || "").slice(0, 8)}`,
          name: baseName,
          size_bytes: sizeBytes,
          sha256: String(readRes.sha256 || ""),
          text,
        });
      } catch (err) {
        const rawMsg = err instanceof Error ? err.message : String(err);
        const cleanMsg = rawMsg.replace(/^[a-z0-9_]+:\s*/i, "");
        errors.push(`'${baseName}': ${cleanMsg}`);
      }
    }

    return { cancelled: false, attachments, errors };
  });

  ipcMain.handle("lunmi:window-control", async (event, args: { action?: unknown }) => {
    if (!isTrustedSenderFrame(event, mainWindow, expectedIndexFileUrl)) {
      throw new Error("Sender não autorizado.");
    }
    const action = args?.action;
    if (!isWindowControlAction(action)) {
      throw new Error(`Ação de janela inválida: use apenas ${WINDOW_CONTROL_ACTIONS.join(", ")}.`);
    }
    if (!mainWindow || mainWindow.isDestroyed()) {
      return { minimized: false, maximized: false, closed: true };
    }
    const result = applyWindowControl(mainWindow, action);
    return { ...(result ?? { minimized: false, maximized: false }), closed: result === null };
  });

  ipcMain.handle("lunmi:restart-backend", async (event) => {
    if (updates?.installing) throw new Error("update_installing: Reinício para atualização em curso.");
    if (!isTrustedSenderFrame(event, mainWindow, expectedIndexFileUrl)) {
      throw new Error("Sender não autorizado.");
    }
    if (!backendSupervisor) {
      throw new Error("Bridge não inicializada.");
    }
    // Reinício explícito do backend: refaz apenas o handshake numa sessão nova.
    // Não reexecuta nenhuma instrução anterior — repetir a tarefa é decisão do usuário.
    // A captura fecha antes de a árvore morrer e nunca é retomada na sessão nova.
    await cancelVoiceInputCapture();
    const ready = await backendSupervisor.restart();
    handshakeCompletedMs = elapsedMs();
    lastSession = ready;
    if (chatProjection?.syncSession(ready)) {
      setDraft("");
      lastVoiceState = null;
    }
    if (ready.capabilities.chat_history) await backendSupervisor.sendRendererCommand('chat.history.get', {});
    broadcastPresence({ type: "session", session: ready });
    return ready;
  });

  ipcMain.handle("lunmi:shutdown", async (event) => {
    if (!isTrustedSenderFrame(event, mainWindow, expectedIndexFileUrl)) {
      throw new Error("Sender não autorizado.");
    }
    if (!backendSupervisor) {
      return { status: "stopped", session_id: "" };
    }
    await cancelVoiceInputCapture();
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
  quitting = true;
  if (backendSupervisor) {
    await backendSupervisor.shutdown();
  }
  app.quit();
}

function isElectronMainRuntime(): boolean {
  return Boolean(process.versions?.electron) && Boolean(app) && typeof app.whenReady === "function";
}

/** Conflito de conjunto canónico com o atalho atual do Node; o Node fica sempre intacto. */
function nodeShortcutConflicts(canonical: string): boolean {
  const shortcut = presencePrefs.node.shortcut;
  if (!shortcut) {
    return false;
  }
  if (sameVoiceShortcut(canonical, shortcut)) {
    return true;
  }
  // Electron aceita aliases que não fazem parte da gramática do helper; no Windows
  // CommandOrControl/CmdOrCtrl são Ctrl e Command/Cmd são Super.
  const normalized = shortcut
    .replace(/CommandOrControl|CmdOrCtrl/g, "Ctrl")
    .replace(/Command|Cmd/g, "Super");
  return normalized !== shortcut && sameVoiceShortcut(canonical, normalized);
}

function bootstrap(): void {
  nativeTheme.themeSource = "dark";
  session.defaultSession.setPermissionRequestHandler((_wc, _permission, callback) => {
    callback(false);
  });

  const repoRoot = getRepoRoot();
  const resourcesRoot = getResourcesRoot();
  const userDataRoot = getUserDataRoot();
  const sandboxDir = process.env.LUNMI_SANDBOX_DIR || null;
  const handshakeOnly = process.env.LUNMI_HANDSHAKE_ONLY === "1";

  // Preferências de tela do escopo em uso: sandbox quando houver, senão o estado gravável
  // do utilizador (LOCALAPPDATA\Lunmi no pacote), nunca a pasta instalada.
  prefsLocalDir = path.join(sandboxDir ?? userDataRoot, ".local");
  presenceLocalDir = prefsLocalDir;
  appliedPrefs = readPreferencesFile(prefsLocalDir);
  presencePrefs = readPresenceFile(presenceLocalDir);
  chatProjection = new ChatProjectionStore(publishChatDelta);
  // VOICE REFINEMENTS V2: helper Windows owned; prefs e registo separados do Node.
  voiceHoldCoordinator = new VoiceHoldCoordinator({
    getSessionId: () => lastSession?.session_id ?? null,
    isQuitting: () => quitting || updates?.installing === true,
    publish: publishVoiceHoldPhase,
    cancelCapture: () => cancelVoiceInputCapture(),
    showMain: (phase) => {
      if (!backgroundVoice.backgroundRelease(phase)) showMainWindow("chat");
    },
  });
  voiceHotkey = new VoiceHotkeyObserver({
    python: resolvePythonExecutable(repoRoot, resourcesRoot),
    cwd: repoRoot,
    onPhase: (phase, reason) => voiceHoldCoordinator?.onPhase(phase, reason),
    onFailure: () => voiceShortcutStore?.handleObserverFailure(),
  });
  voiceShortcutStore = new VoiceInputShortcutStore(prefsLocalDir, voiceHotkey, {
    nodeConflict: (canonical) => nodeShortcutConflicts(canonical),
  });

  backendSupervisor = new PythonBackendSupervisor({
    repoRoot,
    resourcesRoot,
    userDataRoot,
    clientVersion: app.getVersion(),
    sandboxDir,
    handshakeOnly,
    // Aceite/sandbox: usa a configuração real (provider/modelo/credencial) só em RAM, com a
    // persistência confinada ao sandbox. Sem esta variável o comportamento é o de produção.
    settingsInRam: process.env.LUNMI_SETTINGS_IN_RAM === "1",
    getHostPids: collectLiveElectronHostPids,
    onIntegrationAuthOpen: (url) => shell.openExternal(url),
    onEvent: (envelope) => {
      if (envelope.type === "integrations.changed") {
        const gmail = (envelope.payload as Record<string, unknown>).gmail as { state?: string } | undefined;
        integrationConnecting = ["connecting", "composio_connected", "gmail_connecting"].includes(gmail?.state ?? "");
      }
      if (envelope.type === "session.ready") {
        const payload = envelope.payload as unknown as SessionReadyPayload;
        const newSession = lastSession?.session_id !== payload.session_id;
        lastSession = payload;
        // Sessão nova: nenhuma geração/gesto da sessão antiga sobrevive.
        if (newSession) {
          integrationConnecting = false;
          voiceHoldCoordinator?.sessionChanged();
          backgroundVoice.reset();
        }
        if (chatProjection?.syncSession(payload)) {
          setDraft("");
          lastVoiceState = null;
        }
        broadcastPresence({ type: "session", session: payload });
        refreshNodeStatus();
        // A sessão mudou mesmo com o status igual (`idle`): o `session_id` do Node é público.
        publishNode();
      } else if (envelope.type === "task.error") {
        const code = (envelope.payload as Record<string, unknown>).code;
        if (typeof code === "string" && BACKEND_FAILURE_CODES.has(code)) {
          integrationConnecting = false;
          lastSession = null;
          backgroundVoice.reset();
          voiceHoldCoordinator?.sessionChanged();
          broadcastPresence({ type: "session", session: null });
          refreshNodeStatus();
          publishNode();
        }
      }
      chatProjection?.apply(envelope);
      if (envelope.type === "voice.input.state") {
        const input = envelope.payload as Record<string, unknown>;
        if (input.session_id === lastSession?.session_id) backgroundVoice.input(input);
        refreshBackgroundVoice();
      } else if (envelope.type === "task.started") {
        backgroundVoice.taskStarted((envelope.payload as Record<string, unknown>).task_id);
      } else if (envelope.type === "task.cancelled") {
        backgroundVoice.cancelledTask((envelope.payload as Record<string, unknown>).task_id);
        refreshBackgroundVoice();
      } else if (envelope.type === "task.finished") {
        const finished = envelope.payload as Record<string, unknown>;
        const canonical = chatProjection?.snapshot().task ?? null;
        const result = canonical && canonical.task_id === finished.task_id ? canonical.result : null;
        // A projeção canónica é a autoridade: sem resultado correspondente não há mini chat.
        backgroundVoice.finished(finished.task_id, result?.status);
        refreshBackgroundVoice();
      }
      if (envelope.type === "voice.state") {
        lastVoiceState = envelope.payload as unknown as VoiceStateEventDto;
        sendPresenceTo(companionManager?.window ?? null, { type: "voice", state: lastVoiceState });
      }
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send("lunmi:event", envelope);
      }
    },
  });

  setupIpc();
  setupPresenceIpc();
  setupVoiceInputShortcutIpc();
  mainWindow = createWindow();
  setupUpdates();
  setupTray();
  // Windows a bloquear ou a suspender: cancelar captura e parar fala; nada retoma sozinho.
  registerPowerVoiceGuards(
    powerMonitor,
    () => {
      void stopVoice();
    },
    () => {
      // A fase hold é cancelada e o backend fecha imediatamente; o renderer não é necessário.
      void cancelVoiceInputForLifecycle();
    }
  );

  // N1: atalho guardado. Se o sistema o recusar no arranque, não fica anunciado como ativo.
  if (presencePrefs.node.shortcut && !registerNodeShortcut(presencePrefs.node.shortcut)) {
    presencePrefs = { ...presencePrefs, node: { ...presencePrefs.node, shortcut: null } };
    persistPresence();
  }
  // Regista o atalho de ditado depois do Node: um conflito limpa só o ditado, nunca o Node.
  void voiceShortcutStore.load();

  // Envelope do Node é recalculado no ecrã atual; uma transição em curso é descartada, sem replay.
  const retargetNode = () => {
    companionManager?.retarget();
  };
  screen.on("display-metrics-changed", retargetNode);
  screen.on("display-added", retargetNode);
  screen.on("display-removed", retargetNode);
  powerMonitor.on("resume", () => {
    companionManager?.resume();
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

if (isElectronMainRuntime()) {
  app.setName("Lunmi");
  if (process.platform === "win32") app.setAppUserModelId("org.lunmi.desktop");
  // O movimento é decidido pela preferência da Lunmi, não pelos "Efeitos de animação" do
  // Windows (o Chromium traduzia-os em prefers-reduced-motion e apagava orb e fundo). Os switches
  // só valem antes de `ready`, por isso a mudança aplica-se ao reiniciar.
  const motionPrefs = readPreferencesFile(path.join(process.env.LUNMI_SANDBOX_DIR || getUserDataRoot(), ".local"));
  app.commandLine.appendSwitch(motionPrefs.reduceMotion ? "force-prefers-reduced-motion" : "force-prefers-no-reduced-motion");

  // Uma instância: abrir de novo restaura a existente; nunca um segundo backend Python.
  const hasSingleInstanceLock = app.requestSingleInstanceLock();
  if (!hasSingleInstanceLock) {
    app.quit();
  } else {
    app.on("second-instance", () => {
      showMainWindow();
    });

    void app.whenReady().then(bootstrap);

    app.on("activate", () => {
      if (!quitting) showMainWindow();
    });

    app.on("before-quit", (event) => {
      if (quitting) return;
      event.preventDefault();
      void requestQuit();
    });

    app.on("window-all-closed", () => {
      if (quitting) {
        app.quit();
        return;
      }
      if (presencePrefs.resident) {
        // Sem janelas vivas com o recurso residente, a principal volta: nunca só uma interface
        // invisível. Não cria backend novo (a sessão é a mesma do supervisor).
        showMainWindow();
        return;
      }
      void requestQuit();
    });

    // `session-end` é específico do Windows e não está nas tipagens de `app` do Electron 44.
    type WindowsSessionEndApp = typeof app & { on(event: "session-end", listener: () => void): void };
    (app as WindowsSessionEndApp).on("session-end", () => {
      // Windows a desligar: limpeza síncrona possível; nenhuma promessa de JS de cleanup.
      quitting = true;
      void cancelVoiceInputForLifecycle();
      voiceShortcutStore?.dispose();
      companionManager?.destroy();
      companionManager = null;
      try {
        tray?.destroy();
      } catch {
        // bandeja já removida
      }
      tray = null;
    });
  }
}

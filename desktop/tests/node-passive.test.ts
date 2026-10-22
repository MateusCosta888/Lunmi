/**
 * N4 — modo passivo (`desktop/electron/companion.ts` + guarda partilhada).
 *
 * Regressões causais:
 * - cada tick do morph voltava a chamar `setIgnoreMouseEvents(false)` e devolvia input ao Node
 *   com a tarefa a correr;
 * - a guarda tem de ser a fase RAW (`running`), por isso `show_activity:false` (que esconde o
 *   `status`) não pode desligar o passivo;
 * - o atalho usa a mesma guarda: nenhuma ação de abertura do companheiro passa em `running`.
 *
 * O harness nativo do Root cobre a prova Win32; aqui fica a decisão e o ciclo da janela.
 */
import { afterEach, expect, it, vi } from "vitest";

const fake = vi.hoisted(() => ({
  windows: [] as any[],
  cursor: { x: 0, y: 0 },
  shapeFails: false,
  /** API nativa de input indisponível: `setIgnoreMouseEvents(true)` lança. */
  ignoreFails: false,
  /** Falha única, no próximo `setIgnoreMouseEvents(true)` (falha depois de já estar passivo). */
  failNextIgnore: false,
}));

vi.mock("electron", async () => {
  const { EventEmitter } = await import("node:events");
  class Window extends EventEmitter {
    visible = false;
    destroyed = false;
    bounds: any;
    shapes: any[] = [];
    options: any;
    ignoreCalls: boolean[] = [];
    focused = 0;
    focusable = false;
    blurs = 0;
    shows = 0;
    showsInactive = 0;
    webContents: any = Object.assign(new EventEmitter(), { setWindowOpenHandler() {} });
    constructor(options: any) {
      super();
      this.options = options;
      this.bounds = { x: options.x, y: options.y, width: options.width, height: options.height };
      fake.windows.push(this);
    }
    isDestroyed() { return this.destroyed; }
    isVisible() { return this.visible; }
    getBounds() { return this.bounds; }
    setBounds(bounds: any) { this.bounds = bounds; }
    showInactive() { this.showsInactive += 1; this.visible = true; this.emit("show"); }
    show() { this.shows += 1; this.visible = true; this.emit("show"); }
    hide() { this.visible = false; }
    blur() { this.blurs += 1; }
    destroy() { this.destroyed = true; this.emit("closed"); }
    removeMenu() {}
    setAlwaysOnTop() {}
    setIgnoreMouseEvents(ignore: boolean) {
      if (ignore && (fake.ignoreFails || fake.failNextIgnore)) {
        fake.failNextIgnore = false;
        throw Error("native input API unavailable");
      }
      this.ignoreCalls.push(ignore);
    }
    setFocusable(value: boolean) { this.focusable = value; }
    focus() { this.focused += 1; }
    loadFile() { return Promise.resolve(); }
    setShape(shape: any) { if (fake.shapeFails) throw Error("unsupported shape"); this.shapes.push(shape); }
  }
  return {
    BrowserWindow: Window,
    screen: {
      getPrimaryDisplay: () => ({ workArea: { x: 0, y: 0, width: 2560, height: 1400 } }),
      getDisplayMatching: () => ({ workArea: { x: 0, y: 0, width: 2560, height: 1400 } }),
      getCursorScreenPoint: () => fake.cursor,
    },
  };
});

import { CompanionManager, type CompanionSnapshot } from "../electron/companion";
import {
  NODE_PASSIVE_BLOCKED_ACTIONS,
  NODE_PASSIVE_REASON,
  nodePassiveBlocksAction,
  nodePassiveFromTaskPhase,
} from "../src/shared/node";

const managers: CompanionManager[] = [];

function create(failures: string[], snapshots: CompanionSnapshot[] = []) {
  const manager = new CompanionManager({
    preloadPath: "C:/qa/preload.js",
    indexHtmlPath: "C:/qa/index.html",
    preferences: { enabled: true, side: "left", autoDock: true },
    onFailure: reason => failures.push(reason),
    onStateChange: snapshot => snapshots.push({ ...snapshot }),
  });
  managers.push(manager);
  return manager;
}

function setup() {
  vi.useFakeTimers();
  fake.windows = [];
  fake.shapeFails = false;
  fake.ignoreFails = false;
  fake.failNextIgnore = false;
  const failures: string[] = [];
  const snapshots: CompanionSnapshot[] = [];
  const manager = create(failures, snapshots);
  expect(manager.show("docked")).toBe(true);
  return { manager, win: fake.windows.at(-1) as any, failures, snapshots };
}

afterEach(() => {
  managers.splice(0).forEach(manager => manager.destroy());
  vi.useRealTimers();
  fake.shapeFails = false;
  fake.ignoreFails = false;
  fake.failNextIgnore = false;
});

it("a guarda é a fase RAW: show_activity não entra na decisão", () => {
  expect(nodePassiveFromTaskPhase("running")).toBe(true);
  expect(nodePassiveFromTaskPhase("awaiting_confirm")).toBe(false);
  expect(nodePassiveFromTaskPhase("awaiting_input")).toBe(false);
  expect(nodePassiveFromTaskPhase("finished")).toBe(false);
  expect(nodePassiveFromTaskPhase(null)).toBe(false);
  expect(nodePassiveFromTaskPhase(undefined)).toBe(false);
  // Motivo público fixo e lista fechada do contrato: abrir/focar recusados, fechar/recolher não.
  expect(NODE_PASSIVE_REASON).toBe("passive_running");
  expect([...NODE_PASSIVE_BLOCKED_ACTIONS]).toEqual([
    "node-open",
    "toggle-mini",
    "show-main",
    "show-settings",
  ]);
  for (const action of ["node-close", "node-dock", "hide-mini", "stop-voice", "quit", "node-reveal"]) {
    expect(nodePassiveBlocksAction(action), action).toBe(false);
  }
});

it("cada tick do morph mantém o input ignorado e nunca foca a janela", () => {
  const { manager, win } = setup();
  manager.setPassive(true);
  const from = win.ignoreCalls.length;

  manager.setSurface("chat");
  vi.advanceTimersByTime(1200);

  expect(manager.currentSurface).toBe("chat");
  expect(manager.snapshot().phase).toBe("settled");
  expect(win.ignoreCalls.length).toBeGreaterThan(from);
  // Nenhum tick repôs input enquanto a tarefa corre (a regressão original era `false` por tick).
  expect(win.ignoreCalls.slice(from).every((ignore: boolean) => ignore === true)).toBe(true);
  expect(win.focused).toBe(0);
  expect(win.focusable).toBe(false);
  expect(win.shows).toBe(0);
  expect(vi.getTimerCount()).toBe(0);
});

it("sair do passivo repõe só o input; retomar exige gesto", () => {
  const { manager, win } = setup();
  manager.setPassive(true);
  manager.setPassive(false);

  expect(win.ignoreCalls.at(-1)).toBe(false);
  expect(win.focused).toBe(0);
  expect(win.shows).toBe(0);
  expect(win.focusable).toBe(false);

  // Um gesto humano posterior abre o chat e aí sim recebe foco.
  manager.handleAction("node-open");
  vi.advanceTimersByTime(1200);
  expect(manager.currentSurface).toBe("chat");
  expect(win.focused).toBe(1);
  expect(win.focusable).toBe(true);
});

it("se o input nativo falhar o Node esconde e não reaparece interativo durante a tarefa", () => {
  const { manager, win } = setup();
  fake.ignoreFails = true;

  manager.setPassive(true);
  expect(win.isVisible()).toBe(false);
  expect(win.focusable).toBe(false);

  // Caminho do `ensureCompanion`: não é falha fatal (não abre a principal com foco), mas
  // a janela não volta a aparecer por cima do trabalho do utilizador.
  expect(manager.show("docked")).toBe(true);
  expect(win.isVisible()).toBe(false);
  expect(win.shows + win.showsInactive).toBe(1);

  // Depois de o input voltar, um gesto humano recupera a janela normalmente.
  fake.ignoreFails = false;
  manager.setPassive(false);
  expect(win.isVisible()).toBe(false);
  expect(manager.show("docked")).toBe(true);
  expect(win.isVisible()).toBe(true);
  expect(win.focused).toBe(0);
});

it("input a falhar DEPOIS de entrar passivo esconde sem reportar falha (o main não abre a principal)", () => {
  const { manager, win, failures } = setup();
  manager.setPassive(true);
  expect(win.isVisible()).toBe(true);

  // Falha no caminho de shape (tick/retarget), já dentro do passivo.
  fake.failNextIgnore = true;
  manager.retarget();

  expect(win.isVisible()).toBe(false);
  expect(win.focused).toBe(0);
  expect(win.focusable).toBe(false);
  // Sem `onFailure` não há `restoreMainAfterCompanionFailure`/`showMainWindow` com foco.
  expect(failures).toEqual([]);

  // Nem um morph seguinte a faz reaparecer interativa.
  manager.setSurface("pill");
  vi.advanceTimersByTime(400);
  expect(win.isVisible()).toBe(false);
  expect(win.focused).toBe(0);
  expect(failures).toEqual([]);
});

it("janela criada durante a tarefa com input indisponível fica escondida sem reportar falha", () => {
  vi.useFakeTimers();
  fake.windows = [];
  fake.ignoreFails = true;
  const failures: string[] = [];
  const manager = create(failures);

  // Passivo marcado antes de a janela existir: o `show` já tem de nascer sem input.
  manager.setPassive(true);
  expect(manager.show("docked")).toBe(true);
  const win = fake.windows.at(-1) as any;
  expect(win.isVisible()).toBe(false);
  expect(win.focused).toBe(0);
  expect(failures).toEqual([]);
});

it("chat humano já aberto recupera focusable na saída, sem foco automático", () => {
  const { manager, win } = setup();
  manager.handleAction("node-open");
  vi.advanceTimersByTime(1200);
  expect(manager.currentSurface).toBe("chat");
  expect(win.focused).toBe(1);
  expect(win.focusable).toBe(true);

  manager.setPassive(true);
  expect(win.focusable).toBe(false);
  manager.setPassive(false);
  // Sem `show`/`focus` novo: só o próximo clique nativo recupera o teclado.
  expect(win.focused).toBe(1);
  expect(win.focusable).toBe(true);

  // Círculo/cápsula continuam sem foco ao sair do passivo.
  manager.setSurface("docked");
  vi.advanceTimersByTime(900);
  manager.setPassive(true);
  manager.setPassive(false);
  expect(win.focused).toBe(1);
  expect(win.focusable).toBe(false);
});

it("retarget não escreve no id da transição: a mudança seguinte não regride na publicação", () => {
  const { manager, snapshots } = setup();
  manager.setSurface("revealed");
  vi.advanceTimersByTime(500);
  expect(manager.currentSurface).toBe("revealed");

  // Ecrã/DPI: descarta a fila e assenta. Incrementar aqui o id da instância deixava-o à frente do
  // contador do módulo e a transição seguinte publicava um id menor (o hook descarta-o).
  expect(manager.retarget()).toBe(true);
  manager.setSurface("docked");
  vi.advanceTimersByTime(800);

  const ids = snapshots.map(snapshot => snapshot.transition_id);
  expect(ids.length).toBeGreaterThan(5);
  expect(ids.every((id, i) => i === 0 || id >= ids[i - 1]), JSON.stringify(snapshots)).toBe(true);
  expect(snapshots.at(-1)).toMatchObject({
    surface: "docked",
    phase: "settled",
    transition: null,
  });
});

it("entrar em passivo preserva o morph em curso: anima, assenta e nunca foca nem devolve input", () => {
  const { manager, win } = setup();
  manager.setSurface("chat");
  vi.advanceTimersByTime(80);
  expect(manager.snapshot().phase).toBe("transitioning");
  const ignoresAtEntry = win.ignoreCalls.length;
  const shapesAtEntry = win.shapes.length;

  manager.setPassive(true);
  // Sem salto: o morph aprovado continua em vez de assentar já no destino.
  expect(manager.snapshot().phase).toBe("transitioning");
  expect(vi.getTimerCount()).toBe(1);
  expect(win.ignoreCalls.at(-1)).toBe(true);

  vi.advanceTimersByTime(100);
  expect(win.shapes.length).toBeGreaterThan(shapesAtEntry);
  expect(win.ignoreCalls.slice(ignoresAtEntry).every((ignore: boolean) => ignore === true)).toBe(true);
  expect(win.focused).toBe(0);

  vi.advanceTimersByTime(1200);
  expect(manager.snapshot()).toMatchObject({ surface: "chat", phase: "settled", transition: null });
  expect(vi.getTimerCount()).toBe(0);
  expect(win.focused).toBe(0);
  expect(win.focusable).toBe(false);
  expect(win.shows).toBe(0);
});

it("em passivo não corre a custódia de 120 ms; ao sair, só re-arma se ainda revelado", () => {
  const { manager, win } = setup();
  fake.cursor = { x: win.bounds.x + 26, y: win.bounds.y + 8 };
  manager.handleAction("node-reveal");
  // Assentar primeiro: com ticks de 8 ms, 190 ms ainda pode não ter corrido o último tick
  // (o timer que sobra à entrada do passivo tem de ser o da custódia, não o do morph).
  vi.advanceTimersByTime(500);
  expect(manager.currentSurface).toBe("revealed");
  expect(manager.snapshot().phase).toBe("settled");
  expect(vi.getTimerCount()).toBe(1);

  manager.setPassive(true);
  expect(vi.getTimerCount()).toBe(0);

  // `node-reveal` continua permitido pela lista fechada, mas não re-arma o polling do cursor.
  expect(manager.handleAction("node-reveal")).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
  vi.advanceTimersByTime(2000);
  expect(manager.currentSurface).toBe("revealed");
  expect(vi.getTimerCount()).toBe(0);

  // Sair do passivo devolve o comportamento normal: custódia volta e recolhe fora do círculo.
  manager.setPassive(false);
  expect(vi.getTimerCount()).toBe(1);
  fake.cursor = { x: 1000, y: 1000 };
  vi.advanceTimersByTime(1200);
  expect(manager.currentSurface).toBe("docked");
  expect(vi.getTimerCount()).toBe(0);
  expect(win.focused).toBe(0);
});

it("show e retarget durante o passivo continuam sem input e sem foco", () => {
  const { manager, win } = setup();
  manager.setPassive(true);

  manager.hide();
  expect(manager.show("docked")).toBe(true);
  expect(win.isVisible()).toBe(true);
  expect(win.shows).toBe(0);
  expect(win.showsInactive).toBeGreaterThan(0);
  expect(win.ignoreCalls.at(-1)).toBe(true);

  manager.retarget();
  expect(win.ignoreCalls.at(-1)).toBe(true);
  expect(win.focused).toBe(0);
  expect(win.focusable).toBe(false);
});

/**
 * Janela do LUNMI NODE (`desktop/electron/companion.ts`).
 *
 * Uma única BrowserWindow por lado hospeda o envelope fixo. O main é a única autoridade de
 * superfície, transição e input. A janela é transparente e o desenho é CSS, com bordas suaves; a
 * shape nativa (`setShape`, em faixas) só define a região que recebe cliques. Fora dela o clique
 * passa para a aplicação de trás. A transição é CSS: a região cobre origem e destino e assenta
 * no fim, com um único temporizador.
 *
 * Regras que vêm do gate N0 e não podem regredir:
 * - a janela NUNCA é redimensionada nem movida durante um morph;
 * - a shape é aplicada depois de `show`, nunca antes;
 * - `setShape([])` significa rectângulo inteiro, por isso a geometria devolve sempre faixas;
 * - não há timer em repouso; a custódia de hover só corre em `revealed` e fora do passivo, lê
 *   o cursor do sistema, sem depender de `pointerleave`;
 * - um retarget (ecrã/DPI/suspensão) descarta a fila e assenta no destino, sem replay.
 */
import { BrowserWindow, screen } from "electron";
import { pathToFileURL } from "url";
import {
  beginNodeTransition,
  isNodeVisibleSurface,
  nextNodeSurface,
  NODE,
  NODE_ACTIONS,
  nodeEnvelopeBounds,
  nodeRetractionDecision,
  nodeShapeForSurface,
  nodeTransitionDuration,
  pointInNodeCustody,
  pointInRect,
  type NodeAction,
  type NodeBounds,
  type NodeVisibleSurface,
  type NodeTransitionState,
} from "../src/shared/node";
import type { NodeSide, NodeSurface, NodeTransitionDto, PresenceMode } from "../src/shared/types";

export interface CompanionSnapshot {
  surface: NodeSurface;
  phase: "settled" | "transitioning";
  transition_id: number;
  transition: NodeTransitionDto | null;
}

export interface CompanionPreferences {
  enabled: boolean;
  side: NodeSide;
  autoDock: boolean;
}

export interface CompanionManagerOptions {
  preloadPath: string;
  indexHtmlPath: string;
  iconPath?: string;
  preferences: CompanionPreferences;
  onStateChange?: (snapshot: CompanionSnapshot) => void;
  onFailure?: (reason: string) => void;
  onRendererReady?: () => void;
  onRendererGone?: () => void;
}

/** Intervalo de leitura do cursor enquanto o círculo está revelado (custódia). */
export const CUSTODY_TICK_MS = 120;

export class CompanionManager {
  private win: BrowserWindow | null = null;
  private prefs: CompanionPreferences;
  private side: NodeSide;
  private envelope: NodeBounds;
  private targetSurface: NodeSurface = "hidden";
  private focusChat = true;
  private phase: "settled" | "transitioning" = "settled";
  private transitionId = 0;
  private transition: NodeTransitionState | null = null;
  private pillWidth: number = NODE.pill_width;
  private morphTimer: NodeJS.Timeout | null = null;
  private custodyTimer: NodeJS.Timeout | null = null;
  private custodyOutsideSince: number | null = null;
  private destroying = false;
  /**
   * N4: tarefa a correr → input ignorado, janela nunca focável nem ativada. A superfície e o
   * desenho continuam a ser aplicados normalmente; só a região de input muda de dono. A custódia
   * pára em passivo; o morph em curso continua até assentar (ver `setPassive`).
   */
  private passive = false;
  /**
   * Falha fechada do passivo: o input nativo não pôde ser desligado. A janela fica escondida e
   * não reaparece interativa enquanto a tarefa correr; nenhuma janela por cima do ecrã.
   */
  private passiveHidden = false;

  constructor(private readonly options: CompanionManagerOptions) {
    this.prefs = { ...options.preferences };
    this.side = this.prefs.side;
    this.envelope = nodeEnvelopeBounds(this.side, this.workArea());
  }

  public get window(): BrowserWindow | null {
    return this.win;
  }

  public get currentSurface(): NodeSurface {
    return this.targetSurface;
  }

  /** Legado: `PresenceMode` derivado da superfície, só para o renderer antigo. */
  public get currentMode(): PresenceMode {
    if (this.targetSurface === "docked" || this.targetSurface === "revealed") return "orb";
    if (
      this.targetSurface === "pill" ||
      this.targetSurface === "chat" ||
      this.targetSurface === "attention"
    ) {
      return "mini";
    }
    return "hidden";
  }

  public snapshot(): CompanionSnapshot {
    return {
      surface: this.targetSurface,
      phase: this.phase,
      transition_id: this.transitionId,
      transition: this.transition
        ? {
            id: this.transition.id,
            from: this.transition.from,
            to: this.transition.to,
            duration_ms: this.transition.durationMs,
          }
        : null,
    };
  }

  /** Mostra a janela (se preciso) e assenta numa superfície; `docked` é o repouso normal. */
  public show(surface: NodeVisibleSurface = "docked"): boolean {
    if (!this.prefs.enabled) return false;
    if (!this.ensureWindow()) return false;
    const win = this.win;
    if (!win || win.isDestroyed()) return false;
    if (this.passive && this.passiveHidden) {
      // Input nativo indisponível: continua escondido. Devolver `true` evita que o main trate
      // isto como falha e abra a principal com foco — a recuperação é manual, depois do terminal.
      this.keepHidden(surface);
      return true;
    }
    if (!win.isVisible()) {
      // Antes de aparecer, garantir que o passivo consegue mesmo ignorar input. Falhou: fica
      // escondido em vez de aparecer interativo por cima do trabalho do utilizador.
      if (this.passive && !this.applyInputOwnership()) {
        this.keepHidden(surface);
        return true;
      }
      this.targetSurface = surface;
      this.phase = "settled";
      this.transition = null;
      // A shape é validada ANTES de mostrar: se falhar, a janela nunca aparece como envelope
      // rectangular sem input. Depois do show, o handler volta a aplicá-la (regra N0).
      if (!this.applyFinalShape(surface)) {
        if (this.passive || this.passiveHidden) {
          // Falha fechada em passivo: esconder e manter o pedido, sem destruir nem reportar
          // falha — reportar aqui faria o main abrir a principal com foco.
          this.keepHidden(surface);
          return true;
        }
        this.destroy();
        return false;
      }
      try {
        win.showInactive();
      } catch (err) {
        this.fail(`show_failed: ${(err as Error).message}`);
        this.destroy();
        return false;
      }
      this.applyFocusPolicy(surface);
      this.publish();
      this.updateCustody();
      return true;
    }
    this.setSurface(surface);
    return true;
  }

  public hide(): void {
    this.stopMorph();
    this.stopCustody();
    this.transition = null;
    this.phase = "settled";
    this.targetSurface = "hidden";
    if (this.win && !this.win.isDestroyed()) {
      try {
        this.win.hide();
      } catch {
        // esconder uma janela em destruição não é erro visível
      }
    }
    this.publish();
  }

  /**
   * Liga/desliga o modo passivo (N4). Idempotente: cada gesto de shape, `show`, transição e
   * retarget volta a aplicar a região certa a partir desta flag. Sair do passivo NÃO abre, não
   * foca e não muda a superfície — só devolve o input à aplicação de trás. Em passivo a custódia
   * pára, mas um morph já aprovado continua e assenta normalmente (sem salto para o destino).
   */
  public setPassive(passive: boolean): void {
    this.passive = passive;
    if (!passive) this.passiveHidden = false;
    // N5/F2: a custódia de 120 ms não corre em passivo. Uma transição em curso NÃO é cortado nem
    // assentado à força: continua a animar com o input já ignorado e assenta no destino.
    if (passive) this.stopCustody();
    this.applyInputOwnership();
    // Sair do passivo não abre nem foca: um chat humano que já estava aberto volta a aceitar
    // teclado no próximo clique (sem `show`/`focus` automáticos); círculo/cápsula ficam falsos.
    if (!passive) {
      this.restoreChatFocusable();
      // Comportamento normal de volta: a custódia só re-arma se ainda estiver `revealed`.
      this.updateCustody();
    }
  }

  /** Um chat já aberto por gesto recupera `focusable` sem roubar foco; nada é aberto. */
  private restoreChatFocusable(): void {
    const win = this.win;
    if (!win || win.isDestroyed() || !win.isVisible() || this.passiveHidden) return;
    if (this.targetSurface !== "chat") return;
    try {
      win.setFocusable(true);
    } catch {
      // Sem foco não há teclado, mas a janela continua utilizável por rato.
    }
  }

  /**
   * Região de input e foco coerentes com a flag passiva; nunca ativa a janela. Devolve `false`
   * quando o input nativo recusa o pedido — a janela é escondida (falha fechada) e deixará de
   * reaparecer interativa enquanto a tarefa correr.
   */
  private applyInputOwnership(): boolean {
    const win = this.win;
    if (!win || win.isDestroyed()) return true;
    if (process.platform !== "win32" && process.platform !== "linux") return true;
    try {
      if (this.passive) {
        win.setFocusable(false);
        win.blur();
        win.setIgnoreMouseEvents(true);
        return true;
      }
      win.setIgnoreMouseEvents(false);
      return true;
    } catch {
      if (!this.passive) return false;
      this.failClosedPassive();
      return false;
    }
  }

  /** Input nativo indisponível em passivo: esconder, sem abrir a principal nem pedir foco. */
  private failClosedPassive(): void {
    this.passiveHidden = true;
    const win = this.win;
    if (!win || win.isDestroyed()) return;
    try {
      win.hide();
    } catch {
      // uma janela em destruição não tem o que esconder
    }
  }

  /** Mantém o destino publicado mas sem tornar a janela visível (fallback do passivo). */
  private keepHidden(surface: NodeVisibleSurface): void {
    this.stopMorph();
    this.transition = null;
    this.phase = "settled";
    this.targetSurface = surface;
    this.publish();
  }

  /** Ações fechadas vindas do renderer; `false` = ignorada. */
  public handleAction(action: NodeAction): boolean {
    if (!this.prefs.enabled || this.targetSurface === "hidden") return false;
    if (action === "node-reveal" && this.targetSurface === "revealed") {
      // Reentrada no círculo re-arma a custódia sem criar transição.
      this.armCustody();
      return true;
    }
    const next = nextNodeSurface(this.targetSurface, action);
    if (!next || next === "hidden") return false;
    if (next === this.targetSurface) {
      if (next === "revealed") this.armCustody();
      if (next === "chat") { this.focusChat = true; this.applyFocusPolicy(next); }
      return true;
    }
    this.setSurface(next);
    return true;
  }

  public setSurface(target: NodeSurface, options: { focus?: boolean } = {}): boolean {
    if (!this.prefs.enabled) return false;
    if (!isNodeVisibleSurface(target) || target === this.targetSurface) return false;
    if (!this.win || this.win.isDestroyed()) return false;
    this.focusChat = options.focus !== false;

    // Interromper é seguro: a região cobre origem e destino, e o CSS parte do que está desenhado.
    const from = this.targetSurface;
    this.stopMorph();
    this.targetSurface = target;
    this.phase = "transitioning";
    this.stopCustody();
    const transition = beginNodeTransition({ from, to: target, durationMs: nodeTransitionDuration(target) });
    this.transition = transition;
    this.transitionId = transition.id;
    this.applyShape([
      ...(isNodeVisibleSurface(from) ? nodeShapeForSurface(from, this.side, this.envelope, this.pillWidth) : []),
      ...nodeShapeForSurface(target, this.side, this.envelope, this.pillWidth),
    ]);
    this.publish();
    this.morphTimer = setTimeout(() => this.finishMorph(), transition.durationMs);
    return true;
  }

  /** Se o ponto (ecrã, DIP) cai no que está desenhado; o resto do envelope é de quem está atrás. */
  public containsScreenPoint(point: { x: number; y: number }): boolean {
    if (!isNodeVisibleSurface(this.targetSurface)) return false;
    const local = { x: point.x - this.envelope.x, y: point.y - this.envelope.y };
    return nodeShapeForSurface(this.targetSurface, this.side, this.envelope, this.pillWidth)
      .some((rect) => pointInRect(local, rect));
  }

  /** Largura do balão curto: segue a frase, para a região não apanhar cliques a mais. */
  public setPillWidth(width: number): void {
    if (width === this.pillWidth) return;
    this.pillWidth = width;
    // Também a meio da transição: a frase nova chega com ela, e a região não pode ficar mais estreita.
    if (this.targetSurface === "pill" || this.targetSurface === "attention") {
      this.applyFinalShape(this.targetSurface);
    }
  }

  public applyPreferences(next: CompanionPreferences): boolean {
    const sideChanged = next.side !== this.side;
    this.prefs = { ...next };
    if (!next.enabled) {
      this.hide();
      return true;
    }
    if (sideChanged) {
      this.side = next.side;
      this.retarget();
    }
    this.updateCustody();
    return true;
  }

  /** Ecrã/DPI/suspensão: recalcula o envelope, descarta a fila e assenta no destino. */
  public retarget(): boolean {
    const win = this.win;
    if (!win || win.isDestroyed()) {
      this.envelope = nodeEnvelopeBounds(this.side, this.workArea());
      return false;
    }
    // A invalidação de callbacks antigos é feita por `stopMorph()` + `transition = null`: o id da
    // transição tem um único dono (`beginNodeTransition`) e o retarget não escreve nele. Incrementar
    // aqui deixava o contador da instância à frente do contador do módulo e a publicação seguinte
    // regredia (o hook descarta ids menores e o Node ficava sem conteúdo).
    this.stopMorph();
    this.transition = null;
    this.envelope = nodeEnvelopeBounds(this.side, this.workArea());
    this.phase = "settled";
    try {
      win.setBounds(this.envelope);
    } catch (err) {
      this.fail(`retarget_failed: ${(err as Error).message}`);
      return false;
    }
    if (isNodeVisibleSurface(this.targetSurface)) {
      this.applyFinalShape(this.targetSurface);
    }
    this.publish();
    this.updateCustody();
    return true;
  }

  /** Reaplica geometria/shape após `resume`, sem repetir a animação. */
  public resume(): boolean {
    return this.retarget();
  }

  public destroy(): void {
    this.destroying = true;
    this.stopMorph();
    this.stopCustody();
    const win = this.win;
    this.win = null;
    this.transition = null;
    this.phase = "settled";
    this.targetSurface = "hidden";
    if (win && !win.isDestroyed()) {
      try {
        win.destroy();
      } catch {
        // encerrar não pode falhar por causa da janela do Node
      }
    }
  }

  // -------------------------------------------------------------------------
  // Janela e shape
  // -------------------------------------------------------------------------

  private ensureWindow(): boolean {
    if (this.win && !this.win.isDestroyed()) return true;
    const envelope = nodeEnvelopeBounds(this.side, this.workArea());
    this.envelope = envelope;
    try {
      const win = new BrowserWindow({
        x: envelope.x,
        y: envelope.y,
        width: envelope.width,
        height: envelope.height,
        show: false,
        frame: false,
        transparent: true,
        backgroundColor: "#00000000",
        thickFrame: false,
        roundedCorners: false,
        resizable: false,
        movable: false,
        maximizable: false,
        minimizable: false,
        fullscreenable: false,
        skipTaskbar: true,
        alwaysOnTop: true,
        // Fora do chat humano a janela nunca pode roubar foco ao clicar no arco.
        focusable: false,
        hasShadow: false,
        title: "Lunmi",
        ...(this.options.iconPath ? { icon: this.options.iconPath } : {}),
        webPreferences: {
          preload: this.options.preloadPath,
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: true,
          webSecurity: true,
          allowRunningInsecureContent: false,
          spellcheck: false,
        },
      });
      this.win = win;
      this.targetSurface = "hidden";
      this.phase = "settled";
      this.transition = null;
      try {
        win.removeMenu();
      } catch {
        // sem moldura já não há menu visível
      }
      win.setAlwaysOnTop(true, "normal");

      win.on("close", (event) => {
        // Fechar o Node recolhe; só «Sair» encerra a aplicação.
        if (this.destroying) return;
        event.preventDefault();
        this.hide();
      });
      win.on("show", () => {
        // Shape depois do show: o primeiro frame é preto uniforme e a região entra já formada.
        this.applyFinalShape(this.targetSurface);
      });
      win.on("closed", () => {
        this.win = null;
        this.targetSurface = "hidden";
        this.phase = "settled";
      });
      win.webContents.on("did-finish-load", () => {
        this.options.onRendererReady?.();
      });
      win.webContents.on("render-process-gone", () => {
        this.options.onRendererGone?.();
      });
      win.webContents.on("did-fail-load", (_event, code, description) => {
        this.fail(`load_failed: ${code} ${description}`);
      });
      win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      win.webContents.on("will-navigate", (event, targetUrl) => {
        if (!this.isCompanionUrl(targetUrl)) event.preventDefault();
      });

      void win
        .loadFile(this.options.indexHtmlPath, { query: { window: "companion" } })
        .catch((err: Error) => this.fail(`load_failed: ${err.message}`));
      return true;
    } catch (err) {
      this.fail(`create_failed: ${(err as Error).message}`);
      return false;
    }
  }

  private isCompanionUrl(targetUrl: string): boolean {
    try {
      const expected = new URL(pathToFileURL(this.options.indexHtmlPath).toString());
      const actual = new URL(targetUrl);
      return (
        actual.protocol === expected.protocol &&
        actual.pathname.toLowerCase() === expected.pathname.toLowerCase()
      );
    } catch {
      return false;
    }
  }

  private applyFinalShape(surface: NodeSurface): boolean {
    if (!isNodeVisibleSurface(surface)) return false;
    return this.applyShape(nodeShapeForSurface(surface, this.side, this.envelope, this.pillWidth));
  }

  private applyShape(rects: { x: number; y: number; width: number; height: number }[]): boolean {
    const win = this.win;
    if (!win || win.isDestroyed()) return false;
    if (process.platform !== "win32" && process.platform !== "linux") {
      // Sem shape nativa: nunca deixar a janela presa a `ignoreMouse` (não é usado em N1).
      return true;
    }
    try {
      // A região nova respeita o passivo: uma transição nunca devolve input ao Node enquanto
      // a tarefa corre (era a regressão do `setIgnoreMouseEvents(false)` em cada tick).
      win.setIgnoreMouseEvents(this.passive);
      win.setShape(rects);
      return true;
    } catch (err) {
      if (this.passive) {
        // Durante o passivo, uma shape que não consegue ignorar input deixa a janela escondida:
        // nunca um overlay interativo enquanto a tarefa corre. É um fallback deliberado, não
        // uma falha do companheiro: NÃO chamar `onFailure`, que abre a principal com foco.
        this.failClosedPassive();
        return false;
      }
      this.fail(`shape_unavailable: ${(err as Error).message}`);
      return false;
    }
  }

  // -------------------------------------------------------------------------
  // Morph
  // -------------------------------------------------------------------------

  private finishMorph(): void {
    this.stopMorph();
    this.transition = null;
    this.phase = "settled";
    this.applyFinalShape(this.targetSurface);
    // Só o chat humano (aberto por gesto) recebe foco; todas as outras superfícies não.
    this.applyFocusPolicy(this.targetSurface);
    this.publish();
    this.updateCustody();
  }

  private applyFocusPolicy(surface: NodeSurface): void {
    const win = this.win;
    if (!win || win.isDestroyed()) return;
    // N4: em passivo nenhuma superfície recebe foco, nem o chat: a tarefa a correr manda.
    if (this.passive) {
      this.applyInputOwnership();
      return;
    }
    try {
      if (surface === "chat") {
        win.setFocusable(true);
        if (!win.isVisible()) {
          if (this.focusChat) win.show(); else win.showInactive();
        }
        // O gate offline usa um duplo sem `focus`; em produção é o BrowserWindow real.
        if (this.focusChat) win.focus();
      } else {
        win.setFocusable(false);
        // Sair do chat não deixa o foco preso numa superfície que já não o aceita.
        win.blur();
      }
    } catch {
      // Foco é conveniência: falhar aqui não pode interromper a transição.
    }
  }

  private stopMorph(): void {
    if (this.morphTimer) {
      clearTimeout(this.morphTimer);
      this.morphTimer = null;
    }
  }

  // -------------------------------------------------------------------------
  // Custódia de hover (sem depender de pointerleave)
  // -------------------------------------------------------------------------

  private updateCustody(): void {
    if (
      this.phase !== "settled" ||
      this.targetSurface !== "revealed" ||
      !this.prefs.autoDock ||
      this.passive ||
      !this.win ||
      this.win.isDestroyed()
    ) {
      this.stopCustody();
      return;
    }
    this.armCustody();
  }

  private armCustody(): void {
    // Em passivo não há polling do cursor: nem o `node-reveal` (permitido pela lista fechada)
    // nem a saída de um morph podem re-armar a custódia enquanto a tarefa corre.
    if (this.passive || !this.prefs.autoDock || this.targetSurface !== "revealed") return;
    this.custodyOutsideSince = null;
    if (this.custodyTimer) return;
    this.custodyTimer = setInterval(() => this.tickCustody(), CUSTODY_TICK_MS);
  }

  private stopCustody(): void {
    if (this.custodyTimer) {
      clearInterval(this.custodyTimer);
      this.custodyTimer = null;
    }
    this.custodyOutsideSince = null;
  }

  private tickCustody(): void {
    if (this.phase !== "settled" || this.targetSurface !== "revealed") {
      this.stopCustody();
      return;
    }
    const win = this.win;
    if (!win || win.isDestroyed()) {
      this.stopCustody();
      return;
    }
    let inside = true;
    try {
      const cursor = screen.getCursorScreenPoint();
      inside = pointInNodeCustody(
        { x: cursor.x - this.envelope.x, y: cursor.y - this.envelope.y },
        this.side,
        this.envelope
      );
    } catch {
      // Sem leitura do cursor: manter revelado é melhor que recolher por engano.
      inside = true;
    }
    const now = Date.now();
    if (inside) {
      this.custodyOutsideSince = null;
    } else if (this.custodyOutsideSince === null) {
      this.custodyOutsideSince = now;
    }
    const elapsed = Math.max(0, now - (this.custodyOutsideSince ?? now));
    const decision = nodeRetractionDecision({
      surface: this.targetSurface,
      passive: this.passive,
      inside,
      elapsed,
    });
    // `stop` é a guarda do passivo: mesmo que um tick escape, não continua a ler o cursor.
    if (decision === "stop") {
      this.stopCustody();
      return;
    }
    if (decision === "dock") {
      this.stopCustody();
      this.setSurface("docked");
    }
  }

  // -------------------------------------------------------------------------
  // Publicação e falhas
  // -------------------------------------------------------------------------

  private publish(): void {
    this.options.onStateChange?.(this.snapshot());
  }

  private workArea(): NodeBounds {
    try {
      if (this.win && !this.win.isDestroyed()) {
        return screen.getDisplayMatching(this.win.getBounds()).workArea;
      }
      return screen.getPrimaryDisplay().workArea;
    } catch {
      return { x: 0, y: 0, width: 1920, height: 1040 };
    }
  }

  private fail(reason: string): void {
    this.options.onFailure?.(reason);
  }
}

/** Lista fechada de ações do Node (re-exportada para o main validar sem duplicar strings). */
export const COMPANION_NODE_ACTIONS = NODE_ACTIONS;

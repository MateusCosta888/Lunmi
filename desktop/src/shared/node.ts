/**
 * Geometria e estado puro do LUNMI NODE V1 (`desktop/src/shared/node.ts`).
 *
 * Porte directo dos protótipos aprovados em `output/node-n0/prototype/geometry.cjs`: as mesmas
 * constantes, a mesma curva e as mesmas decisões. Sem Electron, sem DOM, sem timers — o main
 * agenda e aplica `setShape`; este módulo responde «que rect e que região em progresso t» e
 * «que superfície esta ação produz». Assim desenho e input usam exactamente os mesmos números.
 *
 * Todas as coordenadas são DIP. O envelope é o único rect nativo de um lado: as superfícies
 * vivem dentro dele, por isso a janela nunca é redimensionada nem movida durante um morph.
 */
import type {
  ChatProjectionDto,
  NodePreferencesDto,
  NodeSide,
  NodeStatus,
  NodeSurface,
  NodeTone,
  PresenceAction,
} from "./types";

export interface NodeBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface NodePoint {
  x: number;
  y: number;
}

export interface NodeShapeRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** Superfícies que têm rect; `hidden` é ausência de janela, não um rect vazio. */
export type NodeVisibleSurface = Exclude<NodeSurface, "hidden">;

export const NODE = Object.freeze({
  envelope_width: 420,
  envelope_height: 520,
  circle: 52,
  dock_visible: 20,
  margin: 16,
  reveal_top: 10,
  pill_width: 280,
  pill_height: 60,
  chat_width: 380,
  chat_height: 480,
  radius: 26,
  band_height: 2,
  hover_hysteresis_ms: 600,
  /** Calibração Root 2026-10-21: 8 ms pedidos produzem ticks ~15.6 ms no Windows. */
  morph_tick_ms: 8,
});

export const NODE_DURATIONS = Object.freeze({
  docked: 180,
  revealed: 190,
  pill: 260,
  attention: 260,
  chat: 320,
});

export const NODE_SURFACES: readonly NodeSurface[] = Object.freeze([
  "hidden",
  "docked",
  "revealed",
  "pill",
  "chat",
  "attention",
]);

/** Ações do Node aceites a partir do companheiro (contrato N1, secção 4). */
export const NODE_ACTIONS = ["node-reveal", "node-open", "node-close", "node-dock"] as const;
export type NodeAction = (typeof NODE_ACTIONS)[number];

/**
 * Fonte canónica do enum de ações de presença. Main, preload e testes importam daqui: uma ação
 * nova entra nesta lista uma vez e não pode ficar a ser recusada pelo validador antigo.
 */
export const PRESENCE_ACTIONS = [
  "show-main",
  "show-settings",
  "toggle-mini",
  "hide-mini",
  "stop-voice",
  "quit",
  ...NODE_ACTIONS,
] as const satisfies readonly PresenceAction[];

export function isPresenceAction(value: unknown): value is PresenceAction {
  return typeof value === "string" && (PRESENCE_ACTIONS as readonly string[]).includes(value);
}

/** Subconjunto aceite do companheiro: nunca `quit`. */
export const COMPANION_PRESENCE_ACTIONS = [
  "show-main",
  "show-settings",
  "toggle-mini",
  "hide-mini",
  "stop-voice",
  ...NODE_ACTIONS,
] as const satisfies readonly PresenceAction[];

export function isCompanionPresenceAction(value: unknown): value is PresenceAction {
  return typeof value === "string" && (COMPANION_PRESENCE_ACTIONS as readonly string[]).includes(value);
}

/**
 * N4 — modo passivo. A guarda é a fase RAW da projeção (`task.phase`), nunca o `status`
 * publicado: com `show_activity:false` o status efetivo é `null`, mas a tarefa continua a correr
 * e o Node continua passivo. `show_activity` não entra nesta decisão de propósito.
 */
export function nodePassiveFromTaskPhase(phase: string | null | undefined): boolean {
  return phase === "running";
}

/** Motivo público fixo devolvido ao companheiro quando a ação é recusada em passivo. */
export const NODE_PASSIVE_REASON = "passive_running";

/**
 * Ações recusadas ao companheiro enquanto a tarefa corre: nenhuma abre janela, chat ou
 * definições, nenhuma rouba foco. Fechar/recolher (`node-close`, `node-dock`, `hide-mini`),
 * parar voz e sair (só principal) continuam permitidos.
 */
export const NODE_PASSIVE_BLOCKED_ACTIONS = [
  "node-open",
  "toggle-mini",
  "show-main",
  "show-settings",
] as const satisfies readonly PresenceAction[];

export function nodePassiveBlocksAction(action: string): boolean {
  return (NODE_PASSIVE_BLOCKED_ACTIONS as readonly string[]).includes(action);
}

export const NODE_SIDES: readonly NodeSide[] = Object.freeze(["left", "right"]);

export const DEFAULT_NODE_PREFERENCES: NodePreferencesDto = Object.freeze({
  enabled: true,
  side: "right",
  auto_dock: true,
  show_activity: true,
  chat_appearance: "theme",
  shortcut: null,
});

/** Superfícies estáveis que podem ser destino de um morph. */
export function isNodeSurface(value: unknown): value is NodeSurface {
  return typeof value === "string" && (NODE_SURFACES as readonly string[]).includes(value);
}

export function isNodeVisibleSurface(value: unknown): value is NodeVisibleSurface {
  return isNodeSurface(value) && value !== "hidden";
}

export function isNodeSide(value: unknown): value is NodeSide {
  return value === "left" || value === "right";
}

export function isNodeAction(value: unknown): value is NodeAction {
  return typeof value === "string" && (NODE_ACTIONS as readonly string[]).includes(value);
}

const clamp = (value: number, low: number, high: number): number =>
  high < low ? low : Math.min(high, Math.max(low, value));

const clampInt = (value: number, low: number, high: number): number =>
  Math.round(clamp(value, low, high));

function safeWorkArea(workArea: NodeBounds): NodeBounds {
  return {
    x: Number(workArea?.x) || 0,
    y: Number(workArea?.y) || 0,
    width: Math.max(0, Number(workArea?.width) || 0),
    height: Math.max(0, Number(workArea?.height) || 0),
  };
}

/**
 * Rect nativo do envelope: encostado ao topo da área útil, margem 16 do lado pedido, tamanho da
 * maior superfície (420×520) limitado ao ecrã. Um ecrã pequeno nunca produz largura/altura 0.
 */
export function nodeEnvelopeBounds(side: NodeSide, workArea: NodeBounds): NodeBounds {
  const area = safeWorkArea(workArea);
  const width = Math.min(NODE.envelope_width, area.width);
  const height = Math.min(NODE.envelope_height, area.height);
  const margin = Math.min(NODE.margin, Math.max(0, area.width - width));
  const x = isNodeSide(side) && side === "left"
    ? area.x + margin
    : area.x + area.width - margin - width;
  return {
    x: clampInt(x, area.x, area.x + Math.max(0, area.width - width)),
    y: clampInt(area.y, area.y, area.y + Math.max(0, area.height - height)),
    width: Math.round(width),
    height: Math.round(height),
  };
}

function circleX(side: NodeSide, envelope: NodeBounds): number {
  return side === "left" ? 0 : Math.max(0, envelope.width - NODE.circle);
}

/**
 * Rect de conteúdo (local à janela) de uma superfície. `docked` fica parcialmente acima do topo
 * da janela de propósito: só o arco inferior de 20 px é visível, e é a shape que o recorta.
 */
export function nodeContentRect(
  surface: NodeVisibleSurface,
  side: NodeSide,
  envelope: NodeBounds
): NodeBounds {
  const circle = Math.min(NODE.circle, envelope.width, envelope.height);
  const pillWidth = Math.min(NODE.pill_width, envelope.width);
  const pillHeight = Math.min(NODE.pill_height, envelope.height);
  const chatWidth = Math.min(NODE.chat_width, envelope.width);
  const chatHeight = Math.min(NODE.chat_height, envelope.height);

  switch (surface) {
    case "docked":
      return {
        x: circleX(side, envelope),
        y: -(circle - Math.min(NODE.dock_visible, circle)),
        width: circle,
        height: circle,
      };
    case "revealed":
      return {
        x: circleX(side, envelope),
        y: clampInt(NODE.reveal_top, 0, Math.max(0, envelope.height - circle)),
        width: circle,
        height: circle,
      };
    case "pill":
    case "attention":
      return {
        x: side === "left" ? 0 : Math.max(0, envelope.width - pillWidth),
        y: clampInt(NODE.reveal_top, 0, Math.max(0, envelope.height - pillHeight)),
        width: pillWidth,
        height: pillHeight,
      };
    case "chat":
      return {
        x: side === "left" ? 0 : Math.max(0, envelope.width - chatWidth),
        y: 0,
        width: chatWidth,
        height: chatHeight,
      };
    default:
      throw new Error(`unknown_surface: ${String(surface)}`);
  }
}

/** Rect de qualquer superfície, incluindo `hidden` (que devolve `null`). */
export function nodeSurfaceRect(
  surface: NodeSurface,
  side: NodeSide,
  envelope: NodeBounds
): NodeBounds | null {
  if (!isNodeVisibleSurface(surface)) return null;
  return nodeContentRect(surface, side, envelope);
}

/**
 * Largura horizontal de um rect arredondado num y local. Amostrar ao centro da faixa evita a
 * primeira linha degenerada de um círculo.
 */
export function roundedSpan(
  rect: NodeBounds,
  radius: number,
  yLocal: number
): { x0: number; x1: number } {
  const r = Math.max(0, Math.min(radius, rect.width / 2, rect.height / 2));
  const yIn = yLocal - rect.y;
  let inset = 0;
  if (yIn < r) {
    const dy = r - yIn;
    inset = r - Math.sqrt(Math.max(0, r * r - dy * dy));
  } else if (yIn > rect.height - r) {
    const dy = yIn - (rect.height - r);
    inset = r - Math.sqrt(Math.max(0, r * r - dy * dy));
  }
  return { x0: rect.x + inset, x1: rect.x + rect.width - inset };
}

/**
 * Rect arredondado como faixas horizontais em coordenadas de janela. `[]` nunca é devolvido: no
 * Electron `setShape([])` significa o rectângulo inteiro, não "região vazia".
 */
export function nodeShapeRects(
  rect: NodeBounds,
  windowWidth: number,
  windowHeight: number,
  radius: number = NODE.radius,
  bandHeight: number = NODE.band_height
): NodeShapeRect[] {
  const winW = Math.max(1, Math.round(windowWidth));
  const winH = Math.max(1, Math.round(windowHeight));
  const band = Math.max(1, Math.round(bandHeight));
  const top = Math.max(0, Math.round(rect.y));
  const bottom = Math.min(winH, Math.round(rect.y + rect.height));
  const out: NodeShapeRect[] = [];

  for (let y = top; y < bottom; y += band) {
    const height = Math.min(band, bottom - y);
    const { x0, x1 } = roundedSpan(rect, radius, y + height / 2);
    const left = Math.max(0, Math.round(x0));
    const right = Math.min(winW, Math.round(x1));
    const width = right - left;
    if (width <= 0) continue;
    out.push({ x: left, y, width, height });
  }

  if (out.length === 0) {
    const fallbackLeft = clampInt(Math.round(rect.x), 0, winW - 1);
    const fallbackTop = clampInt(Math.round(rect.y), 0, winH - 1);
    const fallbackWidth = clampInt(Math.round(rect.width), 1, winW - fallbackLeft);
    const fallbackHeight = clampInt(Math.round(rect.height), 1, winH - fallbackTop);
    out.push({ x: fallbackLeft, y: fallbackTop, width: fallbackWidth, height: fallbackHeight });
  }
  return out;
}

/** Região nativa de uma superfície estável. */
export function nodeShapeForSurface(
  surface: NodeVisibleSurface,
  side: NodeSide,
  envelope: NodeBounds,
  radius: number = NODE.radius
): NodeShapeRect[] {
  return nodeShapeRects(nodeContentRect(surface, side, envelope), envelope.width, envelope.height, radius);
}

/** Parte visível do círculo recolhido (o arco inferior) em coordenadas de janela. */
export function nodeVisibleDockedRect(side: NodeSide, envelope: NodeBounds): NodeBounds {
  const rect = nodeContentRect("docked", side, envelope);
  const top = Math.max(0, Math.round(rect.y));
  const bottom = Math.min(envelope.height, Math.round(rect.y + rect.height));
  return {
    x: Math.round(rect.x),
    y: top,
    width: Math.max(1, Math.round(rect.width)),
    height: Math.max(1, bottom - top),
  };
}

/**
 * Onde o ponteiro pode ainda "segurar" a revelação: o arco original e o círculo revelado. NÃO é
 * uma região de input — a shape nativa continua a ser apenas o círculo; isto só decide se o
 * timer de recolha pode disparar. É por isso que sair com o rato nunca depende de `pointerleave`.
 */
export function nodeHoverCustodyRects(side: NodeSide, envelope: NodeBounds): NodeBounds[] {
  return [nodeVisibleDockedRect(side, envelope), nodeContentRect("revealed", side, envelope)];
}

export function pointInRect(point: NodePoint | null | undefined, rect: NodeBounds): boolean {
  return Boolean(point)
    && point!.x >= rect.x && point!.x < rect.x + rect.width
    && point!.y >= rect.y && point!.y < rect.y + rect.height;
}

export function pointInNodeCustody(
  point: NodePoint | null | undefined,
  side: NodeSide,
  envelope: NodeBounds
): boolean {
  return nodeHoverCustodyRects(side, envelope).some((rect) => pointInRect(point, rect));
}

/**
 * Decisão do timer de recolha. O timer só corre em `revealed`; qualquer outra superfície para o
 * timer. `inside` vem do cursor em coordenadas de ecrã, não de um evento de saída.
 */
export function nodeRetractionDecision(input: {
  surface: NodeSurface;
  passive?: boolean;
  inside: boolean;
  elapsed: number;
  hysteresisMs?: number;
}): "stop" | "hold" | "wait" | "dock" {
  if (input.surface !== "revealed" || input.passive) return "stop";
  if (input.inside) return "hold";
  const hysteresis = Math.max(0, input.hysteresisMs ?? NODE.hover_hysteresis_ms);
  return input.elapsed >= hysteresis ? "dock" : "wait";
}

export function easeInOutCubic(t: number): number {
  const k = clamp(t, 0, 1);
  return k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
}

export function interpolateNodeRect(from: NodeBounds, to: NodeBounds, t: number): NodeBounds {
  const k = easeInOutCubic(t);
  return {
    x: from.x + (to.x - from.x) * k,
    y: from.y + (to.y - from.y) * k,
    width: from.width + (to.width - from.width) * k,
    height: from.height + (to.height - from.height) * k,
  };
}

export function nodeTransitionDuration(
  to: NodeSurface,
  options: { reduced?: boolean; scale?: number } = {}
): number {
  if (options.reduced) return 0;
  const base = isNodeVisibleSurface(to) ? NODE_DURATIONS[to] : 0;
  const factor = Number.isFinite(options.scale) && (options.scale as number) > 0 ? (options.scale as number) : 1;
  return Math.round(base * factor);
}

/**
 * Sequência causal: o mesmo objeto cresce na horizontal até à cápsula e só depois na vertical
 * até ao chat. Nunca um círculo interpolado directamente para o chat.
 */
export function planNodeSequence(from: NodeSurface, to: NodeSurface): NodeVisibleSurface[] {
  if (!isNodeSurface(to) || !isNodeSurface(from)) {
    throw new Error(`unknown_surface: ${String(to)}`);
  }
  if (from === to) return [];
  if (to === "hidden") return [];
  if (to !== "chat") return [to];
  if (from === "pill" || from === "attention" || from === "chat") return ["chat"];
  return ["pill", "chat"];
}

/**
 * Superfície seguinte de uma ação fechada. `null` = nada a fazer. Ações idempotentes devolvem a
 * superfície atual (o chamador não cria transição, apenas re-arma a custódia se for `revealed`).
 */
export function nextNodeSurface(surface: NodeSurface, action: NodeAction): NodeSurface | null {
  if (surface === "hidden") return null;
  switch (action) {
    case "node-reveal":
      if (surface === "docked") return "revealed";
      return surface === "revealed" ? "revealed" : null;
    case "node-open":
      // UMA ação humana abre o chat a partir de qualquer superfície visível. O morph interno
      // mantém N0 (`pill` → `chat`); a cápsula é uma etapa visual, não um clique extra.
      return "chat";
    case "node-close":
      // Fechar é um gesto: o chat volta ao círculo revelado; um segundo gesto recolhe.
      if (surface === "chat" || surface === "pill" || surface === "attention") return "revealed";
      return "docked";
    case "node-dock":
      return "docked";
    default:
      return null;
  }
}

/** Janela visível de `done` no Node (timer do main); nunca muda a superfície. */
export const NODE_SUCCESS_VISIBLE_MS = 4000;

export type NodeTaskPhase =
  | "idle"
  | "running"
  | "awaiting_confirm"
  | "awaiting_input"
  | "finished"
  | "failed"
  | "cancelled";

/** Entrada da redução N2: só a projeção pública, nunca texto de conversa ou instrução. */
export interface NodeStatusInput {
  session_present: boolean;
  phase: NodeTaskPhase | null;
  has_confirm: boolean;
  has_input: boolean;
  /** `TaskResultDto.status` cru (SUCCESS/NOT_VERIFIED/FAILED/CANCELLED). */
  result_status: string | null;
  false_success: boolean;
  has_error: boolean;
}

/** Extrai SÓ os campos de estado da projeção; nenhum texto/instrução entra no Node. */
export function nodeStatusInputFromProjection(
  chat: ChatProjectionDto,
  sessionPresent: boolean
): NodeStatusInput {
  return {
    session_present: sessionPresent,
    phase: chat.task?.phase ?? null,
    has_confirm: Boolean(chat.confirm),
    has_input: Boolean(chat.input),
    result_status: chat.task?.result?.status ?? null,
    false_success: Boolean(chat.task?.result?.false_success),
    has_error: Boolean(chat.task?.error),
  };
}

/**
 * Estado finito do Node a partir da projeção real. `phase` sozinha não decide o desfecho: um
 * SUCCESS sem evidência (`false_success`) é `not_verified`, e uma fase terminal sem resultado
 * também não pode ser anunciada como sucesso.
 */
export function deriveNodeStatus(input: NodeStatusInput): NodeStatus {
  if (!input.session_present) return "offline";
  if (input.has_confirm || input.phase === "awaiting_confirm") return "needs_confirm";
  if (input.has_input || input.phase === "awaiting_input") return "needs_input";
  if (input.phase === "running") return "working";

  const status = (input.result_status ?? "").trim().toUpperCase();
  // `result.status` é a autoridade terminal. A projeção colapsa QUALQUER não-SUCCESS em fase
  // `failed`/`cancelled`; usar a fase antes disto transformaria NOT_VERIFIED/PARTIAL/ALIEN em
  // falha. Status reconhecido decide; status desconhecido é incerteza, nunca sucesso nem falha
  // inventada. Só quando NÃO há resultado é que a fase/erro servem de fallback.
  if (status) {
    if (status === "SUCCESS") return input.false_success ? "not_verified" : "done";
    if (status === "CANCELLED") return "cancelled";
    if (status === "FAILED" || status === "ERROR") return "failed";
    return "not_verified";
  }
  if (input.phase === "cancelled") return "cancelled";
  if (input.phase === "failed" || input.has_error) return "failed";
  if (input.phase === "finished") return "not_verified";
  return "idle";
}

/** Pendências e erros nunca são escondidos por `show_activity`. */
export function isNodeAttentionStatus(status: NodeStatus | null): boolean {
  return status === "needs_confirm"
    || status === "needs_input"
    || status === "not_verified"
    || status === "failed"
    || status === "cancelled"
    || status === "offline";
}

/**
 * Política de `show_activity`: desligado esconde apenas atividade transitória (`working`/`done`).
 * `idle` continua neutro e tudo o que exige atenção continua visível.
 */
export function effectiveNodeStatus(
  status: NodeStatus | null,
  showActivity: boolean
): NodeStatus | null {
  if (showActivity) return status;
  if (status === "working" || status === "done") return null;
  return status;
}

/** Marca de `done` já mostrado e expirado, por sessão+tarefa. */
export interface NodeDoneExpiry {
  session_id: string | null;
  task_id: string | null;
}

/**
 * Um `done` cujo timer já expirou não pode voltar ao aparecer um resync da MESMA sessão (por
 * exemplo, recarregar só a janela do companheiro → `session.ready` com o mesmo `session_id`).
 * Só uma tarefa nova ou uma sessão nova limpam a marca — aí o `done` volta a valer.
 */
export function applyNodeDoneExpiry(
  status: NodeStatus,
  expiry: NodeDoneExpiry | null,
  sessionId: string | null,
  taskId: string | null
): NodeStatus {
  if (status !== "done" || !expiry) return status;
  return expiry.session_id === sessionId && expiry.task_id === taskId ? "idle" : status;
}

export function nodeDoneExpiryMatches(
  expiry: NodeDoneExpiry | null,
  sessionId: string | null,
  taskId: string | null
): boolean {
  return Boolean(expiry) && expiry!.session_id === sessionId && expiry!.task_id === taskId;
}

export type NodeStatusSurface = "none" | "pill" | "attention" | "docked";

/**
 * Política fechada status→superfície (N2), executada só pelo main. O renderer nunca escolhe
 * destino nem recebe texto: só desenha a superfície que a shape já abriu.
 *
 * - `working`/`done` com `show_activity` → cápsula (`pill`);
 * - `needs_confirm`/`needs_input`/`not_verified`/`failed`/`cancelled`/`offline` → `attention`;
 * - chat humano aberto é preservado; nenhuma superfície automática rouba foco;
 * - `idle` não força nada, mas uma cápsula automática recolhe para `docked`;
 * - `suppressed` (gesto explícito de recolha) vence até o status mudar.
 */
export function nodeSurfaceForStatus(input: {
  status: NodeStatus | null;
  show_activity: boolean;
  current: NodeSurface;
  auto_pill: boolean;
  suppressed?: boolean;
}): NodeStatusSurface {
  if (input.current === "chat" || input.current === "hidden") return "none";
  if (input.suppressed) return "none";
  const effective = effectiveNodeStatus(input.status, input.show_activity);
  if (effective === "working" || effective === "done") {
    return input.current === "pill" ? "none" : "pill";
  }
  if (isNodeAttentionStatus(effective)) {
    return input.current === "attention" ? "none" : "attention";
  }
  if (input.current === "pill" && input.auto_pill) return "docked";
  return "none";
}

/** Tom visual derivado do status. A UI nunca escolhe um tom por conta própria. */
export function nodeToneFor(status: NodeStatus | null): NodeTone {
  switch (status) {
    case "working":
      return "working";
    case "needs_confirm":
    case "needs_input":
      return "needs";
    case "done":
      return "success";
    case "not_verified":
    case "cancelled":
    case "failed":
      return "error";
    case "offline":
      return "offline";
    case "idle":
    case null:
    default:
      return "neutral";
  }
}

export interface NodeTransitionState {
  id: number;
  from: NodeSurface;
  to: NodeSurface;
  fromRect: NodeBounds | null;
  toRect: NodeBounds;
  durationMs: number;
  startedAt: number;
}

export interface NodeTransitionSample {
  id: number;
  progress: number;
  elapsed: number;
  rect: NodeBounds;
  done: boolean;
}

let nextNodeTransitionId = 0;

/** Ids de transição são monotónicos: um callback antigo compara o id antes de tocar na shape. */
export function beginNodeTransition(input: {
  from: NodeSurface;
  to: NodeVisibleSurface;
  fromRect: NodeBounds | null;
  toRect: NodeBounds;
  durationMs: number;
  startedAt: number;
}): NodeTransitionState {
  nextNodeTransitionId += 1;
  return {
    id: nextNodeTransitionId,
    from: input.from,
    to: input.to,
    fromRect: input.fromRect,
    toRect: input.toRect,
    durationMs: Math.max(0, Math.round(input.durationMs)),
    startedAt: input.startedAt,
  };
}

/** Só para testes: repõe a numeração local de transições. */
export function resetNodeTransitionIds(): void {
  nextNodeTransitionId = 0;
}

export function sampleNodeTransition(
  transition: NodeTransitionState,
  now: number
): NodeTransitionSample {
  const elapsed = Math.max(0, now - transition.startedAt);
  const progress = transition.durationMs <= 0 ? 1 : clamp(elapsed / transition.durationMs, 0, 1);
  // Sem origem (`hidden`): o morph começa já no destino — não há rect anterior a interpolar.
  const fromRect = transition.fromRect ?? transition.toRect;
  return {
    id: transition.id,
    progress,
    elapsed,
    rect: interpolateNodeRect(fromRect, transition.toRect, progress),
    done: progress >= 1,
  };
}

export function nodeShapeForTransition(
  transition: NodeTransitionState,
  progress: number,
  envelope: NodeBounds,
  radius: number = NODE.radius
): NodeShapeRect[] {
  const fromRect = transition.fromRect ?? transition.toRect;
  const rect = interpolateNodeRect(fromRect, transition.toRect, progress);
  return nodeShapeRects(rect, envelope.width, envelope.height, radius);
}

/** Aceleradores aceites: 1–4 modificadores + uma tecla nomeada, sem espaços nem caracteres soltos. */
const SHORTCUT_PATTERN = new RegExp(
  "^(?:(?:Control|Ctrl|Alt|Shift|Super|Meta)\\+){1,4}" +
    "(?:[A-Z0-9]|F(?:[1-9]|1[0-9]|2[0-4])|Space|Tab|Enter|Escape|Up|Down|Left|Right)$"
);

export function isValidNodeShortcut(raw: unknown): raw is string {
  return typeof raw === "string" && raw.length <= 48 && SHORTCUT_PATTERN.test(raw);
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Leitura tolerante das preferências do Node (ficheiro antigo, inválido ou gigante). */
export function parseNodePreferences(raw: unknown): NodePreferencesDto {
  const source = isPlainObject(raw) ? raw : {};
  return {
    enabled: typeof source.enabled === "boolean" ? source.enabled : DEFAULT_NODE_PREFERENCES.enabled,
    side: isNodeSide(source.side) ? source.side : DEFAULT_NODE_PREFERENCES.side,
    auto_dock: typeof source.auto_dock === "boolean" ? source.auto_dock : DEFAULT_NODE_PREFERENCES.auto_dock,
    show_activity:
      typeof source.show_activity === "boolean"
        ? source.show_activity
        : DEFAULT_NODE_PREFERENCES.show_activity,
    shortcut: source.shortcut === null
      ? null
      : isValidNodeShortcut(source.shortcut)
        ? (source.shortcut as string)
        : DEFAULT_NODE_PREFERENCES.shortcut,
    chat_appearance: source.chat_appearance === "black" ? "black" : "theme",
  };
}

export type NodePreferencesPatchResult =
  | { ok: true; next: NodePreferencesDto }
  | { ok: false; error: "invalid_payload" };

/**
 * Valida um patch vindo da janela principal. Chaves desconhecidas, tipos errados ou atalho
 * inválido recusam o patch INTEIRO: nada de aplicar metade e gravar depois.
 */
export function applyNodePreferencesPatch(
  current: NodePreferencesDto,
  patch: unknown
): NodePreferencesPatchResult {
  if (!isPlainObject(patch)) return { ok: false, error: "invalid_payload" };
  const allowed = new Set(Object.keys(DEFAULT_NODE_PREFERENCES));
  for (const key of Object.keys(patch)) {
    if (!allowed.has(key)) return { ok: false, error: "invalid_payload" };
  }
  const next: NodePreferencesDto = { ...current };
  if ("enabled" in patch) {
    if (typeof patch.enabled !== "boolean") return { ok: false, error: "invalid_payload" };
    next.enabled = patch.enabled;
  }
  if ("side" in patch) {
    if (!isNodeSide(patch.side)) return { ok: false, error: "invalid_payload" };
    next.side = patch.side;
  }
  if ("auto_dock" in patch) {
    if (typeof patch.auto_dock !== "boolean") return { ok: false, error: "invalid_payload" };
    next.auto_dock = patch.auto_dock;
  }
  if ("show_activity" in patch) {
    if (typeof patch.show_activity !== "boolean") return { ok: false, error: "invalid_payload" };
    next.show_activity = patch.show_activity;
  }
  if ("shortcut" in patch) {
    if (patch.shortcut !== null && !isValidNodeShortcut(patch.shortcut)) {
      return { ok: false, error: "invalid_payload" };
    }
    next.shortcut = patch.shortcut as string | null;
  }
  if ("chat_appearance" in patch) {
    if (patch.chat_appearance !== "theme" && patch.chat_appearance !== "black") {
      return { ok: false, error: "invalid_payload" };
    }
    next.chat_appearance = patch.chat_appearance;
  }
  return { ok: true, next };
}

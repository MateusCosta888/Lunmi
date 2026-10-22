/**
 * Projeção pública da conversa no processo principal (`desktop/electron/chat-projection.ts`).
 *
 * Uma conversa, duas vistas: o main reduz os eventos reais da ponte com a função pura de
 * `src/shared/chatProjection.ts` e publica snapshot + deltas por cursor. O renderer não é dono do
 * estado e nada depende de timers de uma janela oculta.
 *
 * `syncSession` só reinicia quando o `session_id` muda de facto: o `session.ready` emitido pelo
 * `syncHostPids` depois de incluir o PID do companheiro conserva mensagens, tarefa e pendências.
 */
import {
  acknowledgeChatRequest,
  applyChatEvent,
  createChatProjection,
  markChatStreamCancelled,
  projectChat,
  resetChatProjection,
  type ChatProjectionState,
} from "../src/shared/chatProjection";
import type { Envelope } from "../src/shared/protocol";
import type {
  ChatProjectionDeltaDto,
  ChatProjectionDto,
  SessionReadyPayload,
} from "../src/shared/types";

export class ChatProjectionStore {
  private state: ChatProjectionState = createChatProjection("");

  constructor(private readonly publish: (delta: ChatProjectionDeltaDto) => void) {}

  public get sessionId(): string {
    return this.state.sessionId;
  }

  public snapshot(): ChatProjectionDto {
    return projectChat(this.state);
  }

  /**
   * Fase RAW da tarefa corrente, sem projetar a conversa. É a guarda do passivo N4: chamada a
   * cada delta (incluindo tokens), por isso não pode custar um `projectChat` inteiro.
   */
  public get taskPhase(): string | null {
    return this.state.task?.phase ?? null;
  }

  /** Sessão nova reinicia; a mesma sessão (resync de PIDs) não toca em nada. */
  public syncSession(session: SessionReadyPayload | null): boolean {
    if (!session || typeof session.session_id !== "string" || !session.session_id) {
      return false;
    }
    if (this.state.sessionId === session.session_id) {
      return false;
    }
    const outcome = resetChatProjection(this.state, session.session_id);
    this.state = outcome.state;
    this.publish(outcome.delta);
    return true;
  }

  /**
   * Comando de decisão já aceite pelo backend: retira a pendência consumida da projeção antes
   * de qualquer evento. Só toca na tarefa/pedido exatos ainda à espera; um terminal ou um pedido
   * novo entretanto chegado vence (a função pura não o altera).
   */
  public acknowledgeRequest(taskId: unknown, requestId: unknown): boolean {
    if (typeof requestId !== "string" || !requestId) return false;
    const next = acknowledgeChatRequest(
      this.state,
      typeof taskId === "string" && taskId ? taskId : undefined,
      requestId
    );
    if (next.task === this.state.task && next.confirm === this.state.confirm && next.input === this.state.input) {
      return false;
    }
    this.state = next;
    this.publishDelta({ task: next.task, confirm: next.confirm, input: next.input });
    return true;
  }

  /**
   * Parar aceite: invalida o reconciliador canónico para que texto tardio não continue a entrar.
   * Exige a tarefa original ainda em curso — um ACK tardio de `t1` não pode parar a resposta de
   * `t2`. Não inventa um evento terminal: só fecha o texto em curso como cancelado nesta projeção.
   */
  public cancelStream(taskId: unknown): boolean {
    if (typeof taskId !== "string" || !taskId) return false;
    if (!this.state.task || this.state.task.task_id !== taskId) return false;
    if (!this.state.stream) return false;
    const before = this.state.messages;
    const next = markChatStreamCancelled(this.state);
    this.state = next;
    const beforeById = new Map(before.map((message) => [message.message_id, message]));
    const changed = next.messages.filter((message) => beforeById.get(message.message_id) !== message);
    if (!changed.length) return false;
    this.publishDelta({ messages: changed });
    return true;
  }

  /** Cursor novo por comando local: a vista só aplica deltas que avançam o cursor. */
  private publishDelta(patch: Omit<ChatProjectionDeltaDto, "cursor" | "session_id">): void {
    const cursor = this.state.cursor + 1;
    this.state = { ...this.state, cursor };
    this.publish({ ...patch, cursor, session_id: this.state.sessionId });
  }

  /** Aplica um envelope da ponte; devolve `true` quando algo mudou e foi publicado. */
  public apply(envelope: Envelope): boolean {
    if (envelope.type === "session.ready") {
      return this.syncSession(envelope.payload as unknown as SessionReadyPayload);
    }
    const outcome = applyChatEvent(this.state, envelope);
    if (!outcome) {
      return false;
    }
    this.state = outcome.state;
    this.publish(outcome.delta);
    return true;
  }
}

/**
 * Efeito de um comando já respondido pelo backend sobre a projeção. `null`/`accepted:false` não
 * mexem em nada; `task.confirm`/`task.answer` só retiram o pedido exato ainda pendente; `task.cancel`
 * só bloqueia o stream quando o backend confirmou `cancelled !== false`. Sem evento terminal novo.
 */
export function applyProjectionCommand(
  store: ChatProjectionStore,
  type: string,
  payload: Record<string, unknown>,
  result: Record<string, unknown> | null,
  sessionId?: string
): boolean {
  if (!result || result.accepted === false) return false;
  if (sessionId !== undefined && store.sessionId !== sessionId) return false;
  if (type === "task.confirm" || type === "task.answer") {
    // O ACK tem de ser do pedido exato enviado; um eco de outro pedido não limpa a vista.
    if (result.request_id !== payload.request_id) return false;
    return store.acknowledgeRequest(payload.task_id, payload.request_id);
  }
  if (type === "task.cancel" && result.cancelled !== false) {
    return store.cancelStream(payload.task_id);
  }
  return false;
}

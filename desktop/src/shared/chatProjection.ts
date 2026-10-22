/**
 * Projeção pública da conversa (`desktop/src/shared/chatProjection.ts`) — BACKGROUND V1 / BG2.
 *
 * Uma conversa, duas vistas. O processo principal reduz os eventos reais da ponte com estas
 * funções e publica snapshot + deltas por cursor; o renderer (janela principal e mini chat)
 * aplica exatamente os mesmos deltas. O caminho de compatibilidade para um preload antigo usa
 * esta mesma redução sobre `lunmi:event` — nunca um segundo parser de `ResponseStream`.
 *
 * Estado interno (`stream`) é o reconciliador de texto e não sai no DTO público.
 */
import { ResponseStream, upsertMessage } from "./responseStream";
import type {
  ChatMessageDto,
  ChatProjectionDeltaDto,
  ChatProjectionDto,
  ConfirmRequestDto,
  InputRequestDto,
  PublicResponseDto,
  TaskResultDto,
  TaskStateDto,
} from "./types";

export const MAX_RECENT_TASKS = 6;

const BACKEND_FAILURE_CODES = new Set<string>([
  "backend_exited",
  "backend_spawn_failed",
  "backend_pipe_error",
]);

export interface ChatEventLike {
  type: string;
  payload: Record<string, unknown>;
}

export interface ChatProjectionState {
  sessionId: string;
  cursor: number;
  messages: ChatMessageDto[];
  recentTasks: TaskStateDto[];
  task: TaskStateDto | null;
  confirm: ConfirmRequestDto | null;
  input: InputRequestDto | null;
  /** Reconciliador de texto da tarefa atual; nulo no renderer, que só aplica deltas. */
  stream: ResponseStream | null;
}

export interface ChatProjectionOutcome {
  state: ChatProjectionState;
  delta: ChatProjectionDeltaDto;
}

export function createChatProjection(sessionId = ""): ChatProjectionState {
  return {
    sessionId,
    cursor: 0,
    messages: [],
    recentTasks: [],
    task: null,
    confirm: null,
    input: null,
    stream: null,
  };
}

/** DTO público do snapshot: sem `stream` e sem nada que o backend não publicou. */
export function projectChat(state: ChatProjectionState): ChatProjectionDto {
  return {
    session_id: state.sessionId,
    cursor: state.cursor,
    messages: state.messages,
    recent_tasks: state.recentTasks,
    task: state.task,
    confirm: state.confirm,
    input: state.input,
  };
}

/** Renderer: estado local a partir do snapshot do main (o reconciliador fica no main). */
export function chatStateFromDto(dto: ChatProjectionDto): ChatProjectionState {
  return {
    sessionId: dto.session_id,
    cursor: dto.cursor,
    messages: dto.messages,
    recentTasks: dto.recent_tasks,
    task: dto.task,
    confirm: dto.confirm,
    input: dto.input,
    stream: null,
  };
}

export function resetChatProjection(
  state: ChatProjectionState,
  sessionId: string
): ChatProjectionOutcome {
  const next = createChatProjection(sessionId);
  next.cursor = state.cursor + 1;
  return {
    state: next,
    delta: {
      session_id: sessionId,
      cursor: next.cursor,
      reset: true,
      messages: [],
      recent_tasks: [],
      task: null,
      confirm: null,
      input: null,
    },
  };
}

function recentTasksFor(previous: TaskStateDto[], task: TaskStateDto | null): TaskStateDto[] {
  if (!task) return previous;
  return [task, ...previous.filter(item => item.task_id !== task.task_id)].slice(0, MAX_RECENT_TASKS);
}

function withTask(state: ChatProjectionState, task: TaskStateDto | null): Pick<
  ChatProjectionDeltaDto,
  "task" | "recent_tasks"
> {
  return { task, recent_tasks: recentTasksFor(state.recentTasks, task) };
}

function taskFromResult(previous: TaskStateDto | null, result: TaskResultDto): TaskStateDto | null {
  if (!previous) return previous;
  const phase: TaskStateDto["phase"] =
    result.status === "SUCCESS" ? "finished" : result.status === "CANCELLED" ? "cancelled" : "failed";
  return { ...previous, phase, result, progress: "" };
}

function pendingTask(task: TaskStateDto | null, phase: TaskStateDto["phase"], requestId: string): TaskStateDto | null {
  return task ? { ...task, phase, pending_request_id: requestId } : task;
}

/**
 * Reduz um envelope da ponte. Devolve `null` quando o evento não muda nada (ruído, sessão
 * antiga, tarefa alheia) — o chamador não incrementa o cursor nem publica delta.
 */
export function applyChatEvent(
  state: ChatProjectionState,
  event: ChatEventLike
): ChatProjectionOutcome | null {
  const payload = event.payload ?? {};
  const sessionId = payload.session_id;
  if (typeof sessionId === "string" && sessionId && state.sessionId && sessionId !== state.sessionId) {
    return null;
  }

  const commit = (
    next: Partial<ChatProjectionState> & {
      delta: Omit<ChatProjectionDeltaDto, "cursor" | "session_id">;
      recent_tasks?: TaskStateDto[];
    }
  ): ChatProjectionOutcome => {
    // `recent_tasks` vem do DTO (snake_case) para o delta e para o estado (camelCase).
    const { delta, recent_tasks, ...statePatch } = next;
    const cursor = state.cursor + 1;
    const merged: ChatProjectionState = {
      ...state,
      ...statePatch,
      recentTasks: recent_tasks ?? state.recentTasks,
      cursor,
    };
    return {
      state: merged,
      delta: { ...delta, cursor, session_id: merged.sessionId },
    };
  };

  if (event.type.startsWith("response.") || event.type.startsWith("operation.")) {
    if (payload.task_id !== state.task?.task_id) return null;
    const message = state.stream?.accept(event.type, payload as unknown as PublicResponseDto);
    if (!message) return null;
    return commit({ messages: upsertMessage(state.messages, message), delta: { messages: [message] } });
  }

  switch (event.type) {
    case "chat.message": {
      const message = payload as unknown as ChatMessageDto;
      if (typeof message.text !== "string") return null;
      if (message.task_id && state.task && message.task_id !== state.task.task_id) return null;
      const accepted =
        state.stream?.legacy(message) ??
        (state.stream && message.role === "assistant" ? null : message);
      if (!accepted) return null;
      return commit({ messages: upsertMessage(state.messages, accepted), delta: { messages: [accepted] } });
    }
    case "task.started": {
      const taskId = String(payload.task_id ?? "");
      const mode = payload.mode === "auto" ? "auto" : payload.mode === "converse" ? "converse" : "execute";
      const interrupted = state.stream?.stop("interrupted") ?? [];
      const messages = interrupted.reduce(upsertMessage, state.messages);
      const started: TaskStateDto = {
        task_id: taskId,
        mode,
        instruction: String(payload.instruction ?? ""),
        phase: "running",
        progress: "",
        result: null,
        error: null,
      };
      return commit({
        messages,
        task: started,
        confirm: null,
        input: null,
        stream: new ResponseStream(state.sessionId, taskId),
        ...withTask(state, started),
        delta: {
          ...(interrupted.length ? { messages: interrupted } : {}),
          ...withTask(state, started),
          confirm: null,
          input: null,
        },
      });
    }
    case "task.progress": {
      const taskId = String(payload.task_id ?? "");
      if (!state.task || state.task.task_id !== taskId) return null;
      const task = { ...state.task, progress: String(payload.state ?? "") };
      return commit({ task, ...withTask(state, task), delta: { ...withTask(state, task) } });
    }
    case "task.need_confirm": {
      if (payload.task_id !== state.task?.task_id) return null;
      const requestId = String(payload.request_id);
      const task = pendingTask(state.task, "awaiting_confirm", requestId);
      const confirm = payload as unknown as ConfirmRequestDto;
      return commit({ task, confirm, ...withTask(state, task), delta: { ...withTask(state, task), confirm } });
    }
    case "task.need_input": {
      if (payload.task_id !== state.task?.task_id) return null;
      const requestId = String(payload.request_id);
      const task = pendingTask(state.task, "awaiting_input", requestId);
      const input = payload as unknown as InputRequestDto;
      return commit({ task, input, ...withTask(state, task), delta: { ...withTask(state, task), input } });
    }
    case "task.finished": {
      const result = payload as unknown as TaskResultDto;
      if (result.task_id !== state.task?.task_id) return null;
      const interrupted = state.stream?.stop("interrupted") ?? [];
      const messages = interrupted.reduce(upsertMessage, state.messages);
      const task = taskFromResult(state.task, result);
      return commit({
        messages,
        task,
        confirm: null,
        input: null,
        ...withTask(state, task),
        delta: {
          ...(interrupted.length ? { messages: interrupted } : {}),
          ...withTask(state, task),
          confirm: null,
          input: null,
        },
      });
    }
    case "task.cancelled": {
      if (payload.task_id !== state.task?.task_id) return null;
      const interrupted = state.stream?.stop("cancelled") ?? [];
      const messages = interrupted.reduce(upsertMessage, state.messages);
      const result = (payload.result ?? null) as TaskResultDto | null;
      const task = state.task ? { ...state.task, phase: "cancelled" as const, result } : null;
      return commit({
        messages,
        task,
        confirm: null,
        input: null,
        ...withTask(state, task),
        delta: {
          ...(interrupted.length ? { messages: interrupted } : {}),
          ...withTask(state, task),
          confirm: null,
          input: null,
        },
      });
    }
    case "task.error": {
      const code = String(payload.code ?? "");
      // Falha de sessão é do banner de sessão, não erro de chat; o texto parcial é encerrado.
      if (BACKEND_FAILURE_CODES.has(code)) {
        const interrupted = state.stream?.stop("interrupted") ?? [];
        if (!interrupted.length) return null;
        return commit({
          messages: interrupted.reduce(upsertMessage, state.messages),
          delta: { messages: interrupted },
        });
      }
      if (payload.task_id !== state.task?.task_id) return null;
      const interrupted = state.stream?.stop("interrupted") ?? [];
      const messages = interrupted.reduce(upsertMessage, state.messages);
      const task = state.task
        ? { ...state.task, phase: "failed" as const, progress: "", error: String(payload.message ?? "Falha ao executar a tarefa.") }
        : null;
      return commit({
        messages,
        task,
        ...withTask(state, task),
        delta: {
          ...(interrupted.length ? { messages: interrupted } : {}),
          ...withTask(state, task),
        },
      });
    }
    default:
      return null;
  }
}

/**
 * Renderer: aplica um delta do main.
 *
 * Regra de cursor (mesmo em reset): um delta só vale se avançar o cursor e pertencer à sessão
 * atual. Duplicado, velho ou reset atrasado não toca em nada — um snapshot/evento tardio não
 * ressuscita a sessão anterior nem sobrescreve uma mensagem mais nova. O reset descarta o estado
 * antigo mas conserva os campos já coalescidos com ele (a primeira mensagem do novo turno).
 */
export function applyChatDelta(
  state: ChatProjectionState,
  delta: ChatProjectionDeltaDto
): ChatProjectionState {
  const has = (key: keyof ChatProjectionDeltaDto) => Object.prototype.hasOwnProperty.call(delta, key);
  if (delta.cursor <= state.cursor) {
    return state;
  }
  if (!delta.reset && state.sessionId && delta.session_id !== state.sessionId) {
    return state;
  }
  if (delta.reset) {
    const fresh = createChatProjection(delta.session_id);
    fresh.cursor = delta.cursor;
    fresh.messages = (delta.messages ?? []).reduce(upsertMessage, [] as ChatMessageDto[]);
    fresh.recentTasks = delta.recent_tasks ?? [];
    fresh.task = has("task") ? (delta.task ?? null) : null;
    fresh.confirm = has("confirm") ? (delta.confirm ?? null) : null;
    fresh.input = has("input") ? (delta.input ?? null) : null;
    return fresh;
  }
  const messages = (delta.messages ?? []).reduce(upsertMessage, state.messages);
  return {
    ...state,
    sessionId: delta.session_id,
    cursor: delta.cursor,
    messages,
    recentTasks: has("recent_tasks") ? (delta.recent_tasks ?? []) : state.recentTasks,
    task: has("task") ? (delta.task ?? null) : state.task,
    confirm: has("confirm") ? (delta.confirm ?? null) : state.confirm,
    input: has("input") ? (delta.input ?? null) : state.input,
  };
}

/**
 * Stop do utilizador: encerra o texto em curso como cancelado já, sem esperar IPC. No main há
 * `stream` e ele fica marcado como parado, recusando deltas tardios; no renderer (sem stream)
 * a mensagem em `streaming` é marcada localmente e o próximo delta do main reconcilia.
 */
export function markChatStreamCancelled(state: ChatProjectionState): ChatProjectionState {
  if (state.stream) {
    const interrupted = state.stream.stop("cancelled");
    return { ...state, messages: interrupted.reduce(upsertMessage, state.messages) };
  }
  return {
    ...state,
    messages: state.messages.map((message) =>
      message.state === "streaming" ? { ...message, state: "cancelled" as const } : message
    ),
  };
}

/** Resposta otimista a um pedido: limpa a pendência e retoma a tarefa, sem inventar autoridade. */
export function acknowledgeChatRequest(
  state: ChatProjectionState,
  taskId: string | undefined,
  requestId: string
): ChatProjectionState {
  // Só retoma uma tarefa ainda à espera deste pedido; um evento terminal chegou primeiro e vale.
  const task =
    state.task &&
    state.task.task_id === taskId &&
    state.task.pending_request_id === requestId &&
    (state.task.phase === "awaiting_confirm" || state.task.phase === "awaiting_input")
      ? { ...state.task, phase: "running" as const, pending_request_id: undefined }
      : state.task;
  return {
    ...state,
    task,
    confirm: state.confirm?.request_id === requestId ? null : state.confirm,
    input: state.input?.request_id === requestId ? null : state.input,
  };
}

/**
 * Coalescer do main: mantém o cursor mais recente e aplica as mensagens por ordem.
 * Um reset coalescido com o primeiro evento do novo turno preserva esse evento: o delta
 * resultante ainda é `reset`, mas carrega a primeira mensagem — o renderer não a perde.
 */
export function mergeChatDelta(
  previous: ChatProjectionDeltaDto,
  next: ChatProjectionDeltaDto
): ChatProjectionDeltaDto {
  if (next.reset) return next;
  const merged: ChatProjectionDeltaDto = { ...previous, ...next };
  const messages = (previous.messages ?? []).reduce(upsertMessage, [] as ChatMessageDto[]);
  merged.messages = (next.messages ?? []).reduce(upsertMessage, messages);
  if (previous.recent_tasks) merged.recent_tasks = previous.recent_tasks;
  if (next.recent_tasks) merged.recent_tasks = next.recent_tasks;
  return merged;
}

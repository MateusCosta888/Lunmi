import type { ChatMessageDto, PublicResponseDto } from "../../shared/types";

interface ResponseState {
  message: ChatMessageDto;
  seq: number;
  terminal: boolean;
  gap: boolean;
  offset: number;
}

/** One task's append-only public text; tool/stable events advance sequence, never add bubbles. */
export class ResponseStream {
  private responses = new Map<string, ResponseState>();
  private activeId = "";
  private stopped = false;

  constructor(private sessionId: string, private taskId: string) {}

  accept(type: string, payload: PublicResponseDto): ChatMessageDto | null {
    const operation = type === "operation.started" || type === "operation.finished";
    if (this.stopped || payload.session_id !== this.sessionId || payload.task_id !== this.taskId
      || !payload.response_id || (!operation && !payload.message_id) || !Number.isSafeInteger(payload.seq) || payload.seq < 0) return null;
    const id = payload.response_id;
    if (type === "response.started") {
      if (this.responses.has(id)) return null;
      this.activeId = id;
      this.responses.set(id, {
        message: { role: "assistant", text: "", task_id: this.taskId, message_id: payload.message_id,
          response_id: id, state: "streaming" },
        seq: payload.seq, terminal: false, gap: false, offset: 0,
      });
      return null;
    }
    const response = this.responses.get(id);
    if (!response || id !== this.activeId || response.terminal || (!operation && payload.message_id !== response.message.message_id)
      || payload.seq <= response.seq) return null;
    response.gap ||= payload.seq !== response.seq + 1;
    response.seq = payload.seq;
    if (type === "response.completed") {
      if (typeof payload.text !== "string") return null;
      response.message = { ...response.message, text: payload.text, state: "completed" };
      response.terminal = true;
      return response.message;
    }
    if (type === "response.error" || type === "response.cancelled") {
      response.message = { ...response.message, state: type === "response.cancelled" ? "cancelled" : "interrupted" };
      response.terminal = true;
      return response.message;
    }
    if (type !== "response.text.delta") return null;
    if (typeof payload.text !== "string" || payload.offset !== response.offset) response.gap = true;
    response.message = response.gap
      ? { ...response.message, state: "interrupted" }
      : { ...response.message, text: response.message.text + payload.text, state: "streaming" };
    if (!response.gap) response.offset += Array.from(payload.text!).length;
    return response.message;
  }

  stop(state: "cancelled" | "interrupted" = "cancelled"): ChatMessageDto[] {
    this.stopped = true;
    return Array.from(this.responses.values()).filter(response => !response.terminal).map(response => {
      response.terminal = true;
      response.message = { ...response.message, state };
      return response.message;
    });
  }

  legacy(message: ChatMessageDto): ChatMessageDto | null {
    if (message.role !== "assistant") return message;
    if (this.stopped) return null;
    const response = message.response_id ? this.responses.get(message.response_id) : undefined;
    if (!response) return message;
    if (message.response_id !== this.activeId || (response.terminal && response.message.state !== "completed")) return null;
    // A compatibility snapshot must not turn a missing stream terminal into success.
    return { ...message, state: response.message.state, message_id: response.message.message_id };
  }
}

export function upsertMessage(messages: ChatMessageDto[], message: ChatMessageDto): ChatMessageDto[] {
  const index = messages.findIndex(item => message.message_id
    ? item.message_id === message.message_id && item.task_id === message.task_id
    : Boolean(message.response_id) && item.response_id === message.response_id && item.task_id === message.task_id);
  if (index < 0) return [...messages, message];
  return messages.map((item, i) => i === index ? message : item);
}

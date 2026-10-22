import { describe, expect, it, vi } from "vitest";
import {
  applyChatDelta,
  applyChatEvent,
  createChatProjection,
  mergeChatDelta,
  projectChat,
  type ChatProjectionState,
} from "../src/shared/chatProjection";
import { applyProjectionCommand, ChatProjectionStore } from "../electron/chat-projection";
import type { ChatProjectionDeltaDto } from "../src/shared/types";
import type { Envelope } from "../src/shared/protocol";

const SESSION = "sess-1";

function start(): ChatProjectionState {
  return createChatProjection(SESSION);
}

function reduce(
  state: ChatProjectionState,
  type: string,
  payload: Record<string, unknown> = {}
): { state: ChatProjectionState; delta: ChatProjectionDeltaDto | null } {
  const outcome = applyChatEvent(state, { type, payload: { session_id: SESSION, ...payload } });
  return { state: outcome?.state ?? state, delta: outcome?.delta ?? null };
}

describe("projeção pública da conversa (BG2)", () => {
  it("transmite uma resposta uma única vez, do delta ao completed e sem eco legacy", () => {
    let state = start();
    state = reduce(state, "task.started", { task_id: "t1", mode: "converse" }).state;
    state = reduce(state, "response.started", { task_id: "t1", response_id: "r1", message_id: "m1", seq: 1 }).state;
    state = reduce(state, "response.text.delta", {
      task_id: "t1", response_id: "r1", message_id: "m1", seq: 2, offset: 0, text: "Olá ",
    }).state;
    const partial = reduce(state, "response.text.delta", {
      task_id: "t1", response_id: "r1", message_id: "m1", seq: 3, offset: 4, text: "mundo",
    });
    state = partial.state;
    expect(partial.delta?.messages?.[0].text).toBe("Olá mundo");
    state = reduce(state, "response.completed", {
      task_id: "t1", response_id: "r1", message_id: "m1", seq: 4, text: "Olá mundo",
    }).state;
    // Eco tardio do serviço com o mesmo message_id não cria segunda bolha.
    state = reduce(state, "chat.message", {
      task_id: "t1", role: "assistant", response_id: "r1", message_id: "m1", text: "Olá mundo",
    }).state;
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({ text: "Olá mundo", state: "completed" });
  });

  it("três turnos não geram eco nem perdem mensagens", () => {
    let state = start();
    for (let turn = 1; turn <= 3; turn += 1) {
      const taskId = `t${turn}`;
      state = reduce(state, "task.started", { task_id: taskId, mode: "auto" }).state;
      state = reduce(state, "chat.message", {
        task_id: taskId, role: "user", text: `pergunta ${turn}`, message_id: `${taskId}-u`,
      }).state;
      state = reduce(state, "chat.message", {
        task_id: taskId, role: "assistant", text: `resposta ${turn}`, message_id: `${taskId}-a`,
      }).state;
      state = reduce(state, "chat.message", {
        task_id: taskId, role: "user", text: `pergunta ${turn}`, message_id: `${taskId}-u`,
      }).state;
      state = reduce(state, "task.finished", { task_id: taskId, status: "SUCCESS", message: "" }).state;
    }
    expect(state.messages.map((message) => message.text)).toEqual([
      "pergunta 1", "resposta 1", "pergunta 2", "resposta 2", "pergunta 3", "resposta 3",
    ]);
    expect(state.recentTasks.map((task) => task.task_id)).toEqual(["t3", "t2", "t1"]);
  });

  it("ignora eventos de outra sessão e limpa pendências ao terminar", () => {
    let state = start();
    state = reduce(state, "task.started", { task_id: "t1", mode: "execute" }).state;
    state = reduce(state, "task.need_confirm", {
      task_id: "t1", request_id: "req-1", message: "Confirma?",
    }).state;
    expect(state.confirm?.request_id).toBe("req-1");
    const foreign = applyChatEvent(state, {
      type: "chat.message",
      payload: { session_id: "other", role: "user", text: "alheia", task_id: "t1" },
    });
    expect(foreign).toBeNull();
    state = reduce(state, "task.finished", { task_id: "t1", status: "CANCELLED", message: "" }).state;
    expect(state.confirm).toBeNull();
    expect(state.task?.phase).toBe("cancelled");
  });

  it("aplica cursor: duplicado, velho e reset atrasado não tocam na vista", () => {
    const message = (text: string) => ({ role: "user" as const, text, message_id: "m", task_id: "t" });
    const newer: ChatProjectionState = { ...start(), cursor: 7, messages: [message("new")] };
    expect(applyChatDelta(newer, { session_id: SESSION, cursor: 6, messages: [message("old")] })).toBe(newer);
    expect(applyChatDelta(newer, { session_id: SESSION, cursor: 7, messages: [message("duplicate")] })).toBe(newer);
    const current: ChatProjectionState = { ...start(), cursor: 12, messages: [message("current")] };
    const staleReset = applyChatDelta(current, { session_id: "old", cursor: 10, reset: true, messages: [] });
    expect(staleReset.sessionId).toBe(SESSION);
    expect(staleReset.messages).toEqual(current.messages);
  });

  it("reset coalescido com o primeiro evento do novo turno preserva essa mensagem", () => {
    const message = { role: "user" as const, text: "first", message_id: "m", task_id: "t" };
    const previous: ChatProjectionState = {
      ...createChatProjection("old"),
      cursor: 10,
      messages: [{ role: "user", text: "previous", message_id: "p", task_id: "t0" }],
    };
    const reset: ChatProjectionDeltaDto = {
      session_id: "new", cursor: 11, reset: true, messages: [], recent_tasks: [],
      task: null, confirm: null, input: null,
    };
    const combined = mergeChatDelta(reset, { session_id: "new", cursor: 12, messages: [message] });
    const next = applyChatDelta(previous, combined);
    expect(next.sessionId).toBe("new");
    expect(next.messages).toEqual([message]);
    expect(next.cursor).toBe(12);
  });

  it("snapshot público não carrega o reconciliador interno", () => {
    const snapshot = projectChat(start());
    expect(Object.keys(snapshot).sort()).toEqual([
      "confirm", "cursor", "input", "messages", "recent_tasks", "session_id", "task",
    ]);
  });
});

describe("ChatProjectionStore (main)", () => {
  it("só reinicia a projeção quando o session_id muda de facto", () => {
    const published: ChatProjectionDeltaDto[] = [];
    const store = new ChatProjectionStore((delta) => published.push(delta));
    const ready = (sessionId: string) => ({
      v: 1, session_id: sessionId, pid: 1, capabilities: { converse: true }, limits: {},
    });
    expect(store.syncSession(ready("s1") as never)).toBe(true);
    store.apply({
      v: 1, id: "e1", type: "chat.message",
      payload: { session_id: "s1", role: "user", text: "olá", task_id: "t1", message_id: "m1" },
    } as Envelope);
    const before = store.snapshot();

    // O session.ready do syncHostPids (mesmo id) não limpa mensagens.
    expect(store.syncSession(ready("s1") as never)).toBe(false);
    expect(store.snapshot()).toEqual(before);

    expect(store.syncSession(ready("s2") as never)).toBe(true);
    expect(store.snapshot()).toMatchObject({ session_id: "s2", messages: [] });
    expect(published.at(-1)?.reset).toBe(true);
  });

  it("não publica evento sem efeito", () => {
    const publish = vi.fn();
    const store = new ChatProjectionStore(publish);
    store.syncSession({ v: 1, session_id: "s1" } as never);
    publish.mockClear();
    expect(store.apply({ v: 1, id: "x", type: "memory.changed", payload: { session_id: "s1" } } as Envelope)).toBe(false);
    expect(publish).not.toHaveBeenCalled();
  });

  it("confirmação aceite limpa o pedido exato e não reexibe no mini; ACK tardio não toca num pedido novo", () => {
    const store = new ChatProjectionStore(() => undefined);
    store.syncSession({ v: 1, session_id: SESSION } as never);
    store.apply(envelope("task.started", { session_id: SESSION, task_id: "t1", mode: "converse", instruction: "x" }));
    store.apply(envelope("task.need_confirm", {
      session_id: SESSION, task_id: "t1", request_id: "r1", kind: "confirm", prompt: "Apagar?", options: [],
    }));
    expect(store.snapshot().confirm?.request_id).toBe("r1");

    expect(applyProjectionCommand(
      store, "task.confirm", { request_id: "r1", task_id: "t1", approved: true },
      { request_id: "r1", approved: true }, SESSION
    )).toBe(true);
    expect(store.snapshot().confirm).toBeNull();
    expect(store.snapshot().task?.phase).toBe("running");

    // Pedido novo concorrente: um ACK antigo não o consome.
    store.apply(envelope("task.need_confirm", {
      session_id: SESSION, task_id: "t1", request_id: "r2", kind: "confirm", prompt: "Outra?", options: [],
    }));
    expect(applyProjectionCommand(
      store, "task.confirm", { request_id: "r1", task_id: "t1", approved: true },
      { request_id: "r1", approved: true }, SESSION
    )).toBe(false);
    expect(store.snapshot().confirm?.request_id).toBe("r2");

    // ACK de outro pedido/sessão também não limpa.
    expect(applyProjectionCommand(
      store, "task.answer", { request_id: "r2", task_id: "t1", kind: "choice" },
      { request_id: "outro", accepted: true }, SESSION
    )).toBe(false);
    expect(store.snapshot().confirm?.request_id).toBe("r2");
  });

  it("cancelar t1 em voo não pára t2; stream canónico bloqueia texto tardio", () => {
    const store = new ChatProjectionStore(() => undefined);
    store.syncSession({ v: 1, session_id: SESSION } as never);
    store.apply(envelope("task.started", { session_id: SESSION, task_id: "t1", mode: "converse", instruction: "um" }));
    store.apply(envelope("task.started", { session_id: SESSION, task_id: "t2", mode: "converse", instruction: "dois" }));
    store.apply(envelope("response.started", {
      session_id: SESSION, task_id: "t2", response_id: "resp2", message_id: "m2", seq: 0,
    }));
    store.apply(envelope("response.text.delta", {
      session_id: SESSION, task_id: "t2", response_id: "resp2", message_id: "m2", seq: 1, offset: 0, text: "parcial",
    }));

    const running = store.snapshot();
    expect(applyProjectionCommand(store, "task.cancel", { task_id: "t1" }, { cancelled: true, task_id: "t1" }, SESSION)).toBe(false);
    expect(store.snapshot()).toEqual(running);
    expect(applyProjectionCommand(store, "task.cancel", { task_id: "t2" }, { cancelled: true, task_id: "t2" }, "outra-sessao")).toBe(false);
    expect(store.snapshot()).toEqual(running);

    expect(applyProjectionCommand(store, "task.cancel", { task_id: "t2" }, { cancelled: true, task_id: "t2" }, SESSION)).toBe(true);
    expect(store.snapshot().messages.find((message) => message.message_id === "m2")?.state).toBe("cancelled");

    // Depois do stop aceite, um delta tardio da mesma resposta não reentra na vista.
    const afterCancel = store.snapshot();
    expect(store.apply(envelope("response.text.delta", {
      session_id: SESSION, task_id: "t2", response_id: "resp2", message_id: "m2", seq: 2, offset: 7, text: " tardio",
    }))).toBe(false);
    expect(store.snapshot()).toEqual(afterCancel);
  });
});

function envelope(type: string, payload: Record<string, unknown>): Envelope {
  return { v: 1, id: `e-${type}`, type, payload } as Envelope;
}

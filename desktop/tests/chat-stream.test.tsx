import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ResponseStream } from "../src/features/chat/responseStream";
import { useChatTask } from "../src/features/chat/useChatTask";
import { ChatPanel } from "../src/features/chat/ChatPanel";
import type { Envelope, SessionReadyPayload } from "../src/shared/protocol";
import type { LunmiBridgeApi, SendCommandFn } from "../src/shared/ipc/client";
import type { PublicResponseDto } from "../src/shared/types";

const session = (id = "session-1") => ({ session_id: id, capabilities: { converse: true, execute: true } }) as SessionReadyPayload;
const payload = (seq: number, extra: Partial<PublicResponseDto> = {}): PublicResponseDto => ({
  session_id: "session-1", task_id: "task-1", response_id: "response-1", message_id: "message-1", seq, ...extra,
});

let frames: Map<number, FrameRequestCallback>;
let frameId: number;
beforeEach(() => {
  frames = new Map();
  frameId = 0;
  vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => { frames.set(++frameId, callback); return frameId; }));
  vi.stubGlobal("cancelAnimationFrame", vi.fn((id: number) => { frames.delete(id); }));
});
function paint() {
  act(() => {
    const callbacks = [...frames.values()];
    frames.clear();
    callbacks.forEach(callback => callback(0));
  });
}
function harness() {
  let listener: ((event: Envelope) => void) | undefined;
  const send = vi.fn(async () => ({})) as SendCommandFn;
  window.lunmi = { onEvent: (callback => { listener = callback; return () => { listener = undefined; }; }) } as LunmiBridgeApi;
  const hook = renderHook(({ id, version }) => useChatTask(send, session(id), version), {
    initialProps: { id: "session-1", version: 1 },
  });
  const emit = (type: string, data: object) => act(() => listener?.({ v: 1, type, payload: data } as Envelope));
  const start = () => { emit("task.started", { task_id: "task-1", mode: "converse" }); emit("response.started", payload(1)); };
  return { ...hook, emit, start, send };
}

describe("public response stream", () => {
  it("counts Unicode points, ignores duplicates and stable/tool text, then reconciles one canonical bubble", () => {
    const stream = new ResponseStream("session-1", "task-1");
    stream.accept("response.started", payload(1));
    expect(stream.accept("response.text.delta", payload(2, { text: "Olá 🌙", offset: 0 }))?.text).toBe("Olá 🌙");
    expect(stream.accept("response.text.delta", payload(2, { text: "duplicate", offset: 5 }))).toBeNull();
    expect(stream.accept("response.text.stable", payload(3, { text: "Olá 🌙", start: 0, end: 5 }))).toBeNull();
    expect(stream.accept("operation.started", payload(4, { operation_id: "op-1", message_id: "" }))).toBeNull();
    expect(stream.accept("response.text.delta", payload(5, { text: "!", offset: 5 }))?.text).toBe("Olá 🌙!");
    expect(stream.accept("response.completed", payload(6, { text: "Olá 🌙!" }))?.state).toBe("completed");
    expect(stream.accept("response.text.delta", payload(7, { text: "late", offset: 6 }))).toBeNull();
  });

  it("marks sequence/offset gaps interrupted until a canonical completion arrives", () => {
    const stream = new ResponseStream("session-1", "task-1");
    stream.accept("response.started", payload(1));
    expect(stream.accept("response.text.delta", payload(3, { text: "missing prefix", offset: 4 }))?.state).toBe("interrupted");
    expect(stream.accept("response.text.delta", payload(4, { text: "tail", offset: 0 }))?.text).toBe("");
    expect(stream.accept("response.completed", payload(6, { text: "Full canonical text" }))).toMatchObject({ text: "Full canonical text", state: "completed" });
  });

  it("rejects other sessions, tasks and superseded responses", () => {
    const stream = new ResponseStream("session-1", "task-1");
    stream.accept("response.started", payload(1));
    expect(stream.accept("response.text.delta", payload(2, { session_id: "old", text: "old", offset: 0 }))).toBeNull();
    expect(stream.accept("response.text.delta", payload(2, { task_id: "old", text: "old", offset: 0 }))).toBeNull();
    stream.accept("response.started", payload(1, { response_id: "response-2", message_id: "message-2" }));
    expect(stream.accept("response.completed", payload(2, { text: "stale" }))).toBeNull();
  });
});

describe("chat event consumer", () => {
  it("batches a burst into one frame and upserts the legacy final event", () => {
    const hook = harness();
    hook.start();
    hook.emit("response.text.delta", payload(2, { text: "Olá ", offset: 0 }));
    hook.emit("response.text.delta", payload(3, { text: "🌙", offset: 4 }));
    expect(frames.size).toBe(1);
    expect(hook.result.current.messages).toHaveLength(0);
    paint();
    expect(hook.result.current.messages).toMatchObject([{ text: "Olá 🌙", state: "streaming" }]);
    hook.emit("response.completed", payload(4, { text: "Olá 🌙!" }));
    hook.emit("chat.message", { role: "assistant", text: "Olá 🌙!", task_id: "task-1", response_id: "response-1", message_id: "message-1" });
    expect(hook.result.current.messages).toMatchObject([{ text: "Olá 🌙!", state: "completed" }]);
    expect(frames.size).toBe(0);
  });

  it.each(["response.error", "response.cancelled"])("flushes partial text on %s and refuses late success", type => {
    const hook = harness();
    hook.start();
    hook.emit("response.text.delta", payload(2, { text: "Partial", offset: 0 }));
    hook.emit(type, payload(3));
    hook.emit("response.completed", payload(4, { text: "Late completion" }));
    hook.emit("chat.message", { role: "assistant", text: "Late legacy", task_id: "task-1", response_id: "response-1", message_id: "message-1" });
    expect(hook.result.current.messages).toMatchObject([{ text: "Partial", state: type === "response.error" ? "interrupted" : "cancelled" }]);
    expect(frames.size).toBe(0);
  });

  it("invalidates a stream immediately when Stop is clicked, before cancel IPC resolves", async () => {
    const hook = harness();
    hook.start();
    hook.emit("response.text.delta", payload(2, { text: "Partial", offset: 0 }));
    let resolve!: (value: object) => void;
    vi.mocked(hook.send).mockImplementationOnce(() => new Promise(done => { resolve = done; }) as ReturnType<SendCommandFn>);
    let stopping!: Promise<void>;
    act(() => { stopping = hook.result.current.stop(); });
    hook.emit("response.text.delta", payload(3, { text: "late", offset: 7 }));
    hook.emit("response.completed", payload(4, { text: "late final" }));
    expect(hook.result.current.messages).toMatchObject([{ text: "Partial", state: "cancelled" }]);
    await act(async () => { resolve({}); await stopping; });
  });

  it("cleans pending frames/session text and ignores stale public events", () => {
    const hook = harness();
    hook.start();
    hook.emit("response.text.delta", payload(2, { text: "old", offset: 0 }));
    hook.rerender({ id: "session-2", version: 2 });
    expect(frames.size).toBe(0);
    hook.emit("response.completed", payload(3, { text: "old final" }));
    paint();
    expect(hook.result.current.messages).toEqual([]);
    hook.emit("task.started", { task_id: "task-2", mode: "converse" });
    hook.emit("response.started", payload(1, { session_id: "session-2", task_id: "task-2" }));
    hook.emit("response.text.delta", payload(2, { session_id: "session-2", task_id: "task-2", text: "new", offset: 0 }));
    hook.unmount();
    expect(frames.size).toBe(0);
  });

  it("keeps final-only compatibility and ignores other tasks' progress/finish", () => {
    const hook = harness();
    hook.emit("task.started", { task_id: "task-1", mode: "converse" });
    hook.emit("task.progress", { task_id: "old", state: "unrelated" });
    hook.emit("task.finished", { task_id: "old", status: "SUCCESS" });
    expect(hook.result.current.task).toMatchObject({ phase: "running", progress: "" });
    hook.emit("chat.message", { role: "assistant", task_id: "task-1", text: "Final only", message_id: "final-1" });
    hook.emit("chat.message", { role: "assistant", task_id: "task-1", text: "Final only", message_id: "final-1" });
    hook.emit("task.finished", { task_id: "task-1", status: "SUCCESS" });
    expect(hook.result.current.messages).toHaveLength(1);
    expect(hook.result.current.task?.phase).toBe("finished");
  });

  it("does not mark a partial stream complete when task.finished lacks its response terminal", () => {
    const hook = harness();
    hook.start();
    hook.emit("response.text.delta", payload(2, { text: "Partial", offset: 0 }));
    hook.emit("task.finished", { task_id: "task-1", status: "SUCCESS" });
    expect(hook.result.current.messages).toMatchObject([{ text: "Partial", state: "interrupted" }]);
  });
});

describe("B1 — contrato multi-turno (ordem do serviço: task.started antes do eco)", () => {
  it("preserva três turnos consecutivos, uma vez cada, na ordem real do serviço", () => {
    const hook = harness();
    for (let n = 1; n <= 3; n++) {
      const task_id = `turn-${n}`;
      hook.emit("task.started", { session_id: "session-1", task_id, mode: "converse", instruction: `pergunta ${n}` });
      hook.emit("chat.message", { session_id: "session-1", task_id, role: "user", text: `pergunta ${n}`, message_id: `${task_id}-user` });
      hook.emit("chat.message", { session_id: "session-1", task_id, role: "assistant", text: `resposta ${n}`, message_id: `${task_id}-assistant` });
      hook.emit("task.finished", { session_id: "session-1", task_id, status: "SUCCESS" });
    }
    const users = hook.result.current.messages.filter(m => m.role === "user");
    const assistants = hook.result.current.messages.filter(m => m.role === "assistant");
    expect(users.map(m => m.text)).toEqual(["pergunta 1", "pergunta 2", "pergunta 3"]);
    expect(assistants.map(m => m.text)).toEqual(["resposta 1", "resposta 2", "resposta 3"]);
  });

  it("message_id estável: eco duplicado aparece uma única vez", () => {
    const hook = harness();
    hook.emit("task.started", { session_id: "session-1", task_id: "turn-1", mode: "converse" });
    hook.emit("chat.message", { session_id: "session-1", task_id: "turn-1", role: "user", text: "olá", message_id: "turn-1-user" });
    hook.emit("chat.message", { session_id: "session-1", task_id: "turn-1", role: "user", text: "olá", message_id: "turn-1-user" });
    expect(hook.result.current.messages.filter(m => m.role === "user")).toHaveLength(1);
  });

  it("assistente atrasado do turno anterior não invade a tarefa nova nem duplica", () => {
    const hook = harness();
    hook.emit("task.started", { session_id: "session-1", task_id: "turn-1", mode: "converse" });
    hook.emit("chat.message", { session_id: "session-1", task_id: "turn-1", role: "user", text: "primeira", message_id: "turn-1-user" });
    hook.emit("task.finished", { session_id: "session-1", task_id: "turn-1", status: "SUCCESS" });
    hook.emit("task.started", { session_id: "session-1", task_id: "turn-2", mode: "converse" });
    hook.emit("chat.message", { session_id: "session-1", task_id: "turn-2", role: "user", text: "segunda", message_id: "turn-2-user" });
    // tardio: pertence ao turn-1, que já fechou
    hook.emit("chat.message", { session_id: "session-1", task_id: "turn-1", role: "assistant", text: "ATRASADO", message_id: "turn-1-assistant" });
    hook.emit("task.finished", { session_id: "session-1", task_id: "turn-1", status: "SUCCESS" });
    expect(hook.result.current.messages.map(m => m.text)).not.toContain("ATRASADO");
    expect(hook.result.current.task).toMatchObject({ task_id: "turn-2", phase: "running" });
    expect(hook.result.current.messages.filter(m => m.role === "user").map(m => m.text)).toEqual(["primeira", "segunda"]);
  });

  it("troca de sessão descarta eco e assistente da sessão antiga", () => {
    const hook = harness();
    hook.emit("task.started", { session_id: "session-1", task_id: "turn-1", mode: "converse" });
    hook.rerender({ id: "session-2", version: 2 });
    hook.emit("chat.message", { session_id: "session-1", task_id: "turn-1", role: "user", text: "ECO ANTIGO", message_id: "turn-1-user" });
    hook.emit("chat.message", { session_id: "session-1", task_id: "turn-1", role: "assistant", text: "VELHA", message_id: "turn-1-assistant" });
    expect(hook.result.current.messages.map(m => m.text)).not.toContain("ECO ANTIGO");
    expect(hook.result.current.messages.map(m => m.text)).not.toContain("VELHA");
    // a sessão nova funciona normalmente
    hook.emit("task.started", { session_id: "session-2", task_id: "turn-9", mode: "converse" });
    hook.emit("chat.message", { session_id: "session-2", task_id: "turn-9", role: "user", text: "nova sessão", message_id: "turn-9-user" });
    expect(hook.result.current.messages.filter(m => m.role === "user").map(m => m.text)).toContain("nova sessão");
  });

  it("cancelamento conserva o eco do utilizador e o turno seguinte chega limpo", async () => {
    const hook = harness();
    hook.emit("task.started", { session_id: "session-1", task_id: "turn-1", mode: "converse" });
    hook.emit("chat.message", { session_id: "session-1", task_id: "turn-1", role: "user", text: "interromper", message_id: "turn-1-user" });
    hook.emit("response.started", payload(1, { session_id: "session-1", task_id: "turn-1" }));
    hook.emit("response.text.delta", payload(2, { session_id: "session-1", task_id: "turn-1", text: "Parcial", offset: 0 }));
    await act(async () => { await hook.result.current.stop(); });
    expect(hook.result.current.messages.filter(m => m.role === "user").map(m => m.text)).toEqual(["interromper"]);
    expect(hook.result.current.messages.some(m => m.role === "assistant" && m.state === "cancelled")).toBe(true);
    hook.emit("task.started", { session_id: "session-1", task_id: "turn-2", mode: "converse" });
    hook.emit("chat.message", { session_id: "session-1", task_id: "turn-2", role: "user", text: "de novo", message_id: "turn-2-user" });
    expect(hook.result.current.messages.filter(m => m.role === "user").map(m => m.text)).toEqual(["interromper", "de novo"]);
  });

  it("eco legado sem task_id continua a ser aceite", () => {
    const hook = harness();
    hook.emit("chat.message", { session_id: "session-1", role: "user", text: "sem tarefa", message_id: "legacy-user-1" });
    expect(hook.result.current.messages.filter(m => m.role === "user").map(m => m.text)).toEqual(["sem tarefa"]);
  });
});

describe("automatic chat capability", () => {
  function composer(automatic?: boolean) {
    const send = vi.fn(async () => ({})) as SendCommandFn;
    const ready = { ...session(), capabilities: { ...session().capabilities, ...(automatic === undefined ? {} : { auto: automatic }) } };
    const accepted = vi.fn();
    window.lunmi = { onEvent: () => () => {} } as unknown as LunmiBridgeApi;
    function Panel({ current }: { current: SessionReadyPayload }) {
      const chat = useChatTask(send, current, 1);
      return <ChatPanel chat={chat} session={current}
        attachments={[{ id: "attachment-1", name: "notes.txt", size: 4, status: "ready" }]}
        attachmentPayload={() => [{ name: "notes.txt", text: "data", size_bytes: 4 }]}
        onPickAttachment={() => {}} attachSupported onAttachmentsAccepted={accepted}
        modelPicker={<button type="button">Modelo escolhido</button>}
        autoApprove={{ ready: true, enabled: false, busy: false, onToggle: () => {} }} />;
    }
    const rendered = render(<Panel current={ready} />);
    return { send, accepted, ready, rerender: (next: SessionReadyPayload) => rendered.rerender(<Panel current={next} />) };
  }

  it("hides mode selection only when announced and sends auto with text attachments", async () => {
    const { send, accepted } = composer(true);
    expect(screen.queryByRole("radiogroup", { name: "Modo" })).toBeNull();
    expect(screen.queryByTestId("mode-converse")).toBeNull();
    expect(screen.queryByTestId("mode-execute")).toBeNull();
    expect(screen.queryByTestId("attach-mode-notice")).toBeNull();
    expect(screen.getByRole("button", { name: "Modelo escolhido" })).toBeTruthy();
    expect(screen.getByTestId("header-auto-approve")).toBeTruthy();
    fireEvent.change(screen.getByTestId("composer"), { target: { value: "Cria um ficheiro com o texto anexado" } });
    fireEvent.click(screen.getByTestId("send-task"));
    await waitFor(() => expect(send).toHaveBeenCalledWith("chat.send", {
      mode: "auto", text: "Cria um ficheiro com o texto anexado", attachments: [{ name: "notes.txt", text: "data", size_bytes: 4 }],
    }));
    await waitFor(() => expect(accepted).toHaveBeenCalledOnce());
  });

  it.each([undefined, false])("preserves legacy selectors and attachment rules when auto is %s", async automatic => {
    const { send } = composer(automatic);
    expect(screen.getByRole("radiogroup", { name: "Modo" })).toBeTruthy();
    fireEvent.change(screen.getByTestId("composer"), { target: { value: "resuma" } });
    expect(screen.getByTestId("attach-mode-notice")).toBeTruthy();
    expect((screen.getByTestId("send-task") as HTMLButtonElement).disabled).toBe(true);
    expect(send).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("mode-converse"));
    fireEvent.click(screen.getByTestId("send-task"));
    await waitFor(() => expect(send).toHaveBeenCalledWith("chat.send", {
      mode: "converse", text: "resuma", attachments: [{ name: "notes.txt", text: "data", size_bytes: 4 }],
    }));
  });

  it("preserves an unsent draft when the session starts advertising automatic chat", () => {
    const view = composer(false);
    fireEvent.change(screen.getByTestId("composer"), { target: { value: "Rascunho intacto" } });
    view.rerender({ ...view.ready, capabilities: { ...view.ready.capabilities, auto: true } });
    expect((screen.getByTestId("composer") as HTMLTextAreaElement).value).toBe("Rascunho intacto");
    expect(screen.queryByRole("radiogroup", { name: "Modo" })).toBeNull();
    expect(view.send).not.toHaveBeenCalled();
  });

  it("rejects a direct auto send if the bridge did not advertise it", async () => {
    const hook = harness();
    await act(async () => { expect(await hook.result.current.send("auto", "hello")).toMatchObject({ accepted: false }); });
    expect(hook.send).not.toHaveBeenCalled();
  });
});

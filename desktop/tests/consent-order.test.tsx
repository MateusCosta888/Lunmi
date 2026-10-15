import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useChatTask } from "../src/features/chat/useChatTask";
import type { LunmiBridgeApi, SendCommandFn } from "../src/shared/ipc/client";
import type { Envelope } from "../src/shared/protocol";

describe.each(["confirm", "choice", "location"] as const)("resposta atrasada: %s", kind => {
  function fixture() {
    let listener: (event: Envelope) => void = () => {};
    let resolve: () => void = () => {};
    const api = { onEvent: (callback: typeof listener) => {
      listener = callback;
      return () => {};
    } } as LunmiBridgeApi;
    (window as unknown as { lunmi: LunmiBridgeApi }).lunmi = api;
    const sendCommand = (() => new Promise<void>(done => { resolve = done; })) as SendCommandFn;
    const hook = renderHook(() => useChatTask(sendCommand, null, 0));
    const emit = (type: string, payload: Record<string, unknown>) =>
      listener({ v: 1, id: "event", type, payload });
    const request = (id: string) => emit(kind === "confirm" ? "task.need_confirm" : "task.need_input", {
      task_id: "task", request_id: id, kind, message: id, label: id, candidates: [],
    });
    act(() => {
      emit("task.started", { task_id: "task", mode: "execute" });
      request("first");
    });
    let response: Promise<void>;
    act(() => {
      response = kind === "confirm" ? hook.result.current.approve("first", true)
        : kind === "choice" ? hook.result.current.answerChoice("first", 0)
          : hook.result.current.answerLocation("first", "C:/owned");
    });
    return { hook, emit, request, settle: async () => {
      await act(async () => { resolve(); await response; });
    } };
  }

  it("não volta a executar depois do evento terminal", async () => {
    const test = fixture();
    act(() => test.emit("task.finished", { task_id: "task", status: "SUCCESS", message: "fim" }));
    await test.settle();
    expect(test.hook.result.current.task?.phase).toBe("finished");
    expect(test.hook.result.current.busy).toBe(false);
  });

  it("não apaga nem altera a fase do pedido seguinte", async () => {
    const test = fixture();
    act(() => test.request("second"));
    await test.settle();
    const state = test.hook.result.current;
    expect((kind === "confirm" ? state.confirmRequest : state.inputRequest)?.request_id).toBe("second");
    expect(state.task?.phase).toBe(kind === "confirm" ? "awaiting_confirm" : "awaiting_input");
    expect(state.busy).toBe(true);
  });
});

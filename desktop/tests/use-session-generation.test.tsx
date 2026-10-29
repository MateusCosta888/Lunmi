import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useSession } from "../src/shared/ipc/useSession";
import type { LunmiBridgeApi } from "../src/shared/ipc/client";
import type { Envelope, SessionReadyPayload } from "../src/shared/protocol";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function session(id: string): SessionReadyPayload {
  return {
    v: 1, session_id: id, pid: 123,
    capabilities: { converse: true, execute: true, memory: true, brain: true, web_search: false, browser_use: false },
    limits: { max_line_bytes: 5242880, max_import_bytes: 4194304, max_chat_text_chars: 8000 },
  };
}

function bridge() {
  const hello = deferred<SessionReadyPayload>();
  const restart = deferred<SessionReadyPayload>();
  let listener: ((event: Envelope) => void) | undefined;
  const api = {
    hello: vi.fn(() => hello.promise),
    restartBackend: vi.fn(() => restart.promise),
    onEvent: (callback: (event: Envelope) => void) => { listener = callback; return () => { listener = undefined; }; },
  };
  (window as unknown as { lunmi: LunmiBridgeApi }).lunmi = api as unknown as LunmiBridgeApi;
  return {
    api, hello, restart,
    emit(type: string, payload: unknown) {
      act(() => listener?.({ v: 1, id: "probe", type, payload } as Envelope));
    },
  };
}

afterEach(() => { delete (window as unknown as { lunmi?: unknown }).lunmi; });

describe("useSession — respostas obsoletas", () => {
  it.each(["resolve", "reject"] as const)("ignora hello tardio (%s) depois da falha do backend", async outcome => {
    const b = bridge();
    const { result } = renderHook(() => useSession());
    b.emit("session.ready", session("dead-session"));
    b.emit("task.error", { code: "backend_exited", message: "Backend encerrado." });
    expect(result.current.status).toBe("failed");
    await act(async () => {
      if (outcome === "resolve") b.hello.resolve(session("dead-session"));
      else b.hello.reject(new Error("Erro antigo do hello."));
    });
    expect(result.current.status).toBe("failed");
    expect(result.current.session).toBeNull();
    expect(result.current.errorMessage).toBe("Backend encerrado.");
  });

  it("uma sessão publicada mais recente prevalece sobre o hello pendente", async () => {
    const b = bridge();
    const { result } = renderHook(() => useSession());
    b.emit("session.ready", session("new-session"));
    await act(async () => { b.hello.resolve(session("old-session")); });
    expect(result.current.status).toBe("ready");
    expect(result.current.session?.session_id).toBe("new-session");
    expect(result.current.sessionVersion).toBe(1);
  });

  it.each(["resolve", "reject"] as const)("ignora restart tardio (%s) depois de uma falha mais recente", async outcome => {
    const b = bridge();
    const { result } = renderHook(() => useSession());
    await act(async () => { b.hello.resolve(session("first-session")); });
    let pending!: Promise<void>;
    act(() => { pending = result.current.restart(); });
    expect(result.current.status).toBe("restarting");
    b.emit("task.error", { code: "backend_exited", message: "Reinício interrompido." });
    await act(async () => {
      if (outcome === "resolve") b.restart.resolve(session("dead-restart"));
      else b.restart.reject(new Error("Erro antigo do reinício."));
      await pending;
    });
    expect(result.current.status).toBe("failed");
    expect(result.current.session).toBeNull();
    expect(result.current.errorMessage).toBe("Reinício interrompido.");
    expect(b.api.restartBackend).toHaveBeenCalledTimes(1);
  });

  it("continua a aceitar o hello e o reinício atuais", async () => {
    const b = bridge();
    const { result } = renderHook(() => useSession());
    await act(async () => { b.hello.resolve(session("first-session")); });
    expect(result.current.status).toBe("ready");
    let pending!: Promise<void>;
    act(() => { pending = result.current.restart(); });
    await act(async () => { b.restart.resolve(session("second-session")); await pending; });
    expect(result.current.status).toBe("ready");
    expect(result.current.session?.session_id).toBe("second-session");
    expect(result.current.sessionVersion).toBe(2);
    expect(b.api.hello).toHaveBeenCalledTimes(1);
    expect(b.api.restartBackend).toHaveBeenCalledTimes(1);
  });
});

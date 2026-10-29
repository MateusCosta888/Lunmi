import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { spawn } from "child_process";
import { afterEach, expect, it, vi } from "vitest";
import { PythonBackendSupervisor } from "../electron/backend";
import type { Envelope } from "../src/shared/protocol";

vi.mock("child_process", () => ({ spawn: vi.fn() }));

class Child extends EventEmitter {
  stdin = new PassThrough();
  stdout = new PassThrough();
  stderr = new PassThrough();
  exitCode: number | null = null;
  requests: Envelope[] = [];
  constructor(readonly pid: number) {
    super();
    this.stdin.on("data", (chunk: Buffer) => {
      const request = JSON.parse(chunk.toString("utf8")) as Envelope;
      this.requests.push(request);
      if (request.type === "session.hello" || request.type === "app.shutdown") {
        queueMicrotask(() => this.reply(request.id, { session_id: `session-${pid}`, status: "shutting_down" }));
      }
    });
  }
  reply(id: string, payload: Record<string, unknown>) {
    this.stdout.write(JSON.stringify({ v: 1, id, type: "command.result", payload }) + "\n");
  }
  kill() { return true; } // Exit can arrive later on Windows; the old process remains test-owned.
}

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

it.each(["exit", "stdin", "stdout", "process"])("um %s tardio do backend antigo não invalida a sessão nova", async source => {
  vi.useFakeTimers();
  const children: Child[] = [];
  vi.mocked(spawn).mockImplementation((() => {
    const child = new Child(100 + children.length);
    children.push(child);
    return child;
  }) as unknown as typeof spawn);
  const events: Envelope[] = [];
  const supervisor = new PythonBackendSupervisor({
    repoRoot: ".", clientVersion: "test", getHostPids: () => [], onEvent: event => events.push(event),
  });
  await supervisor.startAndHandshake();
  const previous = children[0];
  const restarting = supervisor.restart();
  await vi.advanceTimersByTimeAsync(1500);
  const ready = await restarting;
  const current = children[1];
  expect(ready.session_id).toBe("session-101");
  const response = supervisor.sendRendererCommand("brain.get", {});
  if (source === "exit") previous.emit("exit", 0, null);
  else if (source === "stdin") previous.stdin.emit("error", new Error("late EPIPE"));
  else if (source === "stdout") previous.stdout.emit("error", new Error("late read error"));
  else previous.emit("error", new Error("late process error"));
  const request = current.requests.find(item => item.type === "brain.get")!;
  current.reply(request.id, { provider: "fixture" });

  await expect(response).resolves.toEqual({ provider: "fixture" });
  expect(supervisor.currentSession).toEqual(ready);
  expect(supervisor.pid).toBe(current.pid);
  expect(events).toEqual([]);
  const currentFailure = supervisor.sendRendererCommand("brain.get", {});
  const rejected = expect(currentFailure).rejects.toThrow("backend_exited");
  current.exitCode = 1;
  current.emit("exit", 1, null);
  await rejected;
  expect(supervisor.currentSession).toBeNull();
  expect(events).toHaveLength(1);
  const stopping = supervisor.shutdown();
  await vi.advanceTimersByTimeAsync(1500);
  await stopping;
});

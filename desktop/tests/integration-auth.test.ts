import { describe, expect, it, vi } from "vitest";
import { IntegrationAuthGate, isComposioAuthUrl } from "../electron/integration-auth";
import { PythonBackendSupervisor } from "../electron/backend";
import type { SessionReadyPayload } from "../src/shared/protocol";

const request = { url: "https://connect.composio.dev/oauth/authorize?state=synthetic", flow_id: "flow1", session_id: "session1" };
const progress = { gmail: { state: "connecting", flow_id: "flow1" } };

describe("Composio browser authority", () => {
  it.each([
    "http://connect.composio.dev/oauth/authorize", "https://connect.composio.dev.evil.test/",
    "https://user:secret@connect.composio.dev/", "https://connect.composio.dev:444/",
    "javascript:alert(1)", "file:///private", "https://accounts.google.com/", "https://connect.composio.dev/\n",
  ])("rejects an unapproved URL: %s", (url) => {
    expect(isComposioAuthUrl(url)).toBe(false);
  });

  it("requires a click, matching flow/session and consumes duplicate URLs once", () => {
    const gate = new IntegrationAuthGate();
    gate.observe(progress);
    expect(gate.accept(request)).toBe(false);
    gate.arm("session1");
    gate.observe(progress);
    expect(gate.accept({ ...request, session_id: "old" })).toBe(false);
    expect(gate.accept({ ...request, flow_id: "other" })).toBe(false);
    expect(gate.accept(request)).toBe(true);
    expect(gate.accept(request)).toBe(false);
    expect(gate.accept({ ...request, url: "https://connect.composio.dev/link/synthetic" })).toBe(true);
    expect(gate.accept({ ...request, url: "https://connect.composio.dev/link/third" })).toBe(false);
  });

  it("cancel, account-ready and different flow invalidate browser authority", () => {
    const gate = new IntegrationAuthGate();
    for (const action of [() => gate.clear(), () => gate.observe({ gmail: { state: "connected" } }),
      () => gate.observe({ gmail: { state: "connecting", flow_id: "different" } })]) {
      gate.arm("session1"); gate.observe(progress); action();
      expect(gate.accept(request)).toBe(false);
    }
  });

  it("private URLs never reach renderer and a cancelled attempt does not open later", async () => {
    const onEvent = vi.fn(); const open = vi.fn(async () => undefined);
    const supervisor = new PythonBackendSupervisor({ repoRoot: ".", clientVersion: "test", getHostPids: () => [], onEvent, onIntegrationAuthOpen: open });
    const internals = supervisor as unknown as { handleLine: (line: string) => void; readyPayload: SessionReadyPayload };
    internals.readyPayload = { session_id: "session1" } as SessionReadyPayload;
    const send = vi.spyOn(supervisor, "_sendInternalCommand").mockResolvedValue({});
    await supervisor.sendRendererCommand("integrations.connect");
    const emit = (type: string, payload: object) => internals.handleLine(JSON.stringify({ v: 1, id: "evt", type, payload }));
    emit("integrations.changed", progress);
    emit("integrations.browser.open", request);
    await supervisor.sendRendererCommand("integrations.cancel");
    await Promise.resolve();
    expect(open).not.toHaveBeenCalled();
    expect(onEvent.mock.calls.every(([event]) => event.type !== "integrations.browser.open")).toBe(true);
    await expect(supervisor.sendRendererCommand("integrations.browser.failed", { flow_id: "flow1" })).rejects.toThrow("forbidden_command");
    expect(send.mock.calls.some(([type]) => type === "integrations.browser.failed")).toBe(false);
  });

  it("opens only an active attempt and reports browser failure without echoing URL", async () => {
    const onEvent = vi.fn(); const open = vi.fn(async () => { throw new Error("private-url"); });
    const supervisor = new PythonBackendSupervisor({ repoRoot: ".", clientVersion: "test", getHostPids: () => [], onEvent, onIntegrationAuthOpen: open });
    const internals = supervisor as unknown as { handleLine: (line: string) => void; readyPayload: SessionReadyPayload; proc: object };
    internals.readyPayload = { session_id: "session1" } as SessionReadyPayload;
    internals.proc = { exitCode: null };
    const send = vi.spyOn(supervisor, "_sendInternalCommand").mockResolvedValue({});
    await supervisor.sendRendererCommand("integrations.connect");
    internals.handleLine(JSON.stringify({ v: 1, id: "e1", type: "integrations.changed", payload: progress }));
    internals.handleLine(JSON.stringify({ v: 1, id: "e2", type: "integrations.browser.open", payload: request }));
    await vi.waitFor(() => expect(send).toHaveBeenCalledWith("integrations.browser.failed", { flow_id: "flow1" }));
    expect(open).toHaveBeenCalledExactlyOnceWith(request.url);
    expect(JSON.stringify(onEvent.mock.calls)).not.toContain("synthetic");
  });
});

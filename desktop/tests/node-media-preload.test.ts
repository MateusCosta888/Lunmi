import { beforeEach, describe, expect, it, vi } from "vitest";
import type { LunmiPresenceApi } from "../src/shared/types";

const fake = vi.hoisted(() => ({ exposed: {} as Record<string, unknown>, invoke: vi.fn() }));
vi.mock("electron", () => ({
  contextBridge: { exposeInMainWorld: (name: string, api: unknown) => { fake.exposed[name] = api; } },
  ipcRenderer: { invoke: fake.invoke, on: vi.fn(), removeListener: vi.fn() },
}));

beforeEach(() => {
  vi.resetModules();
  fake.exposed = {};
  fake.invoke.mockReset();
  window.history.replaceState({}, "", "/");
});

describe("Node media preload", () => {
  it("não expõe o leitor à janela principal", async () => {
    await import("../electron/preload");
    expect((fake.exposed.lunmiPresence as LunmiPresenceApi).media).toBeUndefined();
    expect(fake.exposed.lunmi).toBeDefined();
  });

  it("expõe só os dois canais fechados ao companheiro e transporta o cancelamento do ditado", async () => {
    window.history.replaceState({}, "", "/?window=companion");
    await import("../electron/preload");
    const api = fake.exposed.lunmiPresence as LunmiPresenceApi;
    expect(fake.exposed.lunmi).toBeUndefined();
    expect(Object.keys(api.media!)).toEqual(["get", "command"]);
    await api.media!.get();
    expect(fake.invoke).toHaveBeenLastCalledWith("lunmi:node-media-get");
    await api.media!.command({ command: "pause", session_id: "opaque-1" });
    expect(fake.invoke).toHaveBeenLastCalledWith("lunmi:node-media-command", { command: "pause", session_id: "opaque-1" });
    await api.action("cancel-dictation");
    expect(fake.invoke).toHaveBeenLastCalledWith("lunmi:presence-action", { action: "cancel-dictation" });
    expect(Object.isFrozen(api.media)).toBe(true);
    window.history.replaceState({}, "", "/");
  });
});

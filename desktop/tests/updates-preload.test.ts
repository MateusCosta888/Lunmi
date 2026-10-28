import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => { window.history.replaceState({}, "", "/"); vi.resetModules(); vi.doUnmock("electron"); });

async function preload(companion = false) {
  vi.resetModules();
  if (companion) window.history.replaceState({}, "", "/?window=companion");
  const exposeInMainWorld = vi.fn();
  const ipc = { invoke: vi.fn(), on: vi.fn(), removeListener: vi.fn() };
  vi.doMock("electron", () => ({ contextBridge: { exposeInMainWorld }, ipcRenderer: ipc }));
  await import("../electron/preload");
  const api = exposeInMainWorld.mock.calls.find(call => call[0] === "lunmi")?.[1] as import("../electron/preload").LunmiBridgeApi | undefined;
  return { api, ipc, exposed: exposeInMainWorld.mock.calls.map(call => call[0]) };
}

describe("update preload authority", () => {
  it("exposes only narrow actions and removable state events", async () => {
    const { api, ipc } = await preload();
    expect(Object.keys(api!.updates).sort()).toEqual(["check", "get", "install", "onState"]);
    await api!.updates.get(); await api!.updates.check(); await api!.updates.install();
    expect(ipc.invoke.mock.calls).toEqual([["lunmi:updates-get"], ["lunmi:updates-check"], ["lunmi:updates-install"]]);
    const listener = vi.fn(); const dispose = api!.updates.onState(listener);
    const handler = ipc.on.mock.calls[0][1];
    handler({}, { phase: "ready", currentVersion: "0.1.0-alpha.4" });
    expect(listener).toHaveBeenCalledWith({ phase: "ready", currentVersion: "0.1.0-alpha.4" });
    dispose(); expect(ipc.removeListener).toHaveBeenCalledWith("lunmi:updates-state", handler);
  });
  it("does not give the mini chat update authority", async () => {
    const { api, exposed } = await preload(true);
    expect(api).toBeUndefined(); expect(exposed).toEqual(["lunmiPresence"]);
  });
});

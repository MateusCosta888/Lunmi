/**
 * WEB-SEARCH-V1 — preload: `openWebSource` existe nas duas janelas e usa o canal fechado.
 *
 * O main é quem valida sender/payload/URL; aqui só se prova que o renderer não ganha outro
 * caminho e que nenhum campo além dos três IDs é transportado por engano.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LunmiPresenceApi } from "../src/shared/types";

const fake = vi.hoisted(() => ({
  exposed: {} as Record<string, unknown>,
  invoke: vi.fn(),
}));

vi.mock("electron", () => ({
  contextBridge: {
    exposeInMainWorld: (name: string, api: unknown) => {
      fake.exposed[name] = api;
    },
  },
  ipcRenderer: {
    invoke: fake.invoke,
    on: vi.fn(),
    removeListener: vi.fn(),
  },
}));

const INPUT = {
  session_id: "sess-1",
  task_id: "task-1",
  source_id: "src-0123456789abcdef",
};

describe("preload openWebSource", () => {
  beforeEach(() => {
    vi.resetModules();
    for (const key of Object.keys(fake.exposed)) {
      delete fake.exposed[key];
    }
    fake.invoke.mockReset();
    fake.invoke.mockResolvedValue({ opened: true, reason: "" });
    window.history.replaceState({}, "", "/");
  });

  afterEach(() => {
    window.history.replaceState({}, "", "/");
  });

  it("expõe o método na janela principal e usa lunmi:web-open-source", async () => {
    await import("../electron/preload");

    const api = fake.exposed.lunmiPresence as LunmiPresenceApi;
    expect(api).toBeDefined();
    expect(api.openWebSource).toBeTypeOf("function");

    await expect(api.openWebSource!(INPUT)).resolves.toEqual({
      opened: true,
      reason: "",
    });
    expect(fake.invoke).toHaveBeenCalledWith("lunmi:web-open-source", INPUT);
    expect(fake.exposed.lunmi).toBeDefined();
  });

  it("expõe o método também na janela companheira", async () => {
    window.history.replaceState({}, "", "/?window=companion");
    await import("../electron/preload");

    const api = fake.exposed.lunmiPresence as LunmiPresenceApi;
    expect(api.openWebSource).toBeTypeOf("function");

    await api.openWebSource!(INPUT);
    expect(fake.invoke).toHaveBeenCalledWith("lunmi:web-open-source", INPUT);
    expect(fake.exposed.lunmi).toBeUndefined();
  });
});

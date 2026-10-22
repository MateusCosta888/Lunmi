import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { useChatTask } from "../src/features/chat/useChatTask";
import { COMPANION_COMMAND_ALLOWLIST } from "../src/features/chat/useCompanionChat";
import { COMPANION_ACTIONS, COMPANION_COMMANDS } from "../electron/main";
import { createChatProjection, projectChat } from "../src/shared/chatProjection";
import {
  useNodePreferences,
  useNodeState,
  usePresenceDraft,
  usePresenceMode,
  usePresenceSession,
  usePresenceTheme,
  usePresenceVoiceState,
} from "../src/shared/ipc/presence";
import { DEFAULT_NODE_PREFERENCES } from "../src/shared/node";
import type { NodeStateDto } from "../src/shared/types";
import type { LunmiBridgeApi, SendCommandFn } from "../src/shared/ipc/client";
import type { Envelope, SessionReadyPayload } from "../src/shared/protocol";
import type {
  LunmiPresenceApi,
  PresenceEventDto,
  PresenceSnapshotDto,
} from "../src/shared/types";

afterEach(() => {
  delete (window as unknown as { lunmiPresence?: unknown }).lunmiPresence;
  delete (window as unknown as { lunmi?: unknown }).lunmi;
});

const session: SessionReadyPayload = {
  v: 1,
  session_id: "s1",
  pid: 1,
  capabilities: {
    converse: true, execute: true, memory: true, brain: true,
    web_search: false, browser_use: false, auto: true,
  },
  limits: { max_line_bytes: 1, max_import_bytes: 1, max_chat_text_chars: 1 },
};

function snapshotDto(overrides: Partial<PresenceSnapshotDto> = {}): PresenceSnapshotDto {
  return {
    mode: "orb",
    resident: true,
    session,
    chat: projectChat(createChatProjection("s1")),
    draft: "",
    theme: "celestial",
    voice: null,
    ...overrides,
  };
}

function wire(options: { snapshot?: PresenceSnapshotDto; deferred?: boolean } = {}) {
  const listeners = new Set<(event: PresenceEventDto) => void>();
  let resolveSnapshot: ((snapshot: PresenceSnapshotDto) => void) | null = null;
  const pending = options.deferred
    ? new Promise<PresenceSnapshotDto>((resolve) => { resolveSnapshot = resolve; })
    : Promise.resolve(options.snapshot ?? snapshotDto());
  const setDraft = vi.fn(async () => ({ ok: true }));
  const api: LunmiPresenceApi = {
    snapshot: vi.fn(() => pending),
    action: vi.fn(async () => ({ ok: true, mode: "orb" as const, resident: true })),
    setDraft,
    sendCommand: vi.fn(async () => ({})) as unknown as LunmiPresenceApi["sendCommand"],
    onEvent: (callback) => {
      listeners.add(callback);
      return () => { listeners.delete(callback); };
    },
  };
  (window as unknown as { lunmiPresence?: LunmiPresenceApi }).lunmiPresence = api;
  return {
    api,
    setDraft,
    emit: (event: PresenceEventDto) => {
      for (const callback of [...listeners]) callback(event);
    },
    resolve: async (snapshot: PresenceSnapshotDto) => {
      resolveSnapshot?.(snapshot);
      await Promise.resolve();
      await Promise.resolve();
    },
  };
}

function fakeBridge() {
  const listeners = new Set<(event: Envelope) => void>();
  const api = {
    onEvent: (callback: (event: Envelope) => void) => {
      listeners.add(callback);
      return () => { listeners.delete(callback); };
    },
    sendCommand: vi.fn(async () => ({})) as unknown as LunmiPresenceApi["sendCommand"],
  };
  (window as unknown as { lunmi?: unknown }).lunmi = api as unknown as LunmiBridgeApi;
  return {
    emit: (type: string, payload: Record<string, unknown>) => {
      for (const callback of [...listeners]) {
        callback({ v: 1, id: `evt-${type}`, type, payload });
      }
    },
  };
}

describe("rascunho partilhado e modo", () => {
  it("preserva Dispatch<SetStateAction<string>> e publica o valor resolvido", async () => {
    const remote = wire();
    const hook = renderHook(() => usePresenceDraft());
    await waitFor(() => expect(remote.api.snapshot).toHaveBeenCalled());

    act(() => hook.result.current[1]("texto"));
    act(() => hook.result.current[1]((previous) => `${previous}!`));

    expect(hook.result.current[0]).toBe("texto!");
    expect(remote.setDraft).toHaveBeenLastCalledWith("texto!");
  });

  it("snapshot tardio não desfaz uma edição local", async () => {
    const remote = wire({ deferred: true });
    const hook = renderHook(() => usePresenceDraft());
    act(() => hook.result.current[1]("edição local"));
    await act(async () => remote.resolve(snapshotDto({ draft: "rascunho antigo" })));
    expect(hook.result.current[0]).toBe("edição local");
  });

  it("evento de modo recebido após subscrever vence o snapshot tardio", async () => {
    const remote = wire({ deferred: true });
    const hook = renderHook(() => usePresenceMode());
    act(() => remote.emit({ type: "mode", mode: "mini" }));
    await act(async () => remote.resolve(snapshotDto({ mode: "orb" })));
    expect(hook.result.current).toBe("mini");
  });

  it("evento de sessão recebido após subscrever vence o snapshot tardio", async () => {
    const remote = wire({ deferred: true });
    const hook = renderHook(() => usePresenceSession());
    const next = { ...session, session_id: "s2" };
    act(() => remote.emit({ type: "session", session: next }));
    await act(async () => remote.resolve(snapshotDto({ session })));
    expect(hook.result.current.session?.session_id).toBe("s2");
  });

  it("tema vem no snapshot e um evento mais novo vence o snapshot tardio", async () => {
    const loaded = wire();
    const themeHook = renderHook(() => usePresenceTheme());
    await waitFor(() => expect(themeHook.result.current).toBe("celestial"));
    act(() => loaded.emit({ type: "theme", theme: "redvelvet" }));
    expect(themeHook.result.current).toBe("redvelvet");

    delete (window as unknown as { lunmiPresence?: unknown }).lunmiPresence;
    const late = wire({ deferred: true });
    const lateHook = renderHook(() => usePresenceTheme());
    act(() => late.emit({ type: "theme", theme: "light" }));
    await act(async () => late.resolve(snapshotDto({ theme: "celestial" })));
    expect(lateHook.result.current).toBe("light");
  });

  it("abrir o mini durante a fala recupera o último voice.state do snapshot", async () => {
    const voice = {
      session_id: "s1", enabled: true, auto_speak: true, available: true,
      state: "playing" as const, generation: 3, can_speak_last: true,
      capture_supported: false as const, message: "",
    };
    wire({ snapshot: snapshotDto({ voice }) });
    const hook = renderHook(() => usePresenceVoiceState());
    await waitFor(() => expect(hook.result.current?.state).toBe("playing"));
    expect(hook.result.current?.generation).toBe(3);
  });
});

function nodeState(overrides: Partial<NodeStateDto> = {}): NodeStateDto {
  return {
    v: 1,
    enabled: true,
    side: "right",
    surface: "docked",
    phase: "settled",
    transition_id: 0,
    transition: null,
    status: null,
    tone: "neutral",
    session_id: "s1",
    ...overrides,
  };
}

describe("hooks do NODE N1", () => {
  it("useNodeState: evento novo vence e um snapshot tardio com id antigo não regride", async () => {
    const remote = wire({ deferred: true });
    const hook = renderHook(() => useNodeState());
    act(() => remote.emit({ type: "node", state: nodeState({ surface: "chat", transition_id: 5 }) }));
    expect(hook.result.current.state?.surface).toBe("chat");
    await act(async () =>
      remote.resolve(snapshotDto({ node: nodeState({ surface: "docked", transition_id: 2 }) }))
    );
    expect(hook.result.current.state?.surface).toBe("chat");
  });

  it("useNodePreferences expõe disponibilidade, aplica sucesso e mantém valor em recusa", async () => {
    const remote = wire({ snapshot: snapshotDto({ node_preferences: { ...DEFAULT_NODE_PREFERENCES, side: "left" } }) });
    const setNodePreferences = vi.fn(async (patch: Record<string, unknown>) => {
      if (patch.auto_dock === false) {
        return { ok: false, preferences: { ...DEFAULT_NODE_PREFERENCES, side: "left" }, error: "io_error" };
      }
      return { ok: true, preferences: { ...DEFAULT_NODE_PREFERENCES, side: "left", ...patch } };
    });
    (remote.api as { setNodePreferences?: unknown }).setNodePreferences = setNodePreferences;

    const hook = renderHook(() => useNodePreferences());
    await waitFor(() => expect(hook.result.current.preferences?.side).toBe("left"));
    expect(hook.result.current.available).toBe(true);

    await act(async () => { await hook.result.current.setNodePreferences({ enabled: false }); });
    expect(hook.result.current.preferences?.enabled).toBe(false);
    expect(hook.result.current.error).toBeNull();

    // Recusa honesta: volta ao último valor confirmado e marca erro; sem estado otimista.
    await act(async () => { await hook.result.current.setNodePreferences({ auto_dock: false }); });
    expect(hook.result.current.preferences?.auto_dock).toBe(true);
    expect(hook.result.current.error).toBe("io_error");
  });

  it("sem método no preload, a opção fica indisponível sem falhar", async () => {
    wire({ snapshot: snapshotDto({ node_preferences: DEFAULT_NODE_PREFERENCES }) });
    const hook = renderHook(() => useNodePreferences());
    await waitFor(() => expect(hook.result.current.preferences).not.toBeNull());
    expect(hook.result.current.available).toBe(false);
    await act(async () => { await hook.result.current.setNodePreferences({ enabled: false }); });
    expect(hook.result.current.preferences?.enabled).toBe(true);
  });
});

describe("hook de chat consumindo a projeção do main", () => {
  it("aplica snapshot + deltas e recusa cursor duplicado", async () => {
    const remote = wire();
    const sendCommand = vi.fn(async () => ({})) as unknown as SendCommandFn;
    const hook = renderHook(() => useChatTask(sendCommand, session, 1));
    await waitFor(() => expect(remote.api.snapshot).toHaveBeenCalled());

    act(() => remote.emit({
      type: "chat",
      delta: {
        session_id: "s1", cursor: 1,
        messages: [{ role: "user", text: "enviado", task_id: "t1", message_id: "m1" }],
      },
    }));
    await waitFor(() => expect(hook.result.current.messages.map((m) => m.text)).toEqual(["enviado"]));

    act(() => remote.emit({
      type: "chat",
      delta: {
        session_id: "s1", cursor: 1,
        messages: [{ role: "user", text: "duplicado", task_id: "t1", message_id: "m1" }],
      },
    }));
    expect(hook.result.current.messages.map((m) => m.text)).toEqual(["enviado"]);

    act(() => remote.emit({
      type: "chat",
      delta: { session_id: "s1", cursor: 2, task: {
        task_id: "t1", mode: "converse", instruction: "enviado", phase: "running",
        progress: "Thinking", result: null, error: null,
      } },
    }));
    await waitFor(() => expect(hook.result.current.busy).toBe(true));
  });

  it("delta a chegar antes do snapshot é aplicado depois da hidratação", async () => {
    const remote = wire({ deferred: true });
    const sendCommand = vi.fn(async () => ({})) as unknown as SendCommandFn;
    const hook = renderHook(() => useChatTask(sendCommand, session, 1));
    act(() => remote.emit({
      type: "chat",
      delta: {
        session_id: "s1", cursor: 1,
        messages: [{ role: "user", text: "durante o streaming", task_id: "t1", message_id: "m1" }],
      },
    }));
    await act(async () => remote.resolve(snapshotDto()));
    expect(hook.result.current.messages.map((m) => m.text)).toEqual(["durante o streaming"]);
  });

  it("peer antigo sem lunmiPresence reduz os mesmos eventos (mesmo parser)", async () => {
    delete (window as unknown as { lunmiPresence?: unknown }).lunmiPresence;
    const bridge = fakeBridge();
    const sendCommand = vi.fn(async () => ({})) as unknown as SendCommandFn;
    const hook = renderHook(() => useChatTask(sendCommand, session, 1));
    act(() => {
      bridge.emit("task.started", { session_id: "s1", task_id: "t1", mode: "converse" });
      bridge.emit("chat.message", {
        session_id: "s1", task_id: "t1", role: "user", text: "olá", message_id: "m1",
      });
    });
    await waitFor(() => expect(hook.result.current.messages.map((m) => m.text)).toEqual(["olá"]));
    expect(hook.result.current.busy).toBe(true);
  });
});

describe("allowlists do companheiro", () => {
  it("preload/hook e main concordam e não incluem confirmação, restart ou saída", () => {
    expect([...COMPANION_COMMANDS].sort()).toEqual([...COMPANION_COMMAND_ALLOWLIST].sort());
    for (const forbidden of ["task.confirm", "task.answer", "session.hello", "app.shutdown", "fs.read"]) {
      expect(COMPANION_COMMANDS.has(forbidden)).toBe(false);
    }
    expect([...COMPANION_ACTIONS]).not.toContain("quit");
    expect([...COMPANION_ACTIONS]).toContain("show-settings");
  });
});

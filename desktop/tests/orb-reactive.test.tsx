/**
 * Testes ORB REACTIVE V1 (`desktop/tests/orb-reactive.test.tsx`).
 *
 * jsdom não tem rasterizador: o motor real é exercido em `chat-orb.test.tsx` com contexto
 * falso; aqui o motor é substituído por um registo fake para provar a INTEGRAÇÃO — derivação
 * de modo a partir de estados reais apenas (listening com prioridade; starting/transcribing/
 * working/loading/synthesizing = thinking; playing = speaking; error/null = idle), `setMode`
 * propagado ao motor, `setActive(false)` quando a camada está oculta (superfície chat) ou o
 * componente desmonta (destroy), e o fallback honesto via `onUnavailable` → marca real
 * acessível (img). Node oculto NÃO é "indisponível": a engine existe e fica pausada.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const recorder = vi.hoisted(() => ({
  setModes: [] as string[],
  actives: [] as boolean[],
  destroys: 0,
  unavailable: null as null | (() => void) | "auto",
}));

vi.mock("../src/features/chat/orb/liveRenderer", () => ({
  mountLiveOrb: vi.fn((_canvas: unknown, options?: { onUnavailable?: () => void }) => {
    if (recorder.unavailable === "auto") options?.onUnavailable?.();
    return {
      setMode: (mode: string) => { recorder.setModes.push(mode); },
      setActive: (active: boolean) => { recorder.actives.push(active); },
      destroy: () => { recorder.destroys += 1; },
    };
  }),
}));

import { GalaxyOrb } from "../src/features/chat/orb/GalaxyOrb";
import { deriveOrbMode } from "../src/features/chat/orb/orbState";
import { NodeSurface } from "../src/features/companion/NodeSurface";
import type { LunmiPresenceApi, NodeStateDto, PresenceSnapshotDto } from "../src/shared/types";

beforeEach(() => {
  recorder.setModes = [];
  recorder.actives = [];
  recorder.destroys = 0;
  recorder.unavailable = null;
});

afterEach(() => {
  delete (window as unknown as { lunmiPresence?: unknown }).lunmiPresence;
});

describe("deriveOrbMode — só estados reais", () => {
  it("listening tem prioridade sobre tudo", () => {
    expect(deriveOrbMode({ inputState: "listening", ttsState: "playing", taskWorking: true })).toBe("listening");
    expect(deriveOrbMode({ voicePhase: "listening", ttsState: "playing" })).toBe("listening");
  });
  it("starting/transcribing/loading/synthesizing/working/thinking são thinking", () => {
    expect(deriveOrbMode({ inputState: "starting" })).toBe("thinking");
    expect(deriveOrbMode({ inputState: "transcribing" })).toBe("thinking");
    expect(deriveOrbMode({ ttsState: "loading" })).toBe("thinking");
    expect(deriveOrbMode({ ttsState: "synthesizing" })).toBe("thinking");
    expect(deriveOrbMode({ taskWorking: true })).toBe("thinking");
    expect(deriveOrbMode({ voicePhase: "thinking" })).toBe("thinking");
  });
  it("playing é speaking; nada mais é idle; error/null não é estado de orb", () => {
    expect(deriveOrbMode({ ttsState: "playing" })).toBe("speaking");
    expect(deriveOrbMode({})).toBe("idle");
    expect(deriveOrbMode({ inputState: "idle", ttsState: "ready" })).toBe("idle");
    expect(deriveOrbMode({ voicePhase: "error" })).toBe("idle");
    expect(deriveOrbMode({ voicePhase: null })).toBe("idle");
    expect(deriveOrbMode({ inputState: "unavailable" })).toBe("idle");
  });
});

describe("GalaxyOrb — ponte para o motor", () => {
  it("mode altera o motor e é espelhado em data-mode para o QA", () => {
    const view = render(<GalaxyOrb mode="idle" />);
    expect(screen.getByTestId("chat-orb").getAttribute("data-mode")).toBe("idle");
    view.rerender(<GalaxyOrb mode="listening" />);
    expect(screen.getByTestId("chat-orb").getAttribute("data-mode")).toBe("listening");
    expect(recorder.setModes).toContain("listening");
    view.unmount();
    expect(recorder.destroys).toBe(1);
  });

  it("active=false pausa a engine; o componente deixa de estar ativo no motor", () => {
    const view = render(<GalaxyOrb mode="idle" active />);
    expect(recorder.actives.at(-1)).toBe(true);
    view.rerender(<GalaxyOrb mode="idle" active={false} />);
    expect(recorder.actives.at(-1)).toBe(false);
  });

  it("onUnavailable do motor → marca real acessível (img), sem canvas pintado", () => {
    recorder.unavailable = "auto";
    render(<GalaxyOrb mode="thinking" compact />);
    const orb = screen.getByTestId("chat-orb");
    expect(orb.getAttribute("data-unavailable")).toBe("true");
    expect(orb.querySelector("img")).not.toBeNull(); // BrandMark é <img>; a11y vem do role/label do contentor
    expect(screen.getByRole("img", { name: "Lunmi" })).toBeTruthy();
  });
});

describe("NodeSurface — miniatura na face circular, resto preservado", () => {
  function presence(node: Partial<NodeStateDto> = {}) {
    const nodeState: NodeStateDto = {
      v: 1, enabled: true, side: "right", surface: "docked", phase: "settled", transition_id: 1,
      transition: null, status: null, tone: "neutral", session_id: "s-orb", ...node,
    };
    const snapshot: PresenceSnapshotDto = {
      mode: "orb", resident: true, session: null,
      chat: { session_id: "s-orb", cursor: 0, messages: [], recent_tasks: [], task: null, confirm: null, input: null },
      draft: "", theme: "celestial", voice: null, node: nodeState,
    };
    (window as unknown as { lunmiPresence: unknown }).lunmiPresence = {
      snapshot: vi.fn(async () => snapshot),
      onEvent: () => () => undefined,
      action: vi.fn(async () => ({ ok: true })),
    } as unknown as LunmiPresenceApi;
  }

  it("a face circular contém a orb compacta com o modo derivado do voice_phase", async () => {
    presence({ voice_phase: "listening" });
    render(<NodeSurface />);
    const circle = await screen.findByTestId("node-circle");
    const orb = circle.querySelector('[data-testid="chat-orb"]') as HTMLElement;
    expect(orb.getAttribute("data-compact")).toBe("true");
    expect(orb.getAttribute("data-mode")).toBe("listening");
    expect(circle.querySelector('[data-testid="node-dot"]')).not.toBeNull(); // indicador de tom preservado
  });

  it("working sem voice_phase: thinking na orb e etiqueta da tarefa na cápsula (V1)", async () => {
    presence({ voice_phase: null, status: "working" });
    render(<NodeSurface />);
    const circle = await screen.findByTestId("node-circle");
    expect(circle.querySelector('[data-testid="chat-orb"]')?.getAttribute("data-mode")).toBe("thinking");
    expect(screen.getByTestId("node-pill-text").textContent).toBe("A trabalhar");
    expect(screen.getByTestId("lunmi-node").getAttribute("data-surface")).toBe("docked");
  });

  it("superfície chat: camada do círculo oculta e engine pausada (setActive false), não 'indisponível'", async () => {
    presence({ voice_phase: null, surface: "chat" });
    render(<NodeSurface />);
    await screen.findByTestId("node-chat");
    const circle = screen.getByTestId("node-circle");
    expect(circle.hasAttribute("hidden")).toBe(true);
    const orb = circle.querySelector('[data-testid="chat-orb"]') as HTMLElement;
    expect(orb.getAttribute("data-unavailable")).toBe("false"); // existe e está pausada
    await vi.waitFor(() => expect(recorder.actives.at(-1)).toBe(false)); // camada inactiva → pausa
  });
});

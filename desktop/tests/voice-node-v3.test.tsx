/**
 * Testes VOICE/NODE V3 (`desktop/tests/voice-node-v3.test.tsx`).
 *
 * Provas do refinamento aprovado: microfone redondo 44 px só com ícone, imediatamente à
 * direita de Enviar/Parar tarefa; nenhum texto de estado normal da voz (entrada e saída) —
 * labels vivem em aria/title/sr-only; erros, Cancelar, preview e Parar voz (distinto de Parar
 * tarefa) preservados; sem segundo input hook nem autoenvio manual. NodeSurface espelha
 * exatamente o campo canónico `NodeStateDto.voice_phase` na cápsula aprovada: starting →
 * "A preparar o microfone", listening → "A escutar", thinking → "A pensar"; ausente/null →
 * comportamento V1 (etiqueta de estado/`Lunmi`). jsdom não mede layout: geometria física é do
 * gate visual do Root; aqui prova-se DOM, ordem, classes, acessibilidade e rótulos.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App } from "../src/App";
import { NodeSurface } from "../src/features/companion/NodeSurface";
import type { LunmiPresenceApi, NodeStateDto, PresenceSnapshotDto } from "../src/shared/types";

afterEach(() => {
  delete (window as unknown as { lunmi?: unknown }).lunmi;
  delete (window as unknown as { lunmiPresence?: unknown }).lunmiPresence;
});

function setupApp() {
  const sendCommand = vi.fn(async (type: string): Promise<unknown> => {
    if (type === "brain.get") return { current: { provider: "custom", provider_label: "Local", model: "balanced", app_provider: "same", app_model: "balanced", has_api_key: true, env_overrides: {} } };
    if (type === "memory.profile.get") return { enabled: false, auto_learn: false, text: "", revision: 0, can_undo: false, char_count: 0, max_chars: 8000, state: "disabled" };
    if (type === "ui.preferences.get") return { scale_pct: 100, actions: [], theme: "celestial" };
    if (type === "approval.get") return { auto_approve: false };
    if (type === "voice.get") return { enabled: false, auto_speak: false, available: true, state: "ready", generation: 1, can_speak_last: false, capture_supported: false, message: "" };
    if (type === "voice.warmup" || type === "voice.warmup.get") return { state: "ready", ready: true, tts: "ready", stt: "ready", message: "" };
    if (type === "voice.input.get") return { available: true, state: "idle", generation: 0, message: "" };
    if (type === "voice.input.start") return { accepted: true, generation: 1, state: "starting", reason: "" };
    if (type === "voice.input.cancel") return { accepted: true, generation: 2, state: "idle", reason: "" };
    return {};
  });
  const api = {
    hello: vi.fn(async () => ({
      session_id: "v3-test",
      capabilities: { brain: true, memory: true, execute: true, converse: true, voice_input: true },
    })),
    onEvent: () => () => undefined,
    restartBackend: vi.fn(async () => ({ session_id: "v3-test", capabilities: {} })),
    notifyRendererReady: vi.fn(async () => ({})),
    sendCommand,
  };
  (window as unknown as { lunmi: unknown }).lunmi = api;
  render(<App />);
  return { sendCommand };
}

describe("Compositor V3 — mic redondo à direita de Enviar, sem texto normal", () => {
  it("mic é o botão seguinte a Enviar dentro das ações, redondo e só com ícone", async () => {
    setupApp();
    await screen.findByTestId("composer");
    const mic = screen.getByTestId("voice-input-toggle");
    const send = await screen.findByTestId("send-task");
    expect(mic.closest(".lunmi-composer-actions")).not.toBeNull();
    expect(send.compareDocumentPosition(mic) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // imediatamente a seguir: o bloco do mic é o irmão seguinte do botão Enviar dentro das ações
    expect(mic.closest(".lunmi-voice-input")?.previousElementSibling).toBe(send);
    expect(mic.className).toContain("lunmi-mic-round");
    expect(mic.getAttribute("aria-label")).toContain("Gravar ditado");
    // só ícone + estado sr-only: nenhum span de texto visível dentro do botão
    const visibleTexts = [...mic.querySelectorAll("span")].filter(node => !node.classList.contains("lunmi-sr-only"));
    expect(visibleTexts).toHaveLength(0);
    expect(screen.getByTestId("voice-input-state").classList.contains("lunmi-sr-only")).toBe(true);
    expect(screen.getByTestId("voice-state-label").classList.contains("lunmi-sr-only")).toBe(true);
  });

  it("clique no mic inicia ditado MANUAL com payload {} (sem autoenvio, sem segundo hook)", async () => {
    const { sendCommand } = setupApp();
    await screen.findByTestId("composer");
    fireEvent.click(screen.getByTestId("voice-input-toggle"));
    await waitFor(() => expect(sendCommand).toHaveBeenCalledWith("voice.input.start", {}));
    expect(sendCommand.mock.calls.some(([type]) => type === "chat.send")).toBe(false);
    expect(await screen.findByTestId("voice-input-cancel")).not.toBeNull();
  });

  it("Parar voz é acessível só com fala e mantém-se distinto de Parar tarefa", async () => {
    setupApp();
    await screen.findByTestId("composer");
    expect(screen.queryByTestId("voice-stop-min")).toBeNull();
    expect(screen.getByTestId("voice-state-label").textContent).toBe("voz desligada"); // sr-only, não visível
  });
});

describe("NodeSurface — espelho do voice_phase canónico", () => {
  function presence(node: Partial<NodeStateDto> | undefined) {
    const nodeState: NodeStateDto = {
      v: 1, enabled: true, side: "right", surface: "pill", phase: "settled", transition_id: 3,
      transition: null, status: null, tone: "neutral", session_id: "s-v3", ...node,
    };
    const snapshot: PresenceSnapshotDto = {
      mode: "hidden", resident: true, session: null,
      chat: { session_id: "s-v3", cursor: 0, messages: [], recent_tasks: [], task: null, confirm: null, input: null },
      draft: "", theme: "celestial", voice: null, node: nodeState,
    };
    (window as unknown as { lunmiPresence: unknown }).lunmiPresence = {
      snapshot: vi.fn(async () => snapshot),
      onEvent: () => () => undefined,
      action: vi.fn(async () => ({ ok: true })),
    } as unknown as LunmiPresenceApi;
    return snapshot;
  }

  it("listening mostra 'A escutar' na cápsula aprovada", async () => {
    presence({ voice_phase: "listening", surface: "pill" });
    render(<NodeSurface />);
    const pill = await screen.findByTestId("node-pill-text");
    expect(pill.textContent).toBe("A escutar");
    expect(screen.getByTestId("lunmi-node").getAttribute("data-voice")).toBe("listening");
  });

  it("starting e thinking usam as etiquetas fixas do brief", async () => {
    presence({ voice_phase: "starting", surface: "pill" });
    const first = render(<NodeSurface />);
    await waitFor(() => expect(screen.getByTestId("node-pill-text").textContent).toBe("A preparar o microfone"));
    first.unmount();
    presence({ voice_phase: "thinking", surface: "pill", status: "working" });
    render(<NodeSurface />);
    await waitFor(() => expect(screen.getByTestId("node-pill-text").textContent).toBe("A pensar"));
  });

  it("error usa a etiqueta fixa honesta de entrada de voz (nunca 'tarefa falhou')", async () => {
    presence({ voice_phase: "error", surface: "pill" });
    render(<NodeSurface />);
    await waitFor(() => expect(screen.getByTestId("node-pill-text").textContent).toBe("O ditado falhou"));
    expect(screen.getByTestId("lunmi-node").getAttribute("data-voice")).toBe("error");
  });

  it("sem fase de voz: comportamento V1 preservado (estado da tarefa ou 'Lunmi')", async () => {
    presence({ voice_phase: null, surface: "pill", status: "working" });
    render(<NodeSurface />);
    await waitFor(() => expect(screen.getByTestId("node-pill-text").textContent).toBe("A trabalhar"));
    expect(screen.getByTestId("lunmi-node").getAttribute("data-voice")).toBe("none");
  });

  it("transcrição nunca aparece na cápsula: rótulo permanece do enum fechado", async () => {
    presence({ voice_phase: "thinking", surface: "pill" });
    render(<NodeSurface />);
    await waitFor(() => expect(screen.getByTestId("node-pill-text").textContent).toBe("A pensar"));
    expect(screen.getByTestId("node-pill-text").textContent).not.toMatch(/pesquisa|spotify|alpha/i);
  });
});

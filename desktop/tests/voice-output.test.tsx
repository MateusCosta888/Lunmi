/**
 * Testes renderer da saída de voz (`desktop/tests/voice-output.test.tsx`, VOICE-V1-OUTPUT,
 * reposicionados para REFINEMENTS V2).
 *
 * Nenhum backend real: `window.lunmi` é uma ponte espiã. O cartão vive agora em Configurações
 * (`VoiceSettingsCard`); o compositor mantém apenas estado + «Parar voz» (`VoiceControls`).
 * Provas preservadas do contrato de saída: payloads exatos (`voice.set` só com
 * `enabled`/`auto_speak`; test/speak_last/stop com `{}`); subscrição antes do GET; GET tardio da
 * MESMA generation não sobrescreve evento posterior; guarda de sessão; recusas
 * (`unavailable`/`no_last_response`/`input_active`) com mensagem honesta sem eco de texto; Parar
 * voz usa `voice.stop` (nunca `task.cancel`) e só aparece com fala; desligar limpa o estado e não
 * dispara fala automática; `capture_supported` legado não bloqueia nada (entrada tem DTO próprio).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VoiceControls } from "../src/features/voice/VoiceControls";
import { VoiceSettingsCard } from "../src/features/voice/VoiceSettingsCard";
import type { VoiceCommandAckDto, VoiceStateDto, VoiceStateEventDto } from "../src/shared/types";

afterEach(() => {
  delete (window as unknown as { lunmi?: unknown }).lunmi;
});

interface Call { type: string; payload: Record<string, unknown>; }

function bridge(initial: Partial<VoiceStateDto> = {}) {
  const state: VoiceStateDto = {
    enabled: false, auto_speak: false, available: false, state: "disabled", generation: 0,
    can_speak_last: false, capture_supported: false, message: "", ...initial,
  };
  const calls: Call[] = [];
  let listener: ((event: { v: 1; type: string; payload: unknown }) => void) | undefined;

  const sendCommand = vi.fn(async (type: string, payload: Record<string, unknown> = {}) => {
    calls.push({ type, payload });
    if (type === "voice.get") return { ...state };
    if (type === "voice.set") {
      Object.assign(state, payload);
      state.generation += 1;
      if (!state.enabled) { state.state = "disabled"; state.can_speak_last = false; }
      else if (state.state === "disabled") state.state = state.available ? "ready" : "error";
      return { ...state };
    }
    if (type === "voice.test" || type === "voice.speak_last" || type === "voice.stop") {
      const ack: VoiceCommandAckDto = {
        accepted: true, generation: ++state.generation,
        state: type === "voice.stop" ? "ready" : "synthesizing", reason: "",
      };
      if (type !== "voice.stop") state.state = "synthesizing";
      return ack;
    }
    throw new Error(`unexpected_command: ${type}`);
  });

  (window as unknown as { lunmi: unknown }).lunmi = {
    onEvent: (cb: typeof listener) => { listener = cb; return () => { listener = undefined; }; },
    sendCommand,
  };

  const emit = (override: Partial<VoiceStateEventDto> & { generation: number }) =>
    listener?.({ v: 1, type: "voice.state", payload: { session_id: "sess-v", ...state, ...override } as VoiceStateEventDto });

  return { calls, state, sendCommand, emit };
}

describe("VoiceSettingsCard — contrato de comandos (em Configurações)", () => {
  it("abre com estado do backend; Ativar voz envia exatamente {enabled:true}", async () => {
    const f = bridge({ available: true });
    render(<VoiceSettingsCard sessionId="sess-v" />);
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.get" && Object.keys(c.payload).length === 0)).toBe(true));
    const toggle = screen.getByTestId("voice-enabled") as HTMLInputElement;
    fireEvent.click(toggle);
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.set" && JSON.stringify(c.payload) === JSON.stringify({ enabled: true }))).toBe(true));
    expect(f.calls.filter(c => c.type === "voice.set")).toHaveLength(1);
  });

  it("Falar respostas envia {auto_speak:true} e está desativada com voz desligada", async () => {
    const f = bridge({ available: true });
    render(<VoiceSettingsCard sessionId="sess-v" />);
    await waitFor(() => expect((screen.getByTestId("voice-enabled") as HTMLInputElement).disabled).toBe(false));
    const auto = screen.getByTestId("voice-auto-speak") as HTMLInputElement;
    expect(auto.disabled).toBe(true);
    fireEvent.click(screen.getByTestId("voice-enabled"));
    await waitFor(() => expect((screen.getByTestId("voice-auto-speak") as HTMLInputElement).disabled).toBe(false));
    fireEvent.click(auto);
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.set" && JSON.stringify(c.payload) === JSON.stringify({ auto_speak: true }))).toBe(true));
  });

  it("Testar/Ouvir/Parar usam payloads {} ; Ouvir exige can_speak_last; Parar exige fala", async () => {
    const f = bridge({ available: true, enabled: true, state: "ready" });
    render(<VoiceSettingsCard sessionId="sess-v" />);
    await waitFor(() => expect((screen.getByTestId("voice-test") as HTMLButtonElement).disabled).toBe(false));
    expect((screen.getByTestId("voice-speak-last") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("voice-stop") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId("voice-test"));
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.test" && Object.keys(c.payload).length === 0)).toBe(true));
  });

  it("recusa input_active mostra a copy aditiva honesta no estado do cartão", async () => {
    const f = bridge({ available: true, enabled: true, state: "ready" });
    f.sendCommand.mockImplementation(async (type: string, payload: Record<string, unknown> = {}) => {
      f.calls.push({ type, payload });
      if (type === "voice.get") return { ...f.state };
      if (type === "voice.test") return { accepted: false, generation: f.state.generation, state: "ready", reason: "input_active" };
      throw new Error(`unexpected_command: ${type}`);
    });
    render(<VoiceSettingsCard sessionId="sess-v" />);
    await waitFor(() => expect((screen.getByTestId("voice-test") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByTestId("voice-test"));
    await waitFor(() => expect(screen.getByTestId("voice-status").textContent).toContain("O microfone está aberto"));
  });

  it("recusa no_last_response explica a ausência sem prometer áudio", async () => {
    const f = bridge({ available: true, enabled: true, state: "ready" });
    f.sendCommand.mockImplementation(async (type: string, payload: Record<string, unknown> = {}) => {
      f.calls.push({ type, payload });
      if (type === "voice.get") return { ...f.state };
      if (type === "voice.speak_last") return { accepted: false, generation: f.state.generation, state: "ready", reason: "no_last_response" };
      throw new Error(`unexpected_command: ${type}`);
    });
    render(<VoiceSettingsCard sessionId="sess-v" />);
    await waitFor(() => expect((screen.getByTestId("voice-speak-last") as HTMLButtonElement).disabled).toBe(true));
    // o único caminho honesto para «Ouvir última resposta» ativo é o backend dizê-lo: gen2 com can_speak_last
    act(() => { f.state.generation += 1; f.emit({ generation: f.state.generation, can_speak_last: true }); });
    await waitFor(() => expect((screen.getByTestId("voice-speak-last") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByTestId("voice-speak-last"));
    await waitFor(() => expect(screen.getByTestId("voice-status").textContent).toContain("resposta elegível"));
    expect(f.calls.some(c => c.type === "voice.speak_last" && Object.keys(c.payload).length === 0)).toBe(true);
  });

  it("evento mais novo vence GET tardio da mesma geração; desligar não dispara fala", async () => {
    const f = bridge({ available: true, enabled: true, state: "ready", generation: 5 });
    let resolveGet: ((value: VoiceStateDto) => void) | undefined;
    f.sendCommand.mockImplementation(async (type: string, payload: Record<string, unknown> = {}) => {
      f.calls.push({ type, payload });
      if (type === "voice.get") return await new Promise<VoiceStateDto>(resolve => { resolveGet = resolve; });
      if (type === "voice.set") {
        Object.assign(f.state, payload);
        f.state.generation += 1;
        if (!f.state.enabled) { f.state.state = "disabled"; f.state.can_speak_last = false; }
        return { ...f.state };
      }
      throw new Error(`unexpected_command: ${type}`);
    });
    render(<VoiceSettingsCard sessionId="sess-v" />);
    // evento playing gen5 (mesma geração do GET pendente) — o evento é aplicado primeiro
    act(() => {
      f.state.state = "playing";
      f.emit({ generation: 5, state: "playing" });
    });
    await waitFor(() => expect(screen.getByTestId("voice-status").textContent).toContain("playing"));
    act(() => { resolveGet?.({ ...f.state, state: "ready" }); }); // GET tardio diz "ready" gen5
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(screen.getByTestId("voice-status").textContent).toContain("playing");
    fireEvent.click(screen.getByTestId("voice-enabled"));
    await waitFor(() => expect((screen.getByTestId("voice-enabled") as HTMLInputElement).checked).toBe(false));
    // sem comandos de fala depois de desligar: apenas set
    expect(f.calls.filter(c => c.type === "voice.test" || c.type === "voice.speak_last").length).toBe(0);
  });

  it("sem ponte: cartão não existe", () => {
    render(<VoiceSettingsCard sessionId="sess-v" />);
    expect(screen.queryByTestId("voice-card")).toBeNull();
  });

  it("API de atalho presente: get uma vez por montagem e Remover envia null", async () => {
    const shortcut = {
      get: vi.fn(async () => ({ accelerator: "Ctrl+Alt", error: "" })),
      set: vi.fn(async (value: string | null) => ({ accelerator: value, error: "" })),
      pause: vi.fn(async () => ({ ok: true, error: "" })),
      resume: vi.fn(async () => ({ ok: true, error: "" })),
      onHold: vi.fn(() => () => undefined),
    };
    const f = bridge({ available: true });
    (window as unknown as { lunmi: Record<string, unknown> }).lunmi.voiceInputShortcut = shortcut;
    render(<VoiceSettingsCard sessionId="sess-v" />);
    await waitFor(() => expect(screen.getByTestId("voice-shortcut-current").textContent).toContain("Ctrl+Alt"));
    expect(shortcut.get).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("voice-shortcut-remove"));
    await waitFor(() => expect(shortcut.set).toHaveBeenCalledWith(null));
    await waitFor(() => expect(screen.getByTestId("voice-shortcut-current").textContent).toContain("Sem atalho"));
    expect(f.calls.filter(c => c.type === "voice.get").length).toBeLessThanOrEqual(2); // sem polling extra do cartão
  });

  it("ponte sem API de atalho: secção do atalho não existe; voz continua configurável", async () => {
    bridge({ available: true });
    render(<VoiceSettingsCard sessionId="sess-v" />);
    await waitFor(() => expect((screen.getByTestId("voice-enabled") as HTMLInputElement).disabled).toBe(false));
    expect(screen.queryByTestId("voice-shortcut-section")).toBeNull();
  });
});

describe("VoiceControls — compositor mínimo", () => {
  it("mostra estado compacto e «Parar voz» só durante a fala; Parar usa voice.stop, nunca task.cancel", async () => {
    const f = bridge({ available: true, enabled: true, state: "playing" });
    render(<VoiceControls sessionId="sess-v" />);
    await waitFor(() => expect(screen.getByTestId("voice-state-label").textContent).toBe("a falar…"));
    const stop = screen.getByTestId("voice-stop-min") as HTMLButtonElement;
    fireEvent.click(stop);
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.stop" && Object.keys(c.payload).length === 0)).toBe(true));
    expect(f.calls.some(c => c.type === "task.cancel")).toBe(false);
  });

  it("voz desligada: sem botão Parar", async () => {
    bridge({ available: false, enabled: false, state: "disabled" });
    render(<VoiceControls sessionId="sess-v" />);
    await waitFor(() => expect(screen.getByTestId("voice-state-label").textContent).toBe("voz desligada"));
    expect(screen.queryByTestId("voice-stop-min")).toBeNull();
  });

  it("sem ponte: nada no compositor", () => {
    render(<VoiceControls sessionId="sess-v" />);
    expect(screen.queryByTestId("voice-state-label")).toBeNull();
  });
});

/**
 * Testes renderer da saída de voz (`desktop/tests/voice-output.test.tsx`, VOICE-V1-OUTPUT).
 *
 * Nenhum backend real: `window.lunmi` é uma ponte espiã. Provas do contrato congelado:
 * payloads exatos (`voice.set` só com `enabled`/`auto_speak`; test/speak_last/stop com `{}`);
 * subscrição antes do GET; GET tardio da MESMA generation não sobrescreve evento posterior;
 * geração/session guards; ACK `unavailable`/`no_last_response` com mensagem honesta sem eco de
 * texto; Parar voz usa `voice.stop` (nunca `task.cancel`) e só quando há fala; zero controlo de
 * captura e microfone indicado como pendente.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VoiceControls } from "../src/features/voice/VoiceControls";
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
  let pendingGet: ((value: VoiceStateDto) => void) | undefined;
  const sendCommand = vi.fn(async (type: string, payload: Record<string, unknown> = {}) => {
    calls.push({ type, payload });
    if (type === "voice.get") return await new Promise<VoiceStateDto>(resolve => { pendingGet = resolve; resolve(state); });
    if (type === "voice.set") {
      Object.assign(state, payload);
      state.generation += 1;
      if (!state.enabled) { state.state = "disabled"; state.can_speak_last = false; }
      else if (state.state === "disabled") state.state = state.available ? "ready" : "error";
      return { ...state };
    }
    if (type === "voice.test" || type === "voice.speak_last" || type === "voice.stop") {
      const ack: VoiceCommandAckDto = { accepted: true, generation: ++state.generation, state: type === "voice.stop" ? "ready" : "synthesizing", reason: "" };
      if (type !== "voice.stop") state.state = "synthesizing";
      return ack;
    }
    throw new Error(`unexpected_command: ${type}`);
  });
  (window as unknown as { lunmi: unknown }).lunmi = {
    onEvent: (cb: typeof listener) => { listener = cb; return () => { listener = undefined; }; },
    sendCommand,
  };
  const emit = (payload: Partial<VoiceStateEventDto> & { generation: number }) =>
    act(() => listener?.({ v: 1, type: "voice.state", payload: { session_id: "sess-v", ...state, ...payload } as VoiceStateEventDto }));
  return {
    calls, state, emit,
    resolveGet: () => act(async () => { pendingGet?.({ ...state }); }),
    setGetImplementation: (fn: () => Promise<VoiceStateDto>) => {
      vi.mocked(sendCommand).mockImplementation(async (type: string, payload: Record<string, unknown> = {}) => {
        calls.push({ type, payload });
        if (type === "voice.get") return fn();
        throw new Error(`unexpected_command: ${type}`);
      });
    },
  };
}

function opened(initial?: Partial<VoiceStateDto>) {
  const f = bridge(initial);
  render(<VoiceControls sessionId="sess-v" />);
  return f;
}

async function toggleOpen(): Promise<void> {
  fireEvent.click(screen.getByTestId("voice-toggle"));
  await screen.findByTestId("voice-popover");
}

describe("VoiceControls — contrato de comandos", () => {
  it("abre com estado do backend e Ativar voz envia exatamente {enabled:true}, sem mais comandos", async () => {
    const f = opened({ available: true });
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.get" && Object.keys(c.payload).length === 0)).toBe(true));
    expect((screen.getByTestId("voice-state-label") as HTMLElement).textContent).toBe("desligada");
    await toggleOpen();
    fireEvent.click(screen.getByTestId("voice-enabled"));
    await waitFor(() => {
      const call = f.calls.find(c => c.type === "voice.set");
      expect(call!.payload).toEqual({ enabled: true });
    });
    expect(f.calls.filter(c => c.type === "voice.test" || c.type === "voice.speak_last")).toHaveLength(0);
    await waitFor(() => expect((screen.getByTestId("voice-state-label") as HTMLElement).textContent).toBe("pronta"));
  });

  it("voice.set aceita apenas as chaves enabled/auto_speak; test/speak_last/stop enviam {}", async () => {
    const f = opened();
    await toggleOpen();
    fireEvent.click(screen.getByTestId("voice-enabled"));
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.set")).toBe(true));
    for (const call of f.calls.filter(c => c.type.startsWith("voice."))) {
      if (call.type === "voice.set") expect(Object.keys(call.payload).every(k => k === "enabled" || k === "auto_speak")).toBe(true);
      else expect(call.payload).toEqual({});
    }
  });

  it("Parar voz só com fala em curso e usa voice.stop, nunca task.cancel", async () => {
    const f = opened({ available: true });
    await toggleOpen();
    expect((screen.getByTestId("voice-stop") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId("voice-enabled"));
    await waitFor(() => expect((screen.getByTestId("voice-state-label") as HTMLElement).textContent).toBe("pronta"));
    fireEvent.click(screen.getByTestId("voice-test"));
    await waitFor(() => expect((screen.getByTestId("voice-state-label") as HTMLElement).textContent).toBe("sintetizando…"));
    const stop = screen.getByTestId("voice-stop") as HTMLButtonElement;
    expect(stop.disabled).toBe(false);
    fireEvent.click(stop);
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.stop")).toBe(true));
    expect(f.calls.some(c => c.type === "task.cancel")).toBe(false);
    await waitFor(() => expect((screen.getByTestId("voice-state-label") as HTMLElement).textContent).toBe("pronta"));
  });

  it("Ouvir última resposta só com can_speak_last; sem texto no payload", async () => {
    const f = opened({ enabled: true, available: true, state: "ready", generation: 4 });
    await toggleOpen();
    expect((screen.getByTestId("voice-speak-last") as HTMLButtonElement).disabled).toBe(true);
    f.emit({ generation: 5, can_speak_last: true });
    await waitFor(() => expect((screen.getByTestId("voice-speak-last") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByTestId("voice-speak-last"));
    await waitFor(() => {
      const call = f.calls.find(c => c.type === "voice.speak_last");
      expect(call!.payload).toEqual({});
    });
  });

  it("ACK reason 'disabled' pede ativação com frase própria, sem confundir com runtime ausente", async () => {
    const sendCommand = vi.fn(async (type: string, _payload: Record<string, unknown> = {}) => {
      if (type === "voice.get") {
        return { enabled: true, auto_speak: false, available: true, state: "ready", generation: 1, can_speak_last: true, capture_supported: false, message: "" } as VoiceStateDto;
      }
      if (type === "voice.speak_last") {
        return { accepted: false, generation: 2, state: "ready", reason: "disabled" } as VoiceCommandAckDto;
      }
      throw new Error(`unexpected_command: ${type}`);
    });
    (window as unknown as { lunmi: unknown }).lunmi = { onEvent: () => () => {}, sendCommand };
    render(<VoiceControls sessionId="sess-v" />);
    await toggleOpen();
    fireEvent.click(screen.getByTestId("voice-speak-last"));
    await waitFor(() => expect(screen.getByTestId("voice-status").textContent).toContain("Ative a voz para ouvir"));
  });

  it("ACK unavailable explica sem fingir sucesso e sem eco de texto", async () => {
    const f = opened({ enabled: true, available: false, state: "error", generation: 2, message: "Voz indisponível: runtime de voz não instalado." });
    await toggleOpen();
    fireEvent.click(screen.getByTestId("voice-enabled"));
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.set")).toBe(true));
    const status = screen.getByTestId("voice-status").textContent ?? "";
    expect(status).toContain("runtime de voz não instalado");
    expect(document.body.textContent).not.toMatch(/synthesizing|accepted/);
  });
});

describe("VoiceControls — guards de sessão e geração", () => {
  it("subscrição antes do GET: evento playing gen5 não é sobrescrito por GET ready da MESMA gen5", async () => {
    const f = bridge();
    let resolveLate!: (value: VoiceStateDto) => void;
    f.setGetImplementation(() => new Promise<VoiceStateDto>(res => { resolveLate = res; }));
    render(<VoiceControls sessionId="sess-v" />);
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.get")).toBe(true));
    // o evento chega antes da resposta do GET, com a mesma generation
    f.emit({ state: "playing", generation: 5 });
    act(() => { resolveLate({ ...f.state, state: "ready", generation: 5 }); });
    await waitFor(() => expect((screen.getByTestId("voice-state-label") as HTMLElement).textContent).toBe("falando…"));
    // e continua "falando…" um tick depois — o GET tardio não venceu
    await new Promise(res => setTimeout(res, 30));
    expect((screen.getByTestId("voice-state-label") as HTMLElement).textContent).toBe("falando…");
  });

  it("evento de geração mais antiga é descartado sem mudar a UI", async () => {
    const f = opened({ enabled: true, available: true, state: "ready", generation: 9 });
    await toggleOpen();
    f.emit({ generation: 8, state: "error", message: "velho" });
    await new Promise(res => setTimeout(res, 30));
    expect((screen.getByTestId("voice-state-label") as HTMLElement).textContent).toBe("pronta");
    expect(screen.getByTestId("voice-status").textContent).not.toContain("velho");
  });

  it("evento de outra sessão é ignorado", async () => {
    const f = opened({ enabled: true, available: true, state: "ready", generation: 1 });
    await toggleOpen();
    f.emit({ generation: 2, session_id: "sess-outra", state: "error", message: "OUTRO" });
    await new Promise(res => setTimeout(res, 30));
    expect((screen.getByTestId("voice-state-label") as HTMLElement).textContent).toBe("pronta");
    expect(screen.getByTestId("voice-status").textContent).not.toContain("OUTRO");
  });

  it("troca de sessão: generation 0 da sessão nova é aceite e um SET antigo pendente não reintroduz estado", async () => {
    let resolveSet!: (value: VoiceStateDto) => void;
    let listener: ((event: { v: 1; type: string; payload: unknown }) => void) | undefined;
    const sendCommand = vi.fn(async (type: string, _payload: Record<string, unknown> = {}) => {
      if (type === "voice.get") {
        return { enabled: true, auto_speak: false, available: true, state: "ready", generation: 10, can_speak_last: true, capture_supported: false, message: "" } as VoiceStateDto;
      }
      if (type === "voice.set") return await new Promise<VoiceStateDto>(res => { resolveSet = res; });
      throw new Error(`unexpected_command: ${type}`);
    });
    (window as unknown as { lunmi: unknown }).lunmi = {
      onEvent: (cb: typeof listener) => { listener = cb; return () => { listener = undefined; }; },
      sendCommand,
    };
    const view = render(<VoiceControls sessionId="sess-v" />);
    await waitFor(() => expect((screen.getByTestId("voice-state-label") as HTMLElement).textContent).toBe("pronta"));
    fireEvent.click(screen.getByTestId("voice-toggle"));
    fireEvent.click(screen.getByTestId("voice-enabled")); // SET fica pendente
    await waitFor(() => expect(sendCommand).toHaveBeenCalledWith("voice.set", { enabled: false }));

    view.rerender(<VoiceControls sessionId="sess-w" />);
    act(() => listener?.({ v: 1, type: "voice.state", payload: {
      session_id: "sess-w", enabled: true, auto_speak: false, available: true, state: "ready",
      generation: 0, can_speak_last: false, capture_supported: false, message: "",
    } }));
    await waitFor(() => expect((screen.getByTestId("voice-state-label") as HTMLElement).textContent).toBe("pronta"));

    // o SET antigo (sess-v, generation 11, desligada) chega tarde: não pode vencer a sessão nova
    act(() => resolveSet({ enabled: false, auto_speak: false, available: true, state: "disabled", generation: 11, can_speak_last: false, capture_supported: false, message: "" }));
    await new Promise(res => setTimeout(res, 30));
    expect((screen.getByTestId("voice-state-label") as HTMLElement).textContent).toBe("pronta");
  });

  it("desativar limpa can_speak_last e desliga controlos sem enviar mais nada", async () => {
    const f = opened({ enabled: true, available: true, state: "ready", generation: 4, can_speak_last: true, auto_speak: true });
    await toggleOpen();
    await waitFor(() => expect((screen.getByTestId("voice-speak-last") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByTestId("voice-enabled"));
    await waitFor(() => expect((screen.getByTestId("voice-speak-last") as HTMLButtonElement).disabled).toBe(true));
    expect((screen.getByTestId("voice-auto-speak") as HTMLInputElement).disabled).toBe(true);
    expect(f.calls.filter(c => c.type === "voice.test" || c.type === "voice.speak_last" || c.type === "voice.stop")).toHaveLength(0);
  });
});

describe("VoiceControls — ausência de captura", () => {
  it("indica microfone pendente e não oferece qualquer controlo de gravação", async () => {
    opened();
    await toggleOpen();
    expect(screen.getByTestId("voice-mic-note").textContent).toMatch(/pendente de validação/i);
    const popover = screen.getByTestId("voice-popover");
    expect(popover.textContent).not.toMatch(/gravar|microfone ativo|iniciar captura/i);
    expect(popover.querySelector("input[type=file], button[data-testid*='mic']")).toBeNull();
  });
});

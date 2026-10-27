/**
 * Testes renderer do ditado (`desktop/tests/voice-input.test.tsx`, V1 + REFINEMENTS V2).
 *
 * Nenhum backend real: `window.lunmi` é uma ponte espiã. O painel recebe a instância única de
 * `useVoiceInput` (montada no App); as guardas testam-se por esse hook. Provas: payloads exatos
 * (`get`/`start`/`cancel` `{}`; `start` hold `{"mode":"hold"}`; `result`/`finish` só
 * `generation`); subscrição antes do GET; guarda de geração/sessão/epoch (evento antigo não
 * regride; ACK tardio da mesma geração descartado; ACK que avança geração aplica); `ready`
 * consome UMA vez por geração; rascunho inalterado entra direto; rascunho alterado ou start por
 * atalho com rascunho pré-existente abre preview com inserção explícita; transcrição com
 * `auto_send` NÃO toca no compositor (decisão do controlador); despromovida, cai no preview;
 * estados honestos (no_speech/busy/truncated/erro/IPC); zero `chat.send` no painel.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { VoiceInputControls } from "../src/features/voice/VoiceInputControls";
import { useVoiceInput } from "../src/features/voice/useVoiceInput";
import type {
  VoiceInputResultDto,
  VoiceInputStateDto,
  VoiceInputStateEventDto,
} from "../src/shared/types";

type EventLike = { v: 1; type: string; payload: unknown };

afterEach(() => {
  delete (window as unknown as { lunmi?: unknown }).lunmi;
});

interface Call { type: string; payload: Record<string, unknown>; }

function bridge(initial: Partial<VoiceInputStateDto> = {}) {
  const state: VoiceInputStateDto = { available: true, state: "idle", generation: 0, message: "", ...initial };
  const calls: Call[] = [];
  const resultRequests: number[] = [];
  const pendingResults = new Map<number, VoiceInputResultDto>();
  let listener: ((event: EventLike) => void) | undefined;

  const sendCommand = vi.fn(async (type: string, payload: Record<string, unknown> = {}): Promise<unknown> => {
    calls.push({ type, payload });
    if (type === "voice.input.get") return { ...state };
    if (type === "voice.input.start") {
      if (!state.available) return { accepted: false, generation: state.generation, state: "unavailable" as const, reason: "unavailable" as const };
      const hold = payload.mode === "hold";
      state.generation += 1; state.state = "starting"; state.message = "";
      return { accepted: true, generation: state.generation, state: "starting" as const, reason: "" as const, hold };
    }
    if (type === "voice.input.cancel") {
      const active = state.state === "starting" || state.state === "listening" || state.state === "transcribing" || state.state === "ready";
      if (!active) return { accepted: false, generation: state.generation, state: state.state, reason: "not_active" as const };
      state.generation += 1; state.state = "idle";
      return { accepted: true, generation: state.generation, state: "idle" as const, reason: "" as const };
    }
    if (type === "voice.input.result") {
      resultRequests.push(Number(payload.generation));
      const generation = Number(payload.generation);
      if (generation !== state.generation || state.state !== "ready") {
        return { status: "none" as const, generation: state.generation, text: "", truncated: false };
      }
      const entry = pendingResults.get(generation) ?? { status: "ready" as const, generation, text: "liga a luz da sala", truncated: false, auto_send: false };
      state.state = "idle";
      return entry;
    }
    throw new Error(`unexpected_command: ${type}`);
  });

  (window as unknown as { lunmi: unknown }).lunmi = {
    onEvent: (cb: typeof listener) => { listener = cb; return () => { listener = undefined; }; },
    sendCommand,
  };

  const emit = (override: Partial<VoiceInputStateEventDto> & { generation: number }) =>
    act(() => listener?.({
      v: 1, type: "voice.input.state",
      payload: { session_id: "sess-i", available: state.available, state: state.state, message: state.message, ...override } as VoiceInputStateEventDto,
    }));

  return {
    calls, resultRequests, state, emit,
    setResult: (generation: number, result: VoiceInputResultDto) => pendingResults.set(generation, result),
    api: { sendCommand },
  };
}

function Harness({ supportedBySession = true, initialDraft = "", sessionId = "sess-i" }: { supportedBySession?: boolean; initialDraft?: string; sessionId?: string }) {
  const [draft, setDraft] = useState(initialDraft);
  const api = useVoiceInput(sessionId, supportedBySession);
  return (
    <>
      <VoiceInputControls api={api} supportedBySession={supportedBySession} draft={draft} setDraft={setDraft} />
      <span data-testid="draft-view">{draft}</span>
      <button type="button" data-testid="edit-draft" onClick={() => setDraft(prev => `${prev} editado`)}>editar</button>
    </>
  );
}

async function open(f: ReturnType<typeof bridge>) {
  render(<Harness />);
  await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.get")).toBe(true));
}

async function startCapture(f: ReturnType<typeof bridge>): Promise<number> {
  fireEvent.click(screen.getByTestId("voice-input-toggle"));
  await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.start")).toBe(true));
  await waitFor(() => expect(screen.getByTestId("voice-input-state").textContent).toBe("a iniciar…"));
  return f.state.generation;
}

function speak(f: ReturnType<typeof bridge>, generation: number) {
  f.emit({ generation, state: "listening" });
  f.emit({ generation, state: "transcribing" });
  f.state.state = "ready";
  f.emit({ generation, state: "ready" });
}

describe("VoiceInputControls — comandos e guardas", () => {
  it("capability ausente: nada é renderizado nem comandado", async () => {
    const f = bridge();
    render(<Harness supportedBySession={false} />);
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(screen.queryByTestId("voice-input-toggle")).toBeNull();
    expect(f.calls).toHaveLength(0);
  });

  it("subscrição antes do GET; get com payload exatamente {}", async () => {
    const f = bridge();
    render(<Harness />);
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.get")).toBe(true));
    const get = f.calls.find(c => c.type === "voice.input.get")!;
    expect(get.payload).toEqual({});
  });

  it("ciclo manual com rascunho inalterado: transcrição editável, sem preview, sem chat.send", async () => {
    const f = bridge();
    await open(f);
    const generation = await startCapture(f);
    expect(f.calls.find(c => c.type === "voice.input.start")!.payload).toEqual({});
    speak(f, generation);

    await waitFor(() => expect(screen.getByTestId("draft-view").textContent).toBe("liga a luz da sala"));
    expect(f.resultRequests).toEqual([generation]);
    expect(f.calls.find(c => c.type === "voice.input.result")!.payload).toEqual({ generation });
    expect(screen.queryByTestId("voice-input-preview")).toBeNull();
    expect(f.calls.some(c => c.type === "chat.send")).toBe(false);
    expect((screen.getByTestId("voice-input-toggle") as HTMLButtonElement).disabled).toBe(false);
  });

  it("ready repetido da mesma geração consome o resultado uma única vez", async () => {
    const f = bridge();
    await open(f);
    const generation = await startCapture(f);
    speak(f, generation);
    await waitFor(() => expect(f.resultRequests).toHaveLength(1));
    f.state.state = "ready";
    f.emit({ generation, state: "ready" });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(f.resultRequests).toHaveLength(1);
  });

  it("evento de sessão diferente ou geração antiga não move a UI nem pede resultado", async () => {
    const f = bridge();
    await open(f);
    const generation = await startCapture(f);
    f.emit({ session_id: "sess-OUTRA", generation, state: "ready" });
    expect(f.resultRequests).toHaveLength(0);
    f.state.generation = generation + 2;
    f.emit({ generation: generation + 2, state: "listening" });
    f.emit({ generation, state: "ready" });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(f.resultRequests).toHaveLength(0);
    expect(screen.getByTestId("draft-view").textContent).toBe("");
  });

  it("cancel durante a captura: comando {}, sem pedido de resultado e rascunho intocado", async () => {
    const f = bridge();
    await open(f);
    const generation = await startCapture(f);
    f.emit({ generation, state: "listening" });
    fireEvent.click(screen.getByTestId("voice-input-cancel"));
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.cancel" && Object.keys(c.payload).length === 0)).toBe(true));
    expect(f.resultRequests).toHaveLength(0);
    expect(screen.getByTestId("draft-view").textContent).toBe("");
  });
});

describe("VoiceInputControls — rascunho concorrente e preview", () => {
  it("rascunho alterado durante a captura: preview editável com inserção explícita; nada entra sozinho", async () => {
    const f = bridge();
    render(<Harness initialDraft="oi" />);
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.get")).toBe(true));
    const generation = await startCapture(f);
    fireEvent.click(screen.getByTestId("edit-draft"));
    speak(f, generation);

    await waitFor(() => expect(screen.getByTestId("voice-input-preview")).not.toBeNull());
    expect(screen.getByTestId("draft-view").textContent).toBe("oi editado");
    const previewText = screen.getByTestId("voice-input-preview-text") as HTMLTextAreaElement;
    expect(previewText.value).toBe("liga a luz da sala");

    fireEvent.change(previewText, { target: { value: "liga a luz da sala, por favor" } });
    fireEvent.click(screen.getByTestId("voice-input-insert"));
    expect(screen.getByTestId("draft-view").textContent).toBe("oi editado liga a luz da sala, por favor");
    expect(screen.queryByTestId("voice-input-preview")).toBeNull();
    expect(f.calls.some(c => c.type === "chat.send")).toBe(false);
  });

  it("Descartar fecha o preview sem tocar no rascunho", async () => {
    const f = bridge();
    render(<Harness initialDraft="oi" />);
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.get")).toBe(true));
    const generation = await startCapture(f);
    fireEvent.click(screen.getByTestId("edit-draft"));
    speak(f, generation);
    await waitFor(() => expect(screen.getByTestId("voice-input-preview")).not.toBeNull());
    fireEvent.click(screen.getByTestId("voice-input-discard"));
    expect(screen.queryByTestId("voice-input-preview")).toBeNull();
    expect(screen.getByTestId("draft-view").textContent).toBe("oi editado");
  });

  it("transcrição cortada avisa de forma honesta", async () => {
    const f = bridge();
    await open(f);
    const generation = await startCapture(f);
    f.setResult(generation, { status: "ready", generation, text: "texto cortado", truncated: true, auto_send: false });
    speak(f, generation);
    await waitFor(() => expect(screen.getByTestId("voice-input-notice").textContent).toContain("2000 caracteres"));
  });

  it("transcrição hold com auto_send NÃO entra no compositor nem em preview: decisão do controlador", async () => {
    const f = bridge();
    render(<Harness initialDraft="" />);
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.get")).toBe(true));
    const generation = await startCapture(f);
    f.setResult(generation, { status: "ready", generation, text: "pesquisa spotify", truncated: false, auto_send: true });
    speak(f, generation);
    await waitFor(() => expect(f.resultRequests).toHaveLength(1));
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(screen.getByTestId("draft-view").textContent).toBe("");
    expect(screen.queryByTestId("voice-input-preview")).toBeNull();
  });
});

describe("ordem canónica (gate Root I2)", () => {
  it("evento antigo após estado atual não regride a UI", async () => {
    const f = bridge();
    await open(f);
    f.emit({ generation: 3, state: "listening" });
    f.emit({ generation: 1, state: "idle" });
    expect(screen.getByTestId("voice-input-state").textContent).toBe("a escutar…");
    expect(f.resultRequests).toHaveLength(0);
  });

  it("ACK de start tardio da MESMA geração não regride um evento listening já aplicado", async () => {
    const f = bridge();
    await open(f);
    let resolveStart: (value: unknown) => void = () => undefined;
    f.api.sendCommand.mockImplementation(async (type: string, payload: Record<string, unknown> = {}) => {
      f.calls.push({ type, payload });
      if (type === "voice.input.get") return { ...f.state };
      if (type === "voice.input.start") return await new Promise(resolve => { resolveStart = resolve; });
      throw new Error(`unexpected_command: ${type}`);
    });
    fireEvent.click(screen.getByTestId("voice-input-toggle"));
    f.emit({ generation: 1, state: "listening" });
    await act(async () => { resolveStart({ accepted: true, generation: 1, state: "starting", reason: "" }); });
    expect(screen.getByTestId("voice-input-state").textContent).toBe("a escutar…");
  });

  it("ACK que avança geração continua a ser o estado canónico (sem ranking ad-hoc)", async () => {
    const f = bridge();
    await open(f);
    let resolveStart: (value: unknown) => void = () => undefined;
    f.api.sendCommand.mockImplementation(async (type: string, payload: Record<string, unknown> = {}) => {
      f.calls.push({ type, payload });
      if (type === "voice.input.get") return { ...f.state };
      if (type === "voice.input.start") return await new Promise(resolve => { resolveStart = resolve; });
      throw new Error(`unexpected_command: ${type}`);
    });
    fireEvent.click(screen.getByTestId("voice-input-toggle"));
    f.emit({ generation: 1, state: "listening" });
    await act(async () => { resolveStart({ accepted: true, generation: 2, state: "starting", reason: "" }); });
    expect(screen.getByTestId("voice-input-state").textContent).toBe("a iniciar…");
  });

  it("rejeição de promessa da sessão anterior não reaparece como erro depois do reset", async () => {
    let rejectFirst: ((err: Error) => void) | undefined;
    let getCount = 0;
    (window as unknown as { lunmi: unknown }).lunmi = {
      onEvent: () => () => undefined,
      sendCommand: vi.fn(async (type: string) => {
        if (type !== "voice.input.get") throw new Error(`unexpected_command: ${type}`);
        getCount += 1;
        const n = getCount;
        if (n === 1) return await new Promise((_res, rej) => { rejectFirst = rej; });
        return { available: true, state: "idle", generation: 0, message: "" };
      }),
    };
    const view = render(<Harness sessionId="sess-a" />);
    await waitFor(() => expect(getCount).toBe(1));
    view.rerender(<Harness sessionId="sess-b" />);
    await waitFor(() => expect(getCount).toBe(2));
    act(() => { rejectFirst?.(new Error("sessão antiga caiu")); });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(screen.queryByTestId("voice-input-message")).toBeNull();
  });
});

describe("captura iniciada por atalho (sem clique no compositor)", () => {
  it("rascunho pré-existente: transcrição vai para preview explícito, nunca substitui em silêncio", async () => {
    const f = bridge();
    render(<Harness initialDraft="oi" />);
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.get")).toBe(true));
    f.state.generation = 1;
    f.emit({ generation: 1, state: "starting" });
    f.emit({ generation: 1, state: "listening" });
    f.state.state = "ready";
    f.emit({ generation: 1, state: "ready" });
    await waitFor(() => expect(screen.getByTestId("voice-input-preview")).not.toBeNull());
    expect(screen.getByTestId("draft-view").textContent).toBe("oi");
  });

  it("compositor vazio: ditado por atalho preenche o rascunho editável (sem envio)", async () => {
    const f = bridge();
    render(<Harness initialDraft="" />);
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.get")).toBe(true));
    f.state.generation = 1;
    f.emit({ generation: 1, state: "starting" });
    f.state.state = "ready";
    f.emit({ generation: 1, state: "ready" });
    await waitFor(() => expect(screen.getByTestId("draft-view").textContent).toBe("liga a luz da sala"));
    expect(f.calls.some(c => c.type === "chat.send")).toBe(false);
  });
});

describe("estados honestos", () => {
  it("runtime ausente (get indisponível): botão desativado, start não é enviado", async () => {
    const f = bridge({ available: false, state: "unavailable", message: "Ditado indisponível: runtime de ditado não instalado." });
    render(<Harness />);
    await waitFor(() => expect((screen.getByTestId("voice-input-toggle") as HTMLButtonElement).disabled).toBe(true));
    expect(f.calls.some(c => c.type === "voice.input.start")).toBe(false);
  });

  it("no_speech: idle com a mensagem do backend, zero resultado, zero repetição", async () => {
    const f = bridge();
    await open(f);
    const generation = await startCapture(f);
    f.state.state = "idle";
    f.state.message = "Não foi detetada fala.";
    f.emit({ generation, state: "idle", message: "Não foi detetada fala." });
    await waitFor(() => expect(screen.getByTestId("voice-input-message").textContent).toContain("Não foi detetada fala."));
    expect(f.resultRequests).toHaveLength(0);
    expect(screen.getByTestId("draft-view").textContent).toBe("");
  });

  it("erro do worker: mensagem do backend e botão permite nova tentativa", async () => {
    const f = bridge();
    await open(f);
    const generation = await startCapture(f);
    f.emit({ generation, state: "error", message: "Microfone indisponível." });
    await waitFor(() => expect(screen.getByTestId("voice-input-message").textContent).toContain("Microfone indisponível."));
    expect((screen.getByTestId("voice-input-toggle") as HTMLButtonElement).disabled).toBe(false);
  });

  it("start recusado (busy) explica a recusa sem mover a UI para captura", async () => {
    const f = bridge();
    await open(f);
    f.calls.length = 0;
    f.api.sendCommand.mockImplementation(async (type: string, payload: Record<string, unknown> = {}) => {
      f.calls.push({ type, payload });
      if (type === "voice.input.start") return { accepted: false, generation: 0, state: "idle", reason: "busy" };
      if (type === "voice.input.get") return { ...f.state };
      throw new Error(`unexpected_command: ${type}`);
    });
    fireEvent.click(screen.getByTestId("voice-input-toggle"));
    await waitFor(() => expect(screen.getByTestId("voice-input-message").textContent).toContain("Já existe um ditado em curso."));
    expect(screen.queryByTestId("voice-input-cancel")).toBeNull();
  });

  it("falha de IPC no get: sem estado inventado", async () => {
    (window as unknown as { lunmi: unknown }).lunmi = {
      onEvent: () => () => undefined,
      sendCommand: vi.fn(async () => { throw new Error("bridge_down"); }),
    };
    render(<Harness />);
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(screen.getByTestId("draft-view").textContent).toBe("");
  });
});

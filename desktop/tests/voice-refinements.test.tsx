/**
 * Testes dos refinamentos de voz V2 (`desktop/tests/voice-refinements.test.tsx`).
 *
 * Ponte espiã; nenhum Electron/microfone/teclado/tempo real. Contrato
 * `.agents/VOICE-REFINEMENTS-V2-CONTRACT.md`. Controlador de hold (montado no App, testado aqui
 * num palco mínimo): `pressed` → `voice.input.start {"mode":"hold"}` UMA vez; `released` do mesmo
 * `press_id` → `voice.input.finish {generation}`; `cancelled` invalida; só `auto_send:true` com
 * token `released` válido envia, UMA vez, pela `chat.send` recebida, no modo reportado, sem tocar
 * no rascunho nem anexos; sem token, sem release, press_id antigo, sessão antiga, recusa de envio
 * ou repetição = zero segundas tentativas; recusa despromove para revisão manual. KeyRecorder:
 * `pause` antes de capturar; ordem física apresentada; canónica aplicada ao largar; Escape/blur
 * abandonam e retomam só com teclas livres; AltGr/duplicado/desconhecida/incompleto rejeitam;
 * repeat ignorado; `hold_unavailable` honesto. Warmup: `voice.warmup` uma vez; `voice.readiness`
 * com `ready=true` abre; comando desconhecido ou timeout local abrem degradado com mensagem real.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { VoiceInputControls } from "../src/features/voice/VoiceInputControls";
import { KeyRecorder, useVoiceShortcutApi } from "../src/features/voice/KeyRecorder";
import { useVoiceInput } from "../src/features/voice/useVoiceInput";
import { useHoldController } from "../src/features/voice/useHoldController";
import { useVoiceWarmup, WARMUP_LOCAL_GUARD_MS } from "../src/features/voice/useVoiceWarmup";
import type { VoiceInputResultDto, VoiceInputStateDto } from "../src/shared/types";

type EventLike = { v: 1; type: string; payload: unknown };
type HoldPhase = { session_id: string; press_id: number; phase: "pressed" | "released" | "cancelled"; reason: string };

interface ShortcutOverrides {
  get?: () => Promise<{ accelerator: string | null; error: string }>;
  set?: (value: string | null) => Promise<{ accelerator: string | null; error: string }>;
  pause?: () => Promise<{ ok: boolean; error: string }>;
}

interface Harness {
  calls: Array<{ type: string; payload: Record<string, unknown> }>;
  resultRequests: number[];
  inputState: VoiceInputStateDto;
  emitInput: (override: Partial<{ generation: number; state: string; message: string; session_id: string; available: boolean }> & { generation: number }) => void;
  emitHold: (partial: Partial<HoldPhase> & { press_id: number }) => void;
  emit: (event: EventLike) => void;
  shortcut: { get: ReturnType<typeof vi.fn>; set: ReturnType<typeof vi.fn>; pause: ReturnType<typeof vi.fn>; resume: ReturnType<typeof vi.fn> } | null;
  pendingResults: Map<number, VoiceInputResultDto>;
  mockSend: (impl: (type: string, payload?: Record<string, unknown>) => Promise<unknown>) => void;
}

function installBridge(options: { input?: Partial<VoiceInputStateDto>; results?: Map<number, VoiceInputResultDto>; shortcut?: ShortcutOverrides; withShortcut?: boolean; startImpl?: () => Promise<unknown> } = {}): Harness {
  const inputState: VoiceInputStateDto = { available: true, state: "idle", generation: 0, message: "", ...options.input };
  const calls: Harness["calls"] = [];
  const resultRequests: number[] = [];
  const pendingResults = options.results ?? new Map<number, VoiceInputResultDto>();
  let listener: ((event: EventLike) => void) | undefined;
  let holdListener: ((phase: HoldPhase) => void) | undefined;

  const bridge: Record<string, unknown> = {
    onEvent: (cb: typeof listener) => { listener = cb; return () => { listener = undefined; }; },
    sendCommand: vi.fn(async (type: string, payload: Record<string, unknown> = {}) => {
      calls.push({ type, payload });
      if (type === "voice.input.get") return { ...inputState };
      if (type === "voice.input.start") {
        if (options.startImpl) return options.startImpl();
        inputState.generation += 1; inputState.state = "starting";
        return { accepted: true, generation: inputState.generation, state: "starting", reason: "" };
      }
      if (type === "voice.input.finish") {
        if (Number(payload.generation) !== inputState.generation) return { accepted: false, generation: inputState.generation, state: inputState.state, reason: "not_active" };
        inputState.state = "transcribing";
        return { accepted: true, generation: inputState.generation, state: "transcribing", reason: "" };
      }
      if (type === "voice.input.cancel") {
        inputState.generation += 1; inputState.state = "idle";
        return { accepted: true, generation: inputState.generation, state: "idle", reason: "" };
      }
      if (type === "voice.input.result") {
        resultRequests.push(Number(payload.generation));
        return pendingResults.get(Number(payload.generation))
          ?? { status: "none", generation: inputState.generation, text: "", truncated: false };
      }
      throw new Error(`unexpected_command: ${type}`);
    }),
  };
  const warmupBridge = bridge.sendCommand as ReturnType<typeof vi.fn>;
  const originalImpl = warmupBridge.getMockImplementation() as (type: string, payload?: Record<string, unknown>) => Promise<unknown>;
  warmupBridge.mockImplementation(async (type: string, payload: Record<string, unknown> = {}) => {
    if (type === "voice.warmup") { calls.push({ type, payload }); return { state: "preparing", ready: false, tts: "preparing", stt: "preparing", message: "" }; }
    if (type === "voice.warmup.get") { calls.push({ type, payload }); return { state: "preparing", ready: false, tts: "preparing", stt: "preparing", message: "" }; }
    return originalImpl(type, payload);
  });

  let shortcut: Harness["shortcut"] = null;
  if (options.withShortcut || options.shortcut) {
    const overrides = options.shortcut ?? {};
    shortcut = {
      get: vi.fn(overrides.get ?? (async () => ({ accelerator: "Ctrl+Alt", error: "" }))),
      set: vi.fn(overrides.set ?? (async (value: string | null) => ({ accelerator: value, error: "" }))),
      pause: vi.fn(overrides.pause ?? (async () => ({ ok: true, error: "" }))),
      resume: vi.fn(async () => ({ ok: true, error: "" })),
    };
    bridge.voiceInputShortcut = {
      ...shortcut,
      onHold: vi.fn((cb: (phase: HoldPhase) => void) => { holdListener = cb; return () => { holdListener = undefined; }; }),
    };
  }

  (window as unknown as { lunmi: unknown }).lunmi = bridge;
  return {
    calls, resultRequests, inputState, pendingResults, shortcut,
    mockSend: impl => (bridge.sendCommand as ReturnType<typeof vi.fn>).mockImplementation(async (type: string, payload: Record<string, unknown> = {}) => impl(type, payload)),
    emitInput: override => act(() => listener?.({
      v: 1, type: "voice.input.state",
      payload: {
        session_id: override.session_id ?? "sess-i", available: override.available ?? inputState.available,
        state: override.state ?? inputState.state, message: override.message ?? inputState.message,
        generation: override.generation,
      },
    })),
    emitHold: partial => act(() => holdListener?.({ session_id: "sess-i", reason: "", phase: "pressed", ...partial } as HoldPhase)),
    emit: event => act(() => listener?.(event)),
  };
}

afterEach(() => {
  delete (window as unknown as { lunmi?: unknown }).lunmi;
});

// ---------------------------------------------------------------------------
// Controlador de hold
// ---------------------------------------------------------------------------

function HoldStage({ send, initialDraft = "" }: { send: (mode: string, text: string) => Promise<{ accepted: boolean }>; initialDraft?: string }) {
  const [draft, setDraft] = useState(initialDraft);
  const api = useVoiceInput("sess-i", true);
  const hold = useHoldController({
    api, sessionId: "sess-i",
    send: (mode, text) => send(mode, text),
    getMode: () => "converse",
  });
  return <>
    <VoiceInputControls api={api} supportedBySession draft={draft} setDraft={setDraft} holdNotice={hold.notice} />
    <span data-testid="draft-view">{draft}</span>
    <span data-testid="hold-notice">{hold.notice}</span>
  </>;
}

describe("controlador de hold", () => {
  it("pressed envia start {mode:hold} UMA vez; released do mesmo press_id envia finish {generation}", async () => {
    const f = installBridge({ withShortcut: true });
    const send = vi.fn(async () => ({ accepted: true }));
    render(<HoldStage send={send} />);
    await screen.findByTestId("voice-input-toggle");

    f.emitHold({ press_id: 5, phase: "pressed" });
    await waitFor(() => expect(f.calls.filter(c => c.type === "voice.input.start")).toHaveLength(1));
    expect(f.calls.find(c => c.type === "voice.input.start")!.payload).toEqual({ mode: "hold" });

    f.emitHold({ press_id: 5, phase: "released" });
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.finish" && JSON.stringify(c.payload) === JSON.stringify({ generation: 1 }))).toBe(true));
    expect(send).not.toHaveBeenCalled();
  });

  it("auto_send com release válido envia UMA vez pelo caminho existente e não toca no rascunho", async () => {
    const results = new Map<number, VoiceInputResultDto>([
      [1, { status: "ready", generation: 1, text: "pesquisa spotify", truncated: false, auto_send: true }],
    ]);
    const f = installBridge({ withShortcut: true, results });
    const send = vi.fn(async () => ({ accepted: true }));
    render(<HoldStage send={send} />);
    await screen.findByTestId("voice-input-toggle");

    f.emitHold({ press_id: 5, phase: "pressed" });
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.start")).toBe(true));
    f.emitInput({ generation: 1, state: "listening" });
    f.emitHold({ press_id: 5, phase: "released" });
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.finish")).toBe(true));
    f.inputState.state = "ready";
    f.emitInput({ generation: 1, state: "ready" });

    await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    expect(send).toHaveBeenCalledWith("converse", "pesquisa spotify");
    expect(screen.getByTestId("draft-view").textContent).toBe("");
    f.emitInput({ generation: 1, state: "idle" });
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(send).toHaveBeenCalledTimes(1); // sem retry/fila
    expect(screen.queryByTestId("voice-input-preview")).toBeNull();
  });

  it("auto_send SEM release não envia: texto despromovido fica disponível para ação manual", async () => {
    const results = new Map<number, VoiceInputResultDto>([
      [1, { status: "ready", generation: 1, text: "pesquisa spotify", truncated: false, auto_send: true }],
    ]);
    const f = installBridge({ withShortcut: true, results });
    const send = vi.fn(async () => ({ accepted: true }));
    render(<HoldStage send={send} initialDraft="" />);
    await screen.findByTestId("voice-input-toggle");

    f.emitHold({ press_id: 5, phase: "pressed" });
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.start")).toBe(true));
    // sem released: o resultado nunca é enviado automaticamente
    f.inputState.state = "ready";
    f.emitInput({ generation: 1, state: "ready" });
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(send).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByTestId("draft-view").textContent).toBe("pesquisa spotify"));
  });

  it("cancelled antes de released: finish não é chamado e auto_send não envia", async () => {
    const results = new Map<number, VoiceInputResultDto>([
      [1, { status: "ready", generation: 1, text: "texto cancelado", truncated: false, auto_send: true }],
    ]);
    const f = installBridge({ withShortcut: true, results });
    const send = vi.fn(async () => ({ accepted: true }));
    render(<HoldStage send={send} />);
    await screen.findByTestId("voice-input-toggle");

    f.emitHold({ press_id: 5, phase: "pressed" });
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.start")).toBe(true));
    f.emitHold({ press_id: 5, phase: "cancelled", reason: "extra_key" });
    f.inputState.state = "ready";
    f.emitInput({ generation: 1, state: "ready" });
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(f.calls.some(c => c.type === "voice.input.finish" && Number(c.payload.generation) === 1)).toBe(false);
    expect(send).not.toHaveBeenCalled();
  });

  it("released com press_id desconhecido não finaliza turno alheio", async () => {
    const f = installBridge({ withShortcut: true });
    const send = vi.fn(async () => ({ accepted: true }));
    render(<HoldStage send={send} />);
    await screen.findByTestId("voice-input-toggle");

    f.emitHold({ press_id: 5, phase: "pressed" });
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.start")).toBe(true));
    f.emitHold({ press_id: 9, phase: "released" });
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(f.calls.some(c => c.type === "voice.input.finish")).toBe(false);
    expect(send).not.toHaveBeenCalled();
  });

  it("fase de outra sessão não inicia nada", async () => {
    const f = installBridge({ withShortcut: true });
    const send = vi.fn(async () => ({ accepted: true }));
    render(<HoldStage send={send} />);
    await screen.findByTestId("voice-input-toggle");

    f.emitHold({ press_id: 5, phase: "pressed", session_id: "sess-OUTRA" });
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(f.calls.some(c => c.type === "voice.input.start")).toBe(false);
  });

  it("pressed repetido não cria um segundo turno", async () => {
    const f = installBridge({ withShortcut: true });
    const send = vi.fn(async () => ({ accepted: true }));
    render(<HoldStage send={send} />);
    await screen.findByTestId("voice-input-toggle");

    f.emitHold({ press_id: 5, phase: "pressed" });
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.start")).toBe(true));
    f.emitHold({ press_id: 6, phase: "pressed" });
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(f.calls.filter(c => c.type === "voice.input.start")).toHaveLength(1);
  });

  it("recusa de envio: sem retry, aviso honesto e texto disponível para revisão manual", async () => {
    const results = new Map<number, VoiceInputResultDto>([
      [1, { status: "ready", generation: 1, text: "pesquisa spotify", truncated: false, auto_send: true }],
    ]);
    const f = installBridge({ withShortcut: true, results });
    const send = vi.fn(async () => ({ accepted: false }));
    render(<HoldStage send={send} />);
    await screen.findByTestId("voice-input-toggle");

    f.emitHold({ press_id: 5, phase: "pressed" });
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.start")).toBe(true));
    f.emitHold({ press_id: 5, phase: "released" });
    f.inputState.state = "ready";
    f.emitInput({ generation: 1, state: "ready" });
    await waitFor(() => expect(screen.getByTestId("hold-notice").textContent).toContain("não aceitou o envio automático"));
    expect(send).toHaveBeenCalledTimes(1);
    // despromovido: o texto fica disponível como rascunho editável (compositor vazio na altura do
    // gesto) — nunca reenviado nem enfileirado
    await waitFor(() => expect(screen.getByTestId("draft-view").textContent).toBe("pesquisa spotify"));
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(send).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// Corridas do gate Root (reprodução pública equivalente — C2-REPAIR)
// ---------------------------------------------------------------------------

describe("corridas hold (reparações C2)", () => {
  it("release ANTES do ACK é memorizado e finaliza nessa mesma geração, uma vez", async () => {
    let resolveStart: (value: unknown) => void = () => undefined;
    const f = installBridge({ withShortcut: true, startImpl: () => new Promise(resolve => { resolveStart = resolve; }) });
    const send = vi.fn(async () => ({ accepted: true }));
    render(<HoldStage send={send} />);
    await screen.findByTestId("voice-input-toggle");

    f.emitHold({ press_id: 5, phase: "pressed" });
    f.emitHold({ press_id: 5, phase: "released" }); // chega antes do ACK
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(f.calls.filter(c => c.type === "voice.input.finish")).toHaveLength(0);
    await act(async () => { resolveStart({ accepted: true, generation: 1, state: "starting", reason: "" }); });
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.finish" && Number(c.payload.generation) === 1)).toBe(true));
    expect(f.calls.filter(c => c.type === "voice.input.finish")).toHaveLength(1);
    expect(f.calls.filter(c => c.type === "voice.input.start")).toHaveLength(1);
  });

  it("cancel ANTES do ACK: ACK tardio não reanima o gesto nem permite finish", async () => {
    let resolveStart: (value: unknown) => void = () => undefined;
    const f = installBridge({ withShortcut: true, startImpl: () => new Promise(resolve => { resolveStart = resolve; }) });
    const send = vi.fn(async () => ({ accepted: true }));
    render(<HoldStage send={send} />);
    await screen.findByTestId("voice-input-toggle");

    f.emitHold({ press_id: 5, phase: "pressed" });
    f.emitHold({ press_id: 5, phase: "cancelled", reason: "extra_key" });
    await act(async () => { resolveStart({ accepted: true, generation: 1, state: "starting", reason: "" }); });
    f.emitHold({ press_id: 5, phase: "released" });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(f.calls.some(c => c.type === "voice.input.finish")).toBe(false);
    expect(send).not.toHaveBeenCalled();
  });

  it("ACK válido da mesma geração devolve a identidade ao controlador sem regredir a UI", async () => {
    const f = installBridge({ withShortcut: true });
    const send = vi.fn(async () => ({ accepted: true }));
    render(<HoldStage send={send} />);
    await screen.findByTestId("voice-input-toggle");

    // start pendente; o evento listening gen1 chega primeiro; o ACK gen1 só depois
    let resolveStart: (value: unknown) => void = () => undefined;
    const startPromise = new Promise(resolve => { resolveStart = resolve; });
    f.mockSend(async (type: string, payload: Record<string, unknown> = {}) => {
      f.calls.push({ type, payload });
      if (type === "voice.input.get") return { ...f.inputState };
      if (type === "voice.input.start") return startPromise;
      if (type === "voice.input.finish") return { accepted: true, generation: Number(payload.generation), state: "transcribing", reason: "" };
      throw new Error(`unexpected_command: ${type}`);
    });
    f.emitHold({ press_id: 7, phase: "pressed" });
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.start")).toBe(true));
    f.emitInput({ generation: 1, state: "listening" });
    await act(async () => { resolveStart({ accepted: true, generation: 1, state: "starting", reason: "" }); });
    // a UI mantém o evento mais novo...
    expect(screen.getByTestId("voice-input-state").textContent).toBe("a escutar…");
    // ...e o gesto fica utilizável: released finaliza a geração real
    f.emitHold({ press_id: 7, phase: "released" });
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.finish" && Number(c.payload.generation) === 1)).toBe(true));
  });

  it("envio antigo não apaga nem despromove transcrição de turno novo", async () => {
    const results = new Map<number, VoiceInputResultDto>([
      [1, { status: "ready", generation: 1, text: "primeira", truncated: false, auto_send: true }],
      [2, { status: "ready", generation: 2, text: "segunda", truncated: false, auto_send: false }],
    ]);
    const f = installBridge({ withShortcut: true, results });
    let resolveSend: (value: { accepted: boolean }) => void = () => undefined;
    const send = vi.fn(() => new Promise<{ accepted: boolean }>(resolve => { resolveSend = resolve; }));
    render(<HoldStage send={send} initialDraft="" />);
    await screen.findByTestId("voice-input-toggle");

    // turno A: pressed gen1 → released → ready(auto_send) → envio suspenso
    f.emitHold({ press_id: 5, phase: "pressed" });
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.input.start")).toBe(true));
    f.emitHold({ press_id: 5, phase: "released" });
    f.inputState.state = "ready";
    f.emitInput({ generation: 1, state: "ready" });
    await waitFor(() => expect(send).toHaveBeenCalledTimes(1));
    // turno B chega enquanto A aguarda: gesture novo (press_id 6) concluído, resultado manual gen2
    f.emitHold({ press_id: 6, phase: "pressed" });
    await waitFor(() => expect(f.calls.filter(c => c.type === "voice.input.start")).toHaveLength(2));
    f.emitHold({ press_id: 6, phase: "released" });
    f.inputState.state = "ready";
    f.emitInput({ generation: 2, state: "ready" });
    await waitFor(() => expect(f.resultRequests).toContain(2));
    // A é aceite depois: não pode limpar nem despromover B
    await act(async () => { resolveSend({ accepted: true }); });
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(send).toHaveBeenCalledTimes(1);
    // B (manual, vazio na base por gesto por atalho) entra como rascunho; se A o tivesse
    // dismissado/demovido, "segunda" nunca estaria no compositor
    await waitFor(() => expect(screen.getByTestId("draft-view").textContent).toBe("segunda"));
  });

  it("Cancelar o picker com tecla em baixo só retoma o detector após o keyup", async () => {
    const f = installBridge({ withShortcut: true });
    render(<RecorderStage />);
    await beginCapture();
    press("ControlLeft", "Control");
    fireEvent.click(screen.getByTestId("key-recorder-cancel"));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(f.shortcut!.resume).not.toHaveBeenCalled();
    lift("ControlLeft", "Control");
    await waitFor(() => expect(f.shortcut!.resume).toHaveBeenCalledTimes(1));
    expect(f.shortcut!.set).not.toHaveBeenCalled();
  });

  it("desmontar após Cancelar devolve o controlo ao resume nativo exatamente uma vez (gate Root picker-unmount)", async () => {
    const f = installBridge({ withShortcut: true });
    const view = render(<RecorderStage />);
    await beginCapture();
    press("ControlLeft", "Control");
    fireEvent.click(screen.getByTestId("key-recorder-cancel"));
    view.unmount(); // sair de Definições com a tecla ainda em baixo
    lift("ControlLeft", "Control"); // o helper nativo espera as teclas; o renderer não segura listeners
    await waitFor(() => expect(f.shortcut!.resume).toHaveBeenCalledTimes(1));
    await new Promise(resolve => setTimeout(resolve, 10));
    expect(f.shortcut!.resume).toHaveBeenCalledTimes(1); // sem segunda retoma nem listener órfão
    expect(f.shortcut!.set).not.toHaveBeenCalled();
  });

  it("pause que resolve depois do unmount não deixa o detector preso", async () => {
    const shortcut = {
      get: vi.fn(async () => ({ accelerator: null, error: "" })),
      set: vi.fn(async (v: string | null) => ({ accelerator: v, error: "" })),
      pause: vi.fn(() => { let release: (v: { ok: boolean; error: string }) => void = () => undefined;
        const promise = new Promise<{ ok: boolean; error: string }>(r => { release = r; });
        (shortcutOverridesPause as { resolve?: (v: { ok: boolean; error: string }) => void }).resolve = release;
        return promise; }),
      resume: vi.fn(async () => ({ ok: true, error: "" })),
    };
    const shortcutOverridesPause: { resolve?: (v: { ok: boolean; error: string }) => void } = {};
    (window as unknown as { lunmi: unknown }).lunmi = {
      onEvent: () => () => undefined,
      sendCommand: vi.fn(async () => { throw new Error("none"); }),
      voiceInputShortcut: { ...shortcut, onHold: vi.fn(() => () => undefined) },
    };
    const view = render(<RecorderStage />);
    fireEvent.click(screen.getByTestId("key-recorder-change")); // pause fica pendente
    view.unmount();
    shortcutOverridesPause.resolve?.({ ok: true, error: "" }); // resolve APOS o unmount
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(shortcut.resume).toHaveBeenCalledTimes(1); // o controlo volta; nada fica pausado
    expect(shortcut.set).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// KeyRecorder
// ---------------------------------------------------------------------------

function RecorderStage() {
  const api = useVoiceShortcutApi();
  const [applied, setApplied] = useState<string | null>(null);
  const [error, setError] = useState("");
  if (!api) return <span data-testid="recorder-missing">sem API</span>;
  return <>
    <KeyRecorder api={api} onApplied={setApplied} onError={setError} />
    <span data-testid="applied">{applied ?? ""}</span>
    <span data-testid="recorder-error">{error}</span>
  </>;
}

const press = (code: string, key: string, init: Record<string, unknown> = {}) =>
  fireEvent.keyDown(document, { code, key, ...init });
const lift = (code: string, key: string) => fireEvent.keyUp(document, { code, key });

async function beginCapture() {
  fireEvent.click(screen.getByTestId("key-recorder-change"));
  await waitFor(() => expect(screen.getByTestId("key-recorder").getAttribute("data-capturing")).toBe("true"), { timeout: 4000 });
}

describe("KeyRecorder — captura por teclas", () => {
  it("Alterar pausa o detector; ordem física apresentada; canónica aplicada ao largar", async () => {
    const f = installBridge({
      withShortcut: true,
      shortcut: { set: async () => ({ accelerator: "Ctrl+Alt", error: "" }) },
    });
    render(<RecorderStage />);
    await beginCapture();
    await waitFor(() => expect(f.shortcut!.pause).toHaveBeenCalledTimes(1));

    press("AltLeft", "Alt");
    press("ControlLeft", "Control");
    expect(screen.getByTestId("key-recorder-keys").textContent).toBe("Alt + Ctrl");
    lift("AltLeft", "Alt");
    lift("ControlLeft", "Control");
    // §4.2: a string guardada é a ordem visual premida; a canónica é interna ao main/helper
    await waitFor(() => expect(f.shortcut!.set).toHaveBeenCalledWith("Alt+Ctrl"), { timeout: 4000 });
    expect(screen.getByTestId("applied").textContent).toBe("Ctrl+Alt"); // o que o main devolver como efetivo
    await waitFor(() => expect(f.shortcut!.resume).toHaveBeenCalledTimes(1));
  });

  it("Ctrl+V (1 mod + tecla final) envia a forma canónica", async () => {
    const f = installBridge({ withShortcut: true });
    render(<RecorderStage />);
    await beginCapture();
    press("ControlLeft", "Control");
    press("KeyV", "v");
    expect(screen.getByTestId("key-recorder-keys").textContent).toBe("Ctrl + V");
    lift("KeyV", "v");
    lift("ControlLeft", "Control");
    await waitFor(() => expect(f.shortcut!.set).toHaveBeenCalledWith("Ctrl+V"), { timeout: 4000 });
  });

  it("Escape abandona; o detector só retoma quando as teclas ficam livres", async () => {
    const f = installBridge({ withShortcut: true });
    render(<RecorderStage />);
    await beginCapture();
    press("ControlLeft", "Control");
    press("Escape", "Escape");
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(f.shortcut!.set).not.toHaveBeenCalled();
    expect(f.shortcut!.resume).not.toHaveBeenCalled(); // Control ainda fisicamente em baixo
    lift("ControlLeft", "Control");
    await waitFor(() => expect(f.shortcut!.resume).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("key-recorder-change")).not.toBeNull();
  });

  it("blur com tecla em baixo não retoma; só retoma quando as teclas estão livres", async () => {
    const f = installBridge({ withShortcut: true });
    render(<RecorderStage />);
    await beginCapture();
    press("ControlLeft", "Control");
    press("AltLeft", "Alt");
    fireEvent.keyUp(document, { code: "AltLeft", key: "Alt" }); // uma fica em baixo
    act(() => { window.dispatchEvent(new Event("blur")); });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(f.shortcut!.resume).not.toHaveBeenCalled();
    lift("ControlLeft", "Control");
    // a captura já abandonou; keyup seguinte de tecla que ficou em baixo: sem rearma automático
    // (rearma só por nova captura válida) — o contrato diz 'não rearma enquanto premida'
    expect(f.shortcut!.set).not.toHaveBeenCalled();
  });

  it("AltGr é ambíguo: nada é registado e a captura é cancelada com mensagem", async () => {
    const f = installBridge({ withShortcut: true });
    render(<RecorderStage />);
    await beginCapture();
    press("ControlLeft", "Control");
    press("AltRight", "AltGraph");
    lift("AltRight", "AltGraph");
    lift("ControlLeft", "Control");
    expect(f.shortcut!.set).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByTestId("recorder-error").textContent).toContain("AltGr"), { timeout: 4000 });
  });

  it("modificador duplicado rejeita a combinação", async () => {
    const f = installBridge({ withShortcut: true });
    render(<RecorderStage />);
    await beginCapture();
    press("ControlLeft", "Control");
    press("ControlRight", "Control");
    lift("ControlRight", "Control");
    lift("ControlLeft", "Control");
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(f.shortcut!.set).not.toHaveBeenCalled();
    expect(screen.getByTestId("recorder-error").textContent).toContain("repetidas");
  });

  it("tecla desconhecida rejeita e não registra", async () => {
    const f = installBridge({ withShortcut: true });
    render(<RecorderStage />);
    await beginCapture();
    press("ControlLeft", "Control");
    press("Home", "Home");
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(f.shortcut!.set).not.toHaveBeenCalled();
    expect(screen.getByTestId("recorder-error").textContent).toContain("não pode fazer parte");
  });

  it("auto-repeat não duplica a tecla na sequência", async () => {
    const f = installBridge({ withShortcut: true });
    render(<RecorderStage />);
    await beginCapture();
    press("ControlLeft", "Control");
    press("ControlLeft", "Control", { repeat: true });
    press("AltLeft", "Alt");
    lift("AltLeft", "Alt");
    lift("ControlLeft", "Control");
    await waitFor(() => expect(f.shortcut!.set).toHaveBeenCalledWith("Ctrl+Alt"), { timeout: 4000 });
  });

  it("pause devolve hold_unavailable: captura não começa, mensagem honesta", async () => {
    const f = installBridge({ shortcut: { pause: async () => ({ ok: false, error: "hold_unavailable" }) } });
    render(<RecorderStage />);
    fireEvent.click(screen.getByTestId("key-recorder-change"));
    await waitFor(() => expect(screen.getByTestId("recorder-error").textContent).toContain("observador"), { timeout: 4000 });
    expect(screen.queryByTestId("key-recorder-keys")).toBeNull();
    expect(f.calls.length).toBe(0);
  });

  it("conflito devolvido pelo main: copy fixa e valor efetivo (anterior) apresentado", async () => {
    installBridge({
      shortcut: { set: async () => ({ accelerator: "Ctrl+Alt", error: "shortcut_conflict" }) },
    });
    render(<RecorderStage />);
    await beginCapture();
    press("ControlLeft", "Control");
    press("AltLeft", "Alt");
    lift("AltLeft", "Alt");
    lift("ControlLeft", "Control");
    await waitFor(() => expect(screen.getByTestId("recorder-error").textContent).toContain("já está a ser usado"), { timeout: 4000 });
    expect(screen.getByTestId("applied").textContent).toBe("Ctrl+Alt");
  });
});

// ---------------------------------------------------------------------------
// Warmup / readiness
// ---------------------------------------------------------------------------

function WarmupProbe() {
  const warmup = useVoiceWarmup("sess-i");
  return <>
    <span data-testid="warmup-settled">{warmup.settled ? "aberta" : "a-preparar"}</span>
    <span data-testid="warmup-message">{warmup.settledMessage}</span>
  </>;
}

describe("warmup de voz", () => {
  it("inicia UMA vez e abre com o evento ready=true", async () => {
    const f = installBridge();
    render(<WarmupProbe />);
    await waitFor(() => expect(f.calls.some(c => c.type === "voice.warmup" && Object.keys(c.payload).length === 0)).toBe(true));
    expect(f.calls.filter(c => c.type === "voice.warmup")).toHaveLength(1);
    expect(screen.getByTestId("warmup-settled").textContent).toBe("a-preparar");
    f.emit({ v: 1, type: "voice.readiness", payload: { session_id: "sess-i", state: "ready", ready: true, tts: "ready", stt: "unavailable", message: "Entrada de voz indisponível: runtime de ditado não instalado." } });
    await waitFor(() => expect(screen.getByTestId("warmup-settled").textContent).toBe("aberta"));
    expect(screen.getByTestId("warmup-message").textContent).toContain("runtime de ditado");
  });

  it("backend sem comando de warmup abre degradado com mensagem real", async () => {
    installBridge();
    const api = (window as unknown as { lunmi: { sendCommand: ReturnType<typeof vi.fn> } }).lunmi;
    api.sendCommand.mockImplementation(async (type: string) => { throw new Error(`unknown_command: ${type}`); });
    render(<WarmupProbe />);
    await waitFor(() => expect(screen.getByTestId("warmup-settled").textContent).toBe("aberta"));
    expect(screen.getByTestId("warmup-message").textContent).toContain("indisponível");
  });

  it("perna nunca terminal: o timeout local abre degradado com aviso honesto", () => {
    vi.useFakeTimers();
    try {
      installBridge(); // voice.warmup fica em `preparing` sem evento terminal
      render(<WarmupProbe />);
      expect(screen.getByTestId("warmup-settled").textContent).toBe("a-preparar");
      act(() => { vi.advanceTimersByTime(WARMUP_LOCAL_GUARD_MS + 100); });
      expect(screen.getByTestId("warmup-settled").textContent).toBe("aberta");
      expect(screen.getByTestId("warmup-message").textContent).toContain("Tempo limite");
    } finally {
      vi.useRealTimers();
    }
  });
});

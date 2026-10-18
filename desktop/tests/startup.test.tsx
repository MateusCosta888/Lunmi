import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { App } from "../src/App";
import { advanceMeteor, INTRO_DURATION_MS, StartupScreen } from "../src/features/session/StartupScreen";
import type { LunmiBridgeApi } from "../src/shared/ipc/client";
import type { Envelope, SessionReadyPayload } from "../src/shared/protocol";

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function bridge(memory: () => Promise<unknown> = async () => ({ enabled: true, auto_learn: true, text: "", revision: 0, can_undo: false, char_count: 0, max_chars: 8000, state: "empty" }), theme = "celestial") {
  const payload = { session_id: "startup-test", capabilities: { brain: true, memory: true, converse: true, execute: true } } as SessionReadyPayload;
  const sendCommand = vi.fn(async (type: string) => {
    if (type === "memory.profile.get") return memory();
    if (type === "brain.get") return { current: {}, providers: [] };
    if (type === "approval.get") return { auto_approve: false };
    if (type === "ui.preferences.get") return { actions: [], theme };
    return {};
  });
  const api = {
    hello: vi.fn(async () => payload), onEvent: () => () => {}, sendCommand,
    notifyRendererReady: vi.fn(async () => ({ ok: true })),
    restartBackend: vi.fn(async () => payload),
  } as unknown as LunmiBridgeApi;
  window.lunmi = api;
  return { api, sendCommand };
}

it("aguarda memória real e não envia Ready nem tarefas antes dos dados", async () => {
  let resolve!: (data: unknown) => void;
  const fake = bridge(() => new Promise(r => { resolve = r; }));
  render(<App />);
  await waitFor(() => expect(fake.sendCommand).toHaveBeenCalledWith("memory.profile.get", {}));
  expect(screen.getByTestId("startup-screen")).toBeTruthy();
  expect(screen.queryByTestId("composer")).toBeNull();
  expect(fake.api.notifyRendererReady).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Repetir animação" })).toBeNull();
  expect(fake.api.hello).toHaveBeenCalledTimes(1);
  resolve({ enabled: true, auto_learn: true, text: "", revision: 0, can_undo: false, char_count: 0, max_chars: 8000, state: "empty" });
  await screen.findByTestId("composer");
  expect(screen.queryByTestId("startup-screen")).toBeNull();
  await waitFor(() => expect(fake.api.notifyRendererReady).toHaveBeenCalledTimes(1));
  expect(fake.sendCommand.mock.calls.some(([type]) => type === "chat.send")).toBe(false);
});

it("espera a sequência inteira mesmo com os dados prontos antes", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  const fake = bridge();
  render(<App />);
  await act(async () => { await vi.advanceTimersByTimeAsync(INTRO_DURATION_MS - 1); });
  expect(screen.getByTestId("startup-screen")).toBeTruthy();
  expect(screen.queryByTestId("composer")).toBeNull();
  expect(fake.api.notifyRendererReady).not.toHaveBeenCalled();
  await act(async () => { await vi.advanceTimersByTimeAsync(1); });
  expect(screen.getByTestId("startup-screen").getAttribute("data-leaving")).toBe("true");
  expect(screen.queryByTestId("composer")).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(260); });
  expect(screen.getByTestId("composer")).toBeTruthy();
});

it("depois da sequência continua aguardando o carregamento real", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  let resolve!: (data: unknown) => void;
  bridge(() => new Promise(r => { resolve = r; }));
  render(<App />);
  await act(async () => { await vi.advanceTimersByTimeAsync(INTRO_DURATION_MS + 2000); });
  expect(screen.getByTestId("startup-screen")).toBeTruthy();
  expect(screen.queryByTestId("composer")).toBeNull();
  await act(async () => { resolve({ enabled: true, auto_learn: true, text: "", revision: 0, can_undo: false, char_count: 0, max_chars: 8000, state: "empty" }); });
  await act(async () => { await vi.advanceTimersByTimeAsync(260); });
  expect(screen.getByTestId("composer")).toBeTruthy();
});

it("tema claro salvo não escurece os textos da abertura celestial", async () => {
  vi.useFakeTimers();
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  bridge(undefined, "soft-light"); render(<App />);
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(screen.getByTestId("startup-screen")).toBeTruthy();
  expect(document.documentElement.dataset.theme).toBe("celestial");
  await act(async () => { await vi.advanceTimersByTimeAsync(INTRO_DURATION_MS); });
  await act(async () => { await vi.advanceTimersByTimeAsync(260); });
  expect(screen.getByTestId("composer")).toBeTruthy();
  expect(document.documentElement.dataset.theme).toBe("soft-light");
});

it("falha de dados mantém a entrada e permite recarregar sem reiniciar Python", async () => {
  let fail = true;
  const fake = bridge(async () => {
    if (fail) throw new Error("sensitive backend detail");
    return { enabled: true, auto_learn: true, text: "", revision: 0, can_undo: false, char_count: 0, max_chars: 8000, state: "empty" };
  });
  render(<App />);
  await screen.findByRole("alert");
  expect(screen.queryByTestId("composer")).toBeNull();
  expect(document.body.textContent).not.toContain("sensitive backend detail");
  fail = false;
  fireEvent.click(screen.getByRole("button", { name: "Tentar novamente" }));
  await screen.findByTestId("composer");
  expect(fake.api.restartBackend).not.toHaveBeenCalled();
});

it("falha do handshake oferece reinício explícito, sem replay de instruções", async () => {
  const fake = bridge();
  vi.mocked(fake.api.hello).mockRejectedValue(new Error("missing Python"));
  render(<App />);
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "Tentar novamente" }));
  await screen.findByTestId("composer");
  expect(fake.api.restartBackend).toHaveBeenCalledTimes(1);
  expect(fake.sendCommand.mock.calls.some(([type]) => type === "chat.send")).toBe(false);
});

it("retry não usa memória da tentativa anterior para liberar os controles", async () => {
  let first = true, resolve!: (value: unknown) => void;
  const fake = bridge(async () => first ? { enabled: true, auto_learn: true, text: "", revision: 0, can_undo: false, char_count: 0, max_chars: 8000, state: "empty" } : new Promise(r => { resolve = r; }));
  const original = fake.sendCommand.getMockImplementation()!;
  fake.sendCommand.mockImplementation(async type => {
    if (first && type === "brain.get") throw new Error("controls failed");
    return original(type);
  });
  render(<App />);
  await screen.findByRole("alert");
  first = false;
  fireEvent.click(screen.getByRole("button", { name: "Tentar novamente" }));
  await waitFor(() => expect(resolve).toBeTypeOf("function"));
  expect(screen.queryByTestId("composer")).toBeNull();
  expect(fake.api.notifyRendererReady).not.toHaveBeenCalled();
  resolve({ enabled: true, auto_learn: true, text: "", revision: 0, can_undo: false, char_count: 0, max_chars: 8000, state: "empty" });
  await screen.findByTestId("composer");
});

it("velocidade dos meteoros é independente da taxa de quadros", () => {
  const initial = { x: 0, y: 0, dx: -.8, dy: .6, speed: 1080, tail: 200, head: 3, hero: false, opacity: 0 };
  const sixty = { ...initial }, oneTwenty = { ...initial };
  for (let i = 0; i < 60; i++) advanceMeteor(sixty, 1 / 60);
  for (let i = 0; i < 120; i++) advanceMeteor(oneTwenty, 1 / 120);
  expect(sixty.x).toBeCloseTo(oneTwenty.x, 8);
  expect(sixty.y).toBeCloseTo(oneTwenty.y, 8);
  expect(sixty.x).toBeLessThan(0); expect(sixty.y).toBeGreaterThan(0);
});

it("a onda original de 12 meteoros não atrasa por causa do limite contínuo em 4K", () => {
  const gradient = { addColorStop: vi.fn() };
  const ctx = { setTransform: vi.fn(), clearRect: vi.fn(), save: vi.fn(), restore: vi.fn(),
    beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn(), arc: vi.fn(), fill: vi.fn(),
    createLinearGradient: () => gradient, createRadialGradient: () => gradient };
  vi.mocked(HTMLCanvasElement.prototype.getContext).mockReturnValue(ctx as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 3840, height: 2160 } as DOMRect);
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  let frame!: FrameRequestCallback;
  vi.spyOn(window, "requestAnimationFrame").mockImplementation(callback => { frame = callback; return 1; });
  render(<StartupScreen message="Iniciando sistema…" error={false} onRetry={() => {}} />);
  for (let i = 0; i <= 200; i++) { ctx.stroke.mockClear(); frame(i * 1000 / 60); }
  expect(ctx.stroke).toHaveBeenCalledTimes(12);
});

it("refresh com erro após abrir não bloqueia Ready da sessão reiniciada", async () => {
  const fake = bridge();
  const listeners = new Set<(event: Envelope) => void>();
  fake.api.onEvent = callback => { listeners.add(callback); return () => { listeners.delete(callback); }; };
  render(<App />);
  await screen.findByTestId("composer");
  await waitFor(() => expect(fake.api.notifyRendererReady).toHaveBeenCalledTimes(1));
  fake.sendCommand.mockRejectedValueOnce(new Error("refresh failed"));
  fireEvent.click(screen.getByTestId("nav-memory"));
  await screen.findByRole("alert");
  for (const listener of listeners) listener({ v: 1, id: "failure", type: "task.error", payload: { code: "backend_exited", message: "Backend stopped" } });
  await screen.findByTestId("restart-backend");
  vi.mocked(fake.api.restartBackend!).mockResolvedValue({ session_id: "restarted", capabilities: { brain: true, memory: true, converse: true, execute: true } } as SessionReadyPayload);
  fireEvent.click(screen.getByTestId("restart-backend"));
  await waitFor(() => expect(fake.api.notifyRendererReady).toHaveBeenCalledTimes(2));
  expect(screen.queryByTestId("startup-screen")).toBeNull();
});

it("canvas respeita movimento reduzido e remove todos os listeners ao sair", () => {
  const gradient = { addColorStop: vi.fn() };
  const ctx = { setTransform: vi.fn(), clearRect: vi.fn(), createLinearGradient: () => gradient, createRadialGradient: () => gradient };
  vi.mocked(HTMLCanvasElement.prototype.getContext).mockReturnValue(ctx as unknown as CanvasRenderingContext2D);
  const add = vi.fn(), remove = vi.fn();
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: add, removeEventListener: remove }));
  const raf = vi.spyOn(window, "requestAnimationFrame");
  const winRemove = vi.spyOn(window, "removeEventListener");
  const docRemove = vi.spyOn(document, "removeEventListener");
  const rendered = render(<StartupScreen message="Iniciando sistema…" error={false} onRetry={() => {}} />);
  expect(raf).not.toHaveBeenCalled();
  rendered.unmount();
  expect(winRemove).toHaveBeenCalledWith("resize", expect.any(Function));
  expect(docRemove).toHaveBeenCalledWith("visibilitychange", expect.any(Function));
  expect(remove).toHaveBeenCalledWith("change", expect.any(Function));
});

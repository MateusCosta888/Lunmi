/**
 * Testes da orb no chat (`desktop/tests/chat-orb.test.tsx`, ORB REACTIVE V1).
 *
 * Equivalente ao lote do motor anterior, agora contra o `liveRenderer`: a mesma orb permanece
 * fora do scroller entre chat vazio e com histórico (mesmo nó canvas, sem remontar); sem
 * WebGL/2D o fallback é a marca real acessível; com contexto WebGL falso, o desmontar liberta
 * RAF, buffers, texture, program e shaders, sobrevivendo à repetição do StrictMode — e nenhum
 * efeito tardio (setMode/setActive) lança depois de destruído.
 */
import { StrictMode } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ChatPanel } from "../src/features/chat/ChatPanel";
import { GalaxyOrb } from "../src/features/chat/orb/GalaxyOrb";
import { mountLiveOrb } from "../src/features/chat/orb/liveRenderer";
import type { ChatTaskState } from "../src/features/chat/useChatTask";

function state(messages: ChatTaskState["messages"] = []): ChatTaskState {
  return {
    messages, recentTasks: [], task: null, confirmRequest: null, inputRequest: null,
    busy: false, submitting: false, lastError: null, memoryVersion: 0,
    send: vi.fn(async () => ({ accepted: true })), stop: vi.fn(async () => {}),
    approve: vi.fn(async () => {}), answerChoice: vi.fn(async () => {}),
    answerLocation: vi.fn(async () => {}), dismissError: vi.fn(),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("keeps the same orb outside the message scroller across empty and populated chat", () => {
  const chat = state();
  const view = render(<ChatPanel chat={chat} session={null} />);
  const orb = screen.getByTestId("chat-orb");
  const canvas = orb.querySelector("canvas");
  expect(screen.getByText("Como posso ajudar?")).toBeTruthy();
  expect(screen.getByTestId("chat-messages").contains(orb)).toBe(false);
  expect(orb.closest("[data-has-messages]")?.getAttribute("data-has-messages")).toBe("false");
  view.rerender(<ChatPanel chat={state([{ role: "user", text: "Olá", message_id: "user-1" }])} session={null} />);
  expect(screen.getByTestId("chat-orb")).toBe(orb);
  expect(orb.querySelector("canvas")).toBe(canvas);
  expect(screen.queryByText("Como posso ajudar?")).toBeNull();
  expect(orb.closest("[data-has-messages]")?.getAttribute("data-has-messages")).toBe("true");
  fireEvent.scroll(screen.getByTestId("chat-messages"));
  expect(screen.getByTestId("chat-orb")).toBe(orb);
  expect(screen.getByTestId("composer")).toBeTruthy();
});

it("keeps an accessible visual fallback when WebGL is unavailable", () => {
  render(<GalaxyOrb />);
  expect(screen.getByTestId("chat-orb").getAttribute("data-unavailable")).toBe("true");
  expect(screen.getByRole("img", { name: "Lunmi" })).toBeTruthy();
});

function makeGl() {
  const ids = { current: 0 };
  return {
    createShader: () => ({ id: ++ids.current }), createProgram: () => ({ id: ++ids.current }),
    createBuffer: () => ({ id: ++ids.current }), createTexture: () => ({ id: ++ids.current }),
    getShaderParameter: () => true, getProgramParameter: () => true,
    getShaderInfoLog: () => "", getProgramInfoLog: () => "",
    getAttribLocation: () => 0, getUniformLocation: () => ({ id: ++ids.current }),
    shaderSource: vi.fn(), compileShader: vi.fn(), attachShader: vi.fn(), linkProgram: vi.fn(),
    useProgram: vi.fn(), bindBuffer: vi.fn(), bufferData: vi.fn(), bindTexture: vi.fn(),
    texImage2D: vi.fn(), texParameteri: vi.fn(), pixelStorei: vi.fn(), viewport: vi.fn(),
    clearColor: vi.fn(), clear: vi.fn(), drawArrays: vi.fn(), activeTexture: vi.fn(),
    enableVertexAttribArray: vi.fn(), vertexAttribPointer: vi.fn(),
    uniform1f: vi.fn(), uniform2f: vi.fn(), uniform1i: vi.fn(),
    deleteShader: vi.fn(), deleteProgram: vi.fn(), deleteBuffer: vi.fn(), deleteTexture: vi.fn(),
    ARRAY_BUFFER: 1, STATIC_DRAW: 2, FLOAT: 3, VERTEX_SHADER: 4, FRAGMENT_SHADER: 5,
    TEXTURE_2D: 6, TEXTURE_WRAP_S: 7, TEXTURE_WRAP_T: 8, CLAMP_TO_EDGE: 9,
    TEXTURE_MIN_FILTER: 10, TEXTURE_MAG_FILTER: 11, LINEAR: 12, UNPACK_FLIP_Y_WEBGL: 13,
    TEXTURE0: 14, RGB: 15, UNSIGNED_BYTE: 16, TRIANGLES: 17, COLOR_BUFFER_BIT: 18,
  };
}

function make2d() {
  return {
    fillStyle: "", strokeStyle: "", lineWidth: 0, shadowColor: "", shadowBlur: 0,
    fillRect: vi.fn(), clearRect: vi.fn(), beginPath: vi.fn(), arc: vi.fn(), rect: vi.fn(),
    roundRect: vi.fn(), fill: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn(),
    save: vi.fn(), restore: vi.fn(), clip: vi.fn(), drawImage: vi.fn(),
    createRadialGradient: () => ({ addColorStop: vi.fn() }),
  };
}

function mockWebgl() {
  const gl = makeGl();
  const ctx2d = make2d();
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(
    (contextId: string): RenderingContext | null =>
      (contextId === "webgl" ? gl : ctx2d) as unknown as RenderingContext | null
  );
  return gl;
}

it("releases GL objects and RAFs on unmount and survives StrictMode replay", () => {
  const gl = mockWebgl();
  const frames = new Map<number, FrameRequestCallback>();
  let next = 0;
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { frames.set(++next, cb); return next; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  const view = render(<StrictMode><GalaxyOrb mode="idle" /></StrictMode>);
  expect(screen.getByTestId("chat-orb").getAttribute("data-unavailable")).toBe("false");
  expect(frames.size).toBe(1); // o motor descartado pelo replay libertou o seu frame
  view.unmount();
  expect(frames.size).toBe(0);
  // StrictMode = 2 ciclos setup→destroy: cada destroy liberta buffer+texture+program; cada
  // setup liberta os 2 shaders logo após o link. Nada fica em voo depois do unmount.
  expect(gl.deleteBuffer).toHaveBeenCalledTimes(2);
  expect(gl.deleteProgram).toHaveBeenCalledTimes(2);
  expect(gl.deleteShader).toHaveBeenCalledTimes(4);
  expect(gl.deleteTexture).toHaveBeenCalledTimes(2);
});

it("late effects after destroy are inert: setMode/setActive/destroy no-op, zero RAF and resources", () => {
  mockWebgl();
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  let next = 0;
  let scheduled = 0;
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { scheduled += 1; void cb; return ++next; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => { void id; });
  const canvas = document.createElement("canvas");
  document.body.append(canvas);
  const handle = mountLiveOrb(canvas, {});
  const rafBefore = scheduled;
  handle.destroy();
  expect(() => {
    handle.setMode("listening");
    handle.setActive(false);
    handle.setActive(true);
    handle.destroy();
  }).not.toThrow();
  expect(scheduled).toBe(rafBefore); // destroy é definitivo: nenhum RAF novo, nenhum recurso novo
  canvas.remove();
});

it("pointer movement while hidden or over a 0×0 rect cannot poison gaze with NaN on reveal", () => {
  const gl = mockWebgl();
  const frames = new Map<number, FrameRequestCallback>();
  let next = 0;
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { frames.set(++next, cb); return next; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => { frames.delete(id); });
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  const canvas = document.createElement("canvas");
  document.body.append(canvas);
  const handle = mountLiveOrb(canvas, {});
  handle.setActive(false);
  fireEvent(canvas.parentElement || document.body, new MouseEvent("pointermove", { clientX: 120, clientY: 60 }));
  document.dispatchEvent(new MouseEvent("pointermove", { clientX: 4000, clientY: -9000 })); // rect 0×0 em jsdom
  handle.setActive(true);
  const cb = frames.values().next().value;
  frames.clear();
  cb?.(performance.now());
  for (const call of gl.uniform2f.mock.calls) {
    for (const value of call.slice(1)) expect(Number.isFinite(value as number)).toBe(true);
  }
  handle.destroy();
  canvas.remove();
});

it.each([[false, 60], [true, 60], [false, 144], [true, 144]] as const)("gaze on compact=%s, display=%s Hz stays smooth without doubling particle uploads", (compact, displayHz) => {
  const gl = mockWebgl();
  const frames = new Map<number, FrameRequestCallback>();
  let next = 0, now = 1000;
  vi.spyOn(performance, "now").mockImplementation(() => now);
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { frames.set(++next, cb); return next; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  const host = document.createElement("div"), canvas = document.createElement("canvas");
  host.append(canvas); document.body.append(host);
  vi.spyOn(host, "getBoundingClientRect").mockReturnValue({ left: 0, top: 0, width: 100, height: 100 } as DOMRect);
  const handle = mountLiveOrb(canvas, { compact });
  const tick = () => { const pending = [...frames.values()]; frames.clear(); pending.forEach(cb => cb(now)); };
  tick();
  document.dispatchEvent(new MouseEvent("pointermove", { clientX: 100, clientY: 50 }));
  gl.drawArrays.mockClear(); gl.texImage2D.mockClear();
  for (let i = 1; i <= Math.floor(displayHz * 0.2); i++) { now = 1000 + i * 1000 / displayHz; tick(); }
  expect(gl.drawArrays.mock.calls.length).toBeGreaterThanOrEqual(11);
  expect(gl.texImage2D.mock.calls.length).toBeLessThanOrEqual(7);
  const gaze = gl.uniform2f.mock.calls.at(-1)?.[1] as number;
  expect(gaze).toBeGreaterThan(0.8);
  expect(gaze).toBeLessThan(1);
  expect(gaze).toBeCloseTo(1 - Math.exp(-200 / 95), 1);
  // Once pointer interaction expires, keep the original low idle cost.
  now = 5000; tick(); gl.drawArrays.mockClear();
  for (let i = 1; i <= displayHz; i++) { now = 5000 + i * 1000 / displayHz; tick(); }
  expect(gl.drawArrays.mock.calls.length).toBeLessThanOrEqual(compact ? 16 : 31);
  handle.setActive(false);
  expect(frames.size).toBe(0);
  handle.destroy(); host.remove();
});

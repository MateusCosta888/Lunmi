import { StrictMode } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ChatPanel } from "../src/features/chat/ChatPanel";
import { GalaxyOrb } from "../src/features/chat/orb/GalaxyOrb";
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

it("releases GL objects and RAFs on unmount and survives StrictMode replay", () => {
  const ids = { current: 0 };
  const gl = {
    createShader: () => ({ id: ++ids.current }), createProgram: () => ({ id: ++ids.current }),
    createBuffer: () => ({ id: ++ids.current }), createTexture: () => ({ id: ++ids.current }),
    createFramebuffer: () => ({ id: ++ids.current }),
    getShaderParameter: () => true, getProgramParameter: () => true,
    getAttribLocation: () => 0, getUniformLocation: () => ({}),
    checkFramebufferStatus: () => 1, FRAMEBUFFER_COMPLETE: 1, isContextLost: () => false,
    shaderSource: vi.fn(), compileShader: vi.fn(), attachShader: vi.fn(), linkProgram: vi.fn(),
    bindBuffer: vi.fn(), bufferData: vi.fn(), bindTexture: vi.fn(), texImage2D: vi.fn(), texParameteri: vi.fn(),
    bindFramebuffer: vi.fn(), framebufferTexture2D: vi.fn(), viewport: vi.fn(), clearColor: vi.fn(), clear: vi.fn(),
    useProgram: vi.fn(), enable: vi.fn(), disable: vi.fn(), blendFunc: vi.fn(), enableVertexAttribArray: vi.fn(),
    vertexAttribPointer: vi.fn(), uniform1f: vi.fn(), uniform2f: vi.fn(), uniform1i: vi.fn(),
    drawArrays: vi.fn(), disableVertexAttribArray: vi.fn(), activeTexture: vi.fn(),
    deleteShader: vi.fn(), deleteProgram: vi.fn(), deleteBuffer: vi.fn(), deleteTexture: vi.fn(), deleteFramebuffer: vi.fn(),
  };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(gl as unknown as WebGLRenderingContext);
  const frames = new Map<number, FrameRequestCallback>();
  let next = 0;
  vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => { frames.set(++next, cb); return next; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  const view = render(<StrictMode><GalaxyOrb /></StrictMode>);
  expect(screen.getByTestId("chat-orb").getAttribute("data-unavailable")).toBe("false");
  expect(frames.size).toBe(1);
  view.unmount();
  expect(frames.size).toBe(0);
  expect(gl.deleteBuffer).toHaveBeenCalledTimes(4);
  expect(gl.deleteProgram).toHaveBeenCalledTimes(4);
  expect(gl.deleteShader).toHaveBeenCalledTimes(8);
  expect(gl.deleteTexture).toHaveBeenCalledTimes(2);
  expect(gl.deleteFramebuffer).toHaveBeenCalledTimes(2);
});

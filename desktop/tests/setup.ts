/**
 * Setup dos testes de componente (`desktop/tests/setup.ts`).
 *
 * O vitest aqui não roda com `globals: true`, então a limpeza automática do
 * `@testing-library/react` (que depende de um `afterEach` global) não acontece sozinha. Sem
 * `cleanup()` explícito a árvore de um teste sobrevive ao seguinte e dois botões passam a
 * responder pelo mesmo `data-testid` — foi exatamente esse o defeito observado. A limpeza é
 * registrada aqui, uma vez, para toda a suíte.
 */
import { afterEach, beforeEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// jsdom has no rasterizer; canvas rendering is checked in the real Electron acceptance.
beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  // Most shell tests exercise reduced motion; startup timing has dedicated normal-motion cases.
  vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }));
  // Layout/animation geometry is checked in Electron; jsdom only exercises semantics.
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

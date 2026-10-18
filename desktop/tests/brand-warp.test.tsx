/**
 * Marca central com WarpText (`desktop/tests/brand-warp.test.tsx`, LUNMI-LIGHT-GLASS-WARP-BRAND).
 *
 * O jsdom não rasteriza WebGL: o valor dos primeiros testes é o contrato de degradação e ciclo de
 * vida — o texto DOM "Lunmi" permanece legível quando o WebGL não existe, a redução de transparência
 * mantém o texto estático sem sequer abrir um contexto, o componente não recria o nó ao trocar de
 * tema e o unmount não deixa sujeira. A pintura/onda real são medidas na inspeção Electron.
 *
 * Os dois últimos testes vão além do fallback: usam um `ogl` falso e um `requestAnimationFrame`
 * controlado para percorrer o caminho WebGL ATIVO (programa, uniformes e laço) no jsdom. Sem isso o
 * defeito de regressão do movimento reduzido (onda em voo continuava envelhecendo e repintando o
 * texto parado) seria invisível para a suíte.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { WarpText } from "../src/shared/WarpText";
import { applyTheme } from "../src/shared/themeDom";

/** Estado do `ogl` falso: pinturas, uniformes vivos e a fila de quadros do RAF controlado. */
const ogl = vi.hoisted(() => ({
  paints: 0,
  programs: [] as Array<Record<string, { value: unknown }>>,
  frameQueue: [] as Array<(time: number) => void>,
  now: 0,
  handles: 0,
}));

/**
 * `ogl` falso. O `Renderer` respeita o contrato real: ele pede `webgl2` ao canvas e falha quando o
 * contexto não existe — é assim que o jsdom (sem GPU) continua exercitando o caminho de degradação,
 * e é o teste que decide, via `getContext`, se o caminho WebGL ativo está disponível.
 */
vi.mock("ogl", () => {
  class Renderer {
    dpr = 1;
    gl: Record<string, unknown>;
    constructor() {
      const canvas = document.createElement("canvas");
      const context = canvas.getContext("webgl2") as unknown as Record<string, unknown> | null;
      if (!context) throw new Error("WebGL2 indisponível neste ambiente");
      this.gl = Object.assign({}, context, { canvas });
    }
    setSize() {}
    render() { ogl.paints += 1; }
  }
  class Program {
    uniforms: Record<string, { value: unknown }>;
    constructor(_gl: unknown, options: { uniforms: Record<string, { value: unknown }> }) {
      this.uniforms = options.uniforms;
      ogl.programs.push(this.uniforms);
    }
    remove() {}
  }
  class Mesh { constructor() {} }
  class Triangle { remove() {} }
  class Texture { texture = {}; image: unknown = null; needsUpdate = false; constructor() {} }
  return { Renderer, Program, Mesh, Triangle, Texture };
});

/** Contexto WebGL2 falso: o componente só usa estes membros. */
const fakeWebgl2 = () => ({
  drawingBufferWidth: 92,
  drawingBufferHeight: 30,
  clearColor: () => undefined,
  deleteTexture: () => undefined,
  getExtension: () => null,
  LINEAR: 1,
  CLAMP_TO_EDGE: 2,
});

/** Habilita o caminho WebGL ativo mantendo o 2D fora (o jsdom não tem rasterizador). */
function enableWebgl() {
  const mock = getContextMock();
  mock.mockImplementation((id: string) => (id === "webgl2" ? fakeWebgl2() : null) as unknown as RenderingContext);
  return mock;
}

/**
 * Sistema de mídia controlável: guarda os listeners de cada consulta para poder virar a preferência
 * no meio do uso, como o usuário faz nas Configurações do Windows.
 */
function controllableMedia() {
  const listeners = new Map<string, Array<(event: { matches: boolean }) => void>>();
  const queries = new Set<string>();
  vi.stubGlobal("matchMedia", (query: string) => {
    queries.add(query);
    const list = {
      media: query,
      matches: false,
      onchange: null,
      addEventListener: (_type: string, handler: (event: { matches: boolean }) => void) => {
        listeners.set(query, [...(listeners.get(query) ?? []), handler]);
      },
      removeEventListener: (_type: string, handler: (event: { matches: boolean }) => void) => {
        listeners.set(query, (listeners.get(query) ?? []).filter(item => item !== handler));
      },
      addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false,
    };
    return list as unknown as MediaQueryList;
  });
  return {
    queries,
    flip: (query: string, matches: boolean) => {
      for (const handler of listeners.get(query) ?? []) handler({ matches });
    },
  };
}

function stubMatchMedia(reducedTransparency: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: reducedTransparency ? true : query.includes("prefers-reduced-motion"),
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }) as unknown as MediaQueryList);
}

const getContextMock = () => HTMLCanvasElement.prototype.getContext as unknown as ReturnType<typeof vi.fn>;

const GREAT_RECT = { x: 0, y: 0, width: 92, height: 30, top: 0, left: 0, right: 92, bottom: 30, toJSON: () => ({}) } as DOMRect;

/** Executa os quadros de RAF pendentes, ~16 ms por quadro (o componente se reagenda). */
function advanceFrames(count: number) {
  for (let i = 0; i < count; i += 1) {
    const pending = ogl.frameQueue.splice(0, ogl.frameQueue.length);
    if (pending.length === 0) return;
    ogl.now += 16;
    act(() => { pending.forEach(callback => callback(ogl.now)); });
  }
}

const MOTION_QUERY = "(prefers-reduced-motion: reduce)";

beforeEach(() => {
  ogl.paints = 0;
  ogl.programs.length = 0;
  ogl.frameQueue.length = 0;
  ogl.now = 0;
  ogl.handles = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: (time: number) => void) => {
    ogl.frameQueue.push(callback);
    return (ogl.handles += 1);
  });
  vi.stubGlobal("cancelAnimationFrame", (handle: number) => {
    if (handle) ogl.frameQueue.length = 0;
  });
  // O jsdom não tem IntersectionObserver; o laço só depende do contrato observe/disconnect.
  vi.stubGlobal("IntersectionObserver", class { observe() {} disconnect() {} });
});

describe("WarpText — componente (não é mais a marca central)", () => {
  it("sem WebGL, o rótulo DOM 'Lunmi' continua legível como fallback real", () => {
    stubMatchMedia(false);
    render(<WarpText text="Lunmi" testId="warp" />);
    const node = screen.getByTestId("warp");
    expect(node.getAttribute("data-warp-engine")).toBe("react-bits-warptext");
    expect(node.getAttribute("data-mode")).toBe("text");
    expect(node.textContent).toBe("Lunmi");
    expect(node.querySelector("canvas")).toBeNull();
    // Tentou abrir WebGL e degradou honestamente para texto.
    expect(getContextMock()).toHaveBeenCalled();
  });

  it("redução de transparência mantém o texto estático sem abrir WebGL", () => {
    stubMatchMedia(true);
    render(<WarpText text="Lunmi" testId="warp" />);
    const node = screen.getByTestId("warp");
    expect(node.getAttribute("data-mode")).toBe("text");
    expect(node.textContent).toBe("Lunmi");
    expect(node.querySelector("canvas")).toBeNull();
    expect(getContextMock()).not.toHaveBeenCalled();
  });

  it("a cor segue o tema por currentColor e a troca de tema não recria o nó (um loop por instância)", () => {
    stubMatchMedia(true);
    render(<span style={{ color: "currentColor" }}><WarpText text="Lunmi" testId="warp" /></span>);
    const label = screen.getByTestId("warp").querySelector<HTMLElement>(".lunmi-warp-label");
    expect(label).not.toBeNull();
    expect(label!.textContent).toBe("Lunmi");
    expect(label!.style.color.toLowerCase()).toBe("currentcolor");
    applyTheme("soft-light");
    applyTheme("red-velvet");
    applyTheme("celestial");
    // Mesmo nó: nada foi remontado ao trocar de tema.
    expect(screen.getByTestId("warp").querySelector(".lunmi-warp-label")).toBe(label);
  });

  it("reduzir movimento durante a onda zera o transiente e para de repintar o texto", () => {
    const media = controllableMedia();
    enableWebgl();
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue(GREAT_RECT);
    const { container } = render(<WarpText text="Lunmi" testId="warp" />);
    const host = screen.getByTestId("warp");
    expect(media.queries.has(MOTION_QUERY)).toBe(true);
    expect(ogl.programs).toHaveLength(1);
    advanceFrames(2);
    expect(host.getAttribute("data-mode")).toBe("canvas");

    const uniforms = ogl.programs[0];
    const clicks = uniforms.uClicks.value as number[][];
    expect(uniforms.uMotion.value).toBe(1);

    // Uma onda nasce e envelhece normalmente com movimento permitido.
    const canvas = container.querySelector("canvas")!;
    fireEvent.pointerDown(canvas, { clientX: 40, clientY: 15 });
    expect(clicks.some(click => click[3] > 0)).toBe(true);
    advanceFrames(6);
    const inFlight = clicks.filter(click => click[3] > 0);
    expect(inFlight).toHaveLength(1);
    expect(inFlight[0][2]).toBeGreaterThan(0);

    // A preferência chega com a onda no ar: nada pode continuar em movimento nem repintando.
    const paintsBeforeReduction = ogl.paints;
    act(() => { media.flip(MOTION_QUERY, true); });
    expect(uniforms.uMotion.value).toBe(0);
    expect(uniforms.uTime.value).toBe(0);
    expect(uniforms.uPointerActive.value).toBe(0);
    expect(uniforms.uReveal.value).toBe(1);
    expect(clicks.every(click => click[3] === 0 && click[2] === 0)).toBe(true);
    // Um único quadro estático é pintado para aplicar o estado parado.
    expect(ogl.paints).toBe(paintsBeforeReduction + 1);

    // Sem movimento não existe mais nenhum quadro agendado: nada repinta o texto parado.
    advanceFrames(30);
    expect(ogl.paints).toBe(paintsBeforeReduction + 1);
    expect(ogl.frameQueue).toHaveLength(0);
    expect(clicks.every(click => click[3] === 0 && click[2] === 0)).toBe(true);

    // Voltar a animar retoma o laço, uma vez só (sem laço duplicado).
    act(() => { media.flip(MOTION_QUERY, false); });
    expect(uniforms.uMotion.value).toBe(1);
    expect(ogl.frameQueue).toHaveLength(1);
    advanceFrames(4);
    expect(ogl.frameQueue).toHaveLength(1);
    expect(ogl.paints).toBeGreaterThan(paintsBeforeReduction + 1);
  });

  it("o tema atualiza a tinta mesmo com o laço parado por movimento reduzido", async () => {
    const media = controllableMedia();
    enableWebgl();
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue(GREAT_RECT);
    render(<WarpText text="Lunmi" testId="warp" />);
    act(() => { media.flip(MOTION_QUERY, true); });
    advanceFrames(5);
    expect(ogl.frameQueue).toHaveLength(0);
    const paintsParked = ogl.paints;
    // Trocar de tema é a única coisa que ainda pinta: a tinta do texto não pode ficar velha.
    await act(async () => { applyTheme("soft-light"); await Promise.resolve(); });
    expect(ogl.paints).toBeGreaterThan(paintsParked);
  });

  it("perder o contexto WebGL retira o canvas perdido e deixa só o rótulo legível", () => {
    stubMatchMedia(false);
    enableWebgl();
    vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue(GREAT_RECT);
    const { container, unmount } = render(<WarpText text="Lunmi" testId="warp" />);
    const host = screen.getByTestId("warp");
    advanceFrames(2);
    expect(host.getAttribute("data-mode")).toBe("canvas");
    const canvas = container.querySelector("canvas")!;
    expect(canvas).not.toBeNull();

    // O contexto se perde com o componente vivo. `cancelable` é exigido pelo preventDefault real.
    const event = new Event("webglcontextlost", { cancelable: true });
    act(() => { canvas.dispatchEvent(event); });

    // O rótulo volta a ser o único filho: nada pode cobrir "Lunmi" (era exatamente o defeito — o
    // canvas ficava no DOM e o navegador pintava a placa de imagem quebrada por cima).
    expect(host.getAttribute("data-mode")).toBe("text");
    expect(container.querySelector("canvas")).toBeNull();
    expect(host.children).toHaveLength(1);
    const label = host.querySelector<HTMLElement>(".lunmi-warp-label")!;
    expect(label.textContent).toBe("Lunmi");
    expect(label.style.color.toLowerCase()).toBe("currentcolor");
    // Nada repinta depois da perda: nenhum quadro agendado e nenhuma pintura nova.
    const paintsAfterLoss = ogl.paints;
    advanceFrames(10);
    expect(ogl.frameQueue).toHaveLength(0);
    expect(ogl.paints).toBe(paintsAfterLoss);

    // Idempotente: a perda pode ser anunciada de novo e o cleanup não pode tentar remover duas vezes.
    act(() => { canvas.dispatchEvent(new Event("webglcontextlost", { cancelable: true })); });
    expect(host.children).toHaveLength(1);
    expect(() => unmount()).not.toThrow();
    expect(container.querySelector("canvas")).toBeNull();
  });

  it("desmontar não deixa canvas nem erro", () => {
    stubMatchMedia(false);
    const { unmount } = render(<WarpText text="Lunmi" testId="warp" />);
    expect(() => unmount()).not.toThrow();
    expect(document.querySelector(".lunmi-warp canvas")).toBeNull();
  });
});

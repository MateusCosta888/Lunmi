/**
 * Testes dirigidos da superfície do LUNMI NODE V1 (`desktop/tests/node-surface.test.tsx`).
 *
 * Nenhum Electron real: `window.lunmiPresence` é uma ponte espiã com snapshot/eventos
 * controlados. Provas do N2 (renderer):
 * - o objeto é CONTÍNUO: mudar de superfície não remonta o DOM nem troca os elementos;
 * - UM gesto abre o chat: um clique no círculo pede `node-open` uma vez (sem `toggle-mini`,
 *   sem `node-reveal`, sem segundo clique na cápsula);
 * - o conteúdo só entra em `settled`; durante o morph mantém-se o anterior;
 * - o estado vem do main (`status`/`tone`): a etiqueta é a do enum e nunca texto de conversa;
 * - o corpo é preto uniforme sem clip/radius e a geometria vem do helper partilhado
 *   (`nodeContentRect`), sem rect nem IPC por frame;
 * - fechar/Esc pede `node-close` e nunca cancela a tarefa.
 *
 * N3 (renderer):
 * - entrar no chat (por gesto ou por atalho) FOCA o composer, não só a janela;
 * - durante o morph nada recebe input (conteúdo `inert`, controlos desativados) e as camadas
 *   ocultas da superfície assente também não;
 * - os alvos do Node (círculo, cápsula) têm 44 px ou mais.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NodeSurface } from "../src/features/companion/NodeSurface";
import { DEFAULT_NODE_PREFERENCES, nodeContentRect } from "../src/shared/node";
import type {
  ChatProjectionDto,
  NodeStateDto,
  PresenceEventDto,
  PresenceSnapshotDto,
  SessionReadyPayload,
} from "../src/shared/types";

// jsdom não traz PointerEvent; o stub local cai em MouseEvent (mesmo padrão do companion test).
if (!("PointerEvent" in globalThis)) {
  (globalThis as Record<string, unknown>).PointerEvent = (globalThis as Record<string, unknown>).MouseEvent;
}

const SESSION: SessionReadyPayload = {
  v: 1, session_id: "sess-n", pid: 1,
  capabilities: { converse: true, execute: true, memory: true, brain: true, web_search: false, browser_use: false, auto: true },
  limits: { max_line_bytes: 5 * 1024 * 1024, max_import_bytes: 4 * 1024 * 1024, max_chat_text_chars: 8000 },
};

const ENVELOPE = { x: 0, y: 0, width: 420, height: 520 };

function chatProjection(overrides: Partial<ChatProjectionDto> = {}): ChatProjectionDto {
  return { session_id: "sess-n", cursor: 0, messages: [], recent_tasks: [], task: null, confirm: null, input: null, ...overrides };
}

function nodeState(overrides: Partial<NodeStateDto> = {}): NodeStateDto {
  return {
    v: 1,
    enabled: true,
    side: "right",
    surface: "docked",
    phase: "settled",
    transition_id: 1,
    transition: null,
    status: null,
    tone: "neutral",
    session_id: "sess-n",
    ...overrides,
  };
}

interface Harness {
  actions: string[];
  commands: Array<{ type: string; payload: Record<string, unknown> }>;
  emitNode: (next: Partial<NodeStateDto>) => void;
  emitAppearance: (appearance: "theme" | "black") => void;
}

function presence(node: Partial<NodeStateDto> = {}, chat: ChatProjectionDto = chatProjection()): Harness {
  let current = nodeState(node);
  const listeners = new Set<(event: PresenceEventDto) => void>();
  const harness: Harness = {
    actions: [],
    commands: [],
    emitAppearance: appearance => act(() => {
      for (const listener of [...listeners]) listener({ type: "node-prefs", preferences: { ...DEFAULT_NODE_PREFERENCES, chat_appearance: appearance } });
    }),
    emitNode: next => {
      current = nodeState({ ...current, ...next });
      act(() => {
        for (const listener of [...listeners]) listener({ type: "node", state: current } as PresenceEventDto);
      });
    },
  };
  const snapshot: PresenceSnapshotDto = {
    mode: "orb",
    resident: true,
    session: SESSION,
    chat,
    draft: "",
    theme: "celestial",
    voice: null,
    node: current,
  };
  (window as unknown as { lunmiPresence: unknown }).lunmiPresence = {
    snapshot: vi.fn(async () => ({ ...snapshot, chat: { ...snapshot.chat }, node: { ...current } })),
    action: vi.fn(async (action: string) => {
      harness.actions.push(action);
      return { ok: true, mode: snapshot.mode, resident: snapshot.resident };
    }),
    setDraft: vi.fn(async () => ({ ok: true })),
    sendCommand: vi.fn(async (type: string, payload: Record<string, unknown> = {}) => {
      harness.commands.push({ type, payload });
      return { accepted: true };
    }),
    onEvent: (cb: (event: PresenceEventDto) => void) => {
      listeners.add(cb);
      return () => {
        listeners.delete(cb);
      };
    },
  };
  return harness;
}

const innerWidth = Object.getOwnPropertyDescriptor(window, "innerWidth");
const innerHeight = Object.getOwnPropertyDescriptor(window, "innerHeight");

beforeEach(() => {
  // A janela do Node é o envelope: o jsdom por omissão é 1024×768.
  Object.defineProperty(window, "innerWidth", { value: ENVELOPE.width, configurable: true, writable: true });
  Object.defineProperty(window, "innerHeight", { value: ENVELOPE.height, configurable: true, writable: true });
});

afterEach(() => {
  if (innerWidth) Object.defineProperty(window, "innerWidth", innerWidth);
  if (innerHeight) Object.defineProperty(window, "innerHeight", innerHeight);
  delete (window as unknown as { lunmiPresence?: unknown }).lunmiPresence;
});

describe("NodeSurface — objeto contínuo", () => {
  it("aplica a aparência confirmada sem fechar nem remontar o chat", async () => {
    const harness = presence({ surface: "chat" });
    render(<NodeSurface />);
    const node = await screen.findByTestId("lunmi-node");
    const chat = screen.getByTestId("mini-chat");
    expect(node.dataset.chatAppearance).toBe("theme");
    harness.emitAppearance("black");
    expect(node.dataset.chatAppearance).toBe("black");
    expect(screen.getByTestId("mini-chat")).toBe(chat);
    expect(node.dataset.surface).toBe("chat");
    harness.emitAppearance("theme");
    expect(node.dataset.chatAppearance).toBe("theme");
    expect(harness.actions).toHaveLength(0);
  });

  it("mudar de superfície não remonta o DOM: o nó e o círculo são os mesmos elementos", async () => {
    const harness = presence({ surface: "docked" });
    render(<NodeSurface />);
    const root = await screen.findByTestId("lunmi-node");
    const circle = screen.getByTestId("node-circle");
    const body = screen.getByTestId("node-body");

    harness.emitNode({ surface: "revealed", transition_id: 2 });
    await waitFor(() => expect(screen.getByTestId("lunmi-node").getAttribute("data-surface")).toBe("revealed"));

    expect(screen.getByTestId("lunmi-node")).toBe(root);
    expect(screen.getByTestId("node-circle")).toBe(circle);
    expect(screen.getByTestId("node-body")).toBe(body);
  });

  it("um clique no círculo abre o chat: uma só ação node-open, sem cápsula extra nem toggle-mini", async () => {
    const harness = presence({ surface: "revealed" });
    render(<NodeSurface />);
    const circle = await screen.findByTestId("node-circle");
    fireEvent.click(circle);

    expect(harness.actions).toEqual(["node-open"]);
    expect(harness.actions).not.toContain("toggle-mini");
    expect(harness.actions).not.toContain("node-reveal");

    // O main responde com a superfície de chegada: o chat está aberto sem segundo clique.
    harness.emitNode({ surface: "chat", phase: "settled", transition_id: 2 });
    await waitFor(() => expect((screen.getByTestId("node-chat") as HTMLElement).hasAttribute("hidden")).toBe(false));
  });

  it("hover sobre o arco recolhido pede node-reveal (uma vez por mudança)", async () => {
    const harness = presence({ surface: "docked" });
    render(<NodeSurface />);
    const root = await screen.findByTestId("lunmi-node");
    fireEvent.pointerOver(root);
    expect(harness.actions).toEqual(["node-reveal"]);
  });

  it("o conteúdo só entra em settled; durante o morph mantém o anterior", async () => {
    const harness = presence({ surface: "revealed" });
    render(<NodeSurface />);
    const root = await screen.findByTestId("lunmi-node");

    harness.emitNode({ surface: "chat", phase: "transitioning", transition_id: 2 });
    await waitFor(() => expect(root.getAttribute("data-phase")).toBe("transitioning"));
    expect(root.getAttribute("data-visible")).toBe("false");
    // O conteúdo anterior (círculo) continua montado; o chat ainda não é o conteúdo visível.
    expect((screen.getByTestId("node-chat") as HTMLElement).hasAttribute("hidden")).toBe(true);

    harness.emitNode({ surface: "chat", phase: "settled", transition_id: 3 });
    await waitFor(() => expect(root.getAttribute("data-visible")).toBe("true"));
    expect((screen.getByTestId("node-chat") as HTMLElement).hasAttribute("hidden")).toBe(false);
    expect((screen.getByTestId("node-circle") as HTMLElement).hasAttribute("hidden")).toBe(true);
  });
});

describe("NodeSurface — estado publicado pelo main", () => {
  it("a etiqueta vem do enum de status e nunca de texto de conversa", async () => {
    presence(
      { surface: "pill", status: "needs_confirm", tone: "needs" },
      chatProjection({ cursor: 1, messages: [{ role: "assistant", text: "resposta secreta", message_id: "m1" }] })
    );
    render(<NodeSurface />);
    const root = await screen.findByTestId("lunmi-node");
    expect(root.getAttribute("data-tone")).toBe("needs");
    expect(root.getAttribute("data-status")).toBe("needs_confirm");
    expect(screen.getByTestId("node-pill-text").textContent).toBe("Preciso da tua confirmação");
    expect(screen.getByTestId("node-pill").textContent).not.toContain("resposta secreta");
    expect(screen.getByTestId("node-circle").textContent).not.toContain("resposta secreta");
  });

  it("cada estado finito tem etiqueta própria e o tom nunca é inventado", async () => {
    const harness = presence({ surface: "pill", status: "working", tone: "working" });
    render(<NodeSurface />);
    const root = await screen.findByTestId("lunmi-node");
    expect(screen.getByTestId("node-pill-text").textContent).toBe("A trabalhar");

    harness.emitNode({ status: "done", tone: "success" });
    await waitFor(() => expect(screen.getByTestId("node-pill-text").textContent).toBe("Tarefa concluída"));
    expect(root.getAttribute("data-tone")).toBe("success");
  });
});

describe("NodeSurface — forma nativa e geometria partilhada", () => {
  it("o corpo é preto uniforme sem clip/radius: a silhueta é a shape do main", async () => {
    presence({ surface: "revealed" });
    render(<NodeSurface />);
    const body = await screen.findByTestId("node-body");
    expect(body.style.borderRadius).toBe("");
    expect(body.style.clipPath).toBe("");
    expect(body.className).toContain("lunmi-node-body");
  });

  it("as camadas são posicionadas pelo helper partilhado, sem rect nem IPC por frame", async () => {
    const harness = presence({ surface: "chat" });
    render(<NodeSurface />);
    const chat = await screen.findByTestId("node-chat");
    const expected = nodeContentRect("chat", "right", ENVELOPE);
    expect(chat.style.left).toBe(`${expected.x}px`);
    expect(chat.style.top).toBe(`${expected.y}px`);
    expect(chat.style.width).toBe(`${expected.width}px`);
    expect(chat.style.height).toBe(`${expected.height}px`);
    // Nenhum pedido ao main para saber a geometria: só ações fechadas por gesto humano.
    expect(harness.actions).toEqual([]);
  });

  it("o lado esquerdo reflecte a preferência publicada pelo main", async () => {
    const harness = presence({ surface: "revealed", side: "left" });
    render(<NodeSurface />);
    const circle = await screen.findByTestId("node-circle");
    const expected = nodeContentRect("revealed", "left", ENVELOPE);
    expect(circle.style.left).toBe(`${expected.x}px`);
    expect(circle.style.top).toBe(`${expected.y}px`);
    harness.emitNode({ side: "right", transition_id: 2 });
    await waitFor(() => expect(screen.getByTestId("node-circle").style.left).toBe(`${nodeContentRect("revealed", "right", ENVELOPE).x}px`));
  });
});

describe("NodeSurface — N3: foco, morph e input", () => {
  it("entrar no chat por gesto foca o composer (não fica só a janela com o foco)", async () => {
    const harness = presence({ surface: "revealed" });
    render(<NodeSurface />);
    fireEvent.click(await screen.findByTestId("node-circle"));
    expect(harness.actions).toEqual(["node-open"]);

    // O main responde com a superfície de chegada: o composer recebe o foco.
    harness.emitNode({ surface: "chat", phase: "settled", transition_id: 2 });
    await waitFor(() => expect((screen.getByTestId("node-chat") as HTMLElement).hasAttribute("hidden")).toBe(false));
    const input = screen.getByTestId("mini-input") as HTMLTextAreaElement;
    await waitFor(() => expect(document.activeElement).toBe(input));
  });

  it("entrar no chat pelo atalho (o main publica a superfície) foca o composer do mesmo modo", async () => {
    const harness = presence({ surface: "docked" });
    render(<NodeSurface />);
    await screen.findByTestId("lunmi-node");

    harness.emitNode({ surface: "chat", phase: "settled", transition_id: 2 });
    const input = screen.getByTestId("mini-input") as HTMLTextAreaElement;
    await waitFor(() => expect(document.activeElement).toBe(input));
  });

  it("durante o morph nada recebe input: conteúdo inert e controlos desativados", async () => {
    const harness = presence({ surface: "revealed" });
    render(<NodeSurface />);
    await screen.findByTestId("node-circle");
    const content = screen.getByTestId("node-content");

    harness.emitNode({ surface: "chat", phase: "transitioning", transition_id: 2 });
    await waitFor(() => expect(content.getAttribute("inert")).toBe(""));
    const circle = screen.getByTestId("node-circle") as HTMLButtonElement;
    expect(circle.disabled).toBe(true);
    expect(circle.tabIndex).toBe(-1);
    // A camada do chat (ainda escondida) também não aceita input enquanto o morph corre.
    const input = screen.getByTestId("mini-input") as HTMLTextAreaElement;
    expect(input.disabled).toBe(true);
    expect(input.tabIndex).toBe(-1);

    harness.emitNode({ surface: "chat", phase: "settled", transition_id: 3 });
    await waitFor(() => expect(content.getAttribute("inert")).toBeNull());
    expect((screen.getByTestId("mini-input") as HTMLTextAreaElement).disabled).toBe(false);
  });

  it("as camadas ocultas da superfície assente não recebem input", async () => {
    presence({ surface: "chat" });
    render(<NodeSurface />);
    await screen.findByTestId("mini-chat");

    const circle = screen.getByTestId("node-circle") as HTMLButtonElement;
    expect(circle.hasAttribute("hidden")).toBe(true);
    expect(circle.disabled).toBe(true);
    expect(circle.tabIndex).toBe(-1);
    // A camada ativa é a única operável.
    expect((screen.getByTestId("mini-input") as HTMLTextAreaElement).disabled).toBe(false);
    expect((screen.getByTestId("mini-send") as HTMLButtonElement).tabIndex).toBe(0);
  });

  it("os alvos do Node têm 44 px ou mais (círculo e cápsula)", async () => {
    const harness = presence({ surface: "revealed" });
    render(<NodeSurface />);
    const circle = await screen.findByTestId("node-circle");
    expect(parseFloat(circle.style.width)).toBeGreaterThanOrEqual(44);
    expect(parseFloat(circle.style.height)).toBeGreaterThanOrEqual(44);

    harness.emitNode({ surface: "pill", transition_id: 2 });
    await waitFor(() => expect((screen.getByTestId("node-pill") as HTMLElement).hasAttribute("hidden")).toBe(false));
    const pill = screen.getByTestId("node-pill");
    expect(parseFloat(pill.style.height)).toBeGreaterThanOrEqual(44);
  });
});

describe("NodeSurface — o chat real dentro do objeto", () => {
  it("a região do chat mostra a projeção canónica e o rascunho partilhado", async () => {
    presence(
      { surface: "chat" },
      chatProjection({ cursor: 2, messages: [
        { role: "user", text: "um", message_id: "m1", task_id: "t1" },
        { role: "assistant", text: "dois", message_id: "m2", task_id: "t1" },
      ] })
    );
    render(<NodeSurface />);
    await screen.findByTestId("mini-chat");
    await waitFor(() => expect(screen.getAllByTestId(/^mini-message-/)).toHaveLength(2));
    expect(screen.getAllByTestId(/^mini-message-/).map(bubble => bubble.textContent)).toEqual(["um", "dois"]);

    const input = screen.getByTestId("mini-input") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "olá" } });
    expect((window as unknown as { lunmiPresence: { setDraft: ReturnType<typeof vi.fn> } }).lunmiPresence.setDraft).toHaveBeenCalledWith("olá");
  });

  it("pendência continua a pedir «Abrir tarefa» na principal e não abre aprovação no Node", async () => {
    const harness = presence(
      { surface: "chat" },
      chatProjection({ cursor: 1, confirm: { request_id: "r1", kind: "confirm", prompt: "Apagar pasta?", options: [] } as never })
    );
    render(<NodeSurface />);
    const pending = await screen.findByTestId("mini-pending");
    expect(pending.textContent).toContain("Abrir tarefa");
    fireEvent.click(screen.getByTestId("mini-open-task"));
    expect(harness.actions).toContain("show-main");
    expect(harness.commands.filter(command => command.type === "task.confirm")).toHaveLength(0);
  });

  it("fechar e Esc pedem node-close e nunca cancelam a tarefa", async () => {
    const harness = presence({ surface: "chat" });
    render(<NodeSurface />);
    await screen.findByTestId("mini-chat");

    fireEvent.click(screen.getByTestId("mini-close"));
    expect(harness.actions).toEqual(["node-close"]);

    fireEvent.keyDown(screen.getByTestId("lunmi-node"), { key: "Escape" });
    expect(harness.actions).toEqual(["node-close", "node-close"]);
    expect(harness.commands.filter(command => command.type === "task.cancel")).toHaveLength(0);
  });
});

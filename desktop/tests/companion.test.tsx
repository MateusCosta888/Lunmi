/**
 * Testes renderer do companheiro (`desktop/tests/companion.test.tsx`).
 *
 * Nenhum Electron real: `window.lunmiPresence` é uma ponte espiã com snapshot/eventos controlados.
 *
 * NODE V1: a janela companheira deixou de ter o par orb/mini e passou a montar o objeto contínuo
 * (`NodeSurface`); o caminho morto do `OrbSurface` desapareceu com ele — o N1 não tem arrasto
 * livre e a forma é a shape nativa do main. As provas da superfície (objeto contínuo, um único
 * `node-open`, estados do main, corpo preto) estão em `desktop/tests/node-surface.test.tsx`.
 *
 * Aqui ficam as provas do conteúdo real: a projeção canónica (snapshot + deltas por cursor),
 * rascunho partilhado com `setDraft` e preservação em recusa, Enter com IME/busy não envia,
 * confirmação pendente só com «Abrir tarefa» (show-main) e sem aprovação no mini, ações
 * fechadas exatas, voz só mostra/para, e a opção residente em Configurações aparece só quando
 * disponível com falha honesta.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CompanionApp } from "../src/features/companion/CompanionApp";
import { MiniChat } from "../src/features/companion/MiniChat";
import { PreferencesPanel } from "../src/features/preferences/PreferencesPanel";
import type {
  ChatProjectionDto,
  NodeStateDto,
  PresenceEventDto,
  PresenceSnapshotDto,
  SessionReadyPayload,
} from "../src/shared/types";

// jsdom não traz PointerEvent; sem construtor, o fireEvent perde clientX/clientY nos eventos de
// ponteiro. Stub local deste ficheiro (não do setup global): pointer events caem no MouseEvent.
if (!("PointerEvent" in globalThis)) {
  (globalThis as Record<string, unknown>).PointerEvent = (globalThis as Record<string, unknown>).MouseEvent;
}

const SESSION: SessionReadyPayload = {
  v: 1, session_id: "sess-c", pid: 1,
  capabilities: { converse: true, execute: true, memory: true, brain: true, web_search: false, browser_use: false, auto: true },
  limits: { max_line_bytes: 5 * 1024 * 1024, max_import_bytes: 4 * 1024 * 1024, max_chat_text_chars: 8000 },
};

function chatProjection(overrides: Partial<ChatProjectionDto> = {}): ChatProjectionDto {
  return { session_id: "sess-c", cursor: 0, messages: [], recent_tasks: [], task: null, confirm: null, input: null, ...overrides };
}

function nodeState(overrides: Partial<NodeStateDto> = {}): NodeStateDto {
  return {
    v: 1, enabled: true, side: "right", surface: "docked", phase: "settled", transition_id: 1,
    transition: null, status: null, tone: "neutral", session_id: "sess-c", ...overrides,
  };
}

interface Harness {
  snapshot: PresenceSnapshotDto;
  actions: string[];
  commands: Array<{ type: string; payload: Record<string, unknown> }>;
  drafts: string[];
  emit: (event: PresenceEventDto) => void;
  setChat: (chat: ChatProjectionDto) => void;
}

function presence(initial: Partial<PresenceSnapshotDto> = {}, commandImpl?: (type: string, payload: Record<string, unknown>) => Promise<unknown>): Harness {
  const snapshot: PresenceSnapshotDto = {
    mode: "hidden", resident: true, session: SESSION, chat: chatProjection(), draft: "", theme: "celestial", voice: null,
    node: nodeState(), ...initial,
  };
  const listeners = new Set<(event: PresenceEventDto) => void>();
  const harness: Harness = {
    snapshot,
    actions: [],
    commands: [],
    drafts: [],
    emit: event => act(() => { for (const listener of [...listeners]) listener(event); }),
    setChat: chat => { snapshot.chat = chat; },
  };
  (window as unknown as { lunmiPresence: unknown }).lunmiPresence = {
    snapshot: vi.fn(async () => ({ ...snapshot, chat: { ...snapshot.chat } })),
    action: vi.fn(async (action: string) => { harness.actions.push(action); return { ok: true, mode: snapshot.mode, resident: snapshot.resident }; }),
    setDraft: vi.fn(async (text: string) => { harness.drafts.push(text); snapshot.draft = text; return { ok: true }; }),
    sendCommand: vi.fn(async (type: string, payload: Record<string, unknown> = {}) => {
      harness.commands.push({ type, payload });
      if (commandImpl) return commandImpl(type, payload);
      throw new Error(`unexpected_command: ${type}`);
    }),    onEvent: (cb: (event: PresenceEventDto) => void) => { listeners.add(cb); return () => { listeners.delete(cb); }; },
    dragBy: vi.fn(async () => ({ ok: true })),
    endDrag: vi.fn(async () => ({ ok: true })),
    hitTest: vi.fn(async () => ({ ok: true })),
    setResident: vi.fn(async (resident: boolean) => { snapshot.resident = resident; return { resident, companion: null }; }),
  };
  return harness;
}

function api() {
  return (window as unknown as { lunmiPresence: { dragBy: ReturnType<typeof vi.fn>; endDrag: ReturnType<typeof vi.fn>; hitTest: ReturnType<typeof vi.fn>; setResident: ReturnType<typeof vi.fn> } }).lunmiPresence;
}

afterEach(() => {
  delete (window as unknown as { lunmiPresence?: unknown }).lunmiPresence;
  delete (window as unknown as { lunmi?: unknown }).lunmi;
  delete document.documentElement.dataset.companion;
});

describe("CompanionApp — a janela do Node", () => {
  it("monta o objeto contínuo do Node; o caminho morto do orb desapareceu", async () => {
    presence({ node: nodeState({ surface: "revealed" }) });
    render(<CompanionApp />);
    await screen.findByTestId("lunmi-node");
    expect(screen.queryByTestId("companion-orb")).toBeNull();
    // O chat real está montado (o objeto não é remontado) mas só é visível na superfície chat.
    expect((screen.getByTestId("node-chat") as HTMLElement).hasAttribute("hidden")).toBe(true);
    expect((screen.getByTestId("node-circle") as HTMLElement).hasAttribute("hidden")).toBe(false);
  });

  it("tema vem do snapshot e troca por evento aplica sem recarregar a janela", async () => {
    const harness = presence({ theme: "soft-light" });
    render(<CompanionApp />);
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("soft-light"));
    harness.emit({ type: "theme", theme: "red-velvet" } as PresenceEventDto);
    await waitFor(() => expect(document.documentElement.dataset.theme).toBe("red-velvet"));
  });
});

describe("MiniChat — conteúdo real", () => {
  it("mostra a projeção do snapshot: três turnos, uma bolha por message_id", async () => {
    presence({ chat: chatProjection({ cursor: 3, messages: [
      { role: "user", text: "um", message_id: "m1", task_id: "t1" },
      { role: "assistant", text: "dois", message_id: "m2", task_id: "t1" },
      { role: "user", text: "tres", message_id: "m3", task_id: "t2" },
    ] }) });
    render(<MiniChat />);
    await screen.findByTestId("mini-chat");
    await waitFor(() => expect(screen.getAllByTestId(/^mini-message-/)).toHaveLength(3));
    expect(screen.getAllByTestId(/^mini-message-/).map(bubble => bubble.textContent)).toEqual(["um", "dois", "tres"]);
  });

  it("delta com cursor maior acrescenta; cursor antigo é descartado sem eco", async () => {
    const harness = presence({ chat: chatProjection({ cursor: 1, messages: [{ role: "user", text: "base", message_id: "m1" }] }) });
    render(<MiniChat />);
    await screen.findByTestId("mini-chat");
    harness.emit({ type: "chat", delta: { session_id: "sess-c", cursor: 1, messages: [{ role: "user", text: "DUPLICADO", message_id: "m9" }] } });
    expect(screen.queryByText("DUPLICADO")).toBeNull();
    harness.emit({ type: "chat", delta: { session_id: "sess-c", cursor: 2, messages: [{ role: "assistant", text: "novo", message_id: "m2" }] } });
    await waitFor(() => expect(screen.getByText("novo")).toBeTruthy());
    expect(screen.queryByText("DUPLICADO")).toBeNull();
  });

  it("sessão nova (restart) descarta a conversa anterior", async () => {
    const harness = presence({ chat: chatProjection({ cursor: 5, messages: [{ role: "user", text: "antiga", message_id: "m1" }] }) });
    render(<MiniChat />);
    await screen.findByTestId("mini-chat");
    const nextSession = { ...SESSION, session_id: "sess-d" } as SessionReadyPayload;
    harness.setChat({ session_id: "sess-d", cursor: 0, messages: [], recent_tasks: [], task: null, confirm: null, input: null });
    harness.emit({ type: "session", session: nextSession });
    await waitFor(() => expect(screen.queryByText("antiga")).toBeNull());
  });

  it("escrever partilha rascunho via setDraft; Enter envia chat.send {mode,text} e limpa só após aceite", async () => {
    const harness = presence({}, async (type) => {
      if (type === "chat.send") return { accepted: true };
      throw new Error(`unexpected_command: ${type}`);
    });
    render(<MiniChat />);
    const input = (await screen.findByTestId("mini-input")) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "olá lunmi" } });
    expect(harness.drafts).toContain("olá lunmi");
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(harness.commands.some(c => c.type === "chat.send" && c.payload.text === "olá lunmi" && c.payload.mode === "auto")).toBe(true));
    await waitFor(() => expect(input.value).toBe(""));
  });

  it("rascunho digitado durante o await do envio não é apagado pelo aceite", async () => {
    let resolveSend!: (value: unknown) => void;
    const harness = presence({}, async (type) => {
      if (type === "chat.send") return await new Promise(res => { resolveSend = res; });
      throw new Error(`unexpected_command: ${type}`);
    });
    render(<MiniChat />);
    const input = (await screen.findByTestId("mini-input")) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "primeiro" } });
    fireEvent.click(screen.getByTestId("mini-send"));
    await waitFor(() => expect(harness.commands.some(c => c.type === "chat.send")).toBe(true));
    // o utilizador escreve outro texto enquanto o envio está em curso
    fireEvent.change(input, { target: { value: "primeiro mais isto" } });
    act(() => { resolveSend({ accepted: true }); });
    await waitFor(() => expect(input.value).toBe("primeiro mais isto"));
    expect(harness.drafts).toContain("primeiro mais isto");
  });

  it("recusa de envio preserva o texto digitado e mostra erro honesto", async () => {
    presence({}, async () => { throw new Error("forbidden_command: o companheiro não pode enviar isto"); });
    render(<MiniChat />);
    const input = (await screen.findByTestId("mini-input")) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "manter aqui" } });
    fireEvent.click(screen.getByTestId("mini-send"));
    await waitFor(() => expect(screen.getByTestId("mini-error").textContent).toContain("não pode enviar"));
    expect(input.value).toBe("manter aqui");
  });

  it("Enter durante IME ou com busy não envia", async () => {
    const harness = presence({}, async () => ({ accepted: true }));
    render(<MiniChat />);
    const input = (await screen.findByTestId("mini-input")) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "compondo" } });
    fireEvent.keyDown(input, { key: "Enter", isComposing: true, keyCode: 229 });
    expect(harness.commands.filter(c => c.type === "chat.send")).toHaveLength(0);
  });

  it("confirmação pendente: só «Abrir tarefa» (show-main); nenhum caminho de aprovação no mini", async () => {
    const harness = presence({ chat: chatProjection({ cursor: 1, confirm: { request_id: "r1", kind: "confirm", prompt: "Apagar pasta?", options: [] } as never }) });
    render(<MiniChat />);
    const pending = await screen.findByTestId("mini-pending");
    expect(pending.textContent).toContain("Abrir tarefa");
    fireEvent.click(screen.getByTestId("mini-open-task"));
    expect(harness.actions).toContain("show-main");
    expect(harness.commands.filter(c => c.type === "task.confirm")).toHaveLength(0);
    expect(pending.textContent).not.toMatch(/aprovar|confirmar aqui|sim\b/i);
  });

  it("confirmação que chega depois das mensagens fica FIXADA fora da lista e continua acessível", async () => {
    // Critério migrado (N3): a pendência deixou de ser brought-to-view por scroll — está
    // fixada numa barra fora da lista que rola, logo é alcançável em qualquer posição de
    // scroll (critério equivalente ou mais forte: não depende de o leitor estar no fundo).
    const harness = presence({ chat: chatProjection({ cursor: 1, messages: [{ role: "user", text: "apaga a pasta", message_id: "m1", task_id: "t1" }] }) });
    render(<MiniChat />);
    await screen.findByTestId("mini-chat");
    harness.emit({ type: "chat", delta: {
      session_id: "sess-c", cursor: 2,
      confirm: { request_id: "r1", kind: "confirm", prompt: "Apagar pasta?", options: [] } as never,
      task: { task_id: "t1", mode: "auto", instruction: "apaga a pasta", phase: "awaiting_confirm", progress: "", result: null, error: null },
    } });
    const pending = await screen.findByTestId("mini-pending");
    const dock = screen.getByTestId("mini-pending-dock");
    const list = screen.getByTestId("mini-messages");
    expect(dock.contains(pending)).toBe(true);
    expect(list.contains(pending)).toBe(false);
    // E a ação continua a funcionar a partir da barra fixada.
    fireEvent.click(screen.getByTestId("mini-open-task"));
    expect(harness.actions).toContain("show-main");
  });

  it("definições → show-settings; fechar → hide-mini (ação legada sem dono da superfície)", async () => {
    const harness = presence();
    render(<MiniChat />);
    await screen.findByTestId("mini-chat");
    fireEvent.click(screen.getByTestId("mini-settings"));
    fireEvent.click(screen.getByTestId("mini-close"));
    expect(harness.actions).toEqual(["show-settings", "hide-mini"]);
  });

  it("estado de voz vem dos eventos; Parar voz usa a ação fechada, nunca síntese própria", async () => {
    const harness = presence();
    render(<MiniChat />);
    await screen.findByTestId("mini-chat");
    expect((screen.getByTestId("mini-stop-voice") as HTMLButtonElement).disabled).toBe(true);
    harness.emit({ type: "voice", state: { session_id: "sess-c", enabled: true, auto_speak: false, available: true, state: "playing", generation: 4, can_speak_last: true, capture_supported: false, message: "" } });
    await waitFor(() => expect((screen.getByTestId("mini-stop-voice") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByTestId("mini-stop-voice"));
    expect(harness.actions).toContain("stop-voice");
    expect(harness.commands.filter(c => c.type === "voice.speak_last")).toHaveLength(0);
  });
});

describe("MiniChat — N3: scroll, erro local, cancel e alvos", () => {
  /** Lista com métricas de scroll simuladas (o jsdom não tem layout). */
  function scrollableList(): HTMLDivElement {
    const list = screen.getByTestId("mini-messages") as HTMLDivElement;
    Object.defineProperty(list, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(list, "clientHeight", { value: 200, configurable: true });
    Object.defineProperty(list, "scrollTop", { value: 0, writable: true, configurable: true });
    return list;
  }

  it("as mensagens novas não puxam o leitor que se afastou do fundo", async () => {
    const harness = presence();
    render(<MiniChat />);
    await screen.findByTestId("mini-chat");
    const list = scrollableList();

    // O leitor sobe (distância ao fundo = 800 px): já não está no fundo.
    fireEvent.scroll(list);
    harness.emit({ type: "chat", delta: { session_id: "sess-c", cursor: 1, messages: [{ role: "assistant", text: "nova", message_id: "m1" }] } });
    await waitFor(() => expect(screen.getByText("nova")).toBeTruthy());
    expect(list.scrollTop).toBe(0);
  });

  it("com o leitor no fundo, a lista continua a seguir as mensagens novas", async () => {
    const harness = presence();
    render(<MiniChat />);
    await screen.findByTestId("mini-chat");
    const list = scrollableList();

    list.scrollTop = 800; // distância ao fundo = 0 → está no fundo
    fireEvent.scroll(list);
    harness.emit({ type: "chat", delta: { session_id: "sess-c", cursor: 1, messages: [{ role: "assistant", text: "nova", message_id: "m1" }] } });
    await waitFor(() => expect(screen.getByText("nova")).toBeTruthy());
    expect(list.scrollTop).toBe(1000);
  });

  it("o erro local é limpo numa nova tentativa", async () => {
    let refuse = true;
    const harness = presence({}, async (type) => {
      if (type === "chat.send") {
        if (refuse) {
          refuse = false;
          throw new Error("forbidden_command: o companheiro não pode enviar isto");
        }
        return { accepted: true };
      }
      throw new Error(`unexpected_command: ${type}`);
    });
    render(<MiniChat />);
    const input = await screen.findByTestId("mini-input");
    fireEvent.change(input, { target: { value: "olá" } });
    fireEvent.click(screen.getByTestId("mini-send"));
    await waitFor(() => expect(screen.getByTestId("mini-error")).toBeTruthy());

    // Nova tentativa: o erro antigo não continua no ecrã (a árvore fica sempre montada).
    fireEvent.click(screen.getByTestId("mini-send"));
    await waitFor(() => expect(screen.queryByTestId("mini-error")).toBeNull());
    expect(harness.commands.filter(c => c.type === "chat.send")).toHaveLength(2);
  });

  it("o erro local é limpo numa nova sessão", async () => {
    const harness = presence({}, async (type) => {
      if (type === "chat.send") throw new Error("forbidden_command: o companheiro não pode enviar isto");
      throw new Error(`unexpected_command: ${type}`);
    });
    render(<MiniChat />);
    const input = await screen.findByTestId("mini-input");
    fireEvent.change(input, { target: { value: "olá" } });
    fireEvent.click(screen.getByTestId("mini-send"));
    await waitFor(() => expect(screen.getByTestId("mini-error")).toBeTruthy());

    harness.emit({ type: "session", session: { ...SESSION, session_id: "sess-e" } as SessionReadyPayload });
    await waitFor(() => expect(screen.queryByTestId("mini-error")).toBeNull());
  });

  it("Parar (tarefa) e Parar voz são acções distintas", async () => {
    const harness = presence(
      { chat: chatProjection({ cursor: 1, task: { task_id: "t1", mode: "auto", instruction: "faz", phase: "running", progress: "", result: null, error: null } }) },
      async (type) => {
        if (type === "task.cancel") return { ok: true };
        throw new Error(`unexpected_command: ${type}`);
      }
    );
    render(<MiniChat />);
    await screen.findByTestId("mini-chat");
    fireEvent.click(await screen.findByTestId("mini-stop"));
    await waitFor(() => expect(harness.commands.some(c => c.type === "task.cancel")).toBe(true));
    expect(harness.actions).not.toContain("stop-voice");
  });

  it("o histórico de três turnos fica e o stream não apaga o que já foi dito", async () => {
    const harness = presence({ chat: chatProjection({ cursor: 3, messages: [
      { role: "user", text: "um", message_id: "m1", task_id: "t1" },
      { role: "assistant", text: "dois", message_id: "m2", task_id: "t1" },
      { role: "user", text: "tres", message_id: "m3", task_id: "t2" },
    ] }) });
    render(<MiniChat />);
    await screen.findByTestId("mini-chat");
    await waitFor(() => expect(screen.getAllByTestId(/^mini-message-/)).toHaveLength(3));

    harness.emit({ type: "chat", delta: { session_id: "sess-c", cursor: 4, messages: [{ role: "assistant", text: "quatro", message_id: "m4", task_id: "t2" }] } });
    await waitFor(() => expect(screen.getAllByTestId(/^mini-message-/)).toHaveLength(4));
    expect(screen.getAllByTestId(/^mini-message-/).map(bubble => bubble.textContent)).toEqual(["um", "dois", "tres", "quatro"]);
  });

  it("os alvos de toque do chat têm 44 px (regra estática; a medição real é o gate do Root)", async () => {
    const candidates = [
      resolve(process.cwd(), "src/features/companion/companion.css"),
      resolve(process.cwd(), "desktop/src/features/companion/companion.css"),
    ];
    const path = candidates.find(candidate => existsSync(candidate));
    expect(path).toBeTruthy();
    const css = readFileSync(path as string, "utf8");
    expect(css).toMatch(/\.lunmi-mini-icon-button\s*\{[^}]*width:\s*44px[^}]*height:\s*44px/);
    expect(css).toMatch(/\.lunmi-mini-button\s*\{[^}]*min-height:\s*44px/);
    expect(css).toMatch(/\.lunmi-mini-composer textarea\s*\{[^}]*min-height:\s*44px/);
  });
});

describe("Configurações — opção residente", () => {
  const send = vi.fn(async (type: string) => {
    if (type === "approval.get") return { auto_approve: false, mode: "manual", label: "Manual" };
    if (type === "ui.preferences.get") return { window: null, scale_pct: 100, actions: [] };
    return { ok: true };
  });

  it("sem ponte de presença o cartão não aparece", async () => {
    render(<PreferencesPanel sendCommand={send as never} session={SESSION} />);
    await waitFor(() => expect(screen.getByTestId("approval-card")).toBeTruthy());
    expect(screen.queryByTestId("resident-card")).toBeNull();
  });

  it("com presença, alterna com booleano exato e copy honesta por estado", async () => {
    const harness = presence({ resident: true });
    render(<PreferencesPanel sendCommand={send as never} session={SESSION} />);
    const toggle = (await screen.findByTestId("resident-toggle")) as HTMLInputElement;
    expect(toggle.checked).toBe(true);
    // Cópia neutra e verdadeira (Designer de preferências): com o Node desligado, o X esconde a
    // janela e mantém a Lunmi em segundo plano — «Sair» na bandeja termina. A cópia não afirma
    // nem orb nem Node: o detalhe Node off/on vive no cartão Lunmi Node.
    const residentScope = screen.getByTestId("resident-scope").textContent ?? "";
    expect(residentScope).toMatch(/esconde a janela e mantém a Lunmi em segundo plano/i);
    expect(residentScope).not.toMatch(/orb/i);
    fireEvent.click(toggle);
    await waitFor(() => expect(api().setResident).toHaveBeenCalledWith(false));
    await waitFor(() => expect(screen.getByTestId("resident-scope").textContent).toMatch(/Fechar \(X\) termina a Lunmi/));
    expect(harness.snapshot.resident).toBe(false);
  });

  it("falha de gravação mantém o valor anterior com mensagem, sem fingir sucesso", async () => {
    presence();
    api().setResident.mockRejectedValueOnce(new Error("io_error"));
    render(<PreferencesPanel sendCommand={send as never} session={SESSION} />);
    const toggle = (await screen.findByTestId("resident-toggle")) as HTMLInputElement;
    fireEvent.click(toggle);
    await waitFor(() => expect(screen.getByTestId("resident-error").textContent).toContain("valor anterior"));
    expect(toggle.checked).toBe(true);
  });
});

/**
 * Testes do painel de memória v2 (`desktop/tests/memory.test.tsx`, MEMORIA-SIMPLES-V2).
 *
 * Nenhum backend real: `sendCommand` é um espião com as fixtures exatas de
 * `.agents/MEMORY-SIMPLE-V2-CONTRACT.md` §7. O rascunho pertence ao App; o `Harness` abaixo imita
 * esse dono (estado que sobrevive ao desmontar do painel). Provas: get/save/undo com `base_revision`
 * pegajoso do rascunho; `stale_revision` abre revisão humana e o Guardar comum fica BLOQUEADO
 * durante o conflito (só a decisão usa a revisão atual); evento com rascunho sujo não sobrescreve;
 * contador em pontos Unicode; modo off read-only; legado >8000 sem truncar; secundária só carrega
 * ao abrir e mantém dois passos/confirmed e cancelamento como não-erro.
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import React, { useState } from "react";
import { MemoryPanel, type MemoryProfileDraft } from "../src/features/memory/MemoryPanel";
import { BridgeError, type SendCommandFn } from "../src/shared/ipc/client";
import type { SessionReadyPayload } from "../src/shared/protocol";
import type { MemoryItemDto, MemoryOptionsDto, MemoryProfileDto } from "../src/shared/types";

const SESSION: SessionReadyPayload = {
  v: 1, session_id: "sess-test", pid: 4242,
  capabilities: { converse: true, execute: true, memory: true, brain: true, web_search: false, browser_use: false },
  limits: { max_line_bytes: 5 * 1024 * 1024, max_import_bytes: 4 * 1024 * 1024, max_chat_text_chars: 8000, max_profile_chars: 8000 },
};

const OPTIONS: MemoryOptionsDto = { enabled: true, episodes: true, proposals: true, history: false, auto_learn: true };

function profileDoc(overrides: Partial<MemoryProfileDto> = {}): MemoryProfileDto {
  return {
    enabled: true, auto_learn: true, text: "", revision: 0, can_undo: false,
    char_count: 0, max_chars: 8000, state: "empty", ...overrides,
  };
}

function item(id: string, text: string, kind = "preference"): MemoryItemDto {
  return { id, kind, text, status: "active", source: "user_explicit",
    updated_at: "2026-10-14T10:00:00.000000+00:00", created_at: "2026-10-14T09:00:00.000000+00:00" };
}

type Call = { type: string; payload: Record<string, unknown> };

interface HarnessState { profile: MemoryProfileDto; options: MemoryOptionsDto; }

function makeBridge(state: HarnessState, extra: (type: string, payload: Record<string, unknown>) => unknown | undefined): {
  sendCommand: SendCommandFn; calls: Call[];
} {
  const calls: Call[] = [];
  const spy = vi.fn(async (type: string, payload: Record<string, unknown> = {}) => {
    calls.push({ type, payload });
    const custom = extra(type, payload);
    if (custom !== undefined) return custom;
    if (type === "memory.profile.get") return { ...state.profile };
    if (type === "memory.options.get") return { options: { ...state.options } };
    if (type === "memory.options.set") {
      state.options = { ...(payload.options as MemoryOptionsDto) };
      state.profile.enabled = state.options.enabled !== false;
      state.profile.auto_learn = state.options.auto_learn !== false;
      return { options: { ...state.options } };
    }
    if (type === "memory.profile.save") {
      const text = String(payload.text);
      if (payload.base_revision !== state.profile.revision) throw new BridgeError("stale_revision", "O perfil mudou entretanto. Recarregue e decida.");
      state.profile = { ...state.profile, text, revision: state.profile.revision + 1, can_undo: true,
        char_count: text.length, state: text === "" ? "empty" : state.profile.state === "legacy_long" ? "legacy_long" : "ready" };
      return { ...state.profile };
    }
    if (type === "memory.profile.undo") {
      if (!state.profile.can_undo) throw new BridgeError("nothing_to_undo", "Não há alteração anterior para desfazer.");
      state.profile = { ...state.profile, text: "", revision: state.profile.revision + 1, can_undo: false, char_count: 0, state: "empty" };
      return { ...state.profile };
    }
    if (type === "memory.list" || type === "memory.search") return { enabled: true, items: [item("chunk:a1", "Prefere temas escuros")] };
    if (type === "memory.proposals") return { proposals: [item("p1", "Usa atalhos de teclado")] };
    if (type === "memory.propose") return { proposal_id: "p2" };
    if (type === "memory.accept") return { memory_id: "m9" };
    if (type === "memory.reject") return { rejected: true, id: "p1" };
    if (type === "memory.upsert") return { item: item("chunk:a1", "atualizado") };
    if (type === "memory.delete") return { deleted: true, id: "chunk:a1" };
    if (type === "memory.wipe") return { options: { ...state.options } };
    if (type === "memory.export") return { exported: true, characters: 123 };
    if (type === "memory.import") return { imported: true, inserted: 1 };
    throw new Error(`unexpected_command: ${type}`);
  });
  return { sendCommand: spy as unknown as SendCommandFn, calls };
}

/** Dono do rascunho como o App: o estado vive FORA do painel e sobrevive ao desmontar. */
function PanelHarness(props: { bridge: { sendCommand: SendCommandFn }; refreshToken?: number; show?: boolean }): React.ReactElement | null {
  const [draft, setDraft] = useState<MemoryProfileDraft | null>(null);
  if (props.show === false) return null;
  return (
    <MemoryPanel sendCommand={props.bridge.sendCommand} session={SESSION}
      refreshToken={props.refreshToken ?? 0} draft={draft} onDraftChange={setDraft} />
  );
}

function renderPanel(bridge: { sendCommand: SendCommandFn }, refreshToken = 0, show = true) {
  return render(<PanelHarness bridge={bridge} refreshToken={refreshToken} show={show} />);
}

async function waitForDoc(): Promise<void> {
  await waitFor(() => expect(screen.getByTestId("memory-profile-editor")).toBeTruthy());
}

async function openAdvanced(bridge: { sendCommand: SendCommandFn; calls: Call[] }): Promise<void> {
  expect(bridge.calls.some(c => c.type === "memory.list")).toBe(false);
  fireEvent.click(screen.getByTestId("memory-advanced-toggle"));
  await waitFor(() => expect(screen.getByTestId("memory-item-chunk:a1")).toBeTruthy());
}

describe("MemoryPanel v2 — superfície primária", () => {
  it("abre o documento vazio com o título simples, contador e ações indisponíveis", async () => {
    const bridge = makeBridge({ profile: profileDoc(), options: OPTIONS }, () => undefined);
    renderPanel(bridge);
    await waitForDoc();
    expect(screen.getByTestId("memory-panel").textContent).toContain("O que a Lunmi deve saber sobre ti");
    expect(screen.getByTestId("memory-panel").textContent?.toLowerCase()).toContain("trechos relevantes podem ser enviados ao provedor escolhido");
    expect((screen.getByTestId("memory-profile-text") as HTMLTextAreaElement).value).toBe("");
    expect(screen.getByTestId("memory-profile-count").textContent).toBe("0 / 8000");
    expect((screen.getByTestId("memory-profile-save") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("memory-profile-undo") as HTMLButtonElement).disabled).toBe(true);
    expect(bridge.calls.find(c => c.type === "memory.profile.get")!.payload).toEqual({});
    expect(bridge.calls.some(c => c.type === "memory.list" || c.type === "memory.proposals")).toBe(false);
  });

  it("guarda texto livre com base_revision e confirma só após a resposta", async () => {
    const state = { profile: profileDoc(), options: OPTIONS };
    const bridge = makeBridge(state, () => undefined);
    renderPanel(bridge);
    await waitForDoc();
    fireEvent.change(screen.getByTestId("memory-profile-text"), { target: { value: "Prefiro respostas curtas." } });
    expect(screen.getByTestId("memory-draft-hint")).toBeTruthy();
    fireEvent.click(screen.getByTestId("memory-profile-save"));
    await waitFor(() => {
      const call = bridge.calls.find(c => c.type === "memory.profile.save");
      expect(call!.payload).toEqual({ text: "Prefiro respostas curtas.", base_revision: 0 });
    });
    await waitFor(() => expect(screen.getByTestId("memory-notice").textContent).toContain("Guardado"));
    expect(screen.getByTestId("memory-profile-count").textContent).toBe("25 / 8000");
    expect((screen.getByTestId("memory-profile-save") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("memory-profile-undo") as HTMLButtonElement).disabled).toBe(false);
    fireEvent.change(screen.getByTestId("memory-profile-text"), { target: { value: "Outro rascunho." } });
    expect(screen.queryByTestId("memory-notice")).toBeNull();
    expect(screen.getByTestId("memory-draft-hint")).toBeTruthy();
  });

  it("Enter é texto, não item: parágrafos viajam byte a byte no payload", async () => {
    const state = { profile: profileDoc(), options: OPTIONS };
    const bridge = makeBridge(state, () => undefined);
    renderPanel(bridge);
    await waitForDoc();
    const multi = "Prefiro respostas curtas.\n\nUso Python no trabalho.\nListas com - continuam iguais.";
    fireEvent.change(screen.getByTestId("memory-profile-text"), { target: { value: multi } });
    fireEvent.click(screen.getByTestId("memory-profile-save"));
    await waitFor(() => {
      const call = bridge.calls.find(c => c.type === "memory.profile.save");
      expect(call!.payload.text).toBe(multi);
    });
  });

  it("contador usa pontos Unicode (emoji = 1), como o backend", async () => {
    const state = { profile: profileDoc(), options: OPTIONS };
    const bridge = makeBridge(state, () => undefined);
    renderPanel(bridge);
    await waitForDoc();
    fireEvent.change(screen.getByTestId("memory-profile-text"), { target: { value: "café 😀 ok" } });
    expect(screen.getByTestId("memory-profile-count").textContent).toBe("9 / 8000");
  });

  it("desfazer obedece a can_undo, envia base_revision atual e depois fica indisponível", async () => {
    const state = { profile: profileDoc({ text: "Prefiro respostas curtas.", revision: 1, can_undo: true, char_count: 25, state: "ready" }), options: OPTIONS };
    const bridge = makeBridge(state, () => undefined);
    renderPanel(bridge);
    await waitForDoc();
    fireEvent.click(screen.getByTestId("memory-profile-undo"));
    await waitFor(() => {
      const call = bridge.calls.find(c => c.type === "memory.profile.undo");
      expect(call!.payload).toEqual({ base_revision: 1 });
    });
    await waitFor(() => expect(screen.getByTestId("memory-notice").textContent).toContain("desfeita"));
    expect((screen.getByTestId("memory-profile-undo") as HTMLButtonElement).disabled).toBe(true);
  });

  it("nothing_to_undo não quebra: aviso honesto, sem estado de erro", async () => {
    const state = { profile: profileDoc({ text: "x", revision: 3, can_undo: true, char_count: 1, state: "ready" }), options: OPTIONS };
    const bridge = makeBridge(state, (type) => {
      if (type === "memory.profile.undo") return Promise.reject(new BridgeError("nothing_to_undo", "Não há alteração anterior para desfazer."));
      return undefined;
    });
    renderPanel(bridge);
    await waitForDoc();
    fireEvent.click(screen.getByTestId("memory-profile-undo"));
    await waitFor(() => expect(screen.getByTestId("memory-notice").textContent).toContain("desfazer"));
    expect(screen.queryByTestId("memory-error")).toBeNull();
  });

  it("stale_revision: zero escrita, get refeito e revisão humana com os dois textos", async () => {
    const state = { profile: profileDoc({ text: "Prefiro respostas curtas.", revision: 1, can_undo: true, char_count: 25, state: "ready" }), options: OPTIONS };
    let forcedStale = false;
    const bridge = makeBridge(state, (type) => {
      if (type === "memory.profile.get" && forcedStale) {
        forcedStale = false;
        return { ...state.profile, text: "A Lunmi aprendeu: usa tema escuro.", revision: 2, can_undo: true, char_count: 35, state: "ready" };
      }
      if (type === "memory.profile.save") {
        if (forcedStale) return Promise.reject(new BridgeError("stale_revision", "O perfil mudou entretanto. Recarregue e decida."));
        forcedStale = true;
        return undefined;
      }
      return undefined;
    });
    renderPanel(bridge);
    await waitForDoc();
    fireEvent.change(screen.getByTestId("memory-profile-text"), { target: { value: "Meu texto meu" } });
    fireEvent.click(screen.getByTestId("memory-profile-save"));
    await waitFor(() => expect(bridge.calls.filter(c => c.type === "memory.profile.save").length).toBe(1));
    fireEvent.change(screen.getByTestId("memory-profile-text"), { target: { value: "Meu texto meu editado" } });
    fireEvent.click(screen.getByTestId("memory-profile-save"));
    await waitFor(() => expect(screen.getByTestId("memory-conflict")).toBeTruthy());
    expect(screen.getByTestId("memory-conflict-current").textContent).toContain("A Lunmi aprendeu");
    expect(screen.getByTestId("memory-conflict-draft").textContent).toContain("Meu texto meu editado");
    // durante o conflito o Guardar comum está bloqueado: clicar não adiciona nenhuma escrita
    expect((screen.getByTestId("memory-profile-save") as HTMLButtonElement).disabled).toBe(true);
    const savesBefore = bridge.calls.filter(c => c.type === "memory.profile.save").length;
    fireEvent.click(screen.getByTestId("memory-profile-save"));
    expect(bridge.calls.filter(c => c.type === "memory.profile.save").length).toBe(savesBefore);
    fireEvent.click(screen.getByTestId("memory-conflict-use-mine"));
    await waitFor(() => {
      const saves = bridge.calls.filter(c => c.type === "memory.profile.save");
      expect(saves[saves.length - 1].payload).toEqual({ text: "Meu texto meu editado", base_revision: 2 });
    });
    expect(screen.queryByTestId("memory-conflict")).toBeNull();
  });

  it("evento com rascunho sujo: Guardar comum fica bloqueado e nenhuma escrita sai sem decisão humana", async () => {
    const state = { profile: profileDoc({ text: "Base.", revision: 1, can_undo: false, char_count: 5, state: "ready" }), options: OPTIONS };
    const bridge = makeBridge(state, () => undefined);
    const view = renderPanel(bridge);
    await waitForDoc();
    fireEvent.change(screen.getByTestId("memory-profile-text"), { target: { value: "Meu rascunho" } });
    state.profile = { ...state.profile, text: "Base.\n\nApreendido: gosta de café.", revision: 2, can_undo: true, char_count: 40 };
    view.rerender(<PanelHarness bridge={bridge} refreshToken={1} />);
    await waitFor(() => expect(screen.getByTestId("memory-conflict")).toBeTruthy());
    expect((screen.getByTestId("memory-profile-text") as HTMLTextAreaElement).value).toBe("Meu rascunho");
    expect(screen.getByTestId("memory-conflict-draft").textContent).toBe("Meu rascunho");
    expect(screen.getByTestId("memory-conflict-current").textContent).toContain("Apreendido");
    // Guardar/tecla comum bloqueados enquanto o conflito espera decisão
    expect((screen.getByTestId("memory-profile-save") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("memory-profile-undo") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId("memory-profile-save"));
    fireEvent.keyDown(screen.getByTestId("memory-profile-text"), { key: "Enter" });
    expect(bridge.calls.filter(c => c.type === "memory.profile.save").length).toBe(0);
    // decisão humana usa a revisão ATUAL e só ela escreve
    fireEvent.click(screen.getByTestId("memory-conflict-use-mine"));
    await waitFor(() => {
      const call = bridge.calls.filter(c => c.type === "memory.profile.save").pop();
      expect(call!.payload).toEqual({ text: "Meu rascunho", base_revision: 2 });
    });
    await waitFor(() => expect(screen.queryByTestId("memory-conflict")).toBeNull());
  });

  it("rascunho sobrevive à desmontação da aba porque o dono é o harness/App", async () => {
    const state = { profile: profileDoc({ text: "Base.", revision: 1, can_undo: false, char_count: 5, state: "ready" }), options: OPTIONS };
    const bridge = makeBridge(state, () => undefined);
    const view = renderPanel(bridge);
    await waitForDoc();
    fireEvent.change(screen.getByTestId("memory-profile-text"), { target: { value: "Rascunho viajante" } });
    view.rerender(<PanelHarness bridge={bridge} show={false} />);
    expect(screen.queryByTestId("memory-profile-editor")).toBeNull();
    view.rerender(<PanelHarness bridge={bridge} show={true} />);
    await waitForDoc();
    await waitFor(() => expect((screen.getByTestId("memory-profile-text") as HTMLTextAreaElement).value).toBe("Rascunho viajante"));
    expect(screen.queryByTestId("memory-conflict")).toBeNull();
    expect(screen.getByTestId("memory-draft-hint")).toBeTruthy();
  });

  it("sem tempestade de IPC: get devolve clone novo a cada chamada e ainda assim não há polling", async () => {
    const state = { profile: profileDoc({ text: "Base.", revision: 1, can_undo: false, char_count: 5, state: "ready" }), options: OPTIONS };
    const bridge = makeBridge(state, () => undefined); // o default já retorna objeto novo por get
    const view = renderPanel(bridge);
    await waitForDoc();
    view.rerender(<PanelHarness bridge={bridge} refreshToken={0} />);
    view.rerender(<PanelHarness bridge={bridge} refreshToken={0} />);
    await new Promise(resolve => setTimeout(resolve, 400));
    const getCalls = bridge.calls.filter(c => c.type === "memory.profile.get").length;
    expect(getCalls).toBeLessThanOrEqual(2);
    expect((screen.getByTestId("memory-profile-text") as HTMLTextAreaElement).value).toBe("Base.");
  });

  it("memória desligada: caixa read-only com aviso, guardar bloqueado, opção reativa", async () => {
    const state = { profile: profileDoc({ text: "Guardado antes.", revision: 4, enabled: false, auto_learn: false, char_count: 15, state: "disabled" }), options: { ...OPTIONS, enabled: false, auto_learn: false } };
    const bridge = makeBridge(state, () => undefined);
    renderPanel(bridge);
    await waitForDoc();
    await waitFor(() => expect(screen.getByTestId("memory-disabled-notice")).toBeTruthy());
    expect((screen.getByTestId("memory-profile-text") as HTMLTextAreaElement).readOnly).toBe(true);
    expect(screen.getByTestId("memory-profile-text").textContent).toBeTruthy();
    expect((screen.getByTestId("memory-profile-save") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId("memory-option-enabled"));
    await waitFor(() => {
      const call = bridge.calls.find(c => c.type === "memory.options.set");
      expect((call!.payload.options as MemoryOptionsDto).enabled).toBe(true);
      expect(Object.values(call!.payload.options as Record<string, unknown>).every(v => typeof v === "boolean")).toBe(true);
    });
  });

  it("legado acima do teto abre integralmente com aviso de encolher, sem truncar", async () => {
    const long = "L".repeat(9000);
    const state = { profile: profileDoc({ text: long, revision: 9, can_undo: false, char_count: 9000, state: "legacy_long" }), options: OPTIONS };
    const bridge = makeBridge(state, () => undefined);
    renderPanel(bridge);
    await waitForDoc();
    expect((screen.getByTestId("memory-profile-text") as HTMLTextAreaElement).value).toBe(long);
    expect(screen.getByTestId("memory-legacy-notice").textContent).toContain("9000");
    expect(screen.getByTestId("memory-profile-count").textContent).toBe("9000 / 8000");
  });

  it("erro de política mostra a mensagem do backend sem inventar eco", async () => {
    const state = { profile: profileDoc(), options: OPTIONS };
    const bridge = makeBridge(state, (type) => {
      if (type === "memory.profile.save") return Promise.reject(new BridgeError("memory_policy_error", "Memória recusada: conteúdo privado."));
      return undefined;
    });
    renderPanel(bridge);
    await waitForDoc();
    fireEvent.change(screen.getByTestId("memory-profile-text"), { target: { value: "senha=12345" } });
    fireEvent.click(screen.getByTestId("memory-profile-save"));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("conteúdo privado"));
    expect(screen.getByRole("alert").textContent).not.toContain("12345");
  });

  it("auto_learn é opção primária e explica o comportamento", async () => {
    const state = { profile: profileDoc(), options: OPTIONS };
    const bridge = makeBridge(state, () => undefined);
    renderPanel(bridge);
    await waitForDoc();
    expect((screen.getByTestId("memory-option-auto_learn") as HTMLInputElement).checked).toBe(true);
    fireEvent.click(screen.getByTestId("memory-option-auto_learn"));
    await waitFor(() => {
      const call = bridge.calls.find(c => c.type === "memory.options.set");
      expect((call!.payload.options as MemoryOptionsDto).auto_learn).toBe(false);
    });
    await waitFor(() => expect(screen.getByTestId("memory-panel").textContent).toContain("não aprende sozinha"));
  });
});

describe("MemoryPanel v2 — gestão secundária", () => {
  it("carrega só ao abrir e mantém dois passos com confirmed:true", async () => {
    const state = { profile: profileDoc({ text: "Base.", revision: 1, char_count: 5, state: "ready" }), options: OPTIONS };
    const bridge = makeBridge(state, () => undefined);
    renderPanel(bridge);
    await waitForDoc();
    await openAdvanced(bridge);
    expect(bridge.calls.find(c => c.type === "memory.list")!.payload).toEqual({ query: "", limit: 100 });

    fireEvent.click(screen.getByTestId("memory-delete-chunk:a1"));
    expect(bridge.calls.some(c => c.type === "memory.delete")).toBe(false);
    fireEvent.click(screen.getByTestId("confirm-delete-chunk:a1"));
    await waitFor(() => {
      const call = bridge.calls.find(c => c.type === "memory.delete");
      expect(call!.payload).toEqual({ id: "chunk:a1", confirmed: true });
    });

    fireEvent.click(screen.getByTestId("wipe-memory"));
    expect(bridge.calls.some(c => c.type === "memory.wipe")).toBe(false);
    fireEvent.click(screen.getByTestId("confirm-wipe"));
    await waitFor(() => expect(bridge.calls.some(c => c.type === "memory.wipe" && c.payload.confirmed === true)).toBe(true));

    fireEvent.click(screen.getByTestId("import-memory"));
    fireEvent.click(screen.getByTestId("confirm-import"));
    await waitFor(() => {
      const call = bridge.calls.find(c => c.type === "memory.import");
      expect(call!.payload).toEqual({ confirmed: true, use_native_dialog: true });
    });
  });

  it("wipe explicito limpa o rascunho do dono — conteúdo esquecido não ressurge na volta", async () => {
    const state = { profile: profileDoc({ text: "Base.", revision: 1, char_count: 5, state: "ready" }), options: OPTIONS };
    const bridge = makeBridge(state, (type) => {
      if (type === "memory.wipe") { state.profile = profileDoc(); return { options: { ...state.options } }; }
      return undefined;
    });
    const view = renderPanel(bridge);
    await waitForDoc();
    fireEvent.change(screen.getByTestId("memory-profile-text"), { target: { value: "Rascunho a esquecer" } });
    await openAdvanced(bridge);
    fireEvent.click(screen.getByTestId("wipe-memory"));
    fireEvent.click(screen.getByTestId("confirm-wipe"));
    await waitFor(() => expect((screen.getByTestId("memory-profile-text") as HTMLTextAreaElement).value).toBe(""));
    view.rerender(<PanelHarness bridge={bridge} show={false} />);
    view.rerender(<PanelHarness bridge={bridge} show={true} />);
    await waitForDoc();
    expect((screen.getByTestId("memory-profile-text") as HTMLTextAreaElement).value).toBe("");
    expect(screen.queryByTestId("memory-draft-hint")).toBeNull();
  });

  it("buscar com kinds e limite clampado; propostas antigas aceitam/rejeitam recarregando o documento", async () => {
    const state = { profile: profileDoc({ text: "Base.", revision: 1, char_count: 5, state: "ready" }), options: OPTIONS };
    const bridge = makeBridge(state, () => undefined);
    renderPanel(bridge);
    await waitForDoc();
    await openAdvanced(bridge);

    fireEvent.change(screen.getByTestId("memory-limit"), { target: { value: "0" } });
    fireEvent.click(screen.getByTestId("memory-list-button"));
    await waitFor(() => expect([...bridge.calls].reverse().find(c => c.type === "memory.list")!.payload.limit).toBe(1));
    fireEvent.change(screen.getByTestId("memory-search"), { target: { value: "tema" } });
    fireEvent.click(screen.getByTestId("kind-filter-fact"));
    fireEvent.click(screen.getByTestId("memory-search-button"));
    await waitFor(() => {
      const search = bridge.calls.find(c => c.type === "memory.search");
      expect(search!.payload.query).toBe("tema");
      expect(search!.payload.kinds).toEqual(["fact"]);
    });

    expect(screen.getByTestId("proposal-p1")).toBeTruthy();
    fireEvent.click(screen.getByTestId("proposal-p1-accept"));
    await waitFor(() => expect(bridge.calls.some(c => c.type === "memory.accept" && c.payload.id === "p1")).toBe(true));
    await waitFor(() => expect(bridge.calls.filter(c => c.type === "memory.profile.get").length).toBeGreaterThan(1));
    fireEvent.click(screen.getByTestId("proposal-p1-reject"));
    await waitFor(() => expect(bridge.calls.some(c => c.type === "memory.reject" && c.payload.id === "p1")).toBe(true));
  });

  it("cancelar diálogo nativo continua não-erro na secundária", async () => {
    const state = { profile: profileDoc(), options: OPTIONS };
    const bridge = makeBridge(state, (type) => {
      if (type === "memory.export") return { exported: false, cancelled: true };
      if (type === "memory.import") return { imported: false, cancelled: true };
      return undefined;
    });
    renderPanel(bridge);
    await waitForDoc();
    await openAdvanced(bridge);
    fireEvent.click(screen.getByTestId("export-memory"));
    await waitFor(() => expect(screen.getByTestId("memory-notice").textContent).toMatch(/cancel/i));
    expect(screen.queryByTestId("memory-error")).toBeNull();
  });

  it("edição de parágrafo usa memory.upsert legado e recarrega o documento", async () => {
    const state = { profile: profileDoc({ text: "Base.", revision: 1, char_count: 5, state: "ready" }), options: OPTIONS };
    const bridge = makeBridge(state, () => undefined);
    renderPanel(bridge);
    await waitForDoc();
    await openAdvanced(bridge);
    fireEvent.click(screen.getByTestId("memory-edit-button-chunk:a1"));
    fireEvent.change(screen.getByTestId("memory-edit-chunk:a1"), { target: { value: "Prefere temas claros" } });
    fireEvent.click(screen.getByTestId("memory-save-chunk:a1"));
    await waitFor(() => {
      const call = bridge.calls.find(c => c.type === "memory.upsert");
      expect(call!.payload).toEqual({ id: "chunk:a1", text: "Prefere temas claros" });
    });
    await waitFor(() => expect(bridge.calls.filter(c => c.type === "memory.profile.get").length).toBeGreaterThan(1));
  });
});

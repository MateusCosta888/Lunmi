/**
 * Testes do cartão Lunmi Node em Configurações (`desktop/tests/node-preferences.test.tsx`, N1).
 *
 * Nenhum Electron real: `window.lunmiPresence` é uma ponte espiã. Provas do contrato
 * `.agents/LUNMI-NODE-V1-CONTRACT.md`: o cartão só aparece quando a ponte expõe
 * `setNodePreferences` e o snapshot traz `node_preferences`; os patches são exatos em snake_case
 * (`auto_dock`/`show_activity`); sem estado otimista — o valor muda só no resultado canónico do
 * main (ou em evento `node-prefs`), falha de disco/atalho mantém o anterior com erro honesto;
 * busy desativa controlos; a cópia de semântica do X segue resident × enabled; defaults do contrato.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PreferencesPanel } from "../src/features/preferences/PreferencesPanel";
import type {
  NodePreferencesDto,
  NodePreferencesResultDto,
  PresenceEventDto,
  PresenceSnapshotDto,
  SessionReadyPayload,
} from "../src/shared/types";
import type { SendCommandFn } from "../src/shared/ipc/client";

const SESSION: SessionReadyPayload = {
  v: 1, session_id: "sess-n", pid: 1,
  capabilities: { converse: true, execute: true, memory: true, brain: true, web_search: false, browser_use: false, auto: true },
  limits: { max_line_bytes: 5 * 1024 * 1024, max_import_bytes: 4 * 1024 * 1024, max_chat_text_chars: 8000 },
};

/** Defaults fixados no contrato N1. */
function nodePrefs(overrides: Partial<NodePreferencesDto> = {}): NodePreferencesDto {
  return { enabled: true, side: "right", auto_dock: true, show_activity: true, shortcut: null, ...overrides };
}

interface Bridge {
  snapshot: PresenceSnapshotDto;
  patches: Array<Partial<NodePreferencesDto>>;
  emit: (event: PresenceEventDto) => void;
  setNodePreferences: ReturnType<typeof vi.fn>;
}

function bridge(initial: Partial<PresenceSnapshotDto> = {}, withMethod = true): Bridge {
  const snapshot: PresenceSnapshotDto = {
    mode: "hidden", resident: true, session: SESSION,
    chat: { session_id: "sess-n", cursor: 0, messages: [], recent_tasks: [], task: null, confirm: null, input: null },
    draft: "", theme: "celestial", voice: null,
    node_preferences: nodePrefs(), ...initial,
  };
  const listeners = new Set<(event: PresenceEventDto) => void>();
  const harness: Bridge = {
    snapshot,
    patches: [],
    emit: event => act(() => { for (const listener of [...listeners]) listener(event); }),
    setNodePreferences: vi.fn(),
  };
  const methods: Record<string, unknown> = {
    snapshot: vi.fn(async () => ({ ...snapshot })),
    action: vi.fn(async () => ({ ok: true })),
    onEvent: (cb: (event: PresenceEventDto) => void) => { listeners.add(cb); return () => { listeners.delete(cb); }; },
    setResident: vi.fn(async (resident: boolean) => { snapshot.resident = resident; return { resident, companion: null }; }),
  };
  if (withMethod) {
    // Comportamento canónico do main: aplica a patch ao estado do ficheiro e devolve o resultado.
    harness.setNodePreferences.mockImplementation(async (patch: Partial<NodePreferencesDto>) => {
      harness.patches.push(patch);
      const next = { ...(snapshot.node_preferences ?? nodePrefs()), ...patch };
      snapshot.node_preferences = next;
      return { ok: true, preferences: next } satisfies NodePreferencesResultDto;
    });
    methods.setNodePreferences = harness.setNodePreferences;
  }
  (window as unknown as { lunmiPresence: unknown }).lunmiPresence = methods;
  return harness;
}

const send: SendCommandFn = (async (type: string) => {
  if (type === "approval.get") return { auto_approve: false, label: "Manual" };
  if (type === "ui.preferences.get") return { actions: [], theme: "celestial", scale: 1 };
  return {};
}) as SendCommandFn;

function panel() {
  return render(<PreferencesPanel sendCommand={send} session={SESSION} />);
}

const box = (testId: string) => screen.getByTestId(testId) as HTMLInputElement;

afterEach(() => {
  delete (window as unknown as { lunmiPresence?: unknown }).lunmiPresence;
});

describe("Cartão Lunmi Node — visibilidade", () => {
  it("ponte antiga sem setNodePreferences: cartão não aparece", async () => {
    bridge({}, false);
    panel();
    await waitFor(() => expect(screen.queryByTestId("resident-card")).not.toBeNull());
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(screen.queryByTestId("node-card")).toBeNull();
  });

  it("snapshot sem node_preferences: cartão não aparece", async () => {
    bridge({ node_preferences: undefined });
    panel();
    await waitFor(() => expect(screen.queryByTestId("resident-card")).not.toBeNull());
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(screen.queryByTestId("node-card")).toBeNull();
  });

  it("defaults do contrato: ligado, canto direito, recolher e atividade ligados, sem atalho", async () => {
    bridge();
    panel();
    await screen.findByTestId("node-card");
    expect(box("node-enabled").checked).toBe(true);
    expect(box("node-side-right").checked).toBe(true);
    expect(box("node-side-left").checked).toBe(false);
    expect(box("node-auto-dock").checked).toBe(true);
    expect(box("node-show-activity").checked).toBe(true);
    expect(box("node-shortcut-input").value).toBe("");
    expect(screen.queryByTestId("node-shortcut-clear")).toBeNull();
    expect(screen.queryByTestId("node-error")).toBeNull();
  });
});

describe("Cartão Lunmi Node — patches exatos", () => {
  it("cada controlo envia exatamente a sua chave snake_case", async () => {
    const harness = bridge();
    panel();
    await screen.findByTestId("node-card");

    fireEvent.click(screen.getByTestId("node-enabled"));
    await waitFor(() => expect(harness.patches.at(-1)).toEqual({ enabled: false }));

    fireEvent.click(screen.getByTestId("node-side-left"));
    await waitFor(() => expect(harness.patches.at(-1)).toEqual({ side: "left" }));

    fireEvent.click(screen.getByTestId("node-auto-dock"));
    await waitFor(() => expect(harness.patches.at(-1)).toEqual({ auto_dock: false }));

    fireEvent.click(screen.getByTestId("node-show-activity"));
    await waitFor(() => expect(harness.patches.at(-1)).toEqual({ show_activity: false }));

    expect(harness.setNodePreferences).toHaveBeenCalledTimes(4);
  });

  it("Node desligado: lados desativados, restante continua acessível", async () => {
    bridge({ node_preferences: nodePrefs({ enabled: false }) });
    panel();
    await screen.findByTestId("node-card");
    expect(box("node-side-left").disabled).toBe(true);
    expect(box("node-side-right").disabled).toBe(true);
    expect(box("node-enabled").disabled).toBe(false);
    expect(box("node-auto-dock").disabled).toBe(false);
  });

  it("atalho: apply envia o valor aparado, clear envia null; apply sem rascunho está desativado", async () => {
    const harness = bridge();
    panel();
    await screen.findByTestId("node-card");
    const apply = screen.getByTestId("node-shortcut-apply") as HTMLButtonElement;
    expect(apply.disabled).toBe(true);

    fireEvent.change(screen.getByTestId("node-shortcut-input"), { target: { value: "  Control+Alt+Space  " } });
    expect(apply.disabled).toBe(false);
    fireEvent.click(apply);
    await waitFor(() => expect(harness.patches.at(-1)).toEqual({ shortcut: "Control+Alt+Space" }));
    expect(box("node-shortcut-input").value).toBe("Control+Alt+Space");

    fireEvent.click(await screen.findByTestId("node-shortcut-clear"));
    await waitFor(() => expect(harness.patches.at(-1)).toEqual({ shortcut: null }));
    expect(box("node-shortcut-input").value).toBe("");
  });
});

describe("Cartão Lunmi Node — sem estado otimista, falhas honestas", () => {
  it("gravação pendente: controlo desativado com valor antigo; só o resultado do main muda a UI", async () => {
    const harness = bridge();
    panel();
    await screen.findByTestId("node-card");

    const holder: { release: (() => void) | null } = { release: null };
    harness.setNodePreferences.mockImplementationOnce(() =>
      new Promise<NodePreferencesResultDto>(resolve => {
        holder.release = () => resolve({ ok: true, preferences: nodePrefs({ enabled: false }) });
      })
    );
    fireEvent.click(screen.getByTestId("node-enabled"));
    await waitFor(() => expect(harness.setNodePreferences).toHaveBeenCalled());
    expect(box("node-enabled").checked).toBe(true); // valor canónico antigo, não o pedido
    expect(box("node-enabled").disabled).toBe(true); // busy

    holder.release?.();
    await waitFor(() => expect(box("node-enabled").checked).toBe(false));
    await waitFor(() => expect(box("node-enabled").disabled).toBe(false));
  });

  it("io_error: alerta honesto e valores anteriores mantidos", async () => {
    const harness = bridge();
    panel();
    await screen.findByTestId("node-card");

    harness.setNodePreferences.mockImplementationOnce(async () => ({
      ok: false, error: "io_error" as const, preferences: nodePrefs(),
    }));
    fireEvent.click(screen.getByTestId("node-auto-dock"));
    const alert = await screen.findByTestId("node-error");
    expect(alert.textContent).toContain("gravar no disco");
    expect(alert.textContent).toContain("anteriores mantêm-se");
    expect(box("node-auto-dock").checked).toBe(true);
  });

  it("shortcut_conflict: atalho anterior conserva-se com mensagem sobre o conflito", async () => {
    const harness = bridge({ node_preferences: nodePrefs({ shortcut: "Control+Shift+L" }) });
    panel();
    await screen.findByTestId("node-card");

    harness.setNodePreferences.mockImplementationOnce(async () => ({
      ok: false, error: "shortcut_conflict" as const, preferences: nodePrefs({ shortcut: "Control+Shift+L" }),
    }));
    fireEvent.change(screen.getByTestId("node-shortcut-input"), { target: { value: "Control+Alt+Space" } });
    fireEvent.click(screen.getByTestId("node-shortcut-apply"));
    const alert = await screen.findByTestId("node-error");
    expect(alert.textContent).toContain("já está a ser usado");
    expect(box("node-shortcut-input").value).toBe("Control+Shift+L");
    expect(screen.queryByTestId("node-shortcut-clear")).not.toBeNull();
  });

  it("IPC a rebentar (sem resposta do main): erro de disco, valor inalterado", async () => {
    const harness = bridge();
    panel();
    await screen.findByTestId("node-card");
    harness.setNodePreferences.mockRejectedValueOnce(new Error("ipc_down"));
    fireEvent.click(screen.getByTestId("node-enabled"));
    const alert = await screen.findByTestId("node-error");
    expect(alert.textContent).toContain("gravar no disco");
    expect(box("node-enabled").checked).toBe(true);
  });
});

describe("Cartão Lunmi Node — fonte canónica e semântica X", () => {
  it("evento node-prefs do main atualiza o cartão", async () => {
    const harness = bridge();
    panel();
    await screen.findByTestId("node-card");

    harness.emit({ type: "node-prefs", preferences: nodePrefs({ side: "left", show_activity: false }) });
    await waitFor(() => expect(box("node-side-left").checked).toBe(true));
    expect(box("node-show-activity").checked).toBe(false);
  });

  it("cópia de semântica segue resident × enabled", async () => {
    const harness = bridge();
    panel();
    await screen.findByTestId("node-card");
    expect(screen.getByTestId("node-semantics").textContent).toContain("mostra o Node no canto");

    fireEvent.click(screen.getByTestId("node-enabled")); // desliga (main canónico aplica)
    await waitFor(() => expect(screen.getByTestId("node-semantics").textContent).toContain("bandeja"));
    expect(screen.getByTestId("node-semantics").textContent).not.toContain("termina a Lunmi");

    harness.emit({ type: "resident", resident: false });
    await waitFor(() => expect(screen.getByTestId("node-semantics").textContent).toContain("termina a Lunmi"));
  });

  it("resident on: cópia do cartão Segundo plano é neutra — o detalhe Node off/on pertence ao NodeCard", async () => {
    bridge();
    panel();
    await screen.findByTestId("node-card");
    const scope = screen.getByTestId("resident-scope").textContent ?? "";
    expect(scope).toContain("esconde a janela e mantém a Lunmi em segundo plano");
    expect(scope).not.toContain("orb"); // não afirma orb/Node: seria falso com o Node desligado
    expect(screen.getByTestId("node-semantics").textContent).toContain("mostra o Node no canto");
  });
});

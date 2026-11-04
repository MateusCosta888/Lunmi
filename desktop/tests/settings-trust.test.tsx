// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PreferencesPanel } from "../src/features/preferences/PreferencesPanel";

const session = { session_id: "s", pid: 1, capabilities: {} } as never;

function fake(autoApprove = false) {
  return vi.fn(async (type: string, _payload?: unknown) => {
    if (type === "approval.get") return { auto_approve: autoApprove, mode: "manual", label: autoApprove ? "Modo: Autoaprovação" : "Modo: Manual" };
    if (type === "ui.preferences.get") return { actions: [{ label: "Abrir", draft: "Abra o " }] };
    return { actions: [{ label: "Novo", draft: "Abra o " }] };
  });
}
const sets = (send: ReturnType<typeof fake>) => send.mock.calls.filter(([type]) => type === "ui.preferences.set");

afterEach(cleanup);

describe("confiança nas Configurações", () => {
  it("edição de atalho por gravar é guardada ao sair da página", async () => {
    const send = fake();
    const view = render(<PreferencesPanel sendCommand={send as never} session={session} />);
    fireEvent.change(await screen.findByTestId("action-label-0"), { target: { value: "Novo" } });
    expect(screen.getByTestId("actions-dirty")).toBeTruthy();
    view.unmount();
    expect(sets(send)[0][1]).toEqual({ actions: [{ label: "Novo", draft: "Abra o " }] });
  });

  it("depois de gravar não volta a enviar ao sair", async () => {
    const send = fake();
    const view = render(<PreferencesPanel sendCommand={send as never} session={session} />);
    fireEvent.change(await screen.findByTestId("action-label-0"), { target: { value: "Novo" } });
    fireEvent.click(screen.getByTestId("save-preferences"));
    await waitFor(() => expect(screen.queryByTestId("actions-dirty")).toBeNull());
    view.unmount();
    expect(sets(send)).toHaveLength(1);
  });

  it("sem edições não grava nada ao sair", async () => {
    const send = fake();
    const view = render(<PreferencesPanel sendCommand={send as never} session={session} />);
    await screen.findByTestId("action-label-0");
    view.unmount();
    expect(sets(send)).toHaveLength(0);
  });

  it("autoaprovação ligada avisa por texto e o cartão vem antes dos outros", async () => {
    render(<PreferencesPanel sendCommand={fake(true) as never} session={session} />);
    expect((await screen.findByTestId("approval-warning")).textContent).toContain("sem te perguntar");
    const card = screen.getByTestId("approval-card");
    expect(card.compareDocumentPosition(screen.getByTestId("theme-card")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("autoaprovação desligada não mostra aviso", async () => {
    render(<PreferencesPanel sendCommand={fake() as never} session={session} />);
    await screen.findByTestId("action-label-0");
    expect(screen.queryByTestId("approval-warning")).toBeNull();
  });
});

describe("lugar do cartão de atualizações", () => {
  const withUpdates = (state: unknown) => {
    (window as unknown as { lunmi: unknown }).lunmi = { updates: { get: async () => state, onState: () => () => undefined, check: async () => state, install: async () => state } };
  };
  afterEach(() => { delete (window as unknown as { lunmi?: unknown }).lunmi; });

  it("atualização pronta sobe para antes da Aparência", async () => {
    withUpdates({ phase: "ready", currentVersion: "1.0.0", version: "1.1.0" });
    render(<PreferencesPanel sendCommand={fake() as never} session={session} />);
    const card = await screen.findByTestId("updates-card");
    expect(card.compareDocumentPosition(screen.getByTestId("theme-card")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getAllByTestId("updates-card")).toHaveLength(1);
  });

  it("sem atualização fica em Sobre, depois da Aparência", async () => {
    withUpdates({ phase: "idle", currentVersion: "1.0.0" });
    render(<PreferencesPanel sendCommand={fake() as never} session={session} />);
    const card = await screen.findByTestId("updates-card");
    expect(card.compareDocumentPosition(screen.getByTestId("theme-card")) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
  });

  it("procura falhada fica em Sobre, onde a pessoa clicou", async () => {
    withUpdates({ phase: "error", currentVersion: "1.0.0", error: "offline" });
    render(<PreferencesPanel sendCommand={fake() as never} session={session} />);
    const card = await screen.findByTestId("updates-card");
    expect(card.compareDocumentPosition(screen.getByTestId("theme-card")) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
  });
});

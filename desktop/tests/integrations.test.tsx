import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import AppsPanel from "../src/features/integrations/AppsPanel";
import type { IntegrationsDto } from "../src/shared/types";
import type { Envelope, SessionReadyPayload } from "../src/shared/protocol";
import type { LunmiBridgeApi, SendCommandFn } from "../src/shared/ipc/client";

const session = { session_id: "apps-session", capabilities: { integrations: true } } as SessionReadyPayload;
const state = (name: IntegrationsDto["gmail"]["state"], epoch = 1): IntegrationsDto => ({
  gmail: { state: name, epoch, flow_id: null, error: null, account: null, accounts: [] },
});
const connected = (epoch = 2) => ({ gmail: { ...state("connected", epoch).gmail, account: { id: "account-a", email: "test@example.test" } } });
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
let listeners: Set<(event: Envelope) => void>;
function emit(dto: IntegrationsDto, sessionId = session.session_id) {
  act(() => { for (const listener of listeners) listener({ v: 1, id: "event", type: "integrations.changed", payload: { ...dto, session_id: sessionId } }); });
}
function setup(initial = state("disconnected"), handler?: (command: string) => Promise<IntegrationsDto>) {
  const send = vi.fn(async (command: string) => command === "integrations.get" ? initial
    : handler ? handler(command) : state(command === "integrations.connect" ? "connecting" : "disconnected", 2));
  const result = render(<AppsPanel session={session} sendCommand={send as SendCommandFn} />);
  return { ...result, send };
}
beforeEach(() => {
  listeners = new Set();
  window.lunmi = { onEvent: (callback: (event: Envelope) => void) => { listeners.add(callback); return () => { listeners.delete(callback); }; } } as unknown as LunmiBridgeApi;
});

describe("Apps Gmail — conta pessoal Composio", () => {
  it("carrega só quando montado, liga por clique e aguarda confirmação autoritativa", async () => {
    const { send } = setup();
    const button = await screen.findByRole("button", { name: "Ligar Gmail" });
    expect(send.mock.calls).toEqual([["integrations.get", {}]]);
    fireEvent.click(button);
    await screen.findByRole("button", { name: "Cancelar ligação" });
    expect(send.mock.calls[1]).toEqual(["integrations.connect", {}]);
    expect(screen.queryByText("test@example.test")).toBeNull();
    emit(connected());
    expect(screen.getByText("test@example.test")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Desligar da Lunmi" })).toBeTruthy();
    expect(document.querySelector("a, input[type=password]")).toBeNull();
  });

  it("peer antigo fica indisponível sem comandos", () => {
    const send = vi.fn();
    render(<AppsPanel session={{ ...session, capabilities: { ...session.capabilities, integrations: false } }} sendCommand={send} />);
    expect(screen.getByText(/não disponibiliza a ligação/)).toBeTruthy();
    expect(send).not.toHaveBeenCalled();
    expect(listeners.size).toBe(0);
  });

  it("contas múltiplas exigem seleção e enviam só o id escolhido", async () => {
    const choice = state("select_account");
    choice.gmail.accounts = [{ id: "a", email: "a@example.test" }, { id: "b", email: "b@example.test" }];
    const { send } = setup(choice, async () => connected());
    const use = await screen.findByRole("button", { name: "Usar esta conta" });
    expect(screen.getByText("Composio ligado. Escolhe qual conta Gmail queres usar.")).toBeTruthy();
    expect(screen.queryByText(/Conclui a autorização no browser/)).toBeNull();
    expect((use as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getAllByRole("radio").every(radio => !(radio as HTMLInputElement).checked)).toBe(true);
    fireEvent.click(screen.getByRole("radio", { name: "b@example.test" }));
    fireEvent.click(use);
    await screen.findByText("test@example.test");
    expect(send.mock.calls[1]).toEqual(["integrations.select", { account_id: "b" }]);
  });

  it("cancelamento só se confirma depois da resposta e falha sem ecoar segredos", async () => {
    const cancel = deferred<IntegrationsDto>();
    setup(state("gmail_connecting"), () => cancel.promise);
    fireEvent.click(await screen.findByRole("button", { name: "Cancelar ligação" }));
    expect(screen.getByText("A autorizar o Gmail…")).toBeTruthy();
    await act(async () => cancel.reject(new Error("PRIVATE_TOKEN_SENTINEL")));
    expect(screen.getByRole("alert").textContent).toMatch(/pode continuar em curso/);
    expect(document.body.textContent).not.toContain("PRIVATE_TOKEN_SENTINEL");
    expect(screen.getByText("A autorizar o Gmail…")).toBeTruthy();
  });

  it("push novo não é substituído pela resposta inicial atrasada", async () => {
    const request = deferred<IntegrationsDto>();
    render(<AppsPanel session={session} sendCommand={vi.fn(() => request.promise) as SendCommandFn} />);
    emit(connected(3));
    await act(async () => request.resolve(state("connecting", 3)));
    expect(screen.getByText("test@example.test")).toBeTruthy();
    emit(state("connecting", 2));
    expect(screen.getByText("test@example.test")).toBeTruthy();
  });

  it("isola sessão e descarta respostas depois de mudar de sessão ou desmontar", async () => {
    const first = deferred<IntegrationsDto>();
    const send = vi.fn().mockReturnValueOnce(first.promise).mockResolvedValue(state("disconnected", 0));
    const result = render(<AppsPanel session={session} sendCommand={send as SendCommandFn} />);
    result.rerender(<AppsPanel session={{ ...session, session_id: "new-session" }} sendCommand={send as SendCommandFn} />);
    await screen.findByRole("button", { name: "Ligar Gmail" });
    await act(async () => first.resolve(connected(99)));
    emit(connected(100));
    expect(screen.queryByText("test@example.test")).toBeNull();
    result.unmount(); expect(listeners.size).toBe(0);
  });

  it("reconecta e desliga sem alterar estado antes da resposta", async () => {
    const disconnect = deferred<IntegrationsDto>();
    const { send } = setup(connected(), command => command === "integrations.disconnect" ? disconnect.promise : Promise.resolve(state("connecting", 5)));
    fireEvent.click(await screen.findByRole("button", { name: "Desligar da Lunmi" }));
    expect(screen.getByText("test@example.test")).toBeTruthy();
    await act(async () => disconnect.resolve(state("disconnected", 3)));
    await screen.findByRole("button", { name: "Ligar Gmail" });
    expect(send.mock.calls[1]).toEqual(["integrations.disconnect", {}]);
    emit(state("reauth_required", 4));
    expect(screen.getByText("Reconexão necessária")).toBeTruthy();
    expect(screen.queryByText("Autorização expirada")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Reconectar" }));
    await screen.findByRole("button", { name: "Cancelar ligação" });
    expect(send.mock.calls[2]).toEqual(["integrations.connect", {}]);
  });

  it("erro de leitura permite tentar novamente sem iniciar autorização", async () => {
    const send = vi.fn().mockRejectedValueOnce(new Error("secret")).mockResolvedValue(state("disconnected"));
    render(<AppsPanel session={session} sendCommand={send as SendCommandFn} />);
    fireEvent.click(await screen.findByRole("button", { name: "Tentar novamente" }));
    await screen.findByRole("button", { name: "Ligar Gmail" });
    expect(send.mock.calls.map(call => call[0])).toEqual(["integrations.get", "integrations.get"]);
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    emit(state("unavailable", 2));
    expect(screen.getByText("Gmail ainda indisponível nesta ligação")).toBeTruthy();
    expect(screen.getByText(/Não foi possível confirmar uma conta Gmail e as ferramentas necessárias/)).toBeTruthy();
  });
});

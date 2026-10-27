/**
 * Cartão Spotify (I2) — `desktop/tests/spotify.test.tsx`.
 *
 * Só renderer: sem Electron, sem backend, sem Spotify real, sem rede, sem GUI. O contrato é o
 * de I0 (`shared/spotify.ts`); os testes provam payloads fechados, consentimento de uso único,
 * descarte de eventos/respostas antigas por sessão/epoch/revision e falhas honestas.
 */
import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SpotifyCard from "../src/features/integrations/SpotifyCard";
import AppsPanel from "../src/features/integrations/AppsPanel";
import type { IntegrationsDto } from "../src/shared/types";
import type { Envelope, SessionReadyPayload } from "../src/shared/protocol";
import type { LunmiBridgeApi, SendCommandFn } from "../src/shared/ipc/client";
import type { SpotifyStateDto } from "../src/shared/spotify";

const SESSION = "sess-0f0f0f0f0f0f0f0f";
const OTHER_SESSION = "sess-1111111111111111";
const CONSENT = `cns-${"a".repeat(32)}`;
const JOB = `spj-${"b".repeat(16)}`;

const session = {
  v: 1,
  session_id: SESSION,
  pid: 1,
  capabilities: { integrations: true, spotify: true, converse: true, execute: true, memory: true, brain: true, web_search: true, browser_use: true },
  limits: {},
} as unknown as SessionReadyPayload;

const detection: SpotifyStateDto["detection"] = {
  app: "win32", version: "supported", architecture: "x64", cli: "pinned", link: "present", app_running: true,
};

function state(overrides: Partial<SpotifyStateDto> = {}): SpotifyStateDto {
  return {
    setup: "not_configured", connection: "disabled", device: "unknown", revision: 1, epoch: 1,
    job: null, detection, error: null, ...overrides,
  };
}
const configured = (overrides: Partial<SpotifyStateDto> = {}) =>
  state({ setup: "configured", connection: "connected", device: "local", revision: 4, epoch: 2, ...overrides });

const plan = (action: "install" | "repair" | "remove") => ({
  action,
  plan_hash: "c".repeat(64),
  steps: ["detect", "obtain", "backup", "apply", "pair", "verify"],
  changes: { adds_link: action !== "remove", replaces_files: 2, backup_bytes: 5_242_880 },
  requires: { close_spotify: true, network: true, elevation: false },
  expires_in: 600,
  version_pin: "1.3.5.3058",
  cli_version: "2.46.2",
});

let listeners: Set<(event: Envelope) => void>;
function emit(spotify: SpotifyStateDto, sessionId = SESSION) {
  act(() => {
    for (const listener of listeners) {
      listener({ v: 1, id: "event", type: "spotify.changed", payload: { session_id: sessionId, spotify } });
    }
  });
}

function renderCard(send: SendCommandFn, useSession: SessionReadyPayload | null = session) {
  // Retain the complete experimental setup contract; production defaults are tested separately.
  return render(<SpotifyCard session={useSession} sendCommand={send} automaticSetupAvailable />);
}

beforeEach(() => {
  listeners = new Set();
  window.lunmi = {
    onEvent: (callback: (event: Envelope) => void) => {
      listeners.add(callback);
      return () => { listeners.delete(callback); };
    },
  } as unknown as LunmiBridgeApi;
});

describe("Spotify — cartão do painel Apps", () => {
  it("sem capability não consulta nada, não assina eventos e explica a ausência", async () => {
    const send = vi.fn();
    renderCard(send as unknown as SendCommandFn, { ...session, capabilities: { ...session.capabilities, spotify: false } });
    expect(await screen.findByText("Esta versão do backend ainda não suporta o Spotify.")).toBeTruthy();
    expect(send).not.toHaveBeenCalled();
    expect(listeners.size).toBe(0);
  });

  it("monta com um só GET e mostra o estado pronto-a-configurar sem pedir segredos", async () => {
    const send = vi.fn(async (command: string) => {
      if (command === "spotify.get") return { spotify: state() };
      throw new Error(`unexpected ${command}`);
    });
    renderCard(send as unknown as SendCommandFn);
    expect(await screen.findByRole("button", { name: "Configurar Spotify" })).toBeTruthy();
    expect(send.mock.calls).toEqual([["spotify.get", {}]]);
    expect(screen.getByText("Pronto para configurar")).toBeTruthy();
    expect(document.querySelector("a, input")).toBeNull();
    expect(screen.queryByRole("button", { name: /usar este pc/i })).toBeNull();
    expect(document.body.textContent).not.toMatch(/spicetify|hmac|45\.?871|client id/i);
  });

  it("prepara o plano com action certa, mostra resumo/consentimento e só depois aplica o token", async () => {
    const send = vi.fn(async (command: string, payload: Record<string, unknown> = {}) => {
      if (command === "spotify.get") return { spotify: state() };
      if (command === "spotify.plan") {
        expect(payload).toEqual({ action: "install" });
        return { plan: plan("install"), consent: { consent_id: CONSENT, expires_in: 600 } };
      }
      if (command === "spotify.apply") return { accepted: true, job_id: JOB, epoch: 2 };
      throw new Error(`unexpected ${command}`);
    });
    renderCard(send as unknown as SendCommandFn);
    fireEvent.click(await screen.findByRole("button", { name: "Configurar Spotify" }));
    const apply = await screen.findByRole("button", { name: "Instalar ligação" });
    expect(send.mock.calls[1]).toEqual(["spotify.plan", { action: "install" }]);
    expect(screen.getByText("Instalar a ligação da Lunmi")).toBeTruthy();
    expect(screen.getByText(/extensão local da comunidade/)).toBeTruthy();
    expect(screen.getByText(/não envia comandos musicais durante a configuração/)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/nada toca sozinho/i);
    expect(screen.getByText(/Cópia de segurança das tuas definições: 5 MB/)).toBeTruthy();
    expect(screen.getByText(/vai ter de fechar e voltar a abrir/)).toBeTruthy();
    expect(send.mock.calls.some(([command]) => command === "spotify.apply")).toBe(false);

    fireEvent.click(apply);
    await screen.findByText("Configuração aceite. A começar…");
    expect(send.mock.calls[2]).toEqual(["spotify.apply", { consent_id: CONSENT }]);
    expect(screen.queryByRole("button", { name: "Instalar ligação" })).toBeNull();
  });

  it("mostra os estágios reais do job, sem percentagem e sem autoplay", async () => {
    const send = vi.fn(async (command: string) => {
      if (command === "spotify.get") return { spotify: state({ job: { id: JOB, action: "install", stage: "backup", status: "running", error_code: null }, revision: 2 }) };
      throw new Error(`unexpected ${command}`);
    });
    renderCard(send as unknown as SendCommandFn);
    const steps = await screen.findByTestId("spotify-steps");
    expect(screen.getByText("A configurar…")).toBeTruthy();
    expect(screen.getByText("Guardar cópia das tuas definições")).toBeTruthy();
    expect(screen.getByText("Verificar o Spotify deste PC")).toBeTruthy();
    const current = steps.querySelector('[aria-current="step"]');
    expect(current?.getAttribute("data-step")).toBe("backup");
    expect(steps.querySelectorAll('[data-state="done"]').length).toBe(2);
    expect(document.body.textContent).not.toMatch(/\d+\s?%/);
    expect(screen.getByRole("button", { name: "Cancelar" })).toBeTruthy();
  });

  it("rollback mostra a etapa real sem dar verify como concluído", async () => {
    const send = vi.fn(async () => ({
      spotify: state({
        setup: "needs_repair", revision: 8,
        job: { id: JOB, action: "install", stage: "rollback", status: "running", error_code: null },
      }),
    }));
    renderCard(send as unknown as SendCommandFn);
    expect(await screen.findByText("Reverter para a cópia guardada")).toBeTruthy();
    const steps = screen.getByTestId("spotify-steps");
    expect(steps.querySelector('[aria-current="step"]')?.getAttribute("data-step")).toBe("rollback");
    expect(steps.querySelectorAll('[data-state="done"]').length).toBe(0);
    expect(steps.querySelector('[data-step="verify"][data-state="done"]')).toBeNull();
  });

  it("remover não apresenta pair como passo nem o dá como concluído", async () => {
    const send = vi.fn(async () => ({
      spotify: configured({
        revision: 9,
        job: { id: JOB, action: "remove", stage: "backup", status: "running", error_code: null },
      }),
    }));
    renderCard(send as unknown as SendCommandFn);
    expect(await screen.findByText("A remover a ligação…")).toBeTruthy();
    const steps = screen.getByTestId("spotify-steps");
    expect(screen.queryByText("Ligar a Lunmi ao Spotify")).toBeNull();
    expect(steps.querySelector('[data-step="pair"]')).toBeNull();
    expect(steps.querySelector('[data-step="backup"]')?.getAttribute("data-state")).toBe("current");
  });

  it("cancela com o job_id do estado e não inventa um segundo resultado", async () => {
    const send = vi.fn(async (command: string) => {
      if (command === "spotify.get") return { spotify: state({ job: { id: JOB, action: "install", stage: "apply", status: "running", error_code: null }, revision: 2 }) };
      if (command === "spotify.cancel") return { cancelled: true, reason: "cancelled" };
      throw new Error(`unexpected ${command}`);
    });
    renderCard(send as unknown as SendCommandFn);
    fireEvent.click(await screen.findByRole("button", { name: "Cancelar" }));
    await screen.findByText(/Cancelamento pedido/);
    expect(send.mock.calls[1]).toEqual(["spotify.cancel", { job_id: JOB }]);
  });

  it("job falhado não afirma que nada mudou e não repete o efeito", async () => {
    const send = vi.fn(async (command: string) => {
      if (command === "spotify.get") return { spotify: state({ job: { id: JOB, action: "install", stage: "apply", status: "failed", error_code: "write_denied" }, setup: "needs_repair", revision: 3 }) };
      throw new Error(`unexpected ${command}`);
    });
    renderCard(send as unknown as SendCommandFn);
    expect(await screen.findByText("A configuração parou")).toBeTruthy();
    expect(screen.getByText(/Não sabemos se algo ficou alterado/)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/nada foi alterado/i);
    fireEvent.click(screen.getByRole("button", { name: "Voltar a verificar" }));
    expect(send.mock.calls.map(([command]) => command)).toEqual(["spotify.get", "spotify.get"]);
  });

  it("consentimento reutilizado limpa o plano e não há retry automático", async () => {
    const send = vi.fn(async (command: string) => {
      if (command === "spotify.get") return { spotify: state() };
      if (command === "spotify.plan") return { plan: plan("install"), consent: { consent_id: CONSENT, expires_in: 600 } };
      if (command === "spotify.apply") throw new Error("consent_replayed: private detail");
      throw new Error(`unexpected ${command}`);
    });
    renderCard(send as unknown as SendCommandFn);
    fireEvent.click(await screen.findByRole("button", { name: "Configurar Spotify" }));
    fireEvent.click(await screen.findByRole("button", { name: "Instalar ligação" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Este pedido já foi usado. Prepara outro plano.");
    expect(screen.queryByRole("button", { name: "Instalar ligação" })).toBeNull();
    expect(document.body.textContent).not.toContain("private detail");
    expect(send.mock.calls.filter(([command]) => command === "spotify.apply").length).toBe(1);
  });

  it("leitura igual vira estado válido e não erro de formato", async () => {
    const same = state();
    const send = vi.fn(async (command: string) => {
      if (command === "spotify.get") return { spotify: same };
      throw new Error(`unexpected ${command}`);
    });
    renderCard(send as unknown as SendCommandFn);
    fireEvent.click(await screen.findByRole("button", { name: "Voltar a verificar" }));
    expect(await screen.findByText("Pronto para configurar")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(send.mock.calls.map(([command]) => command)).toEqual(["spotify.get", "spotify.get"]);
  });

  it("ACK de connect com epoch/revision antigos é ignorado em silêncio", async () => {
    const failed = configured({ connection: "failed", device: "unknown", revision: 6 });
    const send = vi.fn(async (command: string) => {
      if (command === "spotify.get") return { spotify: failed };
      if (command === "spotify.connect") return { spotify: configured({ revision: 5, device: "remote" }) };
      throw new Error(`unexpected ${command}`);
    });
    renderCard(send as unknown as SendCommandFn);
    expect(await screen.findByText("Não foi possível ligar")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Tentar novamente" }));
    await act(async () => {});
    expect(screen.getByText("Não foi possível ligar")).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(send.mock.calls[1]).toEqual(["spotify.connect", {}]);
  });

  it("falha no apply não afirma rollback e o consentimento não promete silêncio total", async () => {
    const failed = state({
      setup: "needs_repair",
      job: { id: JOB, action: "install", stage: "apply", status: "failed", error_code: "write_denied" },
      error: { code: "write_denied", retryable: false },
    });
    const send = vi.fn(async (command: string) => {
      if (command === "spotify.get") return { spotify: failed };
      throw new Error(`unexpected ${command}`);
    });
    renderCard(send as unknown as SendCommandFn);
    expect(await screen.findByText("A configuração parou")).toBeTruthy();
    expect(screen.getByText("O Windows não permitiu escrever os ficheiros necessários.")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/nada foi alterado|nada foi aplicado/i);
    expect(document.body.textContent).toContain("confirma o estado atual");
    expect(document.body.textContent).not.toMatch(/toca sozinho/i);
  });

  it("descarta revision anterior, sessão alheia e GET atrasado depois de um push novo", async () => {
    let resolveGet!: (value: { spotify: SpotifyStateDto }) => void;
    const send = vi.fn(() => new Promise<{ spotify: SpotifyStateDto }>(resolve => { resolveGet = resolve; }));
    renderCard(send as unknown as SendCommandFn);
    emit(configured({ revision: 7, device: "remote" }));
    expect(await screen.findByText(/o som está noutro dispositivo/)).toBeTruthy();

    emit(configured({ revision: 6, device: "local" }));
    emit(configured({ revision: 8, device: "local" }), OTHER_SESSION);
    expect(screen.getByText(/o som está noutro dispositivo/)).toBeTruthy();

    await act(async () => resolveGet({ spotify: configured({ revision: 3, device: "local" }) }));
    expect(screen.getByText(/o som está noutro dispositivo/)).toBeTruthy();
  });

  it("mudar de sessão limpa plano e estado antigo e volta a consultar", async () => {
    const send = vi.fn(async (command: string) => {
      if (command === "spotify.get") return { spotify: state() };
      if (command === "spotify.plan") return { plan: plan("install"), consent: { consent_id: CONSENT, expires_in: 600 } };
      throw new Error(`unexpected ${command}`);
    });
    const view = renderCard(send as unknown as SendCommandFn);
    fireEvent.click(await screen.findByRole("button", { name: "Configurar Spotify" }));
    await screen.findByRole("button", { name: "Instalar ligação" });

    view.rerender(
      <SpotifyCard session={{ ...session, session_id: OTHER_SESSION }} sendCommand={send as unknown as SendCommandFn} automaticSetupAvailable />
    );
    expect(screen.queryByRole("button", { name: "Instalar ligação" })).toBeNull();
    expect(send.mock.calls.filter(([command]) => command === "spotify.get").length).toBe(2);

    emit(configured({ revision: 9, device: "remote" }), SESSION);
    expect(screen.queryByText(/o som está noutro dispositivo/)).toBeNull();
    emit(configured({ revision: 9, device: "remote" }), OTHER_SESSION);
    expect(await screen.findByText(/o som está noutro dispositivo/)).toBeTruthy();

    view.unmount();
    expect(listeners.size).toBe(0);
  });

  it("ausente abre a página oficial com target download e re-detecta por clique", async () => {
    let current = state({ setup: "absent", detection: { ...detection, app: "absent", app_running: false }, revision: 1 });
    const send = vi.fn(async (command: string, payload: Record<string, unknown> = {}) => {
      if (command === "spotify.get") return { spotify: current };
      if (command === "spotify.open") {
        expect(payload).toEqual({ target: "download" });
        return { opened: true };
      }
      throw new Error(`unexpected ${command}`);
    });
    renderCard(send as unknown as SendCommandFn);
    expect(await screen.findByText("Spotify não encontrado neste PC")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Instalar Spotify" }));
    await screen.findByText(/Depois de instalares, volta aqui/);
    current = state({ revision: 2 });
    fireEvent.click(screen.getByRole("button", { name: "Já instalei — continuar" }));
    expect(await screen.findByText("Pronto para configurar")).toBeTruthy();
    expect(send.mock.calls.map(([command]) => command)).toEqual(["spotify.get", "spotify.open", "spotify.get"]);
  });

  it("desliga e religa com payloads vazios e estado autoritativo", async () => {
    let current = configured();
    const send = vi.fn(async (command: string, payload: Record<string, unknown> = {}) => {
      if (command === "spotify.get") return { spotify: current };
      if (command === "spotify.disconnect") {
        expect(payload).toEqual({});
        current = state({ setup: "configured", connection: "disabled", revision: 5, epoch: 3 });
        return { spotify: current };
      }
      if (command === "spotify.connect") {
        expect(payload).toEqual({});
        current = configured({ revision: 6, epoch: 4 });
        return { spotify: current };
      }
      throw new Error(`unexpected ${command}`);
    });
    renderCard(send as unknown as SendCommandFn);
    expect(await screen.findByText("Pronto · este PC é o destino")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Desligar da Lunmi" }));
    expect(await screen.findByText("Desligado da Lunmi")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Ligar de novo" }));
    expect(await screen.findByText("Pronto · este PC é o destino")).toBeTruthy();
    expect(send.mock.calls.map(([command]) => command)).toEqual(["spotify.get", "spotify.disconnect", "spotify.connect"]);
  });

  it("ligação perdida abre o Spotify já validado com target app, sem ligar nada", async () => {
    const send = vi.fn(async (command: string, payload: Record<string, unknown> = {}) => {
      if (command === "spotify.get") return { spotify: configured({ connection: "disconnected", device: "unknown" }) };
      if (command === "spotify.open") {
        expect(payload).toEqual({ target: "app" });
        return { opened: true };
      }
      throw new Error(`unexpected ${command}`);
    });
    renderCard(send as unknown as SendCommandFn);
    expect(await screen.findByText("Ligação perdida")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Abrir Spotify" }));
    await act(async () => {});
    expect(send.mock.calls[1]).toEqual(["spotify.open", { target: "app" }]);
    expect(send.mock.calls.map(([command]) => command)).toEqual(["spotify.get", "spotify.open"]);
  });

  it("remover passa pelo plano e diz que o Spotify e as playlists ficam", async () => {
    const send = vi.fn(async (command: string, payload: Record<string, unknown> = {}) => {
      if (command === "spotify.get") return { spotify: configured() };
      if (command === "spotify.plan") {
        expect(payload).toEqual({ action: "remove" });
        return { plan: plan("remove"), consent: { consent_id: CONSENT, expires_in: 600 } };
      }
      throw new Error(`unexpected ${command}`);
    });
    renderCard(send as unknown as SendCommandFn);
    await screen.findByText("Pronto · este PC é o destino");
    expect(document.body.textContent).toMatch(/não desinstala o Spotify/);
    fireEvent.click(screen.getByRole("button", { name: "Remover ligação" }));
    expect(await screen.findByText("Remover a ligação da Lunmi")).toBeTruthy();
    expect(screen.getByText(/O Spotify e as tuas playlists ficam/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Remover ligação" })).toBeTruthy();
    expect(send.mock.calls[1]).toEqual(["spotify.plan", { action: "remove" }]);
  });

  it("ligado-mas-remoto e destino desconhecido têm copy distinta e nenhum atalho de ativação", async () => {
    const remote = vi.fn(async () => ({ spotify: configured({ device: "remote" }) }));
    const first = renderCard(remote as unknown as SendCommandFn);
    expect(await screen.findByText("Ligado — o som está noutro dispositivo")).toBeTruthy();
    expect(screen.getByText(/Pede no chat para tocar neste computador/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /usar este pc/i })).toBeNull();
    first.unmount();

    const unknown = vi.fn(async () => ({ spotify: configured({ device: "unknown" }) }));
    renderCard(unknown as unknown as SendCommandFn);
    expect(await screen.findByText("Ligado — destino por confirmar")).toBeTruthy();
    expect(screen.getByText(/Isto não confirma que esteja noutro dispositivo/)).toBeTruthy();
  });

  it("continua a montar-se depois do Gmail, sem alterar o cartão Gmail", async () => {
    const dto: IntegrationsDto = { gmail: { state: "disconnected", epoch: 1, flow_id: null, error: null, account: null, accounts: [] } };
    const send = vi.fn(async (command: string) => {
      if (command === "integrations.get") return dto;
      if (command === "spotify.get") return { spotify: state() };
      throw new Error(`unexpected ${command}`);
    });
    render(<AppsPanel session={session} sendCommand={send as unknown as SendCommandFn} />);
    expect(await screen.findByRole("button", { name: "Ligar Gmail" })).toBeTruthy();
    expect(await screen.findByTestId("spotify-card")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Configurar Spotify" })).toBeTruthy();
    const cards = Array.from(document.querySelectorAll(".lunmi-apps-page .lunmi-apps-card"));
    expect(cards.map(card => card.getAttribute("data-testid"))).toEqual(["gmail-card", "spotify-card"]);
  });
});

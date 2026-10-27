/**
 * Testes dos painéis (`desktop/tests/brain.test.tsx`).
 *
 * Nenhum backend real: um `sendCommand` espião devolve fixtures tipadas e registra os
 * comandos/payloads exatos. Cobre o contrato congelado de 2026-10-17: `brain.list` com
 * `{provider-alvo, role, force, base_url, model, api_key?}`; chave somente de escrita que NÃO
 * desaparece entre listar/verificar e limpa em aplicar/trocar provider/descartar; respostas
 * obsoletas descartadas por identidade de catálogo (provider/role/endpoint/chave, sem modelo) e
 * de seleção (com modelo); escolha sempre por catálogo, sem input de ID; "(padrão do provider)"
 * só para providers com capacidade real (CLI); teclado no popover; AppAgent em provider diferente
 * não recebe a chave principal. Também mantém `consent: true` no probe e os painéis F2b.
 *
 * Redesign 2026-10-27 (settings-redesign): a escolha de provedor passou de dropdown para pílulas
 * com marca real por grupo (API em nuvem / endpoint local / CLI local). As fixtures usam os
 * valores CANÓNICOS do DTO (`kind`: `model_api`/`agent_runtime`; `location`: `cloud_api`/
 * `local_endpoint`/`local_cli`) — os valores antigos (`api`/`cli`/`cloud`/`local_command`) não
 * eram o contrato. As interações `fireEvent.change(select)` migraram para clique na pílula e a
 * asserção de seleção para `aria-pressed`; nenhuma guarda de comportamento foi removida.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BrainPanel } from "../src/features/brain/BrainPanel";
import { PreferencesPanel } from "../src/features/preferences/PreferencesPanel";
import type { SendCommandFn } from "../src/shared/ipc/client";
import type { SessionReadyPayload } from "../src/shared/protocol";
import type {
  ApprovalDto,
  BrainGetDto,
  BrainProbeDto,
  BrainStateDto,
  BrainVerifyDto,
  ModelListingDto,
  UiPreferencesDto,
} from "../src/shared/types";

afterEach(cleanup);

const session: SessionReadyPayload = {
  v: 1,
  session_id: "sess-test-1",
  pid: 4242,
  capabilities: {
    converse: true,
    execute: true,
    memory: true,
    brain: true,
    web_search: false,
    browser_use: false,
  },
  limits: {
    max_line_bytes: 5 * 1024 * 1024,
    max_import_bytes: 4 * 1024 * 1024,
    max_chat_text_chars: 8000,
  },
};

const brainState: BrainStateDto = {
  provider: "openai",
  provider_label: "OpenAI",
  model: "gpt-4o",
  base_url: "",
  has_api_key: true,
  app_provider: "",
  app_model: "",
  effort: "medium",
  env_overrides: { provider: false, model: false, app_provider: false, app_model: false },
};

const brainGet: BrainGetDto = {
  current: brainState,
  providers: [
    {
      id: "openai",
      label: "OpenAI",
      kind: "model_api",
      location: "cloud_api",
      requires_api_key: true,
      supports_model_discovery: true,
      supports_interactive_login: false,
    },
    {
      id: "custom",
      label: "OpenAI Compatible",
      kind: "model_api",
      location: "cloud_api",
      requires_api_key: true,
      supports_model_discovery: true,
      supports_interactive_login: false,
    },
    {
      id: "ollama",
      label: "Ollama",
      kind: "model_api",
      location: "local_endpoint",
      requires_api_key: false,
      supports_model_discovery: true,
      supports_interactive_login: false,
    },
    {
      id: "claude-cli",
      label: "Claude CLI",
      kind: "agent_runtime",
      location: "local_cli",
      requires_api_key: false,
      supports_model_discovery: false,
      supports_interactive_login: true,
    },
  ],
};

const listing: ModelListingDto = {
  provider: "openai",
  label: "OpenAI",
  status: "ready",
  ok: true,
  detail: "",
  selection_problem: null,
  cached: false,
  models: [
    { id: "gpt-4o", label: "GPT-4o", default: false },
    { id: "gpt-4o-mini", label: "GPT-4o mini", default: true },
  ],
};

const verifyResult: BrainVerifyDto = {
  provider: "openai",
  model: "gpt-4o",
  status: "ready",
  detail: "",
  ok: true,
  listing,
};

const probeResult: BrainProbeDto = {
  provider: "openai",
  model: "gpt-4o",
  ok: true,
  latency_ms: 123.4,
  summary: "o provedor respondeu.",
  error_kind: null,
  input_tokens: 12,
  output_tokens: 4,
};

const approval: ApprovalDto = { auto_approve: false, mode: "manual", label: "Modo: Manual" };
const prefs: UiPreferencesDto = {
  window: null,
  scale_pct: 125,
  actions: [{ label: "Abrir aplicativo", draft: "Abra o " }],
};

interface Call {
  type: string;
  payload: Record<string, unknown>;
}

function makeFake(overrides: Partial<Record<string, unknown>> = {}) {
  const calls: Call[] = [];
  const sendCommand = (async (type: string, payload: Record<string, unknown> = {}) => {
    calls.push({ type, payload });
    if (Object.prototype.hasOwnProperty.call(overrides, type)) {
      return overrides[type];
    }
    switch (type) {
      case "brain.get":
        return brainGet;
      case "brain.list":
        return { ...listing, provider: String(payload.provider ?? "openai") };
      case "brain.verify":
        return verifyResult;
      case "brain.probe":
        return probeResult;
      case "brain.set":
        return brainState;
      case "approval.get":
      case "approval.set":
        return approval;
      case "ui.preferences.get":
      case "ui.preferences.set":
        return prefs;
      default:
        throw new Error(`not_implemented: ${type}`);
    }
  }) as unknown as SendCommandFn;
  return { calls, sendCommand };
}

function callsOf(calls: Call[], type: string): Call[] {
  return calls.filter((call) => call.type === type);
}

/** Promessa controlada pelo teste: permite resolver "tarde" e provar descarte de resposta obsoleta. */
function defer<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (error: Error) => void } {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function rendered(fake: { sendCommand: SendCommandFn }): Promise<void> {
  render(<BrainPanel sendCommand={fake.sendCommand} session={session} />);
  await waitFor(() =>
    expect((screen.getByTestId("current-brain") as HTMLElement).textContent).toContain("OpenAI")
  );
}

describe("BrainPanel — fluxo BYOK (contrato 2026-10-17)", () => {
  it("carrega o estado atual com brain.get e mostra o provider salvo", async () => {
    const fake = makeFake();
    await rendered(fake);
    expect(fake.calls[0].type).toBe("brain.get");
    expect(fake.calls[0].payload).toEqual({});
    expect((screen.getByTestId("current-brain") as HTMLElement).textContent).toContain("gpt-4o");
    expect(screen.getByTestId("provider-select-btn-openai").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("provider-select-btn-ollama").getAttribute("aria-pressed")).toBe("false");
  });

  it("brain.list vai com provider-alvo, role, force, base_url e model; api_key só quando há rascunho", async () => {
    const fake = makeFake();
    await rendered(fake);

    fireEvent.click(screen.getByTestId("list-models"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.list")).toHaveLength(1));
    expect(callsOf(fake.calls, "brain.list")[0].payload).toEqual({
      provider: "openai", role: "main", force: false, base_url: "", model: "gpt-4o",
    });

    fireEvent.click(screen.getByTestId("model-select-refresh"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.list")).toHaveLength(2));
    expect(callsOf(fake.calls, "brain.list")[1].payload).toMatchObject({ force: true });

    // rascunho de chave passa a acompanhar a consulta do mesmo alvo
    fireEvent.change(screen.getByTestId("api-key"), { target: { value: "sk-sentinel-12345" } });
    fireEvent.click(screen.getByTestId("model-select-refresh"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.list")).toHaveLength(3));
    expect(callsOf(fake.calls, "brain.list")[2].payload.api_key).toBe("sk-sentinel-12345");
  });

  it("escolher modelo do catálogo NÃO invalida o catálogo (identidade sem modelo)", async () => {
    const fake = makeFake();
    await rendered(fake);
    fireEvent.click(screen.getByTestId("list-models"));
    await screen.findByTestId("model-listing");

    fireEvent.click(screen.getByTestId("model-select"));
    await screen.findByTestId("model-select-list");
    fireEvent.click(screen.getByTestId("model-select-option-gpt-4o-mini"));
    await waitFor(() =>
      expect((screen.getByTestId("model-select") as HTMLElement).textContent).toContain("GPT-4o mini")
    );
    // reabrir: catálogo continua no lugar, sem nova requisição disparada pela escolha
    fireEvent.click(screen.getByTestId("model-select"));
    await screen.findByTestId("model-select-option-gpt-4o");
    expect(callsOf(fake.calls, "brain.list")).toHaveLength(1);
    expect(screen.getByTestId("model-listing")).toBeTruthy();
  });

  it("catálogo de API não oferece '(padrão do provider)'; CLI com capacidade real oferece", async () => {
    const fake = makeFake();
    await rendered(fake);
    fireEvent.click(screen.getByTestId("list-models"));
    await screen.findByTestId("model-listing");
    fireEvent.click(screen.getByTestId("model-select"));
    await screen.findByTestId("model-select-list");
    expect(screen.queryByTestId("model-select-option-default")).toBeNull();
    fireEvent.keyDown(screen.getByTestId("model-select-search"), { key: "Escape" });

    fireEvent.click(screen.getByTestId("provider-select-btn-claude-cli"));
    await waitFor(() => expect(screen.queryByTestId("model-listing")).toBeNull());
    fireEvent.click(screen.getByTestId("list-models"));
    await screen.findByTestId("model-listing");
    fireEvent.click(screen.getByTestId("model-select"));
    await screen.findByTestId("model-select-option-default");
  });

  it("teclado no popover: ArrowDown foca opção, Escape fecha e devolve foco ao gatilho", async () => {
    const fake = makeFake();
    await rendered(fake);
    fireEvent.click(screen.getByTestId("list-models"));
    fireEvent.click(screen.getByTestId("model-select"));
    await screen.findByTestId("model-select-search");
    fireEvent.keyDown(screen.getByTestId("model-select-search"), { key: "ArrowDown" });
    expect(document.activeElement).toBe(screen.getByTestId("model-select-option-gpt-4o"));
    fireEvent.keyDown(screen.getByTestId("model-select-option-gpt-4o"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("model-select-popover")).toBeNull());
    expect(document.activeElement).toBe(screen.getByTestId("model-select"));
  });

  it("list deferred + troca de chave antes de resolver: catálogo descartado, nada aplicado", async () => {
    const deferred = defer<ModelListingDto>();
    const fake = makeFake({ "brain.list": deferred.promise });
    await rendered(fake);
    fireEvent.change(screen.getByTestId("api-key"), { target: { value: "sk-uma" } });
    fireEvent.click(screen.getByTestId("list-models"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.list")).toHaveLength(1));
    // durante a consulta da mesma identidade o botão não permite GET duplicado
    expect((screen.getByTestId("list-models") as HTMLButtonElement).disabled).toBe(true);
    // trocar a chave reabre a consulta permitida (identidade nova) e invalida a resposta antiga
    fireEvent.change(screen.getByTestId("api-key"), { target: { value: "sk-outra" } });
    await waitFor(() => expect((screen.getByTestId("list-models") as HTMLButtonElement).disabled).toBe(false));
    deferred.resolve(listing);
    await waitFor(() =>
      expect((screen.getByTestId("brain-status") as HTMLElement).textContent).toMatch(/descartad/i)
    );
    expect(screen.queryByTestId("model-listing")).toBeNull();
    expect((screen.getByTestId("api-key") as HTMLInputElement).value).toBe("sk-outra");
  });

  it("a chave sobrevive entre listar e verificar e só sai em aplicar/descartar/trocar provider", async () => {
    const fake = makeFake();
    await rendered(fake);
    fireEvent.change(screen.getByTestId("api-key"), { target: { value: "sk-sentinel-12345" } });

    fireEvent.click(screen.getByTestId("verify-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.verify")).toHaveLength(1));
    expect(callsOf(fake.calls, "brain.verify")[0].payload.api_key).toBe("sk-sentinel-12345");
    // fronteira nova: verificar NÃO descarta a chave
    expect((screen.getByTestId("api-key") as HTMLInputElement).value).toBe("sk-sentinel-12345");
    expect(document.body.textContent ?? "").not.toContain("sk-sentinel-12345");

    // botão explícito descarta
    fireEvent.click(screen.getByTestId("api-key-clear"));
    await waitFor(() => expect((screen.getByTestId("api-key") as HTMLInputElement).value).toBe(""));

    // aplicar consome e limpa
    fireEvent.change(screen.getByTestId("api-key"), { target: { value: "sk-sentinel-12345" } });
    fireEvent.click(screen.getByTestId("apply-brain"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.set")).toHaveLength(1));
    expect(callsOf(fake.calls, "brain.set")[0].payload.api_key).toBe("sk-sentinel-12345");
    await waitFor(() => expect((screen.getByTestId("api-key") as HTMLInputElement).value).toBe(""));

    // troca explícita de provider limpa o rascunho do anterior
    fireEvent.change(screen.getByTestId("api-key"), { target: { value: "sk-sentinel-67890" } });
    fireEvent.click(screen.getByTestId("provider-select-btn-ollama"));
    expect(screen.queryByTestId("api-key")).toBeNull();
    fireEvent.click(screen.getByTestId("provider-select-btn-openai"));
    await waitFor(() => expect((screen.getByTestId("api-key") as HTMLInputElement).value).toBe(""));
    expect(document.body.textContent ?? "").not.toContain("sk-sentinel-67890");
  });

  it("AppAgent em provider diferente: consulta usa o provider-alvo e NÃO leva a chave principal", async () => {
    const fake = makeFake();
    await rendered(fake);
    fireEvent.change(screen.getByTestId("api-key"), { target: { value: "sk-principal" } });
    fireEvent.click(screen.getByTestId("app-provider-select-btn-ollama"));
    await screen.findByTestId("app-credential-note");

    fireEvent.click(screen.getByTestId("list-app-models"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.list")).toHaveLength(1));
    const call = callsOf(fake.calls, "brain.list")[0];
    expect(call.payload.provider).toBe("ollama");
    expect(call.payload.role).toBe("app");
    expect(call.payload).not.toHaveProperty("api_key");
  });

  it("transmite base_url explicitamente, inclusive vazio, para não manter silenciosamente o valor antigo", async () => {
    const fake = makeFake();
    await rendered(fake);
    fireEvent.click(screen.getByTestId("provider-select-btn-ollama"));
    const baseUrlInput = await screen.findByTestId("base-url");

    fireEvent.click(screen.getByTestId("verify-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.verify")).toHaveLength(1));
    expect(callsOf(fake.calls, "brain.verify")[0].payload).toHaveProperty("base_url");
    expect(callsOf(fake.calls, "brain.verify")[0].payload.base_url).toBe("");

    fireEvent.change(baseUrlInput, { target: { value: "http://127.0.0.1:11434" } });
    fireEvent.click(screen.getByTestId("verify-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.verify")).toHaveLength(2));
    expect(callsOf(fake.calls, "brain.verify")[1].payload.base_url).toBe("http://127.0.0.1:11434");

    fireEvent.change(screen.getByTestId("base-url"), { target: { value: "" } });
    fireEvent.click(screen.getByTestId("verify-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.verify")).toHaveLength(3));
    expect(callsOf(fake.calls, "brain.verify")[2].payload).toHaveProperty("base_url");
    expect(callsOf(fake.calls, "brain.verify")[2].payload.base_url).toBe("");
  });

  it("brain.probe não dispara sozinho e envia consent: true; brain.verify não pede consentimento", async () => {
    const fake = makeFake();
    await rendered(fake);
    expect(callsOf(fake.calls, "brain.probe")).toHaveLength(0);
    fireEvent.click(screen.getByTestId("verify-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.verify")).toHaveLength(1));
    expect(callsOf(fake.calls, "brain.verify")[0].payload).not.toHaveProperty("consent");
    expect(callsOf(fake.calls, "brain.probe")).toHaveLength(0);
    fireEvent.click(screen.getByTestId("probe-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.probe")).toHaveLength(1));
    expect(callsOf(fake.calls, "brain.probe")[0].payload.consent).toBe(true);
    await screen.findByTestId("probe-result");
  });

  it("descarta o veredito de verify quando a seleção muda durante a operação", async () => {
    const deferred = defer<BrainVerifyDto>();
    const fake = makeFake({ "brain.verify": deferred.promise });
    await rendered(fake);
    fireEvent.click(screen.getByTestId("verify-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.verify")).toHaveLength(1));
    fireEvent.click(screen.getByTestId("provider-select-btn-ollama"));
    deferred.resolve(verifyResult);
    await waitFor(() =>
      expect((screen.getByTestId("brain-status") as HTMLElement).textContent ?? "").toMatch(/descartad/i)
    );
    expect(screen.queryByTestId("verify-result")).toBeNull();
  });

  it("descarta o resultado de probe quando a seleção muda durante o teste", async () => {
    const deferred = defer<BrainProbeDto>();
    const fake = makeFake({ "brain.probe": deferred.promise });
    await rendered(fake);
    fireEvent.click(screen.getByTestId("probe-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.probe")).toHaveLength(1));
    fireEvent.click(screen.getByTestId("provider-select-btn-ollama"));
    deferred.resolve(probeResult);
    await waitFor(() =>
      expect((screen.getByTestId("brain-status") as HTMLElement).textContent ?? "").toMatch(/descartad/i)
    );
    expect(screen.queryByTestId("probe-result")).toBeNull();
  });

  it("avisa quando uma variável de ambiente prevalece sobre a seleção salva", async () => {
    const fake = makeFake({
      "brain.get": {
        ...brainGet,
        current: {
          ...brainState,
          env_overrides: { provider: true, model: false, app_provider: false, app_model: false },
        },
      },
    });
    render(<BrainPanel sendCommand={fake.sendCommand} session={session} />);
    const warning = await screen.findByTestId("env-override-warning");
    expect(warning.textContent ?? "").toMatch(/variável de ambiente/i);
  });
});

describe("B2 — seleção efetiva Host/App", () => {
  it("sem DTO effective (peer antigo) declara não reportado em vez de inferir", async () => {
    const fake = makeFake();
    render(<BrainPanel sendCommand={fake.sendCommand} session={session} />);
    await screen.findByTestId("effective-brain-none");
    expect((screen.getByTestId("effective-brain-none").textContent ?? "")).toMatch(/não reportada/);
  });

  it("mostra Host/App efetivos e herança integral calculada pelo backend", async () => {
    const effective = {
      host: { provider: "gemini", model: "gemini-2.5-flash", provider_inherited: false, model_inherited: false },
      app: { provider: "gemini", model: "gemini-2.5-flash", provider_inherited: true, model_inherited: true },
      app_inheritance: "full" as const,
    };
    const fake = makeFake({ "brain.get": { ...brainGet, current: { ...brainState, effective } } });
    render(<BrainPanel sendCommand={fake.sendCommand} session={session} />);
    const line = await screen.findByTestId("effective-brain");
    expect(line.textContent).toContain("HostAgent: gemini · gemini-2.5-flash");
    expect(line.textContent).toContain("AppAgent: gemini · gemini-2.5-flash");
    expect(line.textContent).toContain("AppAgent herda tudo do principal");
  });

  it("herança parcial: provider herdado com modelo próprio dito com precisão", async () => {
    const effective = {
      host: { provider: "gemini", model: "gemini-2.5-flash", provider_inherited: false, model_inherited: false },
      app: { provider: "gemini", model: "gemini-2.5-pro", provider_inherited: true, model_inherited: false },
      app_inheritance: "provider" as const,
    };
    const fake = makeFake({ "brain.get": { ...brainGet, current: { ...brainState, effective } } });
    render(<BrainPanel sendCommand={fake.sendCommand} session={session} />);
    const line = await screen.findByTestId("effective-brain");
    expect(line.textContent).toContain("herda o provider; modelo próprio");
    expect(line.textContent).toContain("gemini-2.5-pro");
  });

  it("brain.updated com effective atualiza a linha sem novo pedido", async () => {
    let emitEvent: ((event: { v: 1; type: string; payload: unknown }) => void) | undefined;
    (window as unknown as { lunmi: unknown }).lunmi = {
      onEvent: (cb: (event: { v: 1; type: string; payload: unknown }) => void) => { emitEvent = cb; return () => { emitEvent = undefined; }; },
    };
    const fake = makeFake();
    render(<BrainPanel sendCommand={fake.sendCommand} session={session} />);
    await screen.findByTestId("effective-brain-none");
    const before = fake.calls.length;
    emitEvent?.({ v: 1, type: "brain.updated", payload: {
      ...brainState,
      effective: {
        host: { provider: "openai", model: "gpt-4o", provider_inherited: false, model_inherited: false },
        app: { provider: "openai", model: "gpt-4o", provider_inherited: true, model_inherited: true },
        app_inheritance: "full",
      },
    } });
    await waitFor(() => expect(screen.getByTestId("effective-brain").textContent).toContain("herda tudo do principal"));
    expect(fake.calls.length).toBe(before);
  });
});

describe("BrainPanel — pílulas de provedor com marca real (redesign 2026-10-27)", () => {
  it("sem dropdown: pílulas por grupo (API nuvem / endpoint local / CLI) e marca licenciada por id", async () => {
    const fake = makeFake();
    const { container } = render(<BrainPanel sendCommand={fake.sendCommand} session={session} />);
    await screen.findByTestId("brain-panel");
    expect(container.querySelectorAll("select")).toHaveLength(0); // nenhum dropdown de provedor
    expect(screen.getByTestId("provider-select").getAttribute("role")).toBe("group");
    expect(screen.getByTestId("provider-select-group-API em nuvem")).toBeTruthy();
    expect(screen.getByTestId("provider-select-group-Endpoint local")).toBeTruthy();
    expect(screen.getByTestId("provider-select-group-CLI local")).toBeTruthy();
    // marca real (LobeHub lobe-icons, MIT) para ids conhecidos; glifo genérico para os outros
    expect(screen.getByTestId("provider-select-icon-openai").getAttribute("data-mark")).toBe("OpenAI");
    expect(screen.getByTestId("provider-select-icon-openai").tagName.toLowerCase()).toBe("svg");
    expect(screen.getByTestId("provider-select-icon-custom").getAttribute("data-mark")).toBe("generic");
    expect(screen.getByTestId("provider-select-icon-claude-cli").getAttribute("data-mark")).toBe("generic");
    // o nome acessível do controlo vem do texto da pílula, não do glifo
    expect(screen.getByTestId("provider-select-icon-openai").getAttribute("aria-hidden")).toBe("true");
    expect(screen.getByTestId("provider-select-btn-openai").textContent).toContain("OpenAI");
    expect(screen.getByTestId("provider-select-btn-claude-cli").textContent).toContain("Claude CLI");
  });

  it("escolher uma pílula é rascunho local: não envia comando, limpa modelo/chave do anterior", async () => {
    const fake = makeFake();
    await rendered(fake);
    fireEvent.change(screen.getByTestId("api-key"), { target: { value: "sk-antes" } });
    fireEvent.click(screen.getByTestId("provider-select-btn-ollama"));
    expect(screen.queryByTestId("api-key")).toBeNull();
    expect(screen.getByTestId("provider-select-btn-ollama").getAttribute("aria-pressed")).toBe("true"); // rascunho da seleção
    expect(screen.getByTestId("provider-select-btn-openai").getAttribute("aria-pressed")).toBe("false");
    expect(fake.calls.filter(c => c.type === "brain.set" || c.type === "brain.list")).toHaveLength(0);
    // aplicar continua a ser o botão próprio; o endpoint do rascunho local aparece de imediato
    expect(screen.getByTestId("base-url")).toBeTruthy();
    expect(fake.calls.map(c => c.type)).toEqual(["brain.get"]);
  });

  it("só mostra controlos de chave quando o provedor os exige, preservando o endpoint local", async () => {
    const fake = makeFake();
    await rendered(fake);
    expect(screen.getByTestId("api-key")).toBeTruthy();
    fireEvent.click(screen.getByTestId("provider-select-btn-claude-cli"));
    expect(screen.queryByTestId("api-key")).toBeNull();
    expect(screen.queryByTestId("api-key-clear")).toBeNull();
    expect(screen.queryByTestId("remember-credentials")).toBeNull();
    expect(screen.queryByText(/Após aplicar, a chave/)).toBeNull();
    expect(screen.queryByTestId("base-url")).toBeNull();
    fireEvent.click(screen.getByTestId("provider-select-btn-ollama"));
    expect(screen.queryByTestId("api-key")).toBeNull();
    expect(screen.getByTestId("base-url")).toBeTruthy();
    fireEvent.click(screen.getByTestId("provider-select-btn-openai"));
    expect((screen.getByTestId("api-key") as HTMLInputElement).value).toBe("");
    expect(screen.getByTestId("remember-credentials")).toBeTruthy();
    expect(fake.calls.map(c => c.type)).toEqual(["brain.get"]);
  });

  it("provedor indisponível continua visível, desativado e com o motivo dito em texto", async () => {
    const withAvailability = {
      ...brainGet,
      providers: brainGet.providers.map(p => p.id === "claude-cli"
        ? { ...p, available: false, installed: false, unavailable_reason: "só corre em interface gráfica (TUI)." }
        : p.id === "ollama" ? { ...p, available: true, installed: true } : p),
    } as BrainGetDto;
    const fake = makeFake({ "brain.get": withAvailability });
    await rendered(fake);
    const pill = screen.getByTestId("provider-select-btn-claude-cli") as HTMLButtonElement;
    expect(pill.disabled).toBe(true);                       // nunca escondido: desativado
    expect(pill.textContent).toContain("indisponível");
    expect(screen.getByTestId("provider-select-reasons-CLI local").textContent)
      .toContain("Claude CLI: só corre em interface gráfica (TUI).");
    expect(screen.getByTestId("provider-select-installed-ollama").textContent).toBe("instalado");
  });

  it("AppAgent sem dropdown: pílula herdar mantém valor vazio e pílula própria seleciona", async () => {
    const fake = makeFake();
    await rendered(fake);
    const appPicker = screen.getByTestId("app-provider-select");
    expect(appPicker.querySelectorAll("select")).toHaveLength(0);
    expect(screen.getByTestId("app-provider-select-btn-inherit").getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(screen.getByTestId("app-provider-select-btn-ollama"));
    await screen.findByTestId("app-credential-note");
    expect(screen.getByTestId("app-provider-select-btn-ollama").getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByTestId("app-provider-select-btn-inherit").getAttribute("aria-pressed")).toBe("false");
    expect(fake.calls.filter(c => c.type === "brain.set")).toHaveLength(0);
    fireEvent.click(screen.getByTestId("app-provider-select-btn-inherit"));
    await waitFor(() => expect(screen.queryByTestId("app-credential-note")).toBeNull());
  });
});

describe("PreferencesPanel (F2b)", () => {
  it("a copy da autoaprovação limita o efeito à próxima tarefa", async () => {
    const fake = makeFake();
    render(<PreferencesPanel sendCommand={fake.sendCommand} session={session} />);
    const scope = await screen.findByTestId("approval-scope");
    expect(scope.textContent ?? "").toMatch(/próxima tarefa/i);
  });

  it("approval.set alterna a autoaprovação com booleano", async () => {
    const fake = makeFake();
    render(<PreferencesPanel sendCommand={fake.sendCommand} session={session} />);
    await screen.findByTestId("approval-scope");
    const toggle = screen.getByTestId("auto-approve-toggle") as HTMLInputElement;
    expect(toggle.checked).toBe(false);
    fireEvent.click(toggle);
    await waitFor(() => expect(callsOf(fake.calls, "approval.set")).toHaveLength(1));
    expect(callsOf(fake.calls, "approval.set")[0].payload).toEqual({ auto_approve: true });
  });

  it("preferências não exibem escala e enviam somente actions — nunca scale_pct, path, file ou window", async () => {
    const fake = makeFake();
    render(<PreferencesPanel sendCommand={fake.sendCommand} session={session} />);
    await screen.findByTestId("action-row-0");
    expect(screen.queryByTestId("scale-select")).toBeNull();
    fireEvent.click(screen.getByTestId("save-preferences"));
    await waitFor(() => expect(callsOf(fake.calls, "ui.preferences.set")).toHaveLength(1));
    const payload = callsOf(fake.calls, "ui.preferences.set")[0].payload;
    expect(Object.keys(payload)).toEqual(["actions"]);
    expect(payload).not.toHaveProperty("scale_pct");
    expect(payload).not.toHaveProperty("path");
    expect(payload).not.toHaveProperty("file");
    expect(payload).not.toHaveProperty("window");
    expect(payload.actions).toEqual([{ label: "Abrir aplicativo", draft: "Abra o " }]);
  });

  it("edita atalhos respeitando os limites de rótulo e rascunho", async () => {
    const fake = makeFake();
    render(<PreferencesPanel sendCommand={fake.sendCommand} session={session} />);
    await screen.findByTestId("action-row-0");
    fireEvent.change(screen.getByTestId("action-label-0"), { target: { value: "Pesquisar" } });
    fireEvent.click(screen.getByTestId("action-add"));
    expect(screen.getByTestId("action-row-1")).toBeTruthy();
    fireEvent.click(screen.getByTestId("action-remove-1"));
    await waitFor(() => expect(screen.queryByTestId("action-row-1")).toBeNull());
    fireEvent.click(screen.getByTestId("save-preferences"));
    await waitFor(() => expect(callsOf(fake.calls, "ui.preferences.set")).toHaveLength(1));
    expect(callsOf(fake.calls, "ui.preferences.set")[0].payload.actions).toEqual([
      { label: "Pesquisar", draft: "Abra o " },
    ]);
  });
});

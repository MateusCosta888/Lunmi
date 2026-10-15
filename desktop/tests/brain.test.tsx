/**
 * Testes dos painéis F2b (`desktop/tests/brain.test.tsx`).
 *
 * Nenhum backend real: um `sendCommand` espião devolve fixtures tipadas e registra os
 * comandos/payloads exatos. As asserções cobrem os pontos que o contrato exige —
 * `consent: true` no probe, chave de API somente de escrita, aviso de variável de ambiente,
 * escopo de "próxima tarefa" na autoaprovação e ausência de `path`/`file`/`window` nas
 * preferências.
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
      kind: "api",
      location: "cloud",
      requires_api_key: true,
      supports_model_discovery: true,
      supports_interactive_login: false,
    },
    {
      id: "ollama",
      label: "Ollama",
      kind: "local",
      location: "local_endpoint",
      requires_api_key: false,
      supports_model_discovery: true,
      supports_interactive_login: false,
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
        return listing;
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

describe("BrainPanel (F2b)", () => {
  it("carrega o estado atual com brain.get e mostra o provider salvo", async () => {
    const fake = makeFake();
    render(<BrainPanel sendCommand={fake.sendCommand} session={session} />);

    await waitFor(() =>
      expect((screen.getByTestId("current-brain") as HTMLElement).textContent).toContain("OpenAI")
    );
    expect(fake.calls[0].type).toBe("brain.get");
    expect(fake.calls[0].payload).toEqual({});
    expect((screen.getByTestId("current-brain") as HTMLElement).textContent).toContain("gpt-4o");
    expect((screen.getByTestId("provider-select") as HTMLSelectElement).value).toBe("openai");
  });

  it("brain.list envia role main e force=false, e o Atualizar envia force=true", async () => {
    const fake = makeFake();
    render(<BrainPanel sendCommand={fake.sendCommand} session={session} />);
    await waitFor(() =>
      expect((screen.getByTestId("current-brain") as HTMLElement).textContent).toContain("OpenAI")
    );

    fireEvent.click(screen.getByTestId("list-models"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.list")).toHaveLength(1));
    expect(callsOf(fake.calls, "brain.list")[0].payload).toEqual({
      provider: "openai",
      role: "main",
      force: false,
    });
    await screen.findByTestId("model-list");

    fireEvent.click(screen.getByTestId("refresh-models"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.list")).toHaveLength(2));
    expect(callsOf(fake.calls, "brain.list")[1].payload).toMatchObject({ force: true });
  });

  it("brain.probe não dispara sozinho e envia consent: true; brain.verify não pede consentimento", async () => {
    const fake = makeFake();
    render(<BrainPanel sendCommand={fake.sendCommand} session={session} />);
    await waitFor(() =>
      expect((screen.getByTestId("current-brain") as HTMLElement).textContent).toContain("OpenAI")
    );

    // Nenhuma chamada real antes de o usuário clicar.
    expect(callsOf(fake.calls, "brain.probe")).toHaveLength(0);

    fireEvent.click(screen.getByTestId("verify-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.verify")).toHaveLength(1));
    expect(callsOf(fake.calls, "brain.verify")[0].payload).not.toHaveProperty("consent");

    expect(callsOf(fake.calls, "brain.probe")).toHaveLength(0);
    fireEvent.click(screen.getByTestId("probe-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.probe")).toHaveLength(1));
    const probeCall = callsOf(fake.calls, "brain.probe")[0];
    expect(probeCall.payload.consent).toBe(true);
    expect(probeCall.payload.provider).toBe("openai");
    await screen.findByTestId("probe-result");
  });

  it("a chave de API é somente de escrita: enviada quando usada e limpa em seguida", async () => {
    const fake = makeFake();
    render(<BrainPanel sendCommand={fake.sendCommand} session={session} />);
    await waitFor(() =>
      expect((screen.getByTestId("current-brain") as HTMLElement).textContent).toContain("OpenAI")
    );

    const keyInput = screen.getByTestId("api-key") as HTMLInputElement;
    fireEvent.change(keyInput, { target: { value: "sk-sentinel-12345" } });
    expect(keyInput.value).toBe("sk-sentinel-12345");

    fireEvent.click(screen.getByTestId("verify-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.verify")).toHaveLength(1));
    expect(callsOf(fake.calls, "brain.verify")[0].payload.api_key).toBe("sk-sentinel-12345");
    await waitFor(() => expect((screen.getByTestId("api-key") as HTMLInputElement).value).toBe(""));
    expect(document.body.textContent ?? "").not.toContain("sk-sentinel-12345");

    // Reaplica a chave e usa em brain.set: de novo sai do estado local.
    fireEvent.change(screen.getByTestId("api-key"), { target: { value: "sk-sentinel-12345" } });
    fireEvent.click(screen.getByTestId("apply-brain"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.set")).toHaveLength(1));
    expect(callsOf(fake.calls, "brain.set")[0].payload.api_key).toBe("sk-sentinel-12345");
    await waitFor(() => expect((screen.getByTestId("api-key") as HTMLInputElement).value).toBe(""));
    expect(document.body.textContent ?? "").not.toContain("sk-sentinel-12345");
  });

  it("transmite base_url explicitamente, inclusive vazio, para não manter silenciosamente o valor antigo", async () => {
    const fake = makeFake();
    render(<BrainPanel sendCommand={fake.sendCommand} session={session} />);
    await waitFor(() =>
      expect((screen.getByTestId("current-brain") as HTMLElement).textContent).toContain("OpenAI")
    );

    // Provider de endpoint local: o campo de URL base aparece.
    fireEvent.change(screen.getByTestId("provider-select"), { target: { value: "ollama" } });
    const baseUrlInput = await screen.findByTestId("base-url");

    // Vazio continua sendo transmitido: o backend distingue chave ausente de string vazia.
    fireEvent.click(screen.getByTestId("verify-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.verify")).toHaveLength(1));
    const emptyCall = callsOf(fake.calls, "brain.verify")[0];
    expect(emptyCall.payload).toHaveProperty("base_url");
    expect(emptyCall.payload.base_url).toBe("");

    // Preenchido, vai o valor digitado...
    fireEvent.change(baseUrlInput, { target: { value: "http://127.0.0.1:11434" } });
    fireEvent.click(screen.getByTestId("verify-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.verify")).toHaveLength(2));
    expect(callsOf(fake.calls, "brain.verify")[1].payload.base_url).toBe("http://127.0.0.1:11434");

    // ...e apagar de novo volta a transmitir vazio explícito, em vez de omitir a chave.
    fireEvent.change(screen.getByTestId("base-url"), { target: { value: "" } });
    fireEvent.click(screen.getByTestId("verify-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.verify")).toHaveLength(3));
    const cleared = callsOf(fake.calls, "brain.verify")[2];
    expect(cleared.payload).toHaveProperty("base_url");
    expect(cleared.payload.base_url).toBe("");
  });

  it("descarta o veredito de verify quando a seleção muda durante a operação", async () => {
    const deferred = defer<BrainVerifyDto>();
    const fake = makeFake({ "brain.verify": deferred.promise });
    render(<BrainPanel sendCommand={fake.sendCommand} session={session} />);
    await waitFor(() =>
      expect((screen.getByTestId("current-brain") as HTMLElement).textContent).toContain("OpenAI")
    );

    fireEvent.click(screen.getByTestId("verify-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.verify")).toHaveLength(1));
    expect(callsOf(fake.calls, "brain.verify")[0].payload.provider).toBe("openai");

    // A seleção muda enquanto a resposta do provider antigo ainda não chegou.
    fireEvent.change(screen.getByTestId("provider-select"), { target: { value: "ollama" } });
    await waitFor(() =>
      expect((screen.getByTestId("provider-select") as HTMLSelectElement).value).toBe("ollama")
    );

    // A resposta do OpenAI chega tarde e NÃO pode aparecer como veredito do Ollama.
    deferred.resolve(verifyResult);
    await waitFor(() =>
      expect((screen.getByTestId("brain-status") as HTMLElement).textContent ?? "").toMatch(
        /descartad/i
      )
    );
    expect(screen.queryByTestId("verify-result")).toBeNull();
  });

  it("descarta o resultado de probe quando a seleção muda durante o teste", async () => {
    const deferred = defer<BrainProbeDto>();
    const fake = makeFake({ "brain.probe": deferred.promise });
    render(<BrainPanel sendCommand={fake.sendCommand} session={session} />);
    await waitFor(() =>
      expect((screen.getByTestId("current-brain") as HTMLElement).textContent).toContain("OpenAI")
    );

    fireEvent.click(screen.getByTestId("probe-button"));
    await waitFor(() => expect(callsOf(fake.calls, "brain.probe")).toHaveLength(1));
    expect(callsOf(fake.calls, "brain.probe")[0].payload.consent).toBe(true);

    fireEvent.change(screen.getByTestId("provider-select"), { target: { value: "ollama" } });
    await waitFor(() =>
      expect((screen.getByTestId("provider-select") as HTMLSelectElement).value).toBe("ollama")
    );

    deferred.resolve(probeResult);
    await waitFor(() =>
      expect((screen.getByTestId("brain-status") as HTMLElement).textContent ?? "").toMatch(
        /descartad/i
      )
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

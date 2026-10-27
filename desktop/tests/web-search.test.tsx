/**
 * Testes renderer WEB-SEARCH-V1 (`desktop/tests/voice-… não; este ficheiro cobre web/** e as
 * montagens). Ponte espiã `window.lunmi` + `window.lunmiPresence`; nenhum backend/rede/GUI.
 *
 * Provas do contrato `.agents/WEB-SEARCH-V1-CONTRACT.md`:
 * — `web.get` com `{}` uma vez, subscrição antes do GET; sem polling; `web.set` só com as
 *   chaves pedidas (`enabled` booleano estrito / `mode` do enum); sem estado otimista (o valor
 *   muda no DTO devolvido ou em `web.changed`); guardas de `session_id` e `revision` (evento
 *   velho ignorado; GET tardio não vence um evento já aplicado); falha de gravação com copy
 *   honesta e valor anterior.
 * — Cartão escondido sem a capability real `web_search`; chips de estado com as frases fixas;
 *   runtime ausente mostra a mensagem do backend, sem fallback.
 * — Fontes: máx. 5; estado de leitura honesto (conteúdo obtido vs apenas excerto) e data real (só `published_at` é publicação; sem
 *   ele mostra-se a recolha — `date_source` é origem, não data); tooltip com o URL público;
 *   abrir envia APENAS `{session_id, task_id, source_id}` (nunca
 *   URL), uma tentativa por fonte em voo; recusas com copy fixa; sem API `openWebSource` não há
 *   botão (nunca finge abertura); funciona em mensagens de tarefas anteriores da mesma sessão.
 *
 * Provas WEB-SEARCH-ORIGIN-V1 (`.agents/WEB-SEARCH-ORIGIN-V1-CONTRACT.md`): seletor de origem só
 * com native_web AVAILABLE + supported + available + can_disable; preferência indisponível
 * preservada (opção nativa desaparece, Lunmi continua e devolve a escolha); default Lunmi com
 * `enabled` V1 intacto; `origin` viaja sempre com o `brain_id` do DTO canónico; desligar a web
 * bloqueia ambas as origens; profundidade não é fingida ao provedor; linha de evidência tipada
 * simples (também em turno `provider` sem fontes, sem lista inventada).
 *
 * Provas WEB-SEARCH-AUTO-V1 (`.agents/WEB-SEARCH-AUTO-V1-CONTRACT.md`): seletor de PREFERÊNCIA
 * com Auto recomendado (default C0) ao lado de Lunmi/nativa; `auto` e `provider` só com prova
 * nativa; escolhas existentes preservadas pelo DTO sem reescrita; `web.set {origin}` transporta
 * a preferência; aviso discreto apenas para `web_fallback` realmente publicado
 * (`provider_recovered` diz a origem efetiva Lunmi; `cleanup_failed` nega nova tentativa;
 * `provider` explícito indisponível nunca oferece Free Search); linha de evidência estende o
 * motivo só com o par `fallback_triggered` + `provider_recovered` do `chat.message`.
 *
 * Provas WEB-SEARCH-REFINEMENT-20261026 (`.agents/WEB-SEARCH-REFINEMENT-20261026.md`): cada
 * fonte é um chip compacto com título + domínio visíveis; o excerto nunca é renderizado; os
 * metadados honestos (lida/encontrada, data real, cortada, cache, motores) ficam no tooltip e
 * no texto acessível; abertura continua por `{session_id, task_id, source_id}` opacos (nunca a
 * URL no IPC), com erros e estados preservados nas variantes normal e compacta; o alvo do chip
 * é 44px nos dois (regra local 0-2-0 sobre `.lunmi-main > section button` 40px, sem `!important`).
 *
 * WEB-SEARCH-PRECISION-V1: as expectativas de copy exata foram migradas para significados
 * igualmente fortes — heading visível só «Fontes da pesquisa», estado de leitura honesto com a
 * ressalva de página inteira/verificação no metadado acessível, `de cache; cache não garante
 * atualização` e eixo da evidência por consulta («evidência suficiente nesta consulta»).
 * Nenhuma guarda comportamental foi removida ou afrouxada.
 */
import path from "node:path";
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WebSearchCard } from "../src/features/web/WebSearchCard";
import { WebSources } from "../src/features/web/WebSources";
import type { WebSourceDto, WebStateDto } from "../src/shared/types";

const NATIVE_OFF = { supported: false, available: false, can_disable: true, state: "UNKNOWN" } as const;
const NATIVE_OK = { supported: true, available: true, can_disable: true, state: "AVAILABLE" } as const;

function webState(overrides: Partial<WebStateDto> = {}): WebStateDto {
  return {
    session_id: "sess-w", revision: 1, enabled: true, mode: "auto", available: true,
    state: "idle", message: "", brain_id: "brain-0123456789abcdef", preference: "auto",
    origin: "lunmi", native_web: { ...NATIVE_OFF }, web_fallback: "", ...overrides,
  };
}

function source(overrides: Partial<WebSourceDto> = {}): WebSourceDto {
  return {
    id: "src-0123456789abcdef", title: "Título da fonte", url: "https://exemple.org/artigo?a=1&b=2",
    snippet: "Resumo do que a fonte diz.", engines: ["duckduckgo"], published_at: "2026-10-20T00:00:00Z",
    date_source: null, retrieved_at: "2026-10-24T10:00:00Z", read: true, truncated: false, cached: false,
    ...overrides,
  };
}

function bridge(initial: Partial<WebStateDto> = {}) {
  const state = webState(initial);
  const calls: Array<{ type: string; payload: Record<string, unknown> }> = [];
  let listener: ((event: { v: 1; type: string; payload: unknown }) => void) | undefined;
  // Espelho da resolução canónica do backend (web_state.py, plano C0 §2): origem efetiva e
  // fallback pré-tarefa. `provider_recovered`/`cleanup_failed` são do controller em voo e só
  // entram por `emit` explícito — a fixture nunca os inventa.
  const resolve = () => {
    const native = state.native_web;
    const selectable = native.state === "AVAILABLE" && native.supported && native.available && native.can_disable;
    let origin: WebStateDto["origin"] = "none";
    if (state.enabled) {
      if (state.preference === "lunmi") origin = state.available ? "lunmi" : "none";
      else if (state.preference === "auto") origin = selectable && state.available ? "provider" : (state.available ? "lunmi" : "none");
      else if (state.preference === "provider") origin = selectable && state.available ? "provider" : "none";
    }
    state.origin = origin;
    state.web_fallback = state.enabled && state.preference === "provider" && origin !== "provider"
      ? "provider_unavailable"
      : state.enabled && state.preference === "lunmi" && origin !== "lunmi" ? "lunmi_unavailable" : "";
  };
  resolve();
  const sendCommand = vi.fn(async (type: string, payload: Record<string, unknown> = {}) => {
    calls.push({ type, payload });
    if (type === "web.get") return { ...state };
    if (type === "web.set") {
      if ("origin" in payload) {
        if (payload.brain_id !== state.brain_id) {
          throw new Error("stale_brain: O cérebro mudou entretanto; a preferência antiga não foi aplicada.");
        }
        if (payload.origin === "provider" && state.native_web.state !== "AVAILABLE") {
          throw new Error("unavailable: A pesquisa nativa não está disponível para este cérebro.");
        }
        state.preference = payload.origin as WebStateDto["preference"];
      }
      if ("enabled" in payload) state.enabled = payload.enabled as boolean;
      if ("mode" in payload) state.mode = payload.mode as WebStateDto["mode"];
      resolve();
      state.revision += 1;
      return { ...state };
    }
    throw new Error(`unexpected_command: ${type}`);
  });
  (window as unknown as { lunmi: unknown }).lunmi = {
    onEvent: (cb: typeof listener) => { listener = cb; return () => { listener = undefined; }; },
    sendCommand,
  };
  const emit = (next: Partial<WebStateDto> & { revision: number }) =>
    listener?.({ v: 1, type: "web.changed", payload: { ...state, ...next } });
  return { calls, state, sendCommand, emit };
}

function presenceBridge(openImpl?: (input: unknown) => Promise<{ opened: boolean; reason: string }>) {
  const openWebSource = vi.fn(openImpl ?? (async () => ({ opened: true, reason: "" })));
  (window as unknown as { lunmiPresence: unknown }).lunmiPresence = { openWebSource };
  return { openWebSource };
}

afterEach(() => {
  delete (window as unknown as { lunmi?: unknown }).lunmi;
  delete (window as unknown as { lunmiPresence?: unknown }).lunmiPresence;
});

describe("useWebState via WebSearchCard", () => {
  it("sem capability web_search real: nada é renderizado nem comandado", async () => {
    const f = bridge();
    render(<WebSearchCard sessionId="sess-w" supportedBySession={false} />);
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(screen.queryByTestId("web-card")).toBeNull();
    expect(f.calls).toHaveLength(0);
  });

  it("get com payload {} uma vez; cartão mostra o estado canónico", async () => {
    const f = bridge({ mode: "deep" });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await waitFor(() => expect(screen.getByTestId("web-card")).toBeTruthy());
    expect(f.calls.filter(c => c.type === "web.get")).toHaveLength(1);
    expect(f.calls.find(c => c.type === "web.get")!.payload).toEqual({});
    expect((screen.getByTestId("web-enabled") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByTestId("web-mode-deep") as HTMLInputElement).checked).toBe(true);
  });

  it("interruptor envia {enabled:false} e o valor só muda no DTO do backend (sem otimismo)", async () => {
    const f = bridge();
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    let release: ((value: WebStateDto) => void) | undefined;
    f.sendCommand.mockImplementationOnce(() => new Promise<WebStateDto>(resolve => { release = resolve; }));
    fireEvent.click(screen.getByTestId("web-enabled"));
    await waitFor(() => expect(f.sendCommand).toHaveBeenCalledWith("web.set", { enabled: false }));
    expect((screen.getByTestId("web-enabled") as HTMLInputElement).checked).toBe(true); // antigo até ao DTO
    release?.({ ...f.state, enabled: false, revision: f.state.revision + 1 });
    await waitFor(() => expect((screen.getByTestId("web-enabled") as HTMLInputElement).checked).toBe(false));
  });

  it("evento de sessão outra ou revisão mais velha é ignorado", async () => {
    const f = bridge({ revision: 5 });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    f.emit({ session_id: "sess-OUTRA", revision: 9, mode: "quick" });
    f.emit({ revision: 4, mode: "deep" });
    await new Promise(resolve => setTimeout(resolve, 0));
    expect((screen.getByTestId("web-mode-auto") as HTMLInputElement).checked).toBe(true);
  });

  it("falha de gravação: copy honesta e valor anterior preservado", async () => {
    const f = bridge();
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    f.sendCommand.mockImplementationOnce(async () => { throw new Error("io"); });
    fireEvent.click(screen.getByTestId("web-mode-quick"));
    await waitFor(() => expect(screen.getByTestId("web-error").textContent).toContain("anterior mantém-se"));
    expect((screen.getByTestId("web-mode-auto") as HTMLInputElement).checked).toBe(true);
  });

  it("runtime ausente: mensagem fixa do backend, sem fallback; chip searching no cartão", async () => {
    const f = bridge({ available: false, preference: "lunmi", message: "Pesquisa web indisponível: runtime não instalado." });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    expect(screen.getByText("Pesquisa web indisponível: runtime não instalado.")).toBeTruthy();
    f.emit({ revision: f.state.revision + 1, available: true, state: "searching" });
    await waitFor(() => expect(screen.getByTestId("web-state").textContent).toBe("A pesquisar na web…"));
  });
});

describe("WebSearchCard — seletor de origem (WEB-SEARCH-ORIGIN-V1)", () => {
  it("sem prova nativa (UNKNOWN/SUPPORTED/UNAVAILABLE): seletor não existe, nada de opção fingida", async () => {
    const f = bridge();
    const { unmount } = render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    expect(screen.queryByTestId("web-origin-group")).toBeNull();
    for (const state of ["SUPPORTED", "UNAVAILABLE"] as const) {
      f.emit({ revision: f.state.revision + 1, native_web: { supported: true, available: state === "SUPPORTED", can_disable: true, state } });
      await new Promise(resolve => setTimeout(resolve, 0));
      expect(screen.queryByTestId("web-origin-group")).toBeNull();
    }
    unmount();
  });

  it("AVAILABLE com todos os sinais: as três preferências; escolha guardada é Auto por defeito", async () => {
    bridge({ native_web: { ...NATIVE_OK } });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    expect((screen.getByTestId("web-origin-auto") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByTestId("web-origin-lunmi") as HTMLInputElement).checked).toBe(false);
    expect((screen.getByTestId("web-origin-provider") as HTMLInputElement).checked).toBe(false);
  });

  it("escolher nativa envia origin com o brain_id canónico; valor só muda no DTO (sem otimismo)", async () => {
    const f = bridge({ native_web: { ...NATIVE_OK } });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    let release: ((value: WebStateDto) => void) | undefined;
    f.sendCommand.mockImplementationOnce(() => new Promise<WebStateDto>(resolve => { release = resolve; }));
    fireEvent.click(screen.getByTestId("web-origin-provider"));
    await waitFor(() => expect(f.sendCommand).toHaveBeenCalledWith(
      "web.set", { origin: "provider", brain_id: "brain-0123456789abcdef" },
    ));
    expect((screen.getByTestId("web-origin-auto") as HTMLInputElement).checked).toBe(true); // antigo até ao DTO (definição C0: auto)
    release?.({ ...f.state, preference: "provider", origin: "provider", revision: f.state.revision + 1 });
    await waitFor(() => expect((screen.getByTestId("web-origin-provider") as HTMLInputElement).checked).toBe(true));
    expect(screen.getByText(/não usa estes controlos/)).toBeTruthy(); // profundidade não se finge ao provedor
  });

  it("preferência indisponível preservada: opção nativa some, aviso discreto, Lunmi devolve a escolha", async () => {
    const f = bridge({ native_web: { ...NATIVE_OK }, preference: "provider", origin: "provider" });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    f.emit({
      revision: f.state.revision + 1, native_web: { supported: true, available: false, can_disable: true, state: "UNAVAILABLE" },
      preference: "provider", origin: "none", web_fallback: "provider_unavailable",
    });
    await waitFor(() => expect(screen.getByTestId("web-fallback-note").textContent)
      .toBe("A pesquisa nativa não está disponível para este cérebro e a Free Search da Lunmi não é oferecida como escolha."));
    expect(screen.queryByTestId("web-origin-provider")).toBeNull();  // sem promessa cinzenta
    expect(screen.getByTestId("web-origin-lunmi")).toBeTruthy();    // escapatória honesta
    fireEvent.click(screen.getByTestId("web-origin-lunmi"));
    await waitFor(() => expect(f.sendCommand).toHaveBeenCalledWith(
      "web.set", { origin: "lunmi", brain_id: "brain-0123456789abcdef" },
    ));
    await waitFor(() => expect((screen.getByTestId("web-origin-lunmi") as HTMLInputElement).checked).toBe(true));
    expect(screen.queryByTestId("web-fallback-note")).toBeNull();
  });

  it("nenhum dos dois runtimes: aviso diz que ambos estão indisponíveis, nunca que a Lunmi responde", async () => {
    const f = bridge({ native_web: { ...NATIVE_OK }, preference: "provider", origin: "provider" });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    f.emit({
      revision: f.state.revision + 1, native_web: { supported: true, available: false, can_disable: true, state: "UNAVAILABLE" },
      available: false, preference: "provider", origin: "none", web_fallback: "provider_unavailable",
    });
    await waitFor(() => expect(screen.getByTestId("web-fallback-note").textContent)
      .toBe("A pesquisa nativa não está disponível e a Free Search também está indisponível."));
    expect(screen.getByTestId("web-fallback-note").textContent).not.toContain("Free Search da Lunmi não é oferecida");
  });

  it("web global desligada bloqueia ambas as origens (e a profundidade)", async () => {
    bridge({ native_web: { ...NATIVE_OK }, enabled: false });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    expect((screen.getByTestId("web-origin-lunmi") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByTestId("web-origin-provider") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByTestId("web-mode-auto") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByTestId("web-enabled") as HTMLInputElement).disabled).toBe(false);
  });

  it("stale_brain devolvido pela ponte: copy fixa, preferência anterior mantém-se", async () => {
    const f = bridge({ native_web: { ...NATIVE_OK } });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    f.sendCommand.mockImplementationOnce(async () => { throw new Error("stale_brain: O cérebro mudou entretanto; a preferência antiga não foi aplicada."); });
    fireEvent.click(screen.getByTestId("web-origin-provider"));
    await waitFor(() => expect(screen.getByTestId("web-error").textContent).toContain("cérebro mudou entretanto"));
    expect((screen.getByTestId("web-origin-auto") as HTMLInputElement).checked).toBe(true);
  });

  it("DTO sem brain_id canónico: preferência nunca é enviada, erro genérico discreto", async () => {
    const f = bridge({ native_web: { ...NATIVE_OK }, brain_id: "" });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    fireEvent.click(screen.getByTestId("web-origin-provider"));
    await waitFor(() => expect(screen.getByTestId("web-error").textContent).toContain("anterior mantém-se"));
    expect(f.calls.filter(c => c.type === "web.set" && "origin" in c.payload)).toHaveLength(0);
  });
});

describe("WebSearchCard — preferência Auto e aviso de recuperação (WEB-SEARCH-AUTO-V1)", () => {
  it("Auto recomendado: escolher auto envia origin auto com brain_id; só muda no DTO", async () => {
    const f = bridge({ native_web: { ...NATIVE_OK }, preference: "lunmi" });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    expect((screen.getByTestId("web-origin-lunmi") as HTMLInputElement).checked).toBe(true); // escolha existente preservada
    let release: ((value: WebStateDto) => void) | undefined;
    f.sendCommand.mockImplementationOnce(() => new Promise<WebStateDto>(resolve => { release = resolve; }));
    fireEvent.click(screen.getByTestId("web-origin-auto"));
    await waitFor(() => expect(f.sendCommand).toHaveBeenCalledWith(
      "web.set", { origin: "auto", brain_id: "brain-0123456789abcdef" },
    ));
    expect((screen.getByTestId("web-origin-lunmi") as HTMLInputElement).checked).toBe(true); // antigo até ao DTO
    release?.({ ...f.state, preference: "auto", origin: "provider", revision: f.state.revision + 1 });
    await waitFor(() => expect((screen.getByTestId("web-origin-auto") as HTMLInputElement).checked).toBe(true));
  });

  it("sem prova nativa: sem escolha real nada se oferece; auto/nativa nunca fingidos", async () => {
    const f = bridge(); // preferência canónica por defeito: auto, sem provedor provado
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    expect(screen.queryByTestId("web-origin-group")).toBeNull(); // auto ≡ lunmi sem provedor: nada de selector
    for (const state of ["SUPPORTED", "UNAVAILABLE", "UNKNOWN"] as const) {
      f.emit({
        revision: f.state.revision + 1, native_web: { supported: true, available: false, can_disable: true, state },
        preference: "auto", origin: "lunmi", web_fallback: "",
      });
      await new Promise(resolve => setTimeout(resolve, 0));
      expect(screen.queryByTestId("web-origin-provider")).toBeNull();
      expect(screen.queryByTestId("web-origin-auto")).toBeNull();
    }
  });

  it("preferência explícita provider indisponível: diz indisponibilidade, nunca oferece Free Search", async () => {
    const f = bridge({ native_web: { ...NATIVE_OK }, preference: "provider", origin: "provider" });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    f.emit({
      revision: f.state.revision + 1, preference: "provider", origin: "none", web_fallback: "provider_unavailable",
    });
    await waitFor(() => expect(screen.getByTestId("web-fallback-note").textContent)
      .toBe("A pesquisa nativa não está disponível para este cérebro e a Free Search da Lunmi não é oferecida como escolha."));
    expect(screen.getByTestId("web-fallback-note").textContent).not.toMatch(/resposta.*vem.*Free Search|recupera/);
  });

  it("aviso de recuperação só com provider_recovered publicado; some quando o backend limpa o motivo", async () => {
    const f = bridge({ native_web: { ...NATIVE_OK }, preference: "auto", origin: "provider" });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    expect(screen.queryByTestId("web-fallback-note")).toBeNull(); // plano sem falha observada: nunca inventar
    f.emit({
      revision: f.state.revision + 1, preference: "auto", origin: "lunmi",
      web_fallback: "provider_recovered", native_web: { ...NATIVE_OK },
    });
    await waitFor(() => expect(screen.getByTestId("web-fallback-note").textContent)
      .toBe("A pesquisa nativa do Brain falhou neste pedido; a resposta vem da Free Search da Lunmi."));
    expect((screen.getByTestId("web-origin-auto") as HTMLInputElement).checked).toBe(true); // preferência não reescrita pela recuperação
    f.emit({ revision: f.state.revision + 1, preference: "auto", origin: "provider", web_fallback: "" });
    await waitFor(() => expect(screen.queryByTestId("web-fallback-note")).toBeNull());
  });

  it("cleanup_failed: aviso diz que não houve nova tentativa; provider_recovered nunca é afirmado", async () => {
    const f = bridge({ native_web: { ...NATIVE_OK }, preference: "auto", origin: "provider" });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    f.emit({ revision: f.state.revision + 1, preference: "auto", origin: "none", web_fallback: "cleanup_failed" });
    await waitFor(() => expect(screen.getByTestId("web-fallback-note").textContent)
      .toBe("A pesquisa nativa não pôde ser finalizada em segurança; não houve nova tentativa e esta resposta não teve pesquisa web."));
    expect(screen.getByTestId("web-fallback-note").textContent).not.toContain("Free Search");
  });

  it("preferência existente lunmi preservada: DTO mostra lunmi, nada é reescrito nem comandado", async () => {
    const f = bridge({ native_web: { ...NATIVE_OK }, preference: "lunmi", origin: "lunmi" });
    render(<WebSearchCard sessionId="sess-w" supportedBySession />);
    await screen.findByTestId("web-card");
    expect((screen.getByTestId("web-origin-lunmi") as HTMLInputElement).checked).toBe(true);
    expect(f.calls.filter(c => c.type === "web.set")).toHaveLength(0);
  });
});

describe("WebSources — evidência tipada por origem", () => {
  it("turno provider sem fontes: linha discreta de origem e estado; lista nunca inventada", () => {
    presenceBridge();
    render(<WebSources sources={[]} sessionId="sess-w" taskId="task-1" searchSource="provider" evidenceStatus="insufficient" requestStatus="unmet" />);
    const line = screen.getByTestId("web-evidence");
    expect(line.textContent).toContain("pesquisa nativa do Brain");
    expect(line.textContent).toContain("fontes insuficientes para responder");
    expect(line.textContent).toContain("pedido não atendido");
    expect(screen.queryByText(/Fontes da pesquisa/)).toBeNull();
    expect(screen.queryAllByTestId(/^web-source-src-/)).toHaveLength(0);
  });

  it("com fontes: linha acompanha a lista canónica; sem campos de origem: nada de linha", () => {
    presenceBridge();
    const view = render(<WebSources sources={[source({ id: "src-1" })]} sessionId="sess-w" taskId="task-1" searchSource="lunmi" evidenceStatus="suff" requestStatus="met" />);
    expect(view.getByTestId("web-evidence").textContent).toContain("Free Search da Lunmi · evidência suficiente nesta consulta · pedido atendido");
    expect(view.getByText(/^Fontes da pesquisa$/)).toBeTruthy();
    view.unmount();
    render(<WebSources sources={[source({ id: "src-2" })]} sessionId="sess-w" taskId="task-1" />);
    expect(screen.queryByTestId("web-evidence")).toBeNull();
  });

  it("request unknown/evidence null: só a origem, sem alegar atendimento", () => {
    const view = render(<WebSources sources={[]} sessionId="sess-w" taskId="task-1" searchSource="provider" evidenceStatus={null} requestStatus="unknown" />);
    expect(view.getByTestId("web-evidence").textContent).toBe("Origem da resposta: pesquisa nativa do Brain");
    view.unmount();
    render(<WebSources sources={[]} sessionId="sess-w" taskId="task-1" searchSource="none" />);
    expect(screen.queryByTestId("web-sources")).toBeNull();
  });

  it("recuperação auto provada: origem efetiva Lunmi com motivo; sem o par, nada de afirmar recuperação", () => {
    const view = render(<WebSources sources={[source({ id: "src-rec" })]} sessionId="sess-w" taskId="task-1"
      searchSource="lunmi" evidenceStatus="suff" requestStatus="met" fallbackTriggered webFallback="provider_recovered" />);
    expect(view.getByTestId("web-evidence").textContent)
      .toBe("Origem da resposta: Free Search da Lunmi · evidência suficiente nesta consulta · pedido atendido — após falha da pesquisa nativa neste pedido");
    view.unmount();
    const semProva = render(<WebSources sources={[source({ id: "src-sempre" })]} sessionId="sess-w" taskId="task-1"
      searchSource="lunmi" evidenceStatus="suff" requestStatus="met" fallbackTriggered webFallback="" />);
    expect(semProva.getByTestId("web-evidence").textContent).not.toContain("após falha");
    semProva.unmount();
    const soFlag = render(<WebSources sources={[source({ id: "src-flag" })]} sessionId="sess-w" taskId="task-1"
      searchSource="lunmi" fallbackTriggered={false} webFallback="provider_recovered" />);
    expect(soFlag.getByTestId("web-evidence").textContent).not.toContain("após falha");
  });
});

describe("WebSources — verdade das fontes e abertura opaca", () => {
  it("renderiza no máx. 5, com estado de leitura honesto e data real no metadado acessível; tooltip com o URL público", () => {
    presenceBridge();
    const sources = Array.from({ length: 6 }, (_, i) => source({ id: `src-000000000000000${i}`, title: `Fonte ${i}` }));
    render(<WebSources sources={sources} sessionId="sess-w" taskId="task-1" />);
    expect(screen.getAllByTestId(/^web-source-src-/)).toHaveLength(5);
    expect(screen.getByTestId("web-source-read-src-0000000000000000").textContent)
      .toContain("conteúdo da página obtido; não é prova de página inteira nem verificação pela Lunmi");
    const unread = source({ id: "src-unread", read: false, published_at: null, date_source: null });
    render(<WebSources sources={[unread]} sessionId="sess-w" taskId="task-1" />);
    expect(screen.getByTestId("web-source-read-src-unread").textContent)
      .toContain("apenas excerto de pesquisa; conteúdo não obtido nem verificado pela Lunmi");
    expect(screen.getByText(/acedido 2026-10-24/)).toBeTruthy();
    const tooltip = (screen.getByTestId("web-source-open-src-unread") as HTMLButtonElement).title;
    expect(tooltip).toContain("https://exemple.org/artigo?a=1&b=2");
    expect(tooltip).toContain("apenas excerto de pesquisa");
    expect(tooltip).toContain("acedido 2026-10-24");
    expect(tooltip).toContain("motores: duckduckgo");
  });

  it("chip compacto: título e domínio visíveis, sem excerto nem blocos; metadados só acessíveis", () => {
    presenceBridge();
    const s = source({ id: "src-chip", truncated: true, cached: true });
    const { container } = render(<WebSources sources={[s]} sessionId="sess-w" taskId="task-1" />);
    const chip = screen.getByTestId("web-source-open-src-chip");
    expect(chip.textContent).toBe("Título da fonteexemple.orgconteúdo da página obtido; não é prova de página inteira nem verificação pela Lunmi · publicado 2026-10-20 · conteúdo cortado · de cache; cache não garante atualização · motores: duckduckgo");
    expect(chip.querySelector(".lunmi-web-source-title")!.textContent).toBe("Título da fonte");
    expect(chip.querySelector(".lunmi-web-source-domain")!.textContent).toBe("exemple.org");
    expect(container.querySelector(".lunmi-web-source-snippet")).toBeNull();
    expect(chip.closest("li")!.getAttribute("data-read")).toBe("true");
  });

  it("alvo tátil do chip: 44px por regra local com especificidade sobre o piso global (sem !important)", () => {
    const css = readFileSync(path.resolve(__dirname, "../src/features/web/web.css"), "utf8");
    const global = readFileSync(path.resolve(__dirname, "../src/styles.css"), "utf8");
    const local = css.match(/^\.lunmi-web-sources \.lunmi-web-source-open[^{]*\{([^}]*)\}/m);
    expect(local, "regra local com par de classes deve existir").toBeTruthy();
    expect(local![1]).toContain("min-height: 44px");
    expect(css.replace(/\/\*[\s\S]*?\*\//g, "")).not.toContain("!important");
    expect(css).not.toMatch(/\.lunmi-web-source-(open|static)[^{]*\{[^}]*min-height: 3[0-9]px/);
    expect(global).toMatch(/\.lunmi-main > section button \{ min-height: 40px; \}/); // 0-1-2, vencido por 0-2-0
  });

  it("variante compacta mantém o chip sem excerto e abre pelos mesmos ids opacos", async () => {
    const { openWebSource } = presenceBridge();
    const s = source({ id: "src-mini" });
    const { container } = render(<WebSources compact sources={[s]} sessionId="sess-w" taskId="task-m" />);
    expect(container.querySelector(".lunmi-web-source-snippet")).toBeNull();
    expect(screen.getByTestId("web-sources").getAttribute("data-compact")).toBe("true");
    fireEvent.click(screen.getByTestId("web-source-open-src-mini"));
    await waitFor(() => expect(openWebSource).toHaveBeenCalledWith({ session_id: "sess-w", task_id: "task-m", source_id: "src-mini" }));
  });

  it("sem API de abertura: chip estático conserva título, domínio e metadados acessíveis", () => {
    const s = source({ id: "src-static", read: false, published_at: null });
    const view = render(<WebSources sources={[s]} sessionId="sess-w" taskId="task-1" />);
    expect(view.queryByTestId("web-source-open-src-static")).toBeNull();
    const chip = view.getByText("Título da fonte").closest(".lunmi-web-source-static")!;
    expect(chip.textContent).toContain("exemple.org");
    expect(chip.textContent).toContain("apenas excerto de pesquisa");
    expect(chip.textContent).toContain("acedido 2026-10-24");
    expect(chip.getAttribute("title")).toContain("https://exemple.org/artigo?a=1&b=2");
    view.unmount();
  });

  it("sem published_at: mostra-se a recolha mesmo com date_source — origem não é data de publicação", () => {
    presenceBridge();
    const s = source({ id: "src-datesrc", published_at: null, date_source: "snippet", retrieved_at: "2026-10-24T20:00:00Z" });
    render(<WebSources sources={[s]} sessionId="sess-w" taskId="task-1" />);
    expect(screen.getByText(/acedido 2026-10-24/)).toBeTruthy();
    expect(screen.queryByText(/data \(snippet\)/)).toBeNull();
    expect(screen.queryByText(/publicado/)).toBeNull();
  });

  it("abrir envia apenas ids opacos à API da presença; nunca a URL", async () => {
    const { openWebSource } = presenceBridge();
    const s = source({ id: "src-abc" });
    render(<WebSources sources={[s]} sessionId="sess-w" taskId="task-9" />);
    fireEvent.click(screen.getByTestId("web-source-open-src-abc"));
    await waitFor(() => expect(openWebSource).toHaveBeenCalledTimes(1));
    const payload = openWebSource.mock.calls[0][0] as Record<string, unknown>;
    expect(Object.keys(payload).sort()).toEqual(["session_id", "source_id", "task_id"]);
    expect(JSON.stringify(payload)).not.toContain("exemple.org");
  });

  it("recusa not_found com copy fixa; repetição em voo não duplica o pedido", async () => {
    let release: ((value: { opened: boolean; reason: string }) => void) | undefined;
    const { openWebSource } = presenceBridge(() => new Promise(resolve => { release = resolve; }));
    render(<WebSources sources={[source({ id: "src-x" })]} sessionId="sess-w" taskId="task-1" />);
    fireEvent.click(screen.getByTestId("web-source-open-src-x"));
    fireEvent.click(screen.getByTestId("web-source-open-src-x"));
    expect(openWebSource).toHaveBeenCalledTimes(1);
    release?.({ opened: false, reason: "not_found" });
    await waitFor(() => expect(screen.getByTestId("web-source-error-src-x").textContent).toContain("já não está disponível"));
  });

  it("sem API openWebSource (preload antigo): texto sem botão, nunca finge abertura", () => {
    render(<WebSources sources={[source({ id: "src-y" })]} sessionId="sess-w" taskId="task-1" />);
    expect(screen.queryByTestId("web-source-open-src-y")).toBeNull();
    expect(screen.getByTestId("web-source-src-y").textContent).toContain("Título da fonte");
  });

  it("fontes de turnos anteriores da mesma sessão continuam abríveis pelo seu task_id", async () => {
    const { openWebSource } = presenceBridge();
    render(<WebSources sources={[source({ id: "src-old" })]} sessionId="sess-w" taskId="task-antiga" />);
    fireEvent.click(screen.getByTestId("web-source-open-src-old"));
    await waitFor(() => expect(openWebSource).toHaveBeenCalledWith({ session_id: "sess-w", task_id: "task-antiga", source_id: "src-old" }));
  });
});

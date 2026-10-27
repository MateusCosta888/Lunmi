/**
 * WEB-SEARCH-PRECISION-V1 — copy da UI de fontes (`desktop/tests/web-precision-copy.test.tsx`).
 *
 * Prova apenas copy/semântica pública contra os DTOs existentes
 * (`.agents/WEB-SEARCH-PRECISION-V1-EXECUTION.md`, secção Designer): nenhum CSS, layout, DTO,
 * backend ou shared é alterado aqui. O que este ficheiro garante:
 * — a secção visível é curta («Fontes da pesquisa»); a explicação do que `read` significa vive no
 *   metadado acessível e no tooltip de cada chip, não num parágrafo longo repetido;
 * — `read` diz conteúdo obtido, sem afirmar página inteira nem verificação da Lunmi; `!read` diz
 *   que só existe excerto de pesquisa;
 * — cache avisa que não garante atualização; data, corte e motores continuam presentes;
 * — os eixos estão separados: evidência fala desta consulta (`evidence_status`) e atendimento do
 *   pedido vem só de `request_status`; `unknown` continua silencioso, sem novo campo nem alerta;
 * — invariáveis preservadas: chips acessíveis com tooltip, abertura só por IDs opacos, recusa
 *   visível e nenhuma lista inventada quando não há fontes.
 *
 * Ponte espiã `window.lunmiPresence`; nenhum backend, rede ou GUI.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { WebSources } from "../src/features/web/WebSources";
import type { WebSourceDto } from "../src/shared/types";

function source(overrides: Partial<WebSourceDto> = {}): WebSourceDto {
  return {
    id: "src-0123456789abcdef", title: "Título da fonte", url: "https://exemple.org/artigo?a=1",
    snippet: "Resumo do que a fonte diz.", engines: ["duckduckgo"], published_at: "2026-10-20T00:00:00Z",
    date_source: null, retrieved_at: "2026-10-26T10:00:00Z", read: true, truncated: false, cached: false,
    ...overrides,
  };
}

function presenceBridge(openImpl?: (input: unknown) => Promise<{ opened: boolean; reason: string }>) {
  const openWebSource = vi.fn(openImpl ?? (async () => ({ opened: true, reason: "" })));
  (window as unknown as { lunmiPresence: unknown }).lunmiPresence = { openWebSource };
  return { openWebSource };
}

afterEach(() => {
  delete (window as unknown as { lunmiPresence?: unknown }).lunmiPresence;
});

describe("WebSources — copy precisa das fontes (PRECISION V1)", () => {
  it("secção visível curta: só «Fontes da pesquisa», sem parágrafo de aviso repetido", () => {
    presenceBridge();
    render(<WebSources sources={[source({ id: "src-head" }), source({ id: "src-head2" })]} sessionId="sess-p" taskId="task-1" />);
    const heading = screen.getByText(/^Fontes da pesquisa$/);
    expect(heading.textContent).toBe("Fontes da pesquisa");
    expect(heading.textContent).not.toContain("verificação");
    expect(heading.textContent).not.toContain("conteúdo externo");
  });

  it("read=true: metadado diz conteúdo obtido, sem afirmar página inteira nem verificação", () => {
    presenceBridge();
    render(<WebSources sources={[source({ id: "src-read" })]} sessionId="sess-p" taskId="task-1" />);
    const meta = screen.getByTestId("web-source-read-src-read").textContent ?? "";
    expect(meta).toContain("conteúdo da página obtido");
    expect(meta).toContain("não é prova de página inteira nem verificação pela Lunmi");
    expect(meta).not.toMatch(/verificada pela Lunmi|página inteira confirmada/);
    // visível continua a ser só título + domínio; a explicação não está no texto visto
    const visible = screen.getByTestId("web-source-open-src-read");
    expect(visible.querySelector(".lunmi-web-source-title")!.textContent).toBe("Título da fonte");
    expect(visible.querySelector(".lunmi-web-source-domain")!.textContent).toBe("exemple.org");
  });

  it("read=false: metadado diz apenas excerto de pesquisa e nega obtenção/verificação", () => {
    presenceBridge();
    render(<WebSources sources={[source({ id: "src-snip", read: false })]} sessionId="sess-p" taskId="task-1" />);
    const meta = screen.getByTestId("web-source-read-src-snip").textContent ?? "";
    expect(meta).toContain("apenas excerto de pesquisa");
    expect(meta).toContain("conteúdo não obtido nem verificado pela Lunmi");
    expect(meta).not.toContain("conteúdo da página obtido");
  });

  it("metadado preserva data, corte e cache com a ressalva de frescor", () => {
    presenceBridge();
    render(<WebSources sources={[source({ id: "src-meta", truncated: true, cached: true, published_at: null })]} sessionId="sess-p" taskId="task-1" />);
    const meta = screen.getByTestId("web-source-read-src-meta").textContent ?? "";
    expect(meta).toContain("acedido 2026-10-26");
    expect(meta).toContain("conteúdo cortado");
    expect(meta).toContain("de cache; cache não garante atualização");
    expect(meta).toContain("motores: duckduckgo");
    expect(meta).not.toContain("publicado");
    expect(meta.indexOf("acedido")).toBeLessThan(meta.indexOf("conteúdo cortado")); // ordem real preservada
    expect(meta.indexOf("conteúdo cortado")).toBeLessThan(meta.indexOf("de cache"));
    expect(meta.indexOf("de cache")).toBeLessThan(meta.indexOf("motores:"));
  });

  it("eixos separados: evidência desta consulta nunca vira «pedido atendido» sem request_status", () => {
    presenceBridge();
    const soEvidencia = render(<WebSources sources={[source({ id: "src-e1" })]} sessionId="sess-p" taskId="task-1"
      searchSource="lunmi" evidenceStatus="suff" />);
    const linhaE = soEvidencia.getByTestId("web-evidence");
    expect(linhaE.textContent).toContain("evidência suficiente nesta consulta");
    expect(linhaE.textContent).not.toContain("atendido"); // conclusão/peso da evidência ≠ resposta ao pedido
    expect(linhaE.textContent).not.toMatch(/verificada|universal/);
    soEvidencia.unmount();

    const comPedido = render(<WebSources sources={[source({ id: "src-e2" })]} sessionId="sess-p" taskId="task-1"
      searchSource="lunmi" evidenceStatus="suff" requestStatus="met" />);
    expect(comPedido.getByTestId("web-evidence").textContent)
      .toContain("evidência suficiente nesta consulta · pedido atendido");
    comPedido.unmount();

    const doisEixos = render(<WebSources sources={[source({ id: "src-e3" })]} sessionId="sess-p" taskId="task-1"
      searchSource="lunmi" evidenceStatus="partial" requestStatus="unmet" />);
    const linha3 = doisEixos.getByTestId("web-evidence");
    expect(linha3.textContent).toContain("evidência parcial nesta consulta");
    expect(linha3.textContent).toContain("pedido não atendido");
  });

  it("request_status unknown continua silencioso: sem novo campo, sem nova alerta", () => {
    presenceBridge();
    const { container } = render(<WebSources sources={[source({ id: "src-u" })]} sessionId="sess-p" taskId="task-1"
      searchSource="provider" evidenceStatus="suff" requestStatus="unknown" />);
    const line = screen.getByTestId("web-evidence");
    expect(line.textContent).toBe("Origem da resposta: pesquisa nativa do Brain · evidência suficiente nesta consulta");
    expect(line.textContent).not.toContain("atendido");
    expect(container.querySelectorAll("[role=alert]")).toHaveLength(0);
    expect(container.querySelectorAll("[role=status]")).toHaveLength(0);
  });

  it("insuficiente + não atendido: limitação honesta e nenhuma lista inventada", () => {
    presenceBridge();
    const view = render(<WebSources sources={[]} sessionId="sess-p" taskId="task-1"
      searchSource="lunmi" evidenceStatus="insufficient" requestStatus="unmet" />);
    const line = view.getByTestId("web-evidence");
    expect(line.textContent).toContain("fontes insuficientes para responder");
    expect(line.textContent).toContain("pedido não atendido");
    expect(view.queryByText(/Fontes da pesquisa/)).toBeNull(); // sem fontes: sem heading nem lista
    expect(view.queryAllByTestId(/^web-source-src-/)).toHaveLength(0);
  });

  it("invariáveis preservadas: chip acessível, tooltip com URL e metadado, abertura só por IDs opacos", async () => {
    const { openWebSource } = presenceBridge();
    render(<WebSources sources={[source({ id: "src-open" })]} sessionId="sess-p" taskId="task-7" />);
    const chip = screen.getByTestId("web-source-open-src-open");
    expect(chip.tagName).toBe("BUTTON");
    expect(chip.className).toContain("lunmi-web-source-open");
    const tooltip = chip.getAttribute("title") ?? "";
    expect(tooltip).toContain("https://exemple.org/artigo?a=1");
    expect(tooltip).toContain("conteúdo da página obtido");
    fireEvent.click(chip);
    await waitFor(() => expect(openWebSource).toHaveBeenCalledTimes(1));
    const payload = openWebSource.mock.calls[0][0] as Record<string, unknown>;
    expect(Object.keys(payload).sort()).toEqual(["session_id", "source_id", "task_id"]);
    expect(JSON.stringify(payload)).not.toContain("exemple.org");
  });

  it("invariáveis preservadas: recusa visível e chip estático sem a API da presença", async () => {
    presenceBridge(async () => ({ opened: false, reason: "blocked_url" }));
    const view = render(<WebSources sources={[source({ id: "src-block" })]} sessionId="sess-p" taskId="task-1" />);
    fireEvent.click(view.getByTestId("web-source-open-src-block"));
    await waitFor(() => expect(view.getByTestId("web-source-error-src-block").textContent)
      .toContain("bloqueado por segurança"));
    view.unmount();

    delete (window as unknown as { lunmiPresence?: unknown }).lunmiPresence; // preload antigo: sem API de abertura
    const semApi = render(<WebSources sources={[source({ id: "src-static", read: false })]} sessionId="sess-p" taskId="task-1" />);
    expect(semApi.queryByTestId("web-source-open-src-static")).toBeNull();
    expect(semApi.getByTestId("web-source-src-static").textContent).toContain("Título da fonte");
    expect(semApi.getByTestId("web-source-read-src-static").textContent).toContain("apenas excerto de pesquisa");
    expect(semApi.getByText(/^Fontes da pesquisa$/)).toBeTruthy();
  });
});

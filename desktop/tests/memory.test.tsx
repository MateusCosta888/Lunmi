/**
 * Testes do painel de memória (`desktop/tests/memory.test.tsx`, F2b).
 *
 * Nenhum backend real: `sendCommand` é um espião tipado com fixtures. Os testes provam o
 * contrato observável — nomes de comando, payloads exatos, confirmação em dois passos, cancelar
 * como não-erro, filtro de kinds, opções booleanas, descarte de resposta fora de ordem e a
 * separação obrigatória entre `memory.propose` (novo) e `memory.upsert` (existente).
 */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryPanel } from "../src/features/memory/MemoryPanel";
import type { SendCommandFn } from "../src/shared/ipc/client";
import type { SessionReadyPayload } from "../src/shared/protocol";
import type { MemoryItemDto, MemoryOptionsDto } from "../src/shared/types";

const SESSION: SessionReadyPayload = {
  v: 1,
  session_id: "sess-test",
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

const OPTIONS: MemoryOptionsDto = {
  enabled: true,
  episodes: true,
  proposals: true,
  history: false,
};

function item(id: string, text: string, kind = "preference"): MemoryItemDto {
  return {
    id,
    kind,
    text,
    status: "active",
    source: "user_explicit",
    updated_at: "2026-10-14T10:00:00.000000+00:00",
    created_at: "2026-10-14T09:00:00.000000+00:00",
  };
}

type Call = { type: string; payload: Record<string, unknown> };

function makeBridge(handler: (type: string, payload: Record<string, unknown>) => unknown): {
  sendCommand: SendCommandFn;
  calls: Call[];
} {
  const calls: Call[] = [];
  const spy = vi.fn(async (type: string, payload: Record<string, unknown> = {}) => {
    calls.push({ type, payload });
    return handler(type, payload);
  });
  return { sendCommand: spy as unknown as SendCommandFn, calls };
}

function defaultHandler(overrides: Record<string, unknown> = {}) {
  return (type: string): unknown => {
    if (type in overrides) {
      return overrides[type];
    }
    if (type === "memory.list" || type === "memory.search") {
      return { enabled: true, items: [item("m1", "Prefere temas escuros")] };
    }
    if (type === "memory.proposals") {
      return { proposals: [item("p1", "Usa atalhos de teclado")] };
    }
    if (type === "memory.options.get") {
      return { options: OPTIONS };
    }
    if (type === "memory.options.set") {
      return { options: OPTIONS };
    }
    if (type === "memory.propose") {
      return { proposal_id: "p2" };
    }
    if (type === "memory.accept") {
      return { memory_id: "m9" };
    }
    if (type === "memory.reject") {
      return { rejected: true, id: "p1" };
    }
    if (type === "memory.upsert") {
      return { item: item("m1", "atualizado") };
    }
    if (type === "memory.delete") {
      return { deleted: true, id: "m1" };
    }
    if (type === "memory.wipe") {
      return { options: OPTIONS };
    }
    if (type === "memory.export") {
      return { exported: true, characters: 123 };
    }
    if (type === "memory.import") {
      return { imported: true, inserted: 1 };
    }
    throw new Error(`unexpected_command: ${type}`);
  };
}

function renderPanel(bridge: { sendCommand: SendCommandFn }, refreshToken = 0) {
  return render(
    <MemoryPanel sendCommand={bridge.sendCommand} session={SESSION} refreshToken={refreshToken} />
  );
}

async function waitForList(): Promise<void> {
  await waitFor(() => expect(screen.getByTestId("memory-item-m1")).toBeTruthy());
}

describe("MemoryPanel (F2b)", () => {
  it("explica armazenamento local sem prometer inferência privada", async () => {
    renderPanel(makeBridge(defaultHandler()));
    await waitForList();
    expect(screen.getByTestId("memory-panel").textContent).toContain("Trechos relevantes podem ser enviados ao provedor escolhido");
    expect(screen.getByTestId("memory-panel").textContent).not.toContain("fica apenas neste computador");
  });

  it("carrega lista, propostas e opções com os comandos e limites exatos", async () => {
    const bridge = makeBridge(defaultHandler());
    renderPanel(bridge);
    await waitForList();

    const listCall = bridge.calls.find((c) => c.type === "memory.list");
    expect(listCall).toBeDefined();
    expect(listCall!.payload).toEqual({ query: "", limit: 100 });
    expect(bridge.calls.some((c) => c.type === "memory.proposals")).toBe(true);
    expect(bridge.calls.some((c) => c.type === "memory.options.get")).toBe(true);

    // Limite fora da faixa é trazido para 1..100 antes de sair.
    fireEvent.change(screen.getByTestId("memory-limit"), { target: { value: "0" } });
    fireEvent.click(screen.getByTestId("memory-list-button"));
    await waitFor(() => {
      const last = [...bridge.calls].reverse().find((c) => c.type === "memory.list" && c.payload.limit === 1);
      expect(last).toBeDefined();
    });

    fireEvent.change(screen.getByTestId("memory-limit"), { target: { value: "250" } });
    fireEvent.click(screen.getByTestId("memory-list-button"));
    await waitFor(() => {
      const last = [...bridge.calls].reverse().find((c) => c.type === "memory.list" && c.payload.limit === 100);
      expect(last).toBeTruthy();
    });

    // Busca com filtro de kinds manda `kinds` exatamente uma vez por kind marcado.
    fireEvent.change(screen.getByTestId("memory-search"), { target: { value: "tema" } });
    fireEvent.click(screen.getByTestId("kind-filter-fact"));
    fireEvent.click(screen.getByTestId("memory-search-button"));
    await waitFor(() => {
      const search = bridge.calls.find((c) => c.type === "memory.search");
      expect(search).toBeDefined();
      expect(search!.payload.kinds).toEqual(["fact"]);
      expect(search!.payload.query).toBe("tema");
    });
  });

  it("só envia ação destrutiva depois do segundo passo, sempre com confirmed: true", async () => {
    const bridge = makeBridge(defaultHandler());
    renderPanel(bridge);
    await waitForList();

    // Apagar: primeiro clique só arma a confirmação.
    fireEvent.click(screen.getByTestId("memory-delete-m1"));
    expect(bridge.calls.some((c) => c.type === "memory.delete")).toBe(false);
    fireEvent.click(screen.getByTestId("confirm-delete-m1"));
    await waitFor(() => {
      const call = bridge.calls.find((c) => c.type === "memory.delete");
      expect(call).toBeDefined();
      expect(call!.payload).toEqual({ id: "m1", confirmed: true });
    });

    // Limpar tudo: idem.
    fireEvent.click(screen.getByTestId("wipe-memory"));
    expect(bridge.calls.some((c) => c.type === "memory.wipe")).toBe(false);
    fireEvent.click(screen.getByTestId("confirm-wipe"));
    await waitFor(() => {
      const call = bridge.calls.find((c) => c.type === "memory.wipe");
      expect(call).toBeDefined();
      expect(call!.payload).toEqual({ confirmed: true });
    });

    // Importar: idem, com o diálogo nativo pedido pelo main.
    fireEvent.click(screen.getByTestId("import-memory"));
    expect(bridge.calls.some((c) => c.type === "memory.import")).toBe(false);
    fireEvent.click(screen.getByTestId("confirm-import"));
    await waitFor(() => {
      const call = bridge.calls.find((c) => c.type === "memory.import");
      expect(call).toBeDefined();
      expect(call!.payload).toEqual({ confirmed: true, use_native_dialog: true });
    });
  });

  it("trata cancelamento do diálogo nativo como não-erro", async () => {
    const bridge = makeBridge(
      defaultHandler({
        "memory.export": { exported: false, cancelled: true },
        "memory.import": { imported: false, cancelled: true },
      })
    );
    renderPanel(bridge);
    await waitForList();

    fireEvent.click(screen.getByTestId("export-memory"));
    await waitFor(() => {
      expect(screen.getByTestId("memory-notice").textContent).toContain("cancel");
    });
    expect(screen.queryByTestId("memory-error")).toBeNull();

    fireEvent.click(screen.getByTestId("import-memory"));
    fireEvent.click(screen.getByTestId("confirm-import"));
    await waitFor(() => {
      expect(screen.getByTestId("memory-notice").textContent).toContain("cancel");
    });
    expect(screen.queryByTestId("memory-error")).toBeNull();
  });

  it("envia o mapa booleano completo ao alternar uma opção", async () => {
    const bridge = makeBridge(defaultHandler());
    renderPanel(bridge);
    await waitForList();

    fireEvent.click(screen.getByTestId("memory-option-history"));
    await waitFor(() => {
      const call = bridge.calls.find((c) => c.type === "memory.options.set");
      expect(call).toBeDefined();
      const options = call!.payload.options as Record<string, unknown>;
      expect(options).toEqual({
        enabled: true,
        episodes: true,
        proposals: true,
        history: true,
      });
      expect(Object.values(options).every((value) => typeof value === "boolean")).toBe(true);
    });
  });

  it("descarta resposta antiga de memory.list que chega depois da nova", async () => {
    const resolvers: Array<(value: unknown) => void> = [];
    const bridge = makeBridge((type) => {
      if (type === "memory.list") {
        return new Promise((resolve) => resolvers.push(resolve));
      }
      return defaultHandler()(type);
    });

    const view = renderPanel(bridge, 0);
    await waitFor(() => expect(bridge.calls.filter((c) => c.type === "memory.list").length).toBe(1));

    // O refreshToken novo dispara a segunda carga (resposta mais nova).
    view.rerender(
      <MemoryPanel
        sendCommand={bridge.sendCommand}
        session={SESSION}
        refreshToken={1}
      />
    );
    await waitFor(() => expect(bridge.calls.filter((c) => c.type === "memory.list").length).toBe(2));

    // A resposta nova chega primeiro…
    resolvers[1]({ enabled: true, items: [item("novo", "conteúdo novo")] });
    await waitFor(() => expect(screen.getByTestId("memory-item-novo")).toBeTruthy());

    // …e a antiga, atrasada, é ignorada.
    resolvers[0]({ enabled: true, items: [item("antigo", "conteúdo antigo")] });
    await waitFor(() => expect(screen.getByTestId("memory-item-novo")).toBeTruthy());
    expect(screen.queryByTestId("memory-item-antigo")).toBeNull();
  });

  it("separa proposta de memória nova de atualização de memória existente", async () => {
    const bridge = makeBridge(defaultHandler());
    renderPanel(bridge);
    await waitForList();

    fireEvent.change(screen.getByTestId("propose-kind"), { target: { value: "fact" } });
    fireEvent.change(screen.getByTestId("propose-text"), { target: { value: "Usa Windows 11" } });
    fireEvent.click(screen.getByTestId("propose-memory"));
    await waitFor(() => {
      const call = bridge.calls.find((c) => c.type === "memory.propose");
      expect(call).toBeDefined();
      expect(call!.payload).toEqual({ kind: "fact", text: "Usa Windows 11" });
    });
    expect(bridge.calls.some((c) => c.type === "memory.upsert")).toBe(false);

    // Editar um item existente usa memory.upsert com id explícito.
    fireEvent.click(screen.getByTestId("memory-edit-button-m1"));
    fireEvent.change(screen.getByTestId("memory-edit-m1"), {
      target: { value: "Prefere temas claros" },
    });
    fireEvent.click(screen.getByTestId("memory-save-m1"));
    await waitFor(() => {
      const call = bridge.calls.find((c) => c.type === "memory.upsert");
      expect(call).toBeDefined();
      expect(call!.payload).toEqual({ id: "m1", text: "Prefere temas claros" });
    });
  });

  it("desabilita proposta e edição quando a memória está desligada, mantendo as opções usáveis", async () => {
    const bridge = makeBridge(
      defaultHandler({
        "memory.list": { enabled: false, items: [] },
        "memory.options.get": { options: { ...OPTIONS, enabled: false } },
        "memory.proposals": { proposals: [] },
      })
    );
    renderPanel(bridge);
    await waitFor(() => expect(screen.getByTestId("memory-disabled-notice")).toBeTruthy());

    expect((screen.getByTestId("propose-memory") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("memory-option-enabled") as HTMLInputElement).disabled).toBe(false);

    fireEvent.click(screen.getByTestId("memory-option-enabled"));
    await waitFor(() => {
      const call = bridge.calls.find((c) => c.type === "memory.options.set");
      expect(call).toBeDefined();
      expect((call!.payload.options as Record<string, unknown>).enabled).toBe(true);
    });
  });

  it("mostra erro de política sem vazar conteúdo recusado", async () => {
    const bridge = makeBridge((type) => {
      if (type === "memory.propose") {
        throw new Error("memory_policy_error: Memória recusada: conteúdo privado.");
      }
      return defaultHandler()(type);
    });
    renderPanel(bridge);
    await waitForList();

    fireEvent.change(screen.getByTestId("propose-text"), { target: { value: "algo" } });
    fireEvent.click(screen.getByTestId("propose-memory"));
    await waitFor(() => {
      const alert = screen.getByRole("alert");
      expect(alert.textContent).toContain("conteúdo privado");
    });
  });
});

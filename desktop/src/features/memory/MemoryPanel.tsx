/**
 * Painel de memória (`desktop/src/features/memory/MemoryPanel.tsx`, F2b + refinamento 2026-10-15).
 *
 * Regras que o painel não pode contornar:
 * - **Novo perfil passa por proposta/aceite.** `memory.upsert` só edita item ativo existente; criar
 *   um registro sem `id` é recusado pelo backend. Por isso a interface separa
 *   "Atualizar memória existente" de "Propor nova memória (precisa de aceitação)".
 * - **Ação destrutiva exige dois passos.** Apagar, limpar tudo e importar só enviam o comando
 *   depois de um segundo clique explícito, sempre com `confirmed: true`.
 * - **Cancelar não é erro.** `{ exported|imported: false, cancelled: true }` volta ao diálogo
 *   nativo sem virar alerta.
 * - **Resposta atrasada não sobrescreve a nova.** Um número de sequência descarta respostas
 *   fora de ordem.
 * - O DTO público de `MemoryItemDto` não carrega `metadata`; nada de conteúdo bruto é renderizado
 *   além do texto que a própria política já aceitou.
 *
 * Layout (lote D): cabeçalho com privacidade local; coluna principal com busca, itens ativos e
 * propostas; coluna lateral com opções, proposta nova e ações globais. A reorganização é só
 * presentation — comandos, payloads e rascunhos locais permanecem os mesmos.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { describeError } from "../../shared/ipc/client";
import type { SendCommandFn } from "../../shared/ipc/client";
import type { SessionReadyPayload } from "../../shared/protocol";
import type {
  MemoryItemDto,
  MemoryListDto,
  MemoryOptionsDto,
  MemoryProposalsDto,
} from "../../shared/types";

const MEMORY_LABELS: Record<string, string> = {
  enabled: "Ativada", episodes: "Episódios", proposals: "Propostas", history: "Histórico",
  preference: "Preferência", fact: "Fato", habit: "Hábito", decision: "Decisão", episode: "Episódio", conversation: "Conversa",
};

export interface MemoryPanelProps {
  sendCommand: SendCommandFn;
  session: SessionReadyPayload | null;
  /** Muda a cada `memory.changed`; o painel recarrega quando o token muda. */
  refreshToken: number;
}

/** Kinds que podem ser propostos (`PROFILE_KINDS` em `app/memory/contracts.py`). */
const PROPOSABLE_KINDS = ["preference", "fact", "habit", "decision"] as const;

/** Kinds filtráveis em `memory.search` (`KINDS` em `app/memory/contracts.py`). */
const SEARCHABLE_KINDS = [
  "preference",
  "fact",
  "habit",
  "decision",
  "episode",
  "conversation",
] as const;

const MIN_LIMIT = 1;
const MAX_LIMIT = 100;

type DestructiveAction = null | "wipe" | "import" | { delete: string };

function clampLimit(value: number): number {
  if (!Number.isFinite(value)) {
    return MAX_LIMIT;
  }
  const rounded = Math.trunc(value);
  if (rounded < MIN_LIMIT) {
    return MIN_LIMIT;
  }
  if (rounded > MAX_LIMIT) {
    return MAX_LIMIT;
  }
  return rounded;
}

function formatWhen(value: string): string {
  return value.slice(0, 16).replace("T", " ");
}

export function MemoryPanel({
  sendCommand,
  session,
  refreshToken,
}: MemoryPanelProps): React.ReactElement {
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(50);
  const [kinds, setKinds] = useState<string[]>([]);
  const [items, setItems] = useState<MemoryItemDto[]>([]);
  const [proposals, setProposals] = useState<MemoryItemDto[]>([]);
  const [options, setOptions] = useState<MemoryOptionsDto>({});
  const [enabled, setEnabled] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [destructive, setDestructive] = useState<DestructiveAction>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState("");
  const [newKind, setNewKind] = useState<string>(PROPOSABLE_KINDS[0]);
  const [newText, setNewText] = useState("");
  const sequenceRef = useRef(0);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const applyList = useCallback((payload: MemoryListDto) => {
    setItems(payload.items ?? []);
    setEnabled(Boolean(payload.enabled));
  }, []);

  const load = useCallback(async () => {
    if (!session) {
      return;
    }
    const sequence = ++sequenceRef.current;
    try {
      const [list, proposalList, optionResult] = await Promise.all([
        sendCommand<MemoryListDto>("memory.list", { query: "", limit: MAX_LIMIT }),
        sendCommand<MemoryProposalsDto>("memory.proposals", {}),
        sendCommand<{ options: MemoryOptionsDto }>("memory.options.get", {}),
      ]);
      // Resposta de um pedido antigo nunca sobrescreve o estado mais novo.
      if (!mountedRef.current || sequence !== sequenceRef.current) {
        return;
      }
      applyList(list);
      setProposals(proposalList.proposals ?? []);
      setOptions(optionResult.options ?? {});
      setError(null);
    } catch (err) {
      if (mountedRef.current && sequence === sequenceRef.current) {
        setError(describeError(err));
      }
    }
  }, [applyList, sendCommand, session]);

  useEffect(() => {
    if (!session) {
      return;
    }
    void load();
  }, [load, refreshToken, session]);

  const runQuery = useCallback(
    async (mode: "list" | "search") => {
      if (!session) {
        return;
      }
      const effectiveLimit = clampLimit(limit);
      setLimit(effectiveLimit);
      const sequence = ++sequenceRef.current;
      setBusy(true);
      setNotice(null);
      try {
        const payload: Record<string, unknown> =
          mode === "list"
            ? { query: query.trim(), limit: effectiveLimit, respect_enabled: false }
            : { query: query.trim(), limit: effectiveLimit };
        if (mode === "search" && kinds.length > 0) {
          payload.kinds = [...kinds];
        }
        const result = await sendCommand<MemoryListDto>(`memory.${mode}`, payload);
        if (!mountedRef.current || sequence !== sequenceRef.current) {
          return;
        }
        applyList(result);
        setError(null);
      } catch (err) {
        if (mountedRef.current && sequence === sequenceRef.current) {
          setError(describeError(err));
        }
      } finally {
        if (mountedRef.current) {
          setBusy(false);
        }
      }
    },
    [applyList, kinds, limit, query, sendCommand, session]
  );

  const toggleKind = (kind: string) => {
    setKinds((prev) => (prev.includes(kind) ? prev.filter((k) => k !== kind) : [...prev, kind]));
  };

  const setOption = useCallback(
    async (key: string, value: boolean) => {
      const next: MemoryOptionsDto = { ...options, [key]: value };
      setOptions(next);
      try {
        const result = await sendCommand<{ options: MemoryOptionsDto }>("memory.options.set", {
          options: next,
        });
        setOptions(result.options ?? next);
        if (typeof next.enabled === "boolean") {
          setEnabled(next.enabled);
        }
        setError(null);
      } catch (err) {
        setError(describeError(err));
        void load();
      }
    },
    [load, options, sendCommand]
  );

  const submitEdit = useCallback(
    async (id: string) => {
      const text = editingText.trim();
      if (!text) {
        setError("O texto não pode ficar vazio.");
        return;
      }
      try {
        await sendCommand("memory.upsert", { id, text });
        setEditingId(null);
        setEditingText("");
        setError(null);
        await load();
      } catch (err) {
        setError(describeError(err));
      }
    },
    [editingText, load, sendCommand]
  );

  const submitProposal = useCallback(async () => {
    const text = newText.trim();
    if (!text) {
      setError("Escreva o texto da proposta.");
      return;
    }
    try {
      await sendCommand("memory.propose", { kind: newKind, text });
      setNewText("");
      setError(null);
      setNotice("Proposta registrada: ela só entra na memória depois de aceita.");
      await load();
    } catch (err) {
      setError(describeError(err));
    }
  }, [load, newKind, newText, sendCommand]);

  const acceptProposal = useCallback(
    async (id: string) => {
      try {
        await sendCommand("memory.accept", { id });
        setError(null);
        await load();
      } catch (err) {
        setError(describeError(err));
      }
    },
    [load, sendCommand]
  );

  const rejectProposal = useCallback(
    async (id: string) => {
      try {
        await sendCommand("memory.reject", { id });
        setError(null);
        await load();
      } catch (err) {
        setError(describeError(err));
      }
    },
    [load, sendCommand]
  );

  const exportMemory = useCallback(async () => {
    try {
      const result = await sendCommand<{ exported?: boolean; cancelled?: boolean }>(
        "memory.export",
        { use_native_dialog: true }
      );
      if (result.cancelled) {
        setNotice("Exportação cancelada.");
        return;
      }
      setError(null);
      setNotice("Memória exportada.");
    } catch (err) {
      setError(describeError(err));
    }
  }, [sendCommand]);

  const runDestructive = useCallback(async () => {
    if (!destructive) {
      return;
    }
    setBusy(true);
    try {
      if (destructive === "wipe") {
        const result = await sendCommand<{ options: MemoryOptionsDto }>("memory.wipe", {
          confirmed: true,
        });
        setOptions(result.options ?? options);
        setNotice("Memória limpa.");
      } else if (destructive === "import") {
        const result = await sendCommand<{ imported?: boolean; cancelled?: boolean }>(
          "memory.import",
          { confirmed: true, use_native_dialog: true }
        );
        if (result.cancelled) {
          setNotice("Importação cancelada.");
          setDestructive(null);
          return;
        }
        setNotice("Memória importada.");
      } else {
        await sendCommand("memory.delete", { id: destructive.delete, confirmed: true });
        setNotice("Memória apagada.");
      }
      setError(null);
      setDestructive(null);
      await load();
    } catch (err) {
      setError(describeError(err));
      setDestructive(null);
    } finally {
      if (mountedRef.current) {
        setBusy(false);
      }
    }
  }, [destructive, load, options, sendCommand]);

  const actionsDisabled = !session || !enabled;

  return (
    <section
      aria-label="Memória"
      data-testid="memory-panel"
      data-enabled={enabled ? "true" : "false"}
      className="lunmi-memory-page"
    >
      <header className="lunmi-memory-head">
        <div>
          <h2>Memória</h2>
          <p className="lunmi-memory-sub">
            Memórias salvas neste computador. Trechos relevantes podem ser enviados ao provedor escolhido.
            Propostas precisam de aceite; episódios e histórico seguem as opções ativadas.
          </p>
        </div>
        <span className="lunmi-memory-state" data-on={enabled ? "true" : "false"}>
          <i aria-hidden="true" />{enabled ? "Memória ativada" : "Memória desligada"}
        </span>
      </header>

      {!session && (
        <p className="lunmi-memory-muted">A sessão ainda não está pronta; a memória será carregada quando estiver.</p>
      )}

      {error && (
        <div role="alert" data-testid="memory-error" className="lunmi-memory-error">
          {error}
        </div>
      )}

      {notice && !error && (
        <p data-testid="memory-notice" className="lunmi-memory-notice">{notice}</p>
      )}

      {!enabled && (
        <p data-testid="memory-disabled-notice" className="lunmi-memory-warning">
          A memória está desligada. Ativar, propor ou editar ficam indisponíveis até religá-la nas
          opções abaixo.
        </p>
      )}

      <div className="lunmi-memory-layout">
        <div className="lunmi-memory-main">
          <section className="lunmi-memory-card" aria-label="Buscar na memória">
            <h3>Buscar</h3>
            <div className="lunmi-memory-search-row">
              <input
                type="search"
                data-testid="memory-search"
                aria-label="Buscar na memória"
                placeholder="Buscar…"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                className="lunmi-memory-search"
              />
              <label className="lunmi-memory-limit" htmlFor="memory-limit-field">
                limite
                <input
                  id="memory-limit-field"
                  type="number"
                  data-testid="memory-limit"
                  aria-label="Limite de resultados"
                  min={MIN_LIMIT}
                  max={MAX_LIMIT}
                  value={limit}
                  onChange={(event) => setLimit(Number(event.target.value))}
                  className="lunmi-memory-limit-input"
                />
              </label>
              <button
                type="button"
                data-testid="memory-list-button"
                disabled={!session || busy}
                onClick={() => void runQuery("list")}
                className="lunmi-memory-quiet"
              >
                Listar
              </button>
              <button
                type="button"
                data-testid="memory-search-button"
                disabled={!session || busy}
                onClick={() => void runQuery("search")}
                className="lunmi-memory-go"
              >
                Buscar
              </button>
            </div>
            <div className="lunmi-memory-filters" role="group" aria-label="Filtrar por tipo">
              {SEARCHABLE_KINDS.map((kind) => (
                <label key={kind} className="lunmi-memory-chip">
                  <input
                    type="checkbox"
                    data-testid={`kind-filter-${kind}`}
                    checked={kinds.includes(kind)}
                    onChange={() => toggleKind(kind)}
                  />
                  {MEMORY_LABELS[kind] ?? kind}
                </label>
              ))}
            </div>
          </section>

          <section className="lunmi-memory-group" aria-label="Memórias ativas">
            <div className="lunmi-memory-section-head">
              <h3>Memórias ativas</h3>
              <span className="lunmi-memory-count">{items.length}</span>
            </div>
            <ul data-testid="memory-list" className="lunmi-memory-list">
              {items.length === 0 && <li className="lunmi-memory-muted">Nenhuma memória ativa.</li>}
              {items.map((item) => (
                <li key={item.id} data-testid={`memory-item-${item.id}`} className="lunmi-memory-item">
                  <div className="lunmi-memory-item-meta">
                    <span>{MEMORY_LABELS[item.kind] ?? item.kind}</span>
                    <span>{formatWhen(item.updated_at)}</span>
                  </div>
                  {editingId === item.id ? (
                    <div className="lunmi-memory-edit">
                      <textarea
                        data-testid={`memory-edit-${item.id}`}
                        aria-label="Atualizar memória existente"
                        value={editingText}
                        onChange={(event) => setEditingText(event.target.value)}
                        className="lunmi-memory-edit-field"
                      />
                      <div className="lunmi-memory-actions">
                        <button
                          type="button"
                          data-testid={`memory-save-${item.id}`}
                          disabled={actionsDisabled}
                          onClick={() => void submitEdit(item.id)}
                          className="lunmi-memory-go"
                        >
                          Salvar alteração
                        </button>
                        <button type="button" onClick={() => setEditingId(null)} className="lunmi-memory-quiet">
                          Cancelar
                        </button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <p className="lunmi-memory-item-text">{item.text}</p>
                      <div className="lunmi-memory-item-actions">
                        <button
                          type="button"
                          data-testid={`memory-edit-button-${item.id}`}
                          disabled={actionsDisabled}
                          onClick={() => {
                            setEditingId(item.id);
                            setEditingText(item.text);
                          }}
                          className="lunmi-memory-link"
                        >
                          Atualizar memória existente
                        </button>
                        {destructive && typeof destructive === "object" && destructive.delete === item.id ? (
                          <button
                            type="button"
                            data-testid={`confirm-delete-${item.id}`}
                            onClick={() => void runDestructive()}
                            className="lunmi-memory-danger-quiet"
                          >
                            Confirmar exclusão
                          </button>
                        ) : (
                          <button
                            type="button"
                            data-testid={`memory-delete-${item.id}`}
                            disabled={actionsDisabled}
                            onClick={() => setDestructive({ delete: item.id })}
                            className="lunmi-memory-link-danger"
                          >
                            Apagar
                          </button>
                        )}
                      </div>
                    </>
                  )}
                </li>
              ))}
            </ul>
          </section>

          <section className="lunmi-memory-group" aria-label="Propostas pendentes">
            <div className="lunmi-memory-section-head">
              <h3>Propostas pendentes</h3>
              <span className="lunmi-memory-count">{proposals.length}</span>
            </div>
            <ul data-testid="memory-proposals" className="lunmi-memory-list">
              {proposals.length === 0 && <li className="lunmi-memory-muted">Nenhuma proposta pendente.</li>}
              {proposals.map((proposal) => (
                <li key={proposal.id} data-testid={`proposal-${proposal.id}`} className="lunmi-memory-item lunmi-memory-proposal">
                  <div className="lunmi-memory-item-meta">
                    <span>{MEMORY_LABELS[proposal.kind] ?? proposal.kind}</span>
                    <span>aguardando sua decisão</span>
                  </div>
                  <p className="lunmi-memory-item-text">{proposal.text}</p>
                  <div className="lunmi-memory-actions">
                    <button
                      type="button"
                      data-testid={`proposal-${proposal.id}-accept`}
                      disabled={actionsDisabled}
                      onClick={() => void acceptProposal(proposal.id)}
                      className="lunmi-memory-go"
                    >
                      Aceitar
                    </button>
                    <button
                      type="button"
                      data-testid={`proposal-${proposal.id}-reject`}
                      disabled={actionsDisabled}
                      onClick={() => void rejectProposal(proposal.id)}
                      className="lunmi-memory-quiet"
                    >
                      Rejeitar
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          </section>
        </div>

        <div className="lunmi-memory-side">
          <section className="lunmi-memory-card" aria-label="Opções da memória" data-testid="memory-options">
            <h3>Opções</h3>
            <div className="lunmi-memory-option-row">
              {Object.keys(options).length === 0 && <span className="lunmi-memory-muted">carregando…</span>}
              {Object.entries(options).map(([key, value]) => (
                <label key={key} className="lunmi-memory-chip">
                  <input
                    type="checkbox"
                    data-testid={`memory-option-${key}`}
                    checked={value}
                    onChange={(event) => void setOption(key, event.target.checked)}
                  />
                  {MEMORY_LABELS[key] ?? key}
                </label>
              ))}
            </div>
          </section>

          <section className="lunmi-memory-card" aria-label="Propor nova memória">
            <h3>Propor nova memória</h3>
            <p className="lunmi-memory-muted">
              Uma proposta só entra na memória depois de aceita — não é um registro imediato.
            </p>
            <select
              data-testid="propose-kind"
              aria-label="Tipo da proposta"
              value={newKind}
              onChange={(event) => setNewKind(event.target.value)}
              className="lunmi-memory-kind"
            >
              {PROPOSABLE_KINDS.map((kind) => (
                <option key={kind} value={kind}>
                  {MEMORY_LABELS[kind] ?? kind}
                </option>
              ))}
            </select>
            <textarea
              data-testid="propose-text"
              aria-label="Texto da proposta"
              value={newText}
              onChange={(event) => setNewText(event.target.value)}
              className="lunmi-memory-propose-field"
              rows={3}
              placeholder="Ex.: Prefere janelas em tela cheia"
            />
            <button
              type="button"
              data-testid="propose-memory"
              disabled={actionsDisabled}
              onClick={() => void submitProposal()}
              className="lunmi-memory-go"
            >
              Propor
            </button>
          </section>

          <section className="lunmi-memory-card lunmi-memory-danger-zone" aria-label="Ações globais">
            <h3>Ações globais</h3>
            <p className="lunmi-memory-muted">Exportar e importar usam o diálogo do Windows. Apagar e limpar pedem confirmação.</p>
            <div className="lunmi-memory-actions">
              <button
                type="button"
                data-testid="export-memory"
                disabled={!session}
                onClick={() => void exportMemory()}
                className="lunmi-memory-quiet"
              >
                Exportar…
              </button>
              {destructive === "import" ? (
                <button type="button" data-testid="confirm-import" onClick={() => void runDestructive()} className="lunmi-memory-danger-quiet">
                  Confirmar importação
                </button>
              ) : (
                <button type="button" data-testid="import-memory" disabled={!session} onClick={() => setDestructive("import")} className="lunmi-memory-quiet">
                  Importar…
                </button>
              )}
              {destructive === "wipe" ? (
                <button type="button" data-testid="confirm-wipe" onClick={() => void runDestructive()} className="lunmi-memory-danger-quiet">
                  Confirmar limpeza total
                </button>
              ) : (
                <button type="button" data-testid="wipe-memory" disabled={!session} onClick={() => setDestructive("wipe")} className="lunmi-memory-danger-quiet">
                  Limpar tudo
                </button>
              )}
            </div>
          </section>
        </div>
      </div>
    </section>
  );
}

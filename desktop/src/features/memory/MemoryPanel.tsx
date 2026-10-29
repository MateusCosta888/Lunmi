/**
 * Painel de memória v2 (`desktop/src/features/memory/MemoryPanel.tsx`, MEMORIA-SIMPLES-V2).
 *
 * Superfície primária: um único editor de TEXTO LIVRE sobre o documento canónico
 * (`memory.profile.get/save/undo`). Enter é texto, não registro. Guardar confirma só após o
 * backend; Desfazer obedece a `can_undo` (um nível). `stale_revision` nunca reescreve nada: o
 * painel refaz o `get` e abre revisão humana (texto atual vs rascunho preservado), sem auto-merge.
 * Eventos `memory.changed` atualizam o texto confirmado apenas quando não há rascunho sujo; com
 * rascunho sujo, o texto recebido vai para a revisão de conflito e o rascunho fica intacto.
 * O rascunho sobrevive à navegação fora da aba via store em memória do próprio seam (sem
 * localStorage); é limpo no save/adoção e a perda só ocorre ao reiniciar a app.
 *
 * Gestão secundária (recolhida, carregada só ao abrir): busca/filtros, parágrafos indexados
 * (`chunk:<hex>` via `memory.list/search/upsert/delete`), propostas LEGADAS, opções restantes e
 * exportar/importar/limpar. Destrutivos continuam em dois passos com `confirmed: true`. Cancelar
 * diálogo nativo não é erro. Erros nunca ecoam conteúdo pessoal além da mensagem do backend.
 *
 * Regras que o painel não contorna: criar conteúdo é editar o documento (não há propose→accept
 * para texto do próprio utilizador); `memory.upsert` só mexe em parágrafos indexados; API e
 * eventos são os do contrato congelado `.agents/MEMORY-SIMPLE-V2-CONTRACT.md`.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { BridgeError, describeError } from "../../shared/ipc/client";
import type { SendCommandFn } from "../../shared/ipc/client";
import type { SessionReadyPayload } from "../../shared/protocol";
import type {
  MemoryItemDto,
  MemoryListDto,
  MemoryOptionsDto,
  MemoryProfileDto,
  MemoryProposalsDto,
} from "../../shared/types";

const MEMORY_LABELS: Record<string, string> = {
  enabled: "Usar memória", episodes: "Episódios", proposals: "Propostas", history: "Excertos legados da memória (400 caracteres)",
  auto_learn: "Aprender com as conversas",
  preference: "Preferência", fact: "Fato", habit: "Hábito", decision: "Decisão", episode: "Episódio", conversation: "Conversa",
};

export interface MemoryProfileDraft {
  text: string;
  /** Revisão em que o rascunho foi criado; não avança com eventos — é o token CAS do Guardar. */
  baseRevision: number;
}

export interface MemoryPanelProps {
  sendCommand: SendCommandFn;
  session: SessionReadyPayload | null;
  /** Muda a cada `memory.changed`; o painel recarrega quando o token muda. */
  refreshToken: number;
  /** Rascunho do documento, vivido no App (sobrevive à navegação e é por sessão). */
  draft: MemoryProfileDraft | null;
  onDraftChange: (next: MemoryProfileDraft | null) => void;
}

const PROPOSABLE_KINDS = ["preference", "fact", "habit", "decision"] as const;
const SEARCHABLE_KINDS = ["preference", "fact", "habit", "decision", "episode", "conversation"] as const;
const MIN_LIMIT = 1;
const MAX_LIMIT = 100;

type DestructiveAction = null | "wipe" | "import" | { delete: string };

function clampLimit(value: number): number {
  if (!Number.isFinite(value)) return MAX_LIMIT;
  const rounded = Math.trunc(value);
  if (rounded < MIN_LIMIT) return MIN_LIMIT;
  if (rounded > MAX_LIMIT) return MAX_LIMIT;
  return rounded;
}

function formatWhen(value: string): string {
  return value.slice(0, 16).replace("T", " ");
}

interface ConflictState {
  current: MemoryProfileDto;
}

/** Contagem em pontos Unicode, como o backend; `draft.length` contaria UTF-16. */
function docLength(text: string): number {
  return Array.from(text).length;
}

export function MemoryPanel({ sendCommand, session, refreshToken, draft: draftProp, onDraftChange }: MemoryPanelProps): React.ReactElement {
  const [profile, setProfile] = useState<MemoryProfileDto | null>(null);
  const [draft, setDraft] = useState(draftProp?.text ?? "");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<ConflictState | null>(null);
  const [options, setOptions] = useState<MemoryOptionsDto>({});
  const [advancedOpen, setAdvancedOpen] = useState(false);

  // Estado da gestão secundária (legado), carregado só quando aberta.
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(MAX_LIMIT);
  const [kinds, setKinds] = useState<string[]>([]);
  const [items, setItems] = useState<MemoryItemDto[]>([]);
  const [proposals, setProposals] = useState<MemoryItemDto[]>([]);
  const [busy, setBusy] = useState(false);
  const [destructive, setDestructive] = useState<DestructiveAction>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingText, setEditingText] = useState("");
  const [newKind, setNewKind] = useState<string>(PROPOSABLE_KINDS[0]);
  const [newText, setNewText] = useState("");

  const sequenceRef = useRef(0);
  const mountedRef = useRef(true);
  // Espelhos para decisões dentro de callbacks assíncronos: o texto digitado antes do primeiro
  // `get` completar também é rascunho e nunca pode ser descartado pela carga.
  const draftRef = useRef(draftProp?.text ?? "");
  const draftPropRef = useRef(draftProp);
  // Espelho NÃO-reativo do perfil carregado: a carga nunca pode depender do estado `profile`
  // (cada get cria objeto novo → callback novo → effect → loop de IPC).
  const profileStateRef = useRef<MemoryProfileDto | null>(null);
  useEffect(() => { draftPropRef.current = draftProp; }, [draftProp]);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const dirty = profile !== null && draft !== profile.text;

  const applyProfile = useCallback((next: MemoryProfileDto, keepDraft: boolean) => {
    profileStateRef.current = next;
    setProfile(next);
    if (!keepDraft) {
      setDraft(next.text);
      draftRef.current = next.text;
      onDraftChange(null);
    }
  }, [onDraftChange]);

  const loadProfile = useCallback(async (opts: { preserveDraft: boolean }): Promise<MemoryProfileDto | null> => {
    if (!session) return null;
    const sequence = ++sequenceRef.current;
    try {
      const [next, optionResult] = await Promise.all([
        sendCommand<MemoryProfileDto>("memory.profile.get", {}),
        sendCommand<{ options: MemoryOptionsDto }>("memory.options.get", {}),
      ]);
      if (!mountedRef.current || sequence !== sequenceRef.current) return null;
      setOptions(optionResult.options ?? {});
      setError(null);
      const stored = draftPropRef.current;
      const hasDraft = opts.preserveDraft && stored !== null && stored.text !== next.text;
      if (hasDraft) {
        // O rascunho mantém o SEU base_revision: eventos não o re-key para a revisão nova.
        profileStateRef.current = next;
        setProfile(next);
        setDraft(stored!.text);
        draftRef.current = stored!.text;
        // Evento (aprendizagem/API) chegou por cima do rascunho: decisão humana, nunca auto-merge.
        if (stored!.baseRevision !== next.revision) setConflict({ current: next });
      } else if (opts.preserveDraft && !profileStateRef.current && draftRef.current !== "" && draftRef.current !== next.text) {
        // Texto digitado antes do primeiro get completar: vira rascunho oficial, não é perdido.
        profileStateRef.current = next;
        setProfile(next);
        onDraftChange({ text: draftRef.current, baseRevision: next.revision });
      } else {
        applyProfile(next, false);
      }
      return next;
    } catch (err) {
      if (mountedRef.current && sequence === sequenceRef.current) setError(describeError(err));
      return null;
    }
  }, [applyProfile, onDraftChange, sendCommand, session]);

  useEffect(() => {
    if (!session) return;
    void loadProfile({ preserveDraft: true });
  }, [loadProfile, refreshToken, session]);

  // --- superfície primária -------------------------------------------------

  const writeDraft = (text: string) => {
    setNotice(null);
    setDraft(text);
    draftRef.current = text;
    if (!profile) return;
    if (text === profile.text) { onDraftChange(null); return; }
    // base_revision do rascunho é pegajoso: eventos não o re-key para a revisão nova.
    const sticky = draftPropRef.current && draftPropRef.current.text !== profile.text
      ? draftPropRef.current.baseRevision
      : profile.revision;
    const next = { text, baseRevision: sticky };
    draftPropRef.current = next;
    onDraftChange(next);
  };

  const commitWith = useCallback(async (text: string, baseRevision: number, successNotice: string) => {
    setSaving(true);
    try {
      const next = await sendCommand<MemoryProfileDto>("memory.profile.save", { text, base_revision: baseRevision });
      if (!mountedRef.current) return;
      applyProfile(next, false);
      setConflict(null);
      setError(null);
      setNotice(successNotice);
    } catch (err) {
      if (!mountedRef.current) return;
      if (err instanceof BridgeError && err.code === "stale_revision") {
        // Zero escrita no backend. Refaz o get e entrega a decisão a um humano.
        const fresh = await loadProfile({ preserveDraft: true });
        if (fresh) setConflict({ current: fresh });
        setNotice(null);
        return;
      }
      // Recarrega o estado real e só depois mostra o erro — a carga limpa o banner anterior.
      await loadProfile({ preserveDraft: true });
      if (mountedRef.current) setError(describeError(err));
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  }, [applyProfile, loadProfile, sendCommand]);

  const saveDraft = () => {
    if (!profile || !dirty || saving || conflict) return;
    const base = draftPropRef.current && draftPropRef.current.text !== profile.text
      ? draftPropRef.current.baseRevision
      : profile.revision;
    void commitWith(draft, base, "Guardado.");
  };

  const undo = useCallback(async () => {
    if (!profile || !profile.can_undo || dirty || saving || conflict) return;
    setSaving(true);
    try {
      const next = await sendCommand<MemoryProfileDto>("memory.profile.undo", { base_revision: profile.revision });
      if (!mountedRef.current) return;
      applyProfile(next, false);
      setError(null);
      setNotice("Última alteração desfeita.");
    } catch (err) {
      if (!mountedRef.current) return;
      if (err instanceof BridgeError && err.code === "nothing_to_undo") {
        setNotice("Não há mais nada para desfazer.");
        void loadProfile({ preserveDraft: true });
        return;
      }
      setError(describeError(err));
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  }, [applyProfile, conflict, dirty, loadProfile, profile, saving, sendCommand]);

  const setOption = useCallback(async (key: string, value: boolean) => {
    const next = { ...options, [key]: value };
    setOptions(next);
    try {
      const result = await sendCommand<{ options: MemoryOptionsDto }>("memory.options.set", { options: next });
      setOptions(result.options ?? next);
      if (key === "enabled" && typeof result.options?.enabled === "boolean") {
        setProfile(current => {
          const updated = current ? { ...current, enabled: result.options.enabled } : current;
          if (updated) profileStateRef.current = updated;
          return updated;
        });
      }
      setError(null);
    } catch (err) {
      setError(describeError(err));
      void loadProfile({ preserveDraft: true });
    }
  }, [loadProfile, options, sendCommand]);

  const readOnly = !profile || !profile.enabled || saving;
  const canEdit = Boolean(profile?.enabled);

  // --- gestão secundária ---------------------------------------------------

  const loadLegacy = useCallback(async () => {
    if (!session) return;
    try {
      const [list, proposalList] = await Promise.all([
        sendCommand<MemoryListDto>("memory.list", { query: "", limit: MAX_LIMIT }),
        sendCommand<MemoryProposalsDto>("memory.proposals", {}),
      ]);
      if (!mountedRef.current) return;
      setItems(list.items ?? []);
      setProposals(proposalList.proposals ?? []);
    } catch (err) {
      if (mountedRef.current) setError(describeError(err));
    }
  }, [sendCommand, session]);

  useEffect(() => {
    if (advancedOpen) void loadLegacy();
  }, [advancedOpen, loadLegacy, refreshToken]);

  const runQuery = useCallback(async (mode: "list" | "search") => {
    if (!session) return;
    const effectiveLimit = clampLimit(limit);
    setLimit(effectiveLimit);
    setBusy(true);
    setNotice(null);
    try {
      const payload: Record<string, unknown> = mode === "list"
        ? { query: query.trim(), limit: effectiveLimit, respect_enabled: false }
        : { query: query.trim(), limit: effectiveLimit };
      if (mode === "search" && kinds.length > 0) payload.kinds = [...kinds];
      const result = await sendCommand<MemoryListDto>(`memory.${mode}`, payload);
      if (!mountedRef.current) return;
      setItems(result.items ?? []);
      setError(null);
    } catch (err) {
      if (mountedRef.current) setError(describeError(err));
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  }, [kinds, limit, query, sendCommand, session]);

  const toggleKind = (kind: string) => {
    setKinds(prev => (prev.includes(kind) ? prev.filter(k => k !== kind) : [...prev, kind]));
  };

  const submitEdit = useCallback(async (id: string) => {
    const text = editingText.trim();
    if (!text) { setError("O texto não pode ficar vazio."); return; }
    try {
      await sendCommand("memory.upsert", { id, text });
      setEditingId(null);
      setEditingText("");
      setError(null);
      setNotice("Parágrafo atualizado no teu texto.");
      await Promise.all([loadLegacy(), loadProfile({ preserveDraft: false })]);
    } catch (err) {
      setError(describeError(err));
    }
  }, [editingText, loadLegacy, loadProfile, sendCommand]);

  const submitProposal = useCallback(async () => {
    const text = newText.trim();
    if (!text) { setError("Escreva o texto da proposta."); return; }
    try {
      await sendCommand("memory.propose", { kind: newKind, text });
      setNewText("");
      setError(null);
      setNotice("Proposta registrada: ela só entra no texto depois de aceita.");
      await loadLegacy();
    } catch (err) {
      setError(describeError(err));
    }
  }, [loadLegacy, newKind, newText, sendCommand]);

  const settleProposal = useCallback(async (action: "accept" | "reject", id: string) => {
    try {
      await sendCommand(`memory.${action}`, { id });
      setError(null);
      await Promise.all([loadLegacy(), loadProfile({ preserveDraft: false })]);
    } catch (err) {
      setError(describeError(err));
    }
  }, [loadLegacy, loadProfile, sendCommand]);

  const exportMemory = useCallback(async () => {
    try {
      const result = await sendCommand<{ exported?: boolean; cancelled?: boolean }>("memory.export", { use_native_dialog: true });
      if (result.cancelled) { setNotice("Exportação cancelada."); return; }
      setError(null);
      setNotice("Memória exportada.");
    } catch (err) {
      setError(describeError(err));
    }
  }, [sendCommand]);

  const runDestructive = useCallback(async () => {
    if (!destructive) return;
    setBusy(true);
    try {
      if (destructive === "wipe") {
        const result = await sendCommand<{ options: MemoryOptionsDto }>("memory.wipe", { confirmed: true });
        setOptions(result.options ?? options);
        setNotice("Memória limpa. Esta ação não é desfazível.");
      } else if (destructive === "import") {
        const result = await sendCommand<{ imported?: boolean; cancelled?: boolean }>("memory.import", { confirmed: true, use_native_dialog: true });
        if (result.cancelled) { setNotice("Importação cancelada."); setDestructive(null); return; }
        setNotice("Memória importada.");
      } else {
        await sendCommand("memory.delete", { id: destructive.delete, confirmed: true });
        setNotice("Trecho esquecido. Esquecer não é desfazível.");
      }
      setError(null);
      setDestructive(null);
      await Promise.all([loadLegacy(), loadProfile({ preserveDraft: false })]);
    } catch (err) {
      setError(describeError(err));
      setDestructive(null);
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  }, [destructive, loadLegacy, loadProfile, options, sendCommand]);

  const actionsDisabled = !session || !profile?.enabled;

  // --- render ---------------------------------------------------------------

  return (
    <section aria-label="Memória" data-testid="memory-panel"
      data-enabled={profile?.enabled === false ? "false" : "true"} className="lunmi-memory-page lunmi-settings-page">
      <div className="lunmi-settings-content">
      <header className="lunmi-memory-head lunmi-settings-heading">
        <div>
          <h2>O que a Lunmi deve saber sobre ti</h2>
          <p className="lunmi-memory-sub">
            Um texto livre, teu e da Lunmi: escreve aqui preferências e fatos, corrige e apaga o que
            quiseres. O texto fica neste computador; trechos relevantes podem ser enviados ao
            provedor escolhido. O que a Lunmi aprender aparece aqui com o tempo.
          </p>
        </div>
        <span className="lunmi-memory-state" data-on={profile?.enabled !== false ? "true" : "false"}>
          <i aria-hidden="true" />{profile?.enabled !== false ? "Memória ativada" : "Memória desligada"}
        </span>
      </header>

      {!session && (
        <p className="lunmi-memory-muted">A sessão ainda não está pronta; a memória será carregada quando estiver.</p>
      )}

      {error && (
        <div role="alert" data-testid="memory-error" className="lunmi-memory-error">{error}</div>
      )}
      {notice && !error && (
        <p data-testid="memory-notice" className="lunmi-memory-notice">{notice}</p>
      )}
      {profile && !profile.enabled && (
        <p data-testid="memory-disabled-notice" className="lunmi-memory-warning">
          A memória está desligada. O texto continua visível para leitura, mas guardar e desfazer
          ficam indisponíveis até reativares “Usar memória”.
        </p>
      )}

      <div className="lunmi-profile-editor" data-testid="memory-profile-editor">
        <label className="lunmi-sr-only" htmlFor="memory-profile-text">O que a Lunmi deve saber sobre ti</label>
        <textarea
          id="memory-profile-text"
          data-testid="memory-profile-text"
          value={draft}
          onChange={event => writeDraft(event.target.value)}
          readOnly={readOnly}
          rows={10}
          placeholder="Escreve livremente. Por exemplo: prefiro respostas curtas à noite; uso Python no trabalho."
          className="lunmi-profile-text"
        />
        {profile?.state === "legacy_long" && (
          <p data-testid="memory-legacy-notice" className="lunmi-memory-warning">
            Este perfil é maior que o limite ({profile.char_count} &gt; {profile.max_chars}). Nada foi
            cortado: podes encurtá-lo, mas um texto novo acima do limite não guardaria.
          </p>
        )}
        <div className="lunmi-profile-foot">
          <span data-testid="memory-profile-count" className="lunmi-profile-count" aria-live="polite">
            {docLength(draft)} / {profile?.max_chars ?? 8000}
          </span>
          <div className="lunmi-profile-actions">
            <button type="button" data-testid="memory-profile-save"
              disabled={!dirty || saving || !canEdit || conflict !== null}
              title={conflict ? "Resolve o conflito acima primeiro" : undefined}
              onClick={saveDraft} className="lunmi-memory-go">
              Guardar
            </button>
            <button type="button" data-testid="memory-profile-undo"
              disabled={!profile?.can_undo || dirty || saving || !canEdit || conflict !== null}
              title={dirty ? "Guarda ou descarta o teu texto primeiro" : undefined}
              onClick={() => void undo()} className="lunmi-memory-quiet">
              Desfazer última alteração
            </button>
          </div>
        </div>
        {dirty && (
          <p data-testid="memory-draft-hint" className="lunmi-memory-muted">
            Tens alterações por guardar. Podes sair desta aba sem perder o texto.
          </p>
        )}
      </div>

      {conflict && (
        <section aria-label="Revisão de conflito" data-testid="memory-conflict" className="lunmi-conflict">
          <h3>O texto mudou enquanto escrevias</h3>
          <p className="lunmi-memory-muted">
            A Lunmi (ou outra alteração) guardou uma versão nova. Nada foi substituído
            automaticamente — decide qual texto fica.
          </p>
          <div className="lunmi-conflict-panes">
            <div>
              <h4>Versão atual (revisão {conflict.current.revision})</h4>
              <pre data-testid="memory-conflict-current" className="lunmi-conflict-pane">{conflict.current.text || "(vazio)"}</pre>
            </div>
            <div>
              <h4>O teu rascunho</h4>
              <pre data-testid="memory-conflict-draft" className="lunmi-conflict-pane">{draft || "(vazio)"}</pre>
            </div>
          </div>
          <div className="lunmi-profile-actions">
            <button type="button" data-testid="memory-conflict-use-mine" disabled={saving || !canEdit}
              onClick={() => void commitWith(draft, conflict.current.revision, "Guardado: o teu texto ficou por cima.")}>
              Guardar o meu texto por cima
            </button>
            <button type="button" data-testid="memory-conflict-take-current" disabled={saving}
              onClick={() => { applyProfile(conflict.current, false); setConflict(null); setNotice("Ficou a versão atual."); }}>
              Ficar com a versão atual
            </button>
            <button type="button" data-testid="memory-conflict-keep-editing"
              onClick={() => setConflict(null)} className="lunmi-memory-quiet">
              Continuar a editar sem decidir
            </button>
          </div>
        </section>
      )}

      <div className="lunmi-profile-options" role="group" aria-label="Opções principais da memória">
        <label className="lunmi-memory-chip">
          <input type="checkbox" data-testid="memory-option-enabled" checked={options.enabled ?? true}
            onChange={event => void setOption("enabled", event.target.checked)} />
          {MEMORY_LABELS.enabled}
        </label>
        <label className="lunmi-memory-chip">
          <input type="checkbox" data-testid="memory-option-auto_learn" checked={Boolean(options.auto_learn ?? profile?.auto_learn)}
            onChange={event => void setOption("auto_learn", event.target.checked)} />
          {MEMORY_LABELS.auto_learn}
        </label>
        <span className="lunmi-memory-muted">
          {options.auto_learn === false
            ? "Desligado, a Lunmi não aprende sozinha nem cria propostas; o que pedires no chat continua a guardar."
            : "Ligado, declarações claras nas conversas entram no texto automaticamente."}
        </span>
      </div>

      <section className="lunmi-advanced" aria-label="Gestão detalhada">
        <button type="button" data-testid="memory-advanced-toggle" aria-expanded={advancedOpen}
          onClick={() => setAdvancedOpen(open => !open)} className="lunmi-memory-quiet">
          {advancedOpen ? "Ocultar gestão detalhada" : "Gerir em detalhe (busca, propostas antigas, importar/exportar)"}
        </button>

        {advancedOpen && (
          <div className="lunmi-memory-layout" data-testid="memory-advanced">
            <div className="lunmi-memory-main">
              <section className="lunmi-memory-card" aria-label="Buscar na memória">
                <h3>Buscar</h3>
                <div className="lunmi-memory-search-row">
                  <input type="search" data-testid="memory-search" aria-label="Buscar na memória"
                    placeholder="Buscar…" value={query} onChange={event => setQuery(event.target.value)}
                    className="lunmi-memory-search" />
                  <label className="lunmi-memory-limit" htmlFor="memory-limit-field">
                    limite
                    <input id="memory-limit-field" type="number" data-testid="memory-limit"
                      aria-label="Limite de resultados" min={MIN_LIMIT} max={MAX_LIMIT} value={limit}
                      onChange={event => setLimit(Number(event.target.value))} className="lunmi-memory-limit-input" />
                  </label>
                  <button type="button" data-testid="memory-list-button" disabled={!session || busy}
                    onClick={() => void runQuery("list")} className="lunmi-memory-quiet">Listar</button>
                  <button type="button" data-testid="memory-search-button" disabled={!session || busy}
                    onClick={() => void runQuery("search")} className="lunmi-memory-go">Buscar</button>
                </div>
                <div className="lunmi-memory-filters" role="group" aria-label="Filtrar por tipo">
                  {SEARCHABLE_KINDS.map(kind => (
                    <label key={kind} className="lunmi-memory-chip">
                      <input type="checkbox" data-testid={`kind-filter-${kind}`} checked={kinds.includes(kind)}
                        onChange={() => toggleKind(kind)} />
                      {MEMORY_LABELS[kind] ?? kind}
                    </label>
                  ))}
                </div>
              </section>

              <section className="lunmi-memory-group" aria-label="Parágrafos do teu texto">
                <div className="lunmi-memory-section-head">
                  <h3>Parágrafos do teu texto</h3>
                  <span className="lunmi-memory-count">{items.length}</span>
                </div>
                <p className="lunmi-memory-muted">Índice derivado do documento acima. Esquecer um
                  trecho aqui remove o parágrafo e não é desfazível.</p>
                <ul data-testid="memory-list" className="lunmi-memory-list lunmi-memory-records">
                  {items.length === 0 && <li className="lunmi-memory-muted">Nenhum parágrafo indexado.</li>}
                  {items.map(entry => (
                    <li key={entry.id} data-testid={`memory-item-${entry.id}`} className="lunmi-memory-item">
                      <div className="lunmi-memory-item-meta">
                        <span>{MEMORY_LABELS[entry.kind] ?? entry.kind}</span>
                        <span>{formatWhen(entry.updated_at)}</span>
                      </div>
                      {editingId === entry.id ? (
                        <div className="lunmi-memory-edit">
                          <textarea data-testid={`memory-edit-${entry.id}`} aria-label="Atualizar parágrafo"
                            value={editingText} onChange={event => setEditingText(event.target.value)}
                            className="lunmi-memory-edit-field" />
                          <div className="lunmi-memory-actions">
                            <button type="button" data-testid={`memory-save-${entry.id}`} disabled={actionsDisabled}
                              onClick={() => void submitEdit(entry.id)} className="lunmi-memory-go">Salvar alteração</button>
                            <button type="button" onClick={() => setEditingId(null)} className="lunmi-memory-quiet">Cancelar</button>
                          </div>
                        </div>
                      ) : (
                        <>
                          <p className="lunmi-memory-item-text">{entry.text}</p>
                          <div className="lunmi-memory-item-actions">
                            <button type="button" data-testid={`memory-edit-button-${entry.id}`} disabled={actionsDisabled}
                              onClick={() => { setEditingId(entry.id); setEditingText(entry.text); }}
                              className="lunmi-memory-link">Atualizar parágrafo</button>
                            {destructive && typeof destructive === "object" && destructive.delete === entry.id ? (
                              <button type="button" data-testid={`confirm-delete-${entry.id}`}
                                onClick={() => void runDestructive()} className="lunmi-memory-danger-quiet">Confirmar exclusão</button>
                            ) : (
                              <button type="button" data-testid={`memory-delete-${entry.id}`} disabled={actionsDisabled}
                                onClick={() => setDestructive({ delete: entry.id })} className="lunmi-memory-link-danger">Esquecer</button>
                            )}
                          </div>
                        </>
                      )}
                    </li>
                  ))}
                </ul>
              </section>

              <section className="lunmi-memory-group" aria-label="Propostas antigas">
                <div className="lunmi-memory-section-head">
                  <h3>Propostas antigas</h3>
                  <span className="lunmi-memory-count">{proposals.length}</span>
                </div>
                <ul data-testid="memory-proposals" className="lunmi-memory-list lunmi-memory-records">
                  {proposals.length === 0 && <li className="lunmi-memory-muted">Nenhuma proposta pendente.</li>}
                  {proposals.map(proposal => (
                    <li key={proposal.id} data-testid={`proposal-${proposal.id}`} className="lunmi-memory-item lunmi-memory-proposal">
                      <div className="lunmi-memory-item-meta">
                        <span>{MEMORY_LABELS[proposal.kind] ?? proposal.kind}</span>
                        <span>aguardando sua decisão</span>
                      </div>
                      <p className="lunmi-memory-item-text">{proposal.text}</p>
                      <div className="lunmi-memory-actions">
                        <button type="button" data-testid={`proposal-${proposal.id}-accept`} disabled={actionsDisabled}
                          onClick={() => void settleProposal("accept", proposal.id)} className="lunmi-memory-go">Aceitar</button>
                        <button type="button" data-testid={`proposal-${proposal.id}-reject`} disabled={actionsDisabled}
                          onClick={() => void settleProposal("reject", proposal.id)} className="lunmi-memory-quiet">Rejeitar</button>
                      </div>
                    </li>
                  ))}
                </ul>
              </section>

              <section className="lunmi-memory-card" aria-label="Propor nova memória">
                <h3>Propor memória antiga (precisa de aceite)</h3>
                <p className="lunmi-memory-muted">Caminho legado. Para dizer algo à Lunmi, escreve no
                  texto acima.</p>
                <select data-testid="propose-kind" aria-label="Tipo da proposta" value={newKind}
                  onChange={event => setNewKind(event.target.value)} className="lunmi-memory-kind">
                  {PROPOSABLE_KINDS.map(kind => <option key={kind} value={kind}>{MEMORY_LABELS[kind] ?? kind}</option>)}
                </select>
                <textarea data-testid="propose-text" aria-label="Texto da proposta" value={newText}
                  onChange={event => setNewText(event.target.value)} className="lunmi-memory-propose-field" rows={3}
                  placeholder="Ex.: Prefere janelas em tela cheia" />
                <button type="button" data-testid="propose-memory" disabled={actionsDisabled}
                  onClick={() => void submitProposal()} className="lunmi-memory-go">Propor</button>
              </section>
            </div>

            <div className="lunmi-memory-side">
              <section className="lunmi-memory-card" aria-label="Opções da memória" data-testid="memory-options">
                <h3>Outras opções</h3>
                <div className="lunmi-memory-option-row">
                  {Object.keys(options).length === 0 && <span className="lunmi-memory-muted">carregando…</span>}
                  {Object.entries(options).filter(([key]) => key !== "enabled" && key !== "auto_learn").map(([key, value]) => (
                    <label key={key} className="lunmi-memory-chip">
                      <input type="checkbox" data-testid={`memory-option-${key}`} checked={value}
                        onChange={event => void setOption(key, event.target.checked)} />
                      {MEMORY_LABELS[key] ?? key}
                    </label>
                  ))}
                </div>
              </section>

              <section className="lunmi-memory-card lunmi-memory-danger-zone" aria-label="Ações globais">
                <h3>Ações globais</h3>
                <p className="lunmi-memory-muted">Exportar e importar usam o diálogo do Windows. Apagar
                  e limpar pedem confirmação. Limpar e importar não são desfazíveis e não apagam
                  backups.</p>
                <div className="lunmi-memory-actions">
                  <button type="button" data-testid="export-memory" disabled={!session}
                    onClick={() => void exportMemory()} className="lunmi-memory-quiet">Exportar…</button>
                  {destructive === "import" ? (
                    <button type="button" data-testid="confirm-import" onClick={() => void runDestructive()} className="lunmi-memory-danger-quiet">Confirmar importação</button>
                  ) : (
                    <button type="button" data-testid="import-memory" disabled={!session} onClick={() => setDestructive("import")} className="lunmi-memory-quiet">Importar…</button>
                  )}
                  {destructive === "wipe" ? (
                    <button type="button" data-testid="confirm-wipe" onClick={() => void runDestructive()} className="lunmi-memory-danger-quiet">Confirmar limpeza total</button>
                  ) : (
                    <button type="button" data-testid="wipe-memory" disabled={!session} onClick={() => setDestructive("wipe")} className="lunmi-memory-danger-quiet">Limpar tudo</button>
                  )}
                </div>
              </section>
            </div>
          </div>
        )}
      </section>
      </div>
    </section>
  );
}

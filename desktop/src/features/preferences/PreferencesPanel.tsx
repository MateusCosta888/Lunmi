/**
 * Painel de preferências (`desktop/src/features/preferences/PreferencesPanel.tsx`, F2b).
 *
 * - `approval.get` / `approval.set`  -> autoaprovação da **próxima** tarefa (é o que o backend
 *   faz: a flag vale para uma tarefa e volta sozinha ao modo manual).
 * - `ui.preferences.get` / `ui.preferences.set` -> atalhos do painel; escala segue o sistema.
 *
 * O renderer nunca envia o campo `window` (retângulo da janela) nem qualquer caminho de arquivo:
 * o backend recusa `path`/`file` em `ui.preferences.set` e a geometria da janela é do processo
 * principal, não da página.
 */
import React, { useCallback, useEffect, useState } from "react";
import { useNodePreferences, useResidentOption } from "../../shared/ipc/presence";
import { VoiceSettingsCard } from "../voice/VoiceSettingsCard";
import { HistorySettings } from '../chat/HistoryNavigation';
import type { ChatHistorySnapshotDto } from '../../shared/types';
import { WebSearchCard } from "../web/WebSearchCard";
import { UpdatesCard } from "./UpdatesCard";
import { TOKENS, THEMES, getTheme } from "../../shared/theme";
import { Icon } from "../../shared/ui";
import { describeError, type SendCommandFn } from "../../shared/ipc/client";
import type { ApprovalDto, NodePreferencesDto, QuickActionDto, UiPreferencesDto } from "../../shared/types";
import type { SessionReadyPayload } from "../../shared/protocol";

export interface PreferencesPanelProps {
  history?: ChatHistorySnapshotDto;
  historyLocked?: boolean;
  sendCommand: SendCommandFn;
  session: SessionReadyPayload | null;
  onThemeChanged?: (theme: string) => void;
  onTaskResultsChanged?: (show: boolean) => void;
}

export const MAX_ACTIONS = 8;
export const LABEL_MAX = 40;
export const DRAFT_MAX = 200;

/**
 * Opção residente do BACKGROUND V1 (`Continuar em segundo plano ao fechar`).
 * Só aparece quando a ponte do companheiro existe (`available`); falha mantém o valor anterior
 * com mensagem honesta. Persistida pelo main em `.local/desktop-presence.json` — nunca no schema
 * fechado `lunmi-ui.json`.
 */
function ResidentCard(): React.ReactElement | null {
  const { resident, available, busy, setResident } = useResidentOption();
  const [error, setError] = useState<string | null>(null);
  if (!available) return null;
  const apply = async (next: boolean) => {
    setError(null);
    const saved = await setResident(next);
    if (!saved) setError("Não foi possível guardar a preferência de segundo plano. O valor anterior mantém-se.");
  };
  return (
    <div className="lunmi-settings-card" style={cardStyle} data-testid="resident-card">
      <h3 style={subHeadingStyle}>Segundo plano</h3>
      <label
        htmlFor="resident-toggle"
        style={{ display: "flex", alignItems: "flex-start", gap: "8px", fontSize: "13px" }}
      >
        <input
          id="resident-toggle"
          data-testid="resident-toggle"
          type="checkbox"
          checked={resident}
          disabled={busy}
          onChange={() => void apply(!resident)}
        />
        <span>
          Continuar em segundo plano ao fechar
          <br />
          <span style={{ color: TOKENS.textMuted, fontSize: "12px" }} data-testid="resident-scope">
            {resident
              ? "Fechar (X) esconde a janela e mantém a Lunmi em segundo plano; «Sair» na bandeja termina a aplicação."
              : "Fechar (X) termina a Lunmi, como antes."}
          </span>
        </span>
      </label>
      {error && <p role="alert" style={errorStyle} data-testid="resident-error">{error}</p>}
    </div>
  );
}

const NODE_ERROR_TEXT: Record<string, string> = {
  invalid_payload: "Os valores enviados são inválidos; nada foi gravado e os valores anteriores mantêm-se.",
  io_error: "Não foi possível gravar no disco. Os valores anteriores mantêm-se.",
  shortcut_conflict: "Esse atalho já está a ser usado por outra aplicação; o atalho anterior conserva-se.",
  shortcut_unavailable: "Esse atalho não pode ser registado neste sistema; o atalho anterior conserva-se.",
};

/**
 * Preferências do Lunmi Node (contrato `.agents/LUNMI-NODE-V1-CONTRACT.md`).
 * Consome apenas o hook/DTO do Codador; sem estado otimista: os controlos refletem sempre as
 * preferências canónicas devolvidas pelo main. Aparece só quando a ponte expõe `setNodePreferences`.
 */
function NodeCard(): React.ReactElement | null {
  const { preferences, available, busy, error, setNodePreferences } = useNodePreferences();
  const { resident } = useResidentOption();
  const [shortcutDraft, setShortcutDraft] = useState("");
  if (!available || !preferences) return null;
  const apply = (patch: Partial<NodePreferencesDto>) => { void setNodePreferences(patch); };
  const semantics = !resident
    ? "Com «Continuar em segundo plano» desligado, Fechar (X) termina a Lunmi e o Node não aparece."
    : preferences.enabled
      ? "Fechar (X) esconde a janela e mostra o Node no canto escolhido; «Sair» na bandeja termina a aplicação."
      : "Fechar (X) esconde para a bandeja sem Node; abre de novo pela bandeja. O Node desligado nunca termina a sessão.";
  return (
    <div className="lunmi-settings-card" style={cardStyle} data-testid="node-card">
      <h3 style={subHeadingStyle}>Lunmi Node</h3>
      <label style={{ display: "flex", alignItems: "flex-start", gap: "8px", fontSize: "13px" }}>
        <input type="checkbox" data-testid="node-enabled" checked={preferences.enabled} disabled={busy}
          onChange={() => apply({ enabled: !preferences.enabled })} />
        <span>
          Mostrar o Node quando a janela está fechada
          <br />
          <span style={{ color: TOKENS.textMuted, fontSize: "12px" }}>
            Um pequeno objeto preto num canto superior, sempre disponível; não desliga nem termina a Lunmi.
          </span>
        </span>
      </label>

      <details className="lunmi-settings-disclosure">
      <summary>Personalizar o Node</summary>
      <div role="radiogroup" aria-label="Canto do Node" style={{ display: "flex", flexWrap: "wrap", gap: "14px", margin: "10px 0 0", fontSize: "13px" }}>
        <label style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
          <input type="radio" name="node-side" data-testid="node-side-left" value="left"
            checked={preferences.side === "left"} disabled={busy || !preferences.enabled}
            onChange={() => apply({ side: "left" })} />
          Canto superior esquerdo
        </label>
        <label style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
          <input type="radio" name="node-side" data-testid="node-side-right" value="right"
            checked={preferences.side === "right"} disabled={busy || !preferences.enabled}
            onChange={() => apply({ side: "right" })} />
          Canto superior direito
        </label>
      </div>

      <label style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "13px", marginTop: "10px" }}>
        <input type="checkbox" data-testid="node-auto-dock" checked={preferences.auto_dock} disabled={busy}
          onChange={() => apply({ auto_dock: !preferences.auto_dock })} />
        Recolher automaticamente para a borda
      </label>
      <label style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "13px", marginTop: "6px" }}>
        <input type="checkbox" data-testid="node-show-activity" checked={preferences.show_activity} disabled={busy}
          onChange={() => apply({ show_activity: !preferences.show_activity })} />
        <span>Mostrar atividade no Node
          <small style={{ display: "block", color: TOKENS.textMuted, fontSize: "12px" }}>
            Confirmações pendentes e erros continuam visíveis mesmo desligado.
          </small>
        </span>
      </label>

      <div style={{ display: "flex", alignItems: "center", gap: "8px", marginTop: "12px", flexWrap: "wrap" }}>
        <label htmlFor="node-chat-appearance" style={{ fontSize: "13px" }}>Cores do chat de segundo plano</label>
        <select id="node-chat-appearance" data-testid="node-chat-appearance"
          value={preferences.chat_appearance ?? "theme"} disabled={busy}
          style={{ ...controlStyle, minHeight: "44px" }}
          onChange={event => apply({ chat_appearance: event.target.value as "theme" | "black" })}>
          <option value="theme">Seguir o tema</option>
          <option value="black">Preto</option>
        </select>
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: "8px", marginTop: "12px", flexWrap: "wrap" }}>
        <label htmlFor="node-shortcut-input" style={{ fontSize: "12px", color: TOKENS.textMuted }}>Atalho</label>
        <input
          id="node-shortcut-input"
          data-testid="node-shortcut-input"
          type="text"
          value={shortcutDraft || preferences.shortcut || ""}
          placeholder="ex.: Ctrl+Alt+Space"
          disabled={busy}
          onChange={event => setShortcutDraft(event.target.value)}
          style={{ ...controlStyle, width: "200px" }}
        />
        <button type="button" data-testid="node-shortcut-apply" className="lunmi-action-button lunmi-action-quiet" style={secondaryButtonStyle} disabled={busy || !shortcutDraft.trim()}
          onClick={() => { apply({ shortcut: shortcutDraft.trim() }); setShortcutDraft(""); }}>
          Definir atalho
        </button>
        {preferences.shortcut && (
          <button type="button" data-testid="node-shortcut-clear" className="lunmi-action-button lunmi-action-quiet" style={secondaryButtonStyle} disabled={busy}
            onClick={() => { setShortcutDraft(""); apply({ shortcut: null }); }}>
            Remover atalho
          </button>
        )}
      </div>
      <p style={{ margin: "6px 0 0", color: TOKENS.textMuted, fontSize: "12px" }}>
        O atalho abre e recolhe o chat do Node. Se outro programa já o usar, o teu atalho anterior fica intacto.
      </p>
      </details>

      <p style={{ margin: "10px 0 0", fontSize: "12px", color: TOKENS.textSecondary }} data-testid="node-semantics">{semantics}</p>
      {error && <p role="alert" style={errorStyle} data-testid="node-error">{NODE_ERROR_TEXT[error] ?? "A preferência não foi guardada; os valores anteriores mantêm-se."}</p>}
    </div>
  );
}

export function PreferencesPanel({ sendCommand, session, onThemeChanged, onTaskResultsChanged, history, historyLocked = false }: PreferencesPanelProps): React.ReactElement {
  const [approval, setApproval] = useState<ApprovalDto | null>(null);
  const [actions, setActions] = useState<QuickActionDto[]>([]);
  const [statusMessage, setStatusMessage] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [theme, setTheme] = useState("celestial");
  const [reduceMotion, setReduceMotion] = useState(false);
  const [showTaskResults, setShowTaskResults] = useState(false);

  const sessionReady = Boolean(session);

  useEffect(() => {
    let alive = true;
    setError(null);
    Promise.all([
      sendCommand<ApprovalDto>("approval.get", {}),
      sendCommand<UiPreferencesDto>("ui.preferences.get", {}),
    ])
      .then(([approvalDto, prefs]) => {
        if (!alive) {
          return;
        }
        setApproval(approvalDto);
        setActions(prefs.actions ?? []);
        setTheme(getTheme(prefs.theme).id);
        setReduceMotion(prefs.reduce_motion === true);
        setShowTaskResults(prefs.show_task_results === true);
      })
      .catch((err) => {
        if (alive) {
          setError(describeError(err));
        }
      });
    return () => {
      alive = false;
    };
  }, [sendCommand, session]);

  const changeTheme = async (id: string) => {
    if (!sessionReady || busy) return;
    setBusy(true); setError(null);
    try {
      const saved = await sendCommand<UiPreferencesDto>("ui.preferences.set", { theme: id });
      const next = getTheme(saved.theme).id;
      setTheme(next); onThemeChanged?.(next);
      setStatusMessage(`Tema ${getTheme(next).label} aplicado e salvo.`);
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  };

  const toggleReduceMotion = async () => {
    if (!sessionReady || busy) return;
    setBusy(true); setError(null);
    try {
      const saved = await sendCommand<UiPreferencesDto>("ui.preferences.set", { reduce_motion: !reduceMotion });
      setReduceMotion(saved.reduce_motion === true);
      setStatusMessage("Animações guardadas. A mudança aplica-se ao reiniciar a Lunmi.");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  };

  const toggleTaskResults = async () => {
    if (!sessionReady || busy) return;
    setBusy(true); setError(null);
    try {
      const saved = await sendCommand<UiPreferencesDto>("ui.preferences.set", { show_task_results: !showTaskResults });
      if (typeof saved.show_task_results !== "boolean") throw new Error("Esta versão não confirmou a preferência dos resultados.");
      setShowTaskResults(saved.show_task_results);
      onTaskResultsChanged?.(saved.show_task_results);
      setStatusMessage("Apresentação dos resultados guardada.");
    } catch (err) { setError(describeError(err)); }
    finally { setBusy(false); }
  };

  const toggleAutoApprove = useCallback(async () => {
    if (!sessionReady || !approval) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const next = await sendCommand<ApprovalDto>("approval.set", {
        auto_approve: !approval.auto_approve,
      });
      setApproval(next);
      setStatusMessage(next.label);
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }, [approval, sendCommand, sessionReady]);

  const savePreferences = useCallback(async () => {
    if (!sessionReady) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      // Escala e geometria pertencem ao main, não a esta tela.
      const saved = await sendCommand<UiPreferencesDto>("ui.preferences.set", {
        actions: actions.map((action) => ({ label: action.label, draft: action.draft })),
      });
      setActions(saved.actions ?? []);
      setStatusMessage("Preferências de tela salvas.");
    } catch (err) {
      setError(describeError(err));
    } finally {
      setBusy(false);
    }
  }, [actions, sendCommand, sessionReady]);

  const updateAction = useCallback((index: number, patch: Partial<QuickActionDto>) => {
    setActions((prev) =>
      prev.map((action, i) => (i === index ? { ...action, ...patch } : action))
    );
  }, []);

  const addAction = useCallback(() => {
    setActions((prev) =>
      prev.length >= MAX_ACTIONS ? prev : [...prev, { label: "Novo atalho", draft: "" }]
    );
  }, []);

  const removeAction = useCallback((index: number) => {
    setActions((prev) => prev.filter((_, i) => i !== index));
  }, []);

  return (
    <section
      aria-label="Preferências"
      data-testid="preferences-panel"
      className="lunmi-settings-page lunmi-preferences-page"
    >
      <div className="lunmi-settings-content">
      <header className="lunmi-settings-heading">
        <h2 style={headingStyle}>Configurações</h2>
        <p>Personaliza o espaço, a voz e os controlos da Lunmi.</p>
        {error && (
          <p role="alert" data-testid="preferences-error" style={errorStyle}>
            {error}
          </p>
        )}
        {statusMessage && (
          <p role="status" data-testid="preferences-status" style={statusStyle}>
            {statusMessage}
          </p>
        )}
      </header>
      <UpdatesCard />

      <div className="lunmi-settings-card" style={cardStyle} data-testid="theme-card">
        <h3 style={subHeadingStyle}><Icon name="palette" /> Aparência</h3>
        <p style={{ ...statusStyle, color: TOKENS.textMuted }}>Escolha o tema. A mudança é aplicada sem reiniciar.</p>
        <div className="lunmi-theme-grid" role="group" aria-label="Tema da interface">
          {Object.values(THEMES).map(item => <button type="button" key={item.id} data-testid={`theme-${item.id}`}
            className="lunmi-theme-choice" aria-pressed={theme === item.id} disabled={!sessionReady || busy}
            onClick={() => void changeTheme(item.id)}>
            <span className="lunmi-theme-preview" aria-hidden="true" style={{ background: item.colors.bgWindow }}>
              <span style={{ background: item.colors.bgSidebar }} /><i style={{ background: item.colors.bgCardSubtle, boxShadow: item.depth.raised }} />
              <b style={{ background: item.colors.accent }} />
            </span>
            <span className="lunmi-theme-name">{item.label}{theme === item.id && <Icon name="check" size={16} />}</span>
            <span className="lunmi-theme-description">{item.description}</span>
          </button>)}
        </div>
        <label style={{ display: "flex", alignItems: "flex-start", gap: "8px", fontSize: "13px" }}>
          <input type="checkbox" data-testid="reduce-motion" checked={reduceMotion} disabled={!sessionReady || busy}
            onChange={() => void toggleReduceMotion()} />
          Reduzir animações
        </label>
        <p style={{ ...statusStyle, color: TOKENS.textMuted }}>
          Para o orb, o fundo e as transições. Não depende das animações do Windows; aplica-se ao reiniciar a Lunmi.
        </p>
        <div style={{ marginTop: "16px", paddingTop: "16px", borderTop: `1px solid ${TOKENS.borderHairline}` }}>
          <label style={{ display: "flex", alignItems: "flex-start", gap: "8px", fontSize: "13px" }}>
            <input type="checkbox" data-testid="show-task-results" checked={showTaskResults} disabled={!sessionReady || busy}
              onChange={() => void toggleTaskResults()} />
            Mostrar resultado da tarefa no chat
          </label>
          <p style={{ ...statusStyle, color: TOKENS.textMuted }}>Resumo opcional com duração, passos e detalhes de execução.</p>
        </div>
      </div>

      <section className="lunmi-settings-section" aria-label="Ambiente de trabalho">
      <h3 className="lunmi-settings-section-title">Ambiente de trabalho</h3>
      <div className="lunmi-settings-grid">
      <ResidentCard />

      <NodeCard />
      </div>
      </section>

      <section className="lunmi-settings-section" aria-label="Voz e pesquisa">
      <h3 className="lunmi-settings-section-title">Voz e pesquisa</h3>
      <div className="lunmi-settings-grid">
      <VoiceSettingsCard sessionId={session?.session_id} />
      <WebSearchCard sessionId={session?.session_id} supportedBySession={session?.capabilities?.web_search === true} />
      </div>
      </section>

      <section className="lunmi-settings-section" aria-label="Dados e controlo">
      <h3 className="lunmi-settings-section-title">Dados e controlo</h3>
      <div className="lunmi-settings-grid">
      {history && <HistorySettings sendCommand={sendCommand} history={history} locked={historyLocked} />}

      <div className="lunmi-settings-card" style={cardStyle} data-testid="approval-card">
        <h3 style={subHeadingStyle}>Autoaprovação</h3>        <label
          htmlFor="auto-approve"
          style={{ display: "flex", alignItems: "flex-start", gap: "8px", fontSize: "13px" }}
        >
          <input
            id="auto-approve"
            data-testid="auto-approve-toggle"
            type="checkbox"
            checked={Boolean(approval?.auto_approve)}
            disabled={!sessionReady || busy || !approval}
            onChange={() => void toggleAutoApprove()}
          />
          <span>
            Aprovar automaticamente as ações que pedem confirmação
            <br />
            <span style={{ color: TOKENS.textMuted, fontSize: "12px" }} data-testid="approval-scope">
              Vale apenas para a próxima tarefa; depois dela o aplicativo volta ao modo manual.
            </span>
          </span>
        </label>
        <p style={{ margin: "10px 0 0", color: TOKENS.textSecondary, fontSize: "12px" }}>
          Modo atual: <strong>{approval ? approval.label : "…"}</strong>
        </p>
      </div>

      </div>
      </section>

      <div className="lunmi-settings-card" style={cardStyle} data-testid="actions-card">
        <h3 style={subHeadingStyle}>Atalhos do painel</h3>
        <p style={{ margin: "0 0 10px", color: TOKENS.textMuted, fontSize: "12px" }}>
          Até {MAX_ACTIONS} atalhos. O texto é o rascunho que o atalho coloca no campo de escrita.
        </p>

        <div data-testid="action-list" className="lunmi-shortcut-grid">
          {actions.map((action, index) => (
            <div
              key={`action-${index}`}
              data-testid={`action-row-${index}`}
              className="lunmi-shortcut-editor"
            >
              <label htmlFor={`action-label-${index}`} style={labelStyle}>
                Rótulo
              </label>
              <input
                id={`action-label-${index}`}
                data-testid={`action-label-${index}`}
                value={action.label}
                maxLength={LABEL_MAX}
                disabled={!sessionReady || busy}
                onChange={(event) => updateAction(index, { label: event.target.value })}
                style={controlStyle}
              />
              <label htmlFor={`action-draft-${index}`} style={labelStyle}>
                Rascunho
              </label>
              <input
                id={`action-draft-${index}`}
                data-testid={`action-draft-${index}`}
                value={action.draft}
                maxLength={DRAFT_MAX}
                disabled={!sessionReady || busy}
                onChange={(event) => updateAction(index, { draft: event.target.value })}
                style={controlStyle}
              />
              <button
                type="button"
                data-testid={`action-remove-${index}`}
                disabled={!sessionReady || busy}
                onClick={() => removeAction(index)}
                className="lunmi-action-button lunmi-action-quiet" style={secondaryButtonStyle}
              >
                Remover
              </button>
            </div>
          ))}
        </div>

        <div style={{ display: "flex", gap: "8px", marginTop: "12px", flexWrap: "wrap" }}>
          <button
            type="button"
            data-testid="action-add"
            disabled={!sessionReady || busy || actions.length >= MAX_ACTIONS}
            onClick={addAction}
            className="lunmi-action-button lunmi-action-quiet" style={secondaryButtonStyle}
          >
            Adicionar atalho
          </button>
          <button
            type="button"
            data-testid="save-preferences"
            disabled={!sessionReady || busy}
            onClick={() => void savePreferences()}
            className="lunmi-action-button lunmi-action-primary" style={primaryButtonStyle}
          >
            Salvar preferências
          </button>
        </div>
      </div>
      <div className="lunmi-settings-card" style={cardStyle} data-testid="session-card">
        <h3 style={subHeadingStyle}>Sessão atual</h3>
        {session ? (
          <details className="lunmi-session-details">
            <summary>Conexão segura · Python local</summary>
            <span style={{ color: TOKENS.textMuted }} data-testid="session-summary">
              sessão {session.session_id} · pid {session.pid} · capacidades{" "}
              {Object.entries(session.capabilities ?? {})
                .filter(([, enabled]) => Boolean(enabled))
                .map(([name]) => name)
                .join(", ") || "nenhuma"}
            </span>
          </details>
        ) : (
          <p style={{ ...statusStyle, color: TOKENS.textMuted }}>Nenhuma sessão ativa para detalhar.</p>
        )}
      </div>

      </div>
    </section>
  );
}

const headingStyle: React.CSSProperties = {
  margin: "0 0 6px",
  fontSize: "15px",
  color: TOKENS.textPrimary,
};
const subHeadingStyle: React.CSSProperties = {
  margin: "0 0 10px",
  fontSize: "13px",
  color: TOKENS.textSecondary,
};
const labelStyle: React.CSSProperties = {
  display: "block",
  margin: "6px 0 4px",
  fontSize: "12px",
  color: TOKENS.textMuted,
};
const controlStyle: React.CSSProperties = {
  width: "100%",
  padding: "8px 10px",
  borderRadius: "8px",
  border: `1px solid ${TOKENS.borderSubtle}`,
  backgroundColor: TOKENS.bgInput,
  color: TOKENS.textPrimary,
  fontSize: "13px",
};
const cardStyle: React.CSSProperties = {
  padding: "20px",
  borderRadius: "16px",
  border: `1px solid ${TOKENS.borderSubtle}`,
  backgroundColor: TOKENS.bgCard,
};
const errorStyle: React.CSSProperties = { margin: "8px 0 0", color: TOKENS.error, fontSize: "13px" };
const statusStyle: React.CSSProperties = { margin: "8px 0 0", color: TOKENS.success, fontSize: "13px" };
const primaryButtonStyle: React.CSSProperties = {
  padding: "8px 14px",
  borderRadius: "8px",
  border: "none",
  backgroundColor: TOKENS.accent,
  color: TOKENS.onAccent,
  fontSize: "13px",
  fontWeight: 600,
  cursor: "pointer",
};
const secondaryButtonStyle: React.CSSProperties = {
  padding: "8px 14px",
  borderRadius: "8px",
  border: `1px solid ${TOKENS.borderSubtle}`,
  backgroundColor: TOKENS.bgCardSubtle,
  color: TOKENS.textSecondary,
  fontSize: "13px",
  cursor: "pointer",
};

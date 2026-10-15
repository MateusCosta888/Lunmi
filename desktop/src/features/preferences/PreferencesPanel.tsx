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
import { TOKENS, THEMES, getTheme } from "../../shared/theme";
import { Icon } from "../../shared/ui";
import { describeError, type SendCommandFn } from "../../shared/ipc/client";
import type { ApprovalDto, QuickActionDto, UiPreferencesDto } from "../../shared/types";
import type { SessionReadyPayload } from "../../shared/protocol";

export interface PreferencesPanelProps {
  sendCommand: SendCommandFn;
  session: SessionReadyPayload | null;
  onThemeChanged?: (theme: string) => void;
}

export const MAX_ACTIONS = 8;
export const LABEL_MAX = 40;
export const DRAFT_MAX = 200;

export function PreferencesPanel({ sendCommand, session, onThemeChanged }: PreferencesPanelProps): React.ReactElement {
  const [approval, setApproval] = useState<ApprovalDto | null>(null);
  const [actions, setActions] = useState<QuickActionDto[]>([]);
  const [statusMessage, setStatusMessage] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [theme, setTheme] = useState("celestial");

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
      style={{
        flex: "1 1 auto",
        overflowY: "auto",
        padding: "20px",
        display: "grid",
        gap: "16px",
        alignContent: "start",
      }}
    >
      <header>
        <h2 style={headingStyle}>Preferências</h2>
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

      <div style={cardStyle} data-testid="theme-card">
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
      </div>

      <div style={cardStyle} data-testid="session-card">
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

      <div style={cardStyle} data-testid="approval-card">
        <h3 style={subHeadingStyle}>Autoaprovação</h3>
        <label
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

      <div style={cardStyle} data-testid="actions-card">
        <h3 style={subHeadingStyle}>Atalhos do painel</h3>
        <p style={{ margin: "0 0 10px", color: TOKENS.textMuted, fontSize: "12px" }}>
          Até {MAX_ACTIONS} atalhos. O texto é o rascunho que o atalho coloca no campo de escrita.
        </p>

        <div data-testid="action-list" style={{ display: "grid", gap: "10px" }}>
          {actions.map((action, index) => (
            <div
              key={`action-${index}`}
              data-testid={`action-row-${index}`}
              style={{ display: "grid", gap: "6px" }}
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
                style={secondaryButtonStyle}
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
            style={secondaryButtonStyle}
          >
            Adicionar atalho
          </button>
          <button
            type="button"
            data-testid="save-preferences"
            disabled={!sessionReady || busy}
            onClick={() => void savePreferences()}
            style={primaryButtonStyle}
          >
            Salvar preferências
          </button>
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
  padding: "16px",
  borderRadius: "12px",
  border: `1px solid ${TOKENS.borderSubtle}`,
  backgroundColor: TOKENS.bgCard,
  maxWidth: "660px",
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

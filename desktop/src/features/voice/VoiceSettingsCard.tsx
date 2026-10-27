/**
 * Cartão de voz das Configurações (`desktop/src/features/voice/VoiceSettingsCard.tsx`, V2).
 *
 * Os ajustes de voz saíram do compositor para Definições: Ativar voz, Falar respostas, Testar,
 * Ouvir última resposta e Parar voz (contrato de saída, payloads exatos, guardas no `useVoice`),
 * mais o atalho de ditado — `get()` UMA vez por montagem mostra o valor efetivo com a ordem
 * visual preservada pelo main; «Alterar» usa o KeyRecorder (teclas físicas, sem campo de
 * texto); «Remover» envia `null`; erros com copy fixa sanitizada. Sem ponte de voz o cartão
 * não existe; sem API de atalho (ponte antiga) só falta a secção do atalho. O microfone do
 * compositor continua a ser o ditado manual editável; nenhum áudio automático de arranque.
 */
import React, { useEffect, useState } from "react";
import { TOKENS } from "../../shared/theme";
import { useVoice } from "./useVoice";
import { KeyRecorder, shortcutErrorMessage, useVoiceShortcutApi } from "./KeyRecorder";
import "./voice.css";

const cardStyle: React.CSSProperties = {
  padding: "16px", borderRadius: "12px", border: `1px solid ${TOKENS.borderSubtle}`,
  backgroundColor: TOKENS.bgCard, maxWidth: "660px",
};
const subHeadingStyle: React.CSSProperties = { margin: "0 0 12px", fontSize: "15px", fontWeight: 600, color: TOKENS.textPrimary };
const secondaryButtonStyle: React.CSSProperties = {
  padding: "8px 14px", borderRadius: "10px", border: `1px solid ${TOKENS.borderSubtle}`,
  background: TOKENS.bgCardSubtle, color: TOKENS.textSecondary, font: "inherit", fontSize: "13px", cursor: "pointer",
};
const errorStyle: React.CSSProperties = { margin: "8px 0 0", color: TOKENS.error, fontSize: "13px" };

export interface VoiceSettingsCardProps {
  sessionId: string | undefined;
}

export function VoiceSettingsCard({ sessionId }: VoiceSettingsCardProps): React.ReactElement | null {
  const { voice, supported, busy, error, setEnabled, setAutoSpeak, test, speakLast, stopVoice } = useVoice(sessionId);
  const shortcutApi = useVoiceShortcutApi();
  const [binding, setBinding] = useState<string | null>(null);
  const [shortcutError, setShortcutError] = useState("");
  const [shortcutBusy, setShortcutBusy] = useState(false);
  useEffect(() => {
    if (!shortcutApi) return;
    let alive = true;
    shortcutApi.get()
      .then(result => { if (alive) setBinding(result.accelerator); })
      .catch(() => { if (alive) setBinding(null); });
    return () => { alive = false; };
  }, [shortcutApi]);
  if (!supported) return null;
  const state = voice?.state ?? "disabled";
  const speaking = state === "synthesizing" || state === "playing" || state === "loading";
  const enabled = Boolean(voice?.enabled);
  const statusText = error || voice?.message || `Estado: ${state}`;

  const removeShortcut = async () => {
    if (!shortcutApi) return;
    setShortcutBusy(true);
    setShortcutError("");
    try {
      const result = await shortcutApi.set(null);
      setBinding(result.accelerator);
      if (result.error) setShortcutError(shortcutErrorMessage(result.error) || "O atalho não foi removido; o estado anterior mantém-se.");
    } catch {
      setShortcutError("Não foi possível comunicar com o observador de atalhos; o atalho anterior mantém-se.");
    } finally {
      setShortcutBusy(false);
    }
  };

  return (
    <div className="lunmi-voice-settings-card" style={cardStyle} data-testid="voice-card">
      <h3 style={subHeadingStyle}>Voz</h3>
      <label style={{ display: "flex", alignItems: "flex-start", gap: "8px", fontSize: "13px" }}>
        <input type="checkbox" data-testid="voice-enabled" checked={enabled} disabled={busy}
          onChange={event => setEnabled(event.target.checked)} />
        <span>
          Ativar voz
          <br />
          <span style={{ color: TOKENS.textMuted, fontSize: "12px" }}>
            A Lunmi fala respostas quando quiseres; nada é enviado nem gravado por voz sem um gesto teu.
          </span>
        </span>
      </label>
      <label style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "13px", marginTop: "10px" }}>
        <input type="checkbox" data-testid="voice-auto-speak" checked={Boolean(voice?.auto_speak)}
          disabled={busy || !enabled} onChange={event => setAutoSpeak(event.target.checked)} />
        Falar respostas automaticamente
      </label>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "8px", marginTop: "12px" }}>
        <button type="button" data-testid="voice-test" style={secondaryButtonStyle}
          disabled={busy || !enabled || voice?.available !== true} onClick={test}>
          Testar voz
        </button>
        <button type="button" data-testid="voice-speak-last" style={secondaryButtonStyle}
          disabled={busy || !enabled || !voice?.can_speak_last} onClick={speakLast}>
          Ouvir última resposta
        </button>
        <button type="button" data-testid="voice-stop" style={secondaryButtonStyle}
          disabled={busy || !speaking} onClick={stopVoice}>
          Parar voz
        </button>
      </div>
      <p style={{ margin: "8px 0 0", fontSize: "12px", color: TOKENS.textMuted }} role="status" data-testid="voice-status">
        {statusText}
      </p>

      {shortcutApi && (
        <div style={{ marginTop: "14px", borderTop: `1px solid ${TOKENS.borderSubtle}`, paddingTop: "12px" }} data-testid="voice-shortcut-section">
          <p style={{ margin: "0 0 8px", fontSize: "13px" }} data-testid="voice-shortcut-current">
            {binding ? `Atalho de ditado: ${binding}` : "Sem atalho de ditado configurado."}
          </p>
          <p style={{ margin: "0 0 10px", fontSize: "12px", color: TOKENS.textMuted }}>
            Mantém premido o atalho para ditar; ao largar, a transcrição é enviada. O botão de
            microfone no chat continua a ditado manual, sempre revisto antes de enviar.
          </p>
          <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
            <KeyRecorder api={shortcutApi} disabled={shortcutBusy}
              onApplied={accelerator => { setBinding(accelerator); setShortcutError(""); }}
              onError={message => setShortcutError(message)} />
            {binding && (
              <button type="button" data-testid="voice-shortcut-remove" style={secondaryButtonStyle}
                disabled={shortcutBusy} onClick={() => void removeShortcut()}>
                Remover
              </button>
            )}
          </div>
          {shortcutError && <p role="alert" style={errorStyle} data-testid="voice-shortcut-error">{shortcutError}</p>}
        </div>
      )}
    </div>
  );
}

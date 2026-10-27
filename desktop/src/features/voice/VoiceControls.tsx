/**
 * Controlos de saída de voz no compositor (`desktop/src/features/voice/VoiceControls.tsx`).
 *
 * VOICE REFINEMENTS V2: o chat mantém apenas o operacional mínimo — estado de fala e «Parar voz»
 * (comando próprio `voice.stop`, nunca `task.cancel`). As opções (Ativar voz, Falar respostas,
 * Testar, Ouvir última resposta, atalho de ditado) vivem em Configurações → Voz, porque o GO
 * remove os ajustes do compositor. Estados e mensagens vêm do backend; nada de texto de conversa,
 * paths ou segredos no DOM; nenhum áudio automático de arranque.
 */
import React from "react";
import { useVoice } from "./useVoice";
import "./voice.css";

export interface VoiceControlsProps {
  sessionId: string | undefined;
}

export function VoiceControls({ sessionId }: VoiceControlsProps): React.ReactElement | null {
  const { voice, supported, busy, error, stopVoice } = useVoice(sessionId);
  if (!supported) return null;
  const state = voice?.state ?? "disabled";
  const speaking = state === "synthesizing" || state === "playing" || state === "loading";
  const enabled = Boolean(voice?.enabled);
  const label = speaking ? "a falar…"
    : state === "error" ? "voz indisponível"
    : enabled ? "voz pronta"
    : "voz desligada";
  return (
    <div className="lunmi-voice lunmi-voice-min">
      {/* VOICE/NODE V3: sem texto de estado normal no chat; a etiqueta canónica vive em
          Configurações → Voz. O estado continua acessível (sr-only + aria-live) e o erro é
          sempre visível. Parar voz permanece comando próprio, distinto de Parar tarefa. */}
      <span className="lunmi-sr-only" role="status" data-state={state} data-testid="voice-state-label">{label}</span>
      {error && <p className="lunmi-voice-input-message" role="alert" data-testid="voice-error">{error}</p>}
      {speaking && (
        <button type="button" data-testid="voice-stop-min" className="lunmi-mic-round lunmi-mic-stop"
          aria-label="Parar voz" title="Parar voz" disabled={busy} onClick={stopVoice}>
          <StopIcon />
        </button>
      )}
    </div>
  );
}

function StopIcon(): React.ReactElement {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true" focusable="false">
      <rect x="2.5" y="2.5" width="9" height="9" rx="2" fill="currentColor" />
    </svg>
  );
}

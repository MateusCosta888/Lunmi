/**
 * Controlos de saída de voz (`desktop/src/features/voice/VoiceControls.tsx`, VOICE-V1-OUTPUT).
 *
 * Um botão compacto no compositor abre o popover: Ativar voz, Falar respostas, Testar voz,
 * Ouvir última resposta e Parar voz. Parar voz é distinto de Parar tarefa (comando próprio
 * `voice.stop`, nunca `task.cancel`). Não existe qualquer controlo de captura: o microfone é
 * indicado como pendente de validação (`capture_supported` é sempre `false` nesta entrega).
 * Estados e mensagens vêm do backend; nada de texto de conversa, paths ou segredos no DOM.
 */
import React, { useEffect, useRef, useState } from "react";
import { Volume2, VolumeX } from "lucide-react";
import { useVoice } from "./useVoice";
import type { VoiceStateName } from "../../shared/types";
import "./voice.css";

const STATE_LABEL: Record<VoiceStateName, string> = {
  disabled: "desligada",
  loading: "carregando motor…",
  ready: "pronta",
  synthesizing: "sintetizando…",
  playing: "falando…",
  error: "indisponível",
};

export interface VoiceControlsProps {
  sessionId: string | undefined;
}

export function VoiceControls({ sessionId }: VoiceControlsProps): React.ReactElement {
  const { voice, supported, busy, error, setEnabled, setAutoSpeak, test, speakLast, stopVoice } = useVoice(sessionId);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === "Escape" && open) {
      event.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    }
  };

  const state = voice?.state ?? "disabled";
  const speaking = state === "synthesizing" || state === "playing" || state === "loading";
  const enabled = Boolean(voice?.enabled);
  const unavailableMessage = !supported
    ? "Voz indisponível: ponte ausente."
    : error ?? voice?.message ?? "";

  return (
    <div className="lunmi-voice" ref={rootRef} onKeyDown={onKeyDown}>
      <button type="button" data-testid="voice-toggle" className="lunmi-voice-trigger"
        ref={triggerRef} aria-expanded={open} aria-haspopup="dialog"
        onClick={() => setOpen(value => !value)}>
        {enabled ? <Volume2 size={14} aria-hidden="true" /> : <VolumeX size={14} aria-hidden="true" />}
        <span>Voz</span>
        <span className="lunmi-voice-state" data-state={state} data-testid="voice-state-label">
          {supported ? STATE_LABEL[state] : "indisponível"}
        </span>
      </button>

      {open ? (
        <div className="lunmi-voice-pop" role="dialog" aria-label="Controles de voz" data-testid="voice-popover">
          <label className="lunmi-voice-check">
            <input type="checkbox" data-testid="voice-enabled" checked={enabled} disabled={!supported || busy}
              onChange={event => setEnabled(event.target.checked)} />
            Ativar voz
          </label>
          <label className="lunmi-voice-check">
            <input type="checkbox" data-testid="voice-auto-speak" checked={Boolean(voice?.auto_speak)}
              disabled={!supported || busy || !enabled}
              onChange={event => setAutoSpeak(event.target.checked)} />
            Falar respostas
          </label>
          <div className="lunmi-voice-actions">
            <button type="button" data-testid="voice-test" className="lunmi-voice-button"
              disabled={!supported || busy || !enabled || voice?.available !== true}
              onClick={test}>
              Testar voz
            </button>
            <button type="button" data-testid="voice-speak-last" className="lunmi-voice-button"
              disabled={!supported || busy || !enabled || !voice?.can_speak_last}
              onClick={speakLast}>
              Ouvir última resposta
            </button>
            <button type="button" data-testid="voice-stop" className="lunmi-voice-button lunmi-voice-stop"
              disabled={!supported || busy || !speaking}
              onClick={stopVoice}>
              Parar voz
            </button>
          </div>
          <p className="lunmi-voice-status" data-testid="voice-status" role="status">
            {unavailableMessage || `Estado: ${STATE_LABEL[state]}`}
          </p>
          <p className="lunmi-voice-mic-note" data-testid="voice-mic-note">
            Microfone: pendente de validação. Esta versão apenas fala; nada é gravado.
          </p>
        </div>
      ) : null}
    </div>
  );
}

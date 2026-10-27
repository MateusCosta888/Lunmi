/**
 * Controlos de ditado (`desktop/src/features/voice/VoiceInputControls.tsx`, V1 + REFINEMENTS V2).
 *
 * Botão de microfone de 44 px no compositor — o estado mínimo operacional do chat (mic,
 * Cancelar, estado). Recebe a instância única de `useVoiceInput` montada no App (o hook vive uma
 * só vez para que o controlador de hold e este painel consumam o mesmo stream de resultados). O
 * ditado MANUAL continua editável: rascunho inalterado desde o clique entra direto como rascunho
 * editável; rascunho alterado, ou captura iniciada por atalho com rascunho pré-existente, abre
 * preview editável com inserção explícita (nunca substituição silenciosa). Transcrições de turno
 * hold com `autoSend` NÃO passam por aqui — o controlador decide o envio; se o autoenvio falhar,
 * o resultado é despromovido a manual e cai no preview. Os ajustes de voz e do atalho vivem em
 * Configurações → Voz (V2); nada aqui escreve em disco nem chama `chat.send`.
 */
import React, { useEffect, useRef, useState } from "react";
import { Mic } from "lucide-react";
import type { VoiceInputApi } from "./useVoiceInput";
import "./voice.css";

const STATE_LABEL: Record<string, string> = {
  unavailable: "indisponível",
  idle: "pronto para ditar",
  starting: "a iniciar…",
  listening: "a escutar…",
  transcribing: "a transcrever…",
  ready: "pronto",
  error: "erro",
};

export interface VoiceInputControlsProps {
  /** Instância única do hook, montada no App; sem capability a vista nem é renderizada. */
  api: VoiceInputApi;
  /** `session.ready.payload.capabilities.voice_input === true` — ponte antiga esconde o controlo. */
  supportedBySession: boolean;
  draft: string;
  setDraft: React.Dispatch<React.SetStateAction<string>>;
  /** Aviso do controlador de hold (autoenvio falhou; texto ficou para ação manual). */
  holdNotice?: string;
}

export function VoiceInputControls({
  api, supportedBySession, draft, setDraft, holdNotice = "",
}: VoiceInputControlsProps): React.ReactElement | null {
  if (!supportedBySession) return null;
  return <VoiceInputControlsInner api={api} draft={draft} setDraft={setDraft} holdNotice={holdNotice} />;
}

function VoiceInputControlsInner({
  api, draft, setDraft, holdNotice,
}: { api: VoiceInputApi; draft: string; setDraft: React.Dispatch<React.SetStateAction<string>>; holdNotice: string }): React.ReactElement {
  const { state, supported, busy, error, transcript, isActive, start, cancel, dismissTranscript } = api;
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const baseDraftRef = useRef("");
  const startedByClickRef = useRef(false);
  const shortcutStartRef = useRef(false);
  const activePhaseRef = useRef(false);
  const [preview, setPreview] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const stateName = (state?.state ?? (supported ? "idle" : "unavailable")) as string;

  // Origem da captura: `starting` canónico vindo de evento SEM clique nosso é o atalho do
  // Electron (hold). A base do rascunho guarda-se nesse instante; rascunho preexistente em
  // captura por atalho nunca é substituído em silêncio — vai para preview explícito.
  useEffect(() => {
    const terminal = stateName === "idle" || stateName === "unavailable" || stateName === "error";
    if (terminal) { startedByClickRef.current = false; activePhaseRef.current = false; return; }
    if ((stateName === "starting" || stateName === "listening" || stateName === "transcribing" || stateName === "ready") && !activePhaseRef.current) {
      activePhaseRef.current = true;
      if (stateName === "starting") {
        if (startedByClickRef.current) startedByClickRef.current = false;
        else { baseDraftRef.current = draftRef.current; shortcutStartRef.current = true; }
      }
    }
  }, [stateName]);

  // Transcrição manual chegou: aplica a política de rascunho. Turnos hold com `autoSend` são do
  // controlador (App); quando ele despromove o resultado, este efeito corre de novo e a política
  // manual aplica-se (preview se houver conflito).
  useEffect(() => {
    if (!transcript) { setPreview(null); return; }
    if (transcript.autoSend) return; // decisão de envio é do controlador, nunca daqui
    setNotice(transcript.truncated ? "A transcrição foi cortada no limite de 2000 caracteres." : "");
    const unchanged = draftRef.current === baseDraftRef.current;
    const externalStart = shortcutStartRef.current;
    shortcutStartRef.current = false;
    if (unchanged && (!externalStart || baseDraftRef.current === "")) {
      setDraft(transcript.text);
      dismissTranscript();
    } else {
      setPreview(transcript.text);
    }
    // deps: apenas a transcrição; o rascunho é lido por ref no instante do evento.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transcript]);

  const unavailable = stateName === "unavailable" || !supported;
  const message = error ?? state?.message ?? "";
  const showMessage = message !== "" && (stateName === "idle" || stateName === "error");

  const onStart = () => {
    setNotice("");
    startedByClickRef.current = true;
    baseDraftRef.current = draftRef.current;
    void start();
  };

  const insertPreview = () => {
    if (preview === null) return;
    const base = draftRef.current.replace(/\s+$/u, "");
    setDraft(base ? `${base} ${preview}` : preview);
    setPreview(null);
    dismissTranscript();
  };

  const stateAria = unavailable ? "Ditado indisponível"
    : isActive ? `A gravar — ${STATE_LABEL[stateName] ?? stateName}`
    : stateName === "error" ? `Ditado com erro — ${message || "tenta novamente"}`
    : "Gravar ditado";

  return (
    <div className="lunmi-voice-input">
      <div className="lunmi-voice-input-row">
        <button
          type="button"
          data-testid="voice-input-toggle"
          data-state={stateName}
          className="lunmi-mic-round"
          aria-label={stateAria}
          aria-pressed={isActive}
          disabled={unavailable || busy || isActive}
          title={unavailable ? (state?.message || "Ditado indisponível.") : "Ditar; o texto entra como rascunho para rever"}
          onClick={onStart}
        >
          <Mic size={18} aria-hidden="true" />
          <span className="lunmi-sr-only" role="status" data-testid="voice-input-state">
            {STATE_LABEL[stateName] ?? stateName}
          </span>
        </button>
        {isActive && (
          <button
            type="button"
            data-testid="voice-input-cancel"
            className="lunmi-voice-input-cancel"
            disabled={busy}
            onClick={() => void cancel()}
          >
            Cancelar
          </button>
        )}
      </div>
      {(showMessage || stateName === "error") && message && (
        <p className="lunmi-voice-input-message" role="status" data-testid="voice-input-message">{message}</p>
      )}
      {holdNotice && (
        <p className="lunmi-voice-input-message" role="status" data-testid="voice-input-hold-notice">{holdNotice}</p>
      )}
      {notice && !transcript && (
        <p className="lunmi-voice-input-message" role="status" data-testid="voice-input-notice">{notice}</p>
      )}
      {preview !== null && (
        <div className="lunmi-voice-input-preview" data-testid="voice-input-preview" role="region" aria-label="Transcrição para rever">
          <textarea
            data-testid="voice-input-preview-text"
            className="lunmi-voice-input-preview-text"
            aria-label="Transcrição (editável antes de inserir)"
            value={preview}
            rows={2}
            onChange={event => setPreview(event.target.value)}
          />
          <div className="lunmi-voice-input-preview-actions">
            <button type="button" data-testid="voice-input-insert" className="lunmi-voice-button" onClick={insertPreview}>
              Inserir no texto
            </button>
            <button
              type="button"
              data-testid="voice-input-discard"
              className="lunmi-voice-button"
              onClick={() => { setPreview(null); dismissTranscript(); setNotice(""); }}
            >
              Descartar
            </button>
          </div>
          {notice && <p className="lunmi-voice-input-message" role="status" data-testid="voice-input-notice">{notice}</p>}
        </div>
      )}
    </div>
  );
}

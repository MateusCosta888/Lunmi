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
import type { HoldButton } from "./useHoldController";
import "./voice.css";

/** A press shorter than this is a tap, not speech: cancelled with a hint instead of sent. */
const MIN_HOLD_MS = 250;
const METER_BARS = 24;
const HOLD_KEYS = new Set([" ", "Enter"]);

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
  /** Premir-e-falar: enquanto premido escuta; ao soltar transcreve e envia (mesmo fluxo do atalho). */
  hold?: HoldButton;
}

export function VoiceInputControls({
  api, supportedBySession, draft, setDraft, holdNotice = "", hold,
}: VoiceInputControlsProps): React.ReactElement | null {
  if (!supportedBySession) return null;
  return <VoiceInputControlsInner api={api} draft={draft} setDraft={setDraft} holdNotice={holdNotice} hold={hold} />;
}

/** Live loudness bars: the newest sits against the button and the history flows left. */
function VoiceLevelMeter({ onLevel }: { onLevel: VoiceInputApi["onLevel"] }): React.ReactElement {
  const [levels, setLevels] = useState<number[]>(() => Array<number>(METER_BARS).fill(0));
  useEffect(() => onLevel(level => setLevels(previous => [...previous.slice(1), level])), [onLevel]);
  return (
    <div className="lunmi-voice-meter" data-testid="voice-level-meter" aria-hidden="true">
      <div className="lunmi-voice-meter-bars">
        {levels.map((level, index) => (
          <span key={index} style={{ "--level": Math.max(0.18, level) } as React.CSSProperties} />
        ))}
      </div>
    </div>
  );
}

function VoiceInputControlsInner({
  api, draft, setDraft, holdNotice, hold,
}: { api: VoiceInputApi; draft: string; setDraft: React.Dispatch<React.SetStateAction<string>>; holdNotice: string;
     hold?: HoldButton }): React.ReactElement {
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

  // Premir-e-falar: um gesto por vez; soltar envia, um toque curto ou Esc cancela.
  const [held, setHeld] = useState(false);
  // pointerup is followed by lostpointercapture: the ref ends the gesture exactly once.
  const heldRef = useRef(false);
  const heldAtRef = useRef(0);
  const beginHold = () => {
    // Chromium still delivers pointerdown to a disabled button: unavailable never listens.
    if (!hold || heldRef.current || unavailable || busy || isActive) return;
    heldRef.current = true;
    setNotice("");
    heldAtRef.current = performance.now();
    setHeld(true);
    hold.press();
  };
  const endHold = (send: boolean) => {
    if (!hold || !heldRef.current) return;
    heldRef.current = false;
    setHeld(false);
    if (send && performance.now() - heldAtRef.current >= MIN_HOLD_MS) {
      hold.release();
      return;
    }
    hold.cancel();
    if (send) setNotice("Mantém o botão premido enquanto falas; solta para enviar.");
  };

  const insertPreview = () => {
    if (preview === null) return;
    const base = draftRef.current.replace(/\s+$/u, "");
    setDraft(base ? `${base} ${preview}` : preview);
    setPreview(null);
    dismissTranscript();
  };

  const stateAria = unavailable ? "Ditado indisponível"
    : held ? "A ouvir — solta para enviar, Esc cancela"
    : isActive ? `A gravar — ${STATE_LABEL[stateName] ?? stateName}`
    : stateName === "error" ? `Ditado com erro — ${message || "tenta novamente"}`
    : hold ? "Manter premido para falar"
    : "Gravar ditado";

  const holdHandlers: React.ButtonHTMLAttributes<HTMLButtonElement> = hold ? {
    onPointerDown: event => {
      if (event.button > 0) return; // só o botão principal; o direito não fala
      event.preventDefault(); // o rascunho mantém o foco; o gesto é só deste botão
      event.currentTarget.setPointerCapture?.(event.pointerId);
      beginHold();
    },
    onPointerUp: () => endHold(true),
    onPointerCancel: () => endHold(false),
    onLostPointerCapture: () => endHold(true),
    onKeyDown: event => {
      if (event.key === "Escape" && heldRef.current) { event.preventDefault(); endHold(false); return; }
      if (!HOLD_KEYS.has(event.key)) return;
      event.preventDefault();
      if (!event.repeat) beginHold();
    },
    onKeyUp: event => {
      if (!HOLD_KEYS.has(event.key)) return;
      event.preventDefault();
      endHold(true);
    },
    onBlur: () => endHold(false),
  } : { onClick: onStart };

  return (
    <div className="lunmi-voice-input">
      <div className="lunmi-voice-input-row">
        {held && <VoiceLevelMeter onLevel={api.onLevel} />}
        <button
          type="button"
          data-testid="voice-input-toggle"
          data-state={stateName}
          data-held={held || undefined}
          className="lunmi-mic-round"
          aria-label={stateAria}
          aria-pressed={held || isActive}
          disabled={unavailable || (!held && (busy || isActive))}
          title={unavailable ? (state?.message || "Ditado indisponível.")
            : hold ? "Mantém premido para falar · solta para enviar"
            : "Ditar; o texto entra como rascunho para rever"}
          {...holdHandlers}
        >
          <Mic size={18} aria-hidden="true" />
          <span className="lunmi-sr-only" role="status" data-testid="voice-input-state">
            {STATE_LABEL[stateName] ?? stateName}
          </span>
        </button>
        {isActive && !held && (
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
      {stateName === "transcribing" && (
        <p className="lunmi-voice-input-message" aria-hidden="true" data-testid="voice-input-transcribing">A transcrever…</p>
      )}
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

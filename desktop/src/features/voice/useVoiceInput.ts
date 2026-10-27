/**
 * Hook de ditado (`desktop/src/features/voice/useVoiceInput.ts`, VOICE INPUT V1).
 *
 * Contrato congelado `.agents/VOICE-INPUT-V1-CONTRACT.md`: comandos `voice.input.get/start/
 * cancel/result` com payloads exatos — o renderer nunca envia texto, paths, modelo ou dispositivo.
 * O evento `voice.input.state` é subscrito ANTES do GET inicial; geração só cresce e um DTO mais
 * velho (evento atrasado ou GET tardio) nunca sobrescreve estado mais novo; a epoch de sessão repõe
 * a contagem e invalida ACK/result pendentes da sessão anterior. Em `ready`, o texto privado é
 * pedido UMA vez por geração (`voice.input.result {generation}`) e entregue ao componente para
 * inserção explícita — este hook nunca chama `chat.send`, nunca escreve no compositor e nunca
 * guarda transcrição em estado persistente. `start` aceite com `ready` pendente invalida o
 * resultado pela geração superior. `cancel` aceite limpa o transcript local.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { describeError, getBridgeApi } from "../../shared/ipc/client";
import type {
  VoiceInputAckDto,
  VoiceInputResultDto,
  VoiceInputStateDto,
  VoiceInputStateEventDto,
} from "../../shared/types";

const REASON_TEXT: Record<string, string> = {
  unavailable: "Ditado indisponível: runtime ou modelos de fala não estão presentes.",
  busy: "Já existe um ditado em curso.",
  not_active: "Não há ditado em curso para cancelar.",
};

export interface VoiceInputTranscript {
  text: string;
  truncated: boolean;
  generation: number;
  /** V2: `true` só em turno hold com release aceite; decide o autoenvio no controlador, nunca aqui. */
  autoSend: boolean;
}

export interface VoiceInputApi {
  state: VoiceInputStateDto | null;
  supported: boolean;
  busy: boolean;
  error: string | null;
  transcript: VoiceInputTranscript | null;
  isActive: boolean;
  start: (mode?: "manual" | "hold") => Promise<VoiceInputAckDto | null>;
  finish: (generation: number) => Promise<VoiceInputAckDto | null>;
  cancel: () => Promise<VoiceInputAckDto | null>;
  dismissTranscript: () => void;
  demoteTranscript: () => void;
}

export function useVoiceInput(sessionId: string | undefined, enabled = true): VoiceInputApi {
  const api = getBridgeApi();
  const supported = enabled && typeof api?.sendCommand === "function" && typeof api?.onEvent === "function";
  const [state, setState] = useState<VoiceInputStateDto | null>(null);
  const [transcript, setTranscript] = useState<VoiceInputTranscript | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const generationRef = useRef(0);
  const requestEpochRef = useRef(0);
  const sessionEpochRef = useRef(0);
  const consumedGenerationRef = useRef(-1);
  const mountedRef = useRef(true);
  const stateRef = useRef<VoiceInputStateDto | null>(null);
  const transcriptRef = useRef<VoiceInputTranscript | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const commit = useCallback((next: VoiceInputStateDto) => {
    // Gate PRIMEIRO: um DTO de geração substituída não publica nada (nem `stateRef`, nem React
    // state) — o gate Root `renderer-order-gate.test.tsx` exige que a UI visível não regrida.
    if (next.generation < generationRef.current) return;
    const isNewGeneration = next.generation > generationRef.current;
    stateRef.current = next;
    setState(next);
    generationRef.current = next.generation;
    requestEpochRef.current += 1;
    // Geração nova (start/cancel/erro/close) caduca qualquer resultado por inserir: texto obsoleto
    // nunca chega ao compositor.
    if (isNewGeneration && transcriptRef.current && transcriptRef.current.generation < next.generation) {
      setTranscript(null);
      transcriptRef.current = null;
    }
    if (next.state !== "error") setError(null);
    if (next.state === "ready" && consumedGenerationRef.current !== next.generation) {
      consumedGenerationRef.current = next.generation;
      const generation = next.generation;
      const sessionEpoch = sessionEpochRef.current;
      void api!.sendCommand<VoiceInputResultDto>("voice.input.result", { generation })
        .then(result => {
          if (!mountedRef.current || sessionEpochRef.current !== sessionEpoch) return;
          if (generationRef.current !== generation) return; // resultado de geração substituída
          if (result.status !== "ready" || !result.text) return; // none: nada consumível
          const nextTranscript: VoiceInputTranscript = {
            text: result.text, truncated: result.truncated, generation,
            autoSend: result.auto_send === true,
          };
          transcriptRef.current = nextTranscript;
          setTranscript(nextTranscript);
        })
        .catch(err => {
          // Promessa antiga: sessão ou geração substituídas não reativam um erro tardio.
          if (!mountedRef.current) return;
          if (sessionEpochRef.current !== sessionEpoch || generationRef.current !== generation) return;
          setError(describeError(err));
        });
    }
  }, [api]);

  useEffect(() => {
    sessionEpochRef.current += 1;
    generationRef.current = 0;
    requestEpochRef.current += 1;
    consumedGenerationRef.current = -1;
    transcriptRef.current = null;
    stateRef.current = null;
    setState(null);
    setTranscript(null);
    setError(null);
  }, [sessionId]);

  // Subscrição primeiro: eventos que cheguem antes do GET inicial já valem.
  useEffect(() => {
    if (!supported || !api) return;
    return api.onEvent((event) => {
      if (event.type !== "voice.input.state") return;
      const payload = event.payload as unknown as VoiceInputStateEventDto;
      if (sessionId && payload.session_id !== sessionId) return;
      commit({
        available: payload.available, state: payload.state,
        generation: payload.generation, message: payload.message,
      });
    });
  }, [api, commit, sessionId, supported]);

  useEffect(() => {
    if (!supported) return;
    let alive = true;
    const epoch = requestEpochRef.current;
    api!.sendCommand<VoiceInputStateDto>("voice.input.get", {})
      .then(result => {
        if (!alive || !mountedRef.current) return;
        if (requestEpochRef.current !== epoch) return; // chegou evento entretanto: o evento manda
        commit(result);
      })
      .catch(err => { if (alive && mountedRef.current) setError(describeError(err)); });
    return () => { alive = false; };
  }, [api, commit, sessionId, supported]);

  const applyAck = useCallback((ack: VoiceInputAckDto) => {
    if (ack.generation < generationRef.current) return;
    commit({
      available: stateRef.current?.available ?? true,
      state: ack.state,
      generation: ack.generation,
      message: ack.accepted ? "" : (REASON_TEXT[ack.reason] ?? "O pedido de ditado não foi aceite."),
    });
    if (ack.accepted && ack.state === "idle") {
      // cancel aplicado: o resultado por consumir deixa de existir no backend.
      consumedGenerationRef.current = -1;
    }
  }, [commit]);

  const busyRef = useRef(false);

  const sendAckCommand = useCallback(
    async (
      command: "voice.input.start" | "voice.input.cancel" | "voice.input.finish",
      payload: Record<string, unknown>,
    ): Promise<VoiceInputAckDto | null> => {
      if (!supported || busyRef.current) return null;
      busyRef.current = true;
      setBusy(true);
      const epoch = requestEpochRef.current;
      const sessionEpoch = sessionEpochRef.current;
      try {
        const result = await api!.sendCommand<VoiceInputAckDto>(command, payload);
        if (!mountedRef.current || sessionEpochRef.current !== sessionEpoch) return null;
        if (result.generation < generationRef.current) return null; // substituída/cancelada: sem token
        // ACK tardio da MESMA geração depois de um evento aplicado não regride a UI (a epoch de
        // pedido bloqueia a mutação), mas continua a ser identidade válida da geração atual — o
        // controlador precisa da geração real. Um ACK que avança geração é o estado canónico e
        // aplica sempre (sem ranking ad-hoc de estados).
        const lateSameGeneration = result.generation === generationRef.current && requestEpochRef.current !== epoch;
        if (!lateSameGeneration) applyAck(result);
        return result;
      } catch (err) {
        if (mountedRef.current && sessionEpochRef.current === sessionEpoch && requestEpochRef.current === epoch) {
          setError(describeError(err));
        }
        return null;
      } finally {
        busyRef.current = false;
        if (mountedRef.current) setBusy(false);
      }
    }, [api, applyAck, supported]);

  const stateName = state?.state ?? "idle";
  const isActive = stateName === "starting" || stateName === "listening" || stateName === "transcribing";

  return {
    state,
    supported,
    busy,
    error,
    transcript,
    isActive,
    start: (mode?: "manual" | "hold") => sendAckCommand("voice.input.start", mode === "hold" ? { mode: "hold" } : {}),
    finish: (generation: number) => sendAckCommand("voice.input.finish", { generation }),
    cancel: () => sendAckCommand("voice.input.cancel", {}),
    dismissTranscript: () => {
      transcriptRef.current = null;
      setTranscript(null);
    },
    demoteTranscript: () => {
      if (!transcriptRef.current) return;
      const demoted = { ...transcriptRef.current, autoSend: false };
      transcriptRef.current = demoted;
      setTranscript(demoted);
    },
  };
}

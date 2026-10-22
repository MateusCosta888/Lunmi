/**
 * Hook de saída de voz (`desktop/src/features/voice/useVoice.ts`, VOICE-V1-OUTPUT).
 *
 * IPC direta pela ponte real (`getBridgeApi`), conforme contrato congelado
 * `.agents/VOICE-V1-OUTPUT-CONTRACT.md`: comandos `voice.get/set/test/speak_last/stop` com
 * payloads exatos — o renderer NUNCA envia texto, paths ou executáveis. O evento `voice.state`
 * é subscrito ANTES do `voice.get` inicial; ambos passam pela mesma guarda de `generation`
 * (resposta tardia com geração menor não sobrescreve estado mais novo) e pela guarda de
 * `session_id`. Desativar limpa o estado pelo DTO de resposta e não dispara fala automática.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { describeError, getBridgeApi } from "../../shared/ipc/client";
import type { VoiceCommandAckDto, VoiceStateDto, VoiceStateEventDto } from "../../shared/types";

const REASON_TEXT: Record<string, string> = {
  disabled: "Ative a voz para ouvir.",
  unavailable: "Voz indisponível: runtime ou modelos de voz não instalados.",
  no_last_response: "Ainda não há resposta elegível para ouvir.",
};

export interface VoiceApi {
  voice: VoiceStateDto | null;
  supported: boolean;
  busy: boolean;
  error: string | null;
  setEnabled: (next: boolean) => void;
  setAutoSpeak: (next: boolean) => void;
  test: () => void;
  speakLast: () => void;
  stopVoice: () => void;
}

export function useVoice(sessionId: string | undefined): VoiceApi {
  const api = getBridgeApi();
  // Pontes parciais de teste (só `onEvent`) não oferecem comandos: voz indisponível, sem crash.
  const supported = typeof api?.sendCommand === "function";
  const [voice, setVoice] = useState<VoiceStateDto | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generationRef = useRef(0);
  // Epoch local: qualquer evento aplicado invalida um GET inicial ainda em voo, MESMO com a
  // mesma generation (snapshot `ready` gen5 não pode apagar um `playing` gen5 já recebido).
  const requestEpochRef = useRef(0);
  // Epoch de sessão: troca de sessão repõe a contagem (a sessão nova recomeça em generation 0)
  // e invalida SET/ACK/GET pendentes da sessão anterior, que não podem reintroduzir estado velho.
  const sessionEpochRef = useRef(0);
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    sessionEpochRef.current += 1;
    generationRef.current = 0;
    requestEpochRef.current += 1;
    setVoice(null);
    setError(null);
  }, [sessionId]);

  const applyEvent = useCallback((next: VoiceStateDto) => {
    // Geração só cresce; um DTO mais velho (evento atrasado ou GET tardio) nunca vence.
    if (next.generation < generationRef.current) return;
    generationRef.current = next.generation;
    requestEpochRef.current += 1;
    setVoice(next);
    if (next.state !== "error") setError(null);
  }, []);

  // Subscrição primeiro: eventos que cheguem antes do GET inicial já valem.
  useEffect(() => {
    if (!api || typeof api.onEvent !== "function") return;
    return api.onEvent((event) => {
      if (event.type !== "voice.state") return;
      const payload = event.payload as unknown as VoiceStateEventDto;
      if (sessionId && payload.session_id !== sessionId) return;
      applyEvent({
        enabled: payload.enabled, auto_speak: payload.auto_speak, available: payload.available,
        state: payload.state, generation: payload.generation, can_speak_last: payload.can_speak_last,
        capture_supported: false, message: payload.message,
      });
    });
  }, [api, applyEvent, sessionId]);

  useEffect(() => {
    if (!supported) return;
    let alive = true;
    const epoch = requestEpochRef.current;
    api.sendCommand<VoiceStateDto>("voice.get", {})
      .then(result => {
        if (!alive || !mountedRef.current) return;
        // GET tardio: se um evento chegou depois de emitido o pedido, o evento manda.
        if (requestEpochRef.current !== epoch) return;
        applyEvent(result);
      })
      .catch(err => { if (alive && mountedRef.current) setError(describeError(err)); });
    return () => { alive = false; };
  }, [applyEvent, sessionId, supported]);

  const applyAck = useCallback((ack: VoiceCommandAckDto) => {
    if (ack.generation < generationRef.current) return;
    generationRef.current = ack.generation;
    requestEpochRef.current += 1;
    // ACK não traz DTO completo: can_speak_last, auto_speak e flags do último evento ficam.
    setVoice(prev => prev ? {
      ...prev,
      generation: ack.generation,
      state: ack.state,
      message: ack.accepted ? prev.message : (REASON_TEXT[ack.reason] ?? prev.message),
    } : prev);
  }, []);

  const run = useCallback(async (action: () => Promise<unknown>) => {
    if (!supported || busy) return;
    setBusy(true);
    try {
      await action();
    } catch (err) {
      if (mountedRef.current) setError(describeError(err));
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  }, [busy, supported]);

  const sendSet = (payload: { enabled?: boolean; auto_speak?: boolean }) => run(async () => {
    const epoch = sessionEpochRef.current;
    const result = await api!.sendCommand<VoiceStateDto>("voice.set", payload);
    if (mountedRef.current && sessionEpochRef.current === epoch) applyEvent(result);
  });

  const sendAckCommand = (command: "voice.test" | "voice.speak_last" | "voice.stop") => run(async () => {
    const epoch = sessionEpochRef.current;
    const result = await api!.sendCommand<VoiceCommandAckDto>(command, {});
    if (mountedRef.current && sessionEpochRef.current === epoch) applyAck(result);
  });

  return {
    voice,
    supported,
    busy,
    error,
    setEnabled: (next) => void sendSet({ enabled: next }),
    setAutoSpeak: (next) => void sendSet({ auto_speak: next }),
    test: () => sendAckCommand("voice.test"),
    speakLast: () => sendAckCommand("voice.speak_last"),
    stopVoice: () => sendAckCommand("voice.stop"),
  };
}

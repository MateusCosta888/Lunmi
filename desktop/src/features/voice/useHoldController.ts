/**
 * Controlador de hold (`desktop/src/features/voice/useHoldController.ts`, V2 §3).
 *
 * Montado UMA vez no App — funciona com qualquer vista aberta. O main publica fases
 * sanitizadas (`VoiceInputHoldPhaseDto`, sem teclas/texto/geração); este módulo é o único
 * lugar do renderer que liga o gesto físico ao backend. O gesto é uma identidade única
 * (`gestureRef`) criada já no `pressed`, ANTES do ACK de `voice.input.start {"mode":"hold"}`
 * chegar: um `released` pré-ACK fica memorizado e finaliza (`finish {generation}`) assim que a
 * geração é conhecida — uma só vez; um `cancelled` pré-ACK marca o gesto e um ACK tardio já não
 * o reanima. Repetições e press_id antigo não abrem turnos novos. O subscritor vive uma vez e
 * lê `apiRef.current` no momento do evento, por isso mudança de capability/sessão usa a API
 * atual sem nova subscrição por render; a identidade está vinculada à sessão. O envio automático
 * acontece APENAS com `auto_send:true` + gesto `released` válido da MESMA geração, UMA vez, pela
 * `chat.send` existente no modo reportado; os efeitos pós-promise (dismiss/demote/aviso) são
 * validados contra a transcrição ainda corrente — um envio antigo nunca limpa nem despromove o
 * resultado de um turno novo. Recusa de envio: sem retry nem fila; texto fica para ação manual.
 * Keydown/prefixo, repeat, silêncio, cancelamento, timeout/teto, troca de sessão, evento antigo
 * ou `auto_send` ausente/false nunca enviam.
 */
import { useEffect, useRef, useState } from "react";
import { getBridgeApi } from "../../shared/ipc/client";
import type { VoiceInputApi } from "./useVoiceInput";
import type { ChatMode, VoiceInputHoldPhaseDto } from "../../shared/types";

/** Push-to-talk from the composer button: same gesture, finish and auto-send as the shortcut. */
export interface HoldButton {
  press: () => void;
  release: () => void;
  cancel: () => void;
}

export interface HoldControllerApi {
  /** `false` quando a ponte não expõe `voiceInputShortcut.onHold` — nada de hold inventado. */
  holdAvailable: boolean;
  notice: string;
  button: HoldButton;
}

export interface HoldControllerDeps {
  api: VoiceInputApi;
  sessionId: string | undefined;
  send: (mode: ChatMode, text: string) => Promise<{ accepted: boolean }>;
  getMode: () => ChatMode;
}

interface Gesture {
  pressId: number;
  sessionId: string;
  generation: number | null; // conhecida quando o ACK de start chega aceite
  released: boolean;
  cancelled: boolean;
  finished: boolean;
}

export function useHoldController({ api, sessionId, send, getMode }: HoldControllerDeps): HoldControllerApi {
  const shortcut = getBridgeApi()?.voiceInputShortcut as
    | { onHold?: (listener: (phase: VoiceInputHoldPhaseDto) => void) => () => void }
    | undefined;
  const holdAvailable = typeof shortcut?.onHold === "function";
  const [notice, setNotice] = useState("");

  // Referências atuais: o subscritor vive uma vez e nunca fecha sobre uma API antiga.
  const apiRef = useRef(api);
  apiRef.current = api;
  const sendRef = useRef(send);
  sendRef.current = send;
  const getModeRef = useRef(getMode);
  getModeRef.current = getMode;
  const sessionIdRef = useRef(sessionId ?? "");
  const transcriptRef = useRef(api.transcript);
  transcriptRef.current = api.transcript;

  const gestureRef = useRef<Gesture | null>(null);
  const tokenRef = useRef<{ pressId: number; generation: number; sessionId: string } | null>(null);
  const lastPressIdRef = useRef(0);
  const sentGenerationRef = useRef(-1);

  // Troca de sessão: gesto pendente, token e aviso morrem com a sessão anterior.
  useEffect(() => {
    sessionIdRef.current = sessionId ?? "";
    gestureRef.current = null;
    tokenRef.current = null;
    sentGenerationRef.current = -1;
    setNotice("");
  }, [sessionId]);

  const finishOnce = (gesture: Gesture) => {
    if (gesture.finished || gesture.cancelled || gesture.generation === null) return;
    if (gesture.sessionId !== sessionIdRef.current) return;
    gesture.finished = true;
    // O gesto concluído liberta a sua slot: um próximo `pressed` pode começar turno novo;
    // a autorização de envio passa a viver no token da geração finalizada.
    const generation = gesture.generation;
    tokenRef.current = { pressId: gesture.pressId, generation, sessionId: gesture.sessionId };
    if (gestureRef.current === gesture) gestureRef.current = null;
    void apiRef.current.finish(generation).then(ack => {
      // `not_active`: the turn already ended on its own (cap/no speech). Anything else left the
      // microphone open with nothing to close it, so it is cancelled and said, never left running.
      if (ack?.accepted || ack?.reason === "not_active") return;
      if (tokenRef.current?.generation === generation) tokenRef.current = null;
      setNotice("O ditado não terminou como esperado; tenta outra vez.");
      void apiRef.current.cancel();
    });
  };

  // One gesture for both sources: the global shortcut (main's press ids, > 0) and the
  // composer's push-to-talk button (local ids, < 0, so the two never collide).
  const press = (pressId: number) => {
    if (gestureRef.current) return; // grito repetido/prefixo: uma só captura
    const gesture: Gesture = {
      pressId, sessionId: sessionIdRef.current,
      generation: null, released: false, cancelled: false, finished: false,
    };
    gestureRef.current = gesture;
    setNotice("");
    void apiRef.current.start("hold").then(ack => {
      // Validar identidade/época/sessão ANTES de aceitar o ACK (gate Root 2 e 4).
      if (gestureRef.current !== gesture) return; // cancelado, substituído ou nova sessão
      if (gesture.cancelled || gesture.sessionId !== sessionIdRef.current) { gestureRef.current = null; return; }
      if (!ack || !ack.accepted) { gestureRef.current = null; return; }
      gesture.generation = ack.generation;
      if (gesture.released) finishOnce(gesture); // release chegou antes do ACK: finalizar agora
    });
  };

  const release = (pressId: number) => {
    const gesture = gestureRef.current;
    if (!gesture || gesture.pressId !== pressId) return;
    gesture.released = true;
    if (gesture.generation !== null) finishOnce(gesture);
    else if (gesture.cancelled) gestureRef.current = null;
  };

  // cancelled: nunca é release. Invalida o gesto E a autorização pendente, mesmo antes do ACK.
  const cancelGesture = (pressId: number): boolean => {
    const gesture = gestureRef.current;
    if (!gesture || gesture.pressId !== pressId) return false;
    gesture.cancelled = true;
    gesture.generation = null;
    gestureRef.current = null;
    tokenRef.current = null;
    return true;
  };

  useEffect(() => {
    if (!shortcut?.onHold) return;
    return shortcut.onHold((phase: VoiceInputHoldPhaseDto) => {
      if (phase.session_id && phase.session_id !== sessionIdRef.current) return;
      if (phase.phase === "pressed") {
        if (phase.press_id <= lastPressIdRef.current) return; // press_id antigo não reabre turno
        if (gestureRef.current) return;
        lastPressIdRef.current = phase.press_id;
        press(phase.press_id);
        return;
      }
      lastPressIdRef.current = Math.max(lastPressIdRef.current, phase.press_id);
      // The main cancels the capture itself for a shortcut gesture; only the gesture dies here.
      if (phase.phase === "released") release(phase.press_id);
      else cancelGesture(phase.press_id);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shortcut?.onHold]);

  const buttonPressRef = useRef(0);
  const button: HoldButton = {
    press: () => {
      if (gestureRef.current) return;
      buttonPressRef.current -= 1;
      press(buttonPressRef.current);
    },
    release: () => release(buttonPressRef.current),
    cancel: () => {
      // Unlike the shortcut, nobody else closes the microphone for the button.
      if (cancelGesture(buttonPressRef.current)) void apiRef.current.cancel();
    },
  };

  const transcript = api.transcript;
  useEffect(() => {
    if (!transcript || !transcript.autoSend) return;
    const token = tokenRef.current;
    const tokenValid = Boolean(token)
      && token!.generation === transcript.generation
      && token!.sessionId === sessionIdRef.current
      && sentGenerationRef.current !== transcript.generation;
    if (!tokenValid) {
      // Sem token de release válido para esta geração: nunca enviar; revisão manual, com guarda
      // de identidade — um resultado já substituído não despromove um turno novo.
      if (transcriptRef.current === transcript) apiRef.current.demoteTranscript();
      return;
    }
    sentGenerationRef.current = transcript.generation;
    tokenRef.current = null;
    const sentGeneration = transcript.generation;
    void (async () => {
      let accepted = false;
      try {
        const outcome = await sendRef.current(getModeRef.current(), transcript.text);
        accepted = outcome.accepted;
      } catch {
        accepted = false;
      }
      // Efeitos pós-promise só valem se A TRANSCRIÇÃO CORRENTE for ainda a deste envio: um
      // envio antigo nunca limpa nem despromove o resultado de um turno novo (gate Root 6).
      const current = transcriptRef.current;
      if (current === transcript || current?.generation === sentGeneration) {
        if (accepted) {
          apiRef.current.dismissTranscript();
          setNotice("");
        } else {
          apiRef.current.demoteTranscript();
          setNotice("O chat não aceitou o envio automático; a transcrição ficou disponível para ação manual.");
        }
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transcript]);

  return { holdAvailable, notice, button };
}

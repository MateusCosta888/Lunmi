/**
 * Warmup de voz (`desktop/src/features/voice/useVoiceWarmup.ts`, VOICE REFINEMENTS V2 §2).
 *
 * O loading só abre a aplicação com `voice.warmup`/`voice.warmup.get` a devolver
 * `ready=true` — quando ambas as pernas (TTS e STT) chegaram a estado terminal (`ready`,
 * `unavailable` ou `error`) — ou com o timeout publicado. `voice.warmup` é idempotente e não
 * abre microfone nem produz áudio; `voice.warmup.get` é leitura pura. Subscreve-se
 * `voice.readiness` ANTES de iniciar, e um `settled` posterior não regride. Sem ponte ou com
 * backend antigo (comando desconhecido) a aplicação abre em shell degradado com mensagem real:
 * nada fica bloqueado para sempre. O timeout local é uma retaguarda visível — o backend já
 * termina cada perna aos 30 s (`VOICE_WARMUP_TIMEOUT_MS`), publicado no contrato.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { getBridgeApi } from "../../shared/ipc/client";

/** 2 pernas × 30 s + margem de 5 s; o estado terminal do backend costuma chegar antes. */
export const WARMUP_LOCAL_GUARD_MS = 65_000;

export interface VoiceWarmupApi {
  warmup: { state: "idle" | "preparing" | "ready"; ready: boolean; tts: string; stt: string; message: string } | null;
  settled: boolean;
  settledMessage: string;
}

/** DTO válido do warmup (§2.1): sem `state`/`ready` reconhecíveis não há preparação conhecida. */
const isWarmupDto = (value: unknown): value is NonNullable<VoiceWarmupApi["warmup"]> => {
  const dto = value as { state?: unknown; ready?: unknown } | null;
  return Boolean(dto) && typeof dto!.state === "string" && typeof dto!.ready === "boolean";
};

export function useVoiceWarmup(sessionId: string | undefined): VoiceWarmupApi {
  const api = getBridgeApi();
  const supported = typeof api?.sendCommand === "function" && typeof api?.onEvent === "function";
  const [warmup, setWarmup] = useState<VoiceWarmupApi["warmup"]>(null);
  const [settled, setSettled] = useState(false);
  const [settledMessage, setSettledMessage] = useState("");
  const settledRef = useRef(false);
  const sessionIdRef = useRef(sessionId ?? "");
  sessionIdRef.current = sessionId ?? "";

  const degradeOpen = useCallback((message: string) => {
    if (settledRef.current) return;
    settledRef.current = true;
    setSettled(true);
    setSettledMessage(message);
  }, []);

  const apply = useCallback((next: unknown) => {
    if (!isWarmupDto(next)) {
      // Ponte antiga ou backend sem warmup: degradação honesta — nada de abrir com `ready` fingido.
      degradeOpen("Preparação de voz indisponível neste backend.");
      return;
    }
    if (next.ready && settledRef.current) return; // terminal não regride
    setWarmup(prev => (prev && prev.ready && !next.ready ? prev : next));
    if (next.ready && !settledRef.current) {
      settledRef.current = true;
      setSettled(true);
      setSettledMessage(next.message ?? "");
    }
  }, [degradeOpen]);

  useEffect(() => {
    if (!supported || !api) {
      // Sem ponte: abrir com verdade; o resto da aplicação trata a indisponibilidade.
      degradeOpen("Ponte indisponível: voz não preparada.");
      return;
    }
    settledRef.current = false;
    setSettled(false);
    setSettledMessage("");
    setWarmup(null);
    let alive = true;
    const unsubscribe = api.onEvent((event) => {
      if (!alive || event.type !== "voice.readiness") return;
      const payload = event.payload as { session_id?: string } & NonNullable<VoiceWarmupApi["warmup"]>;
      if (sessionIdRef.current && payload.session_id && payload.session_id !== sessionIdRef.current) return;
      apply(payload);
    });
    api.sendCommand("voice.warmup", {})
      .then(result => { if (alive) apply(result); })
      .catch(() => {
        // Backend antigo sem warmup: degradação honesta, não bloqueio.
        if (alive) degradeOpen("Preparação de voz indisponível neste backend.");
      });
    const guard = window.setTimeout(() => {
      if (!alive) return;
      degradeOpen("Tempo limite ao preparar a voz. A aplicação abre sem voz preparada.");
    }, WARMUP_LOCAL_GUARD_MS);
    return () => { alive = false; window.clearTimeout(guard); unsubscribe(); };
  }, [api, apply, degradeOpen, sessionId, supported]);

  return { warmup, settled, settledMessage };
}

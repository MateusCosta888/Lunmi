/**
 * Derivação do modo visual da orb (`desktop/src/features/chat/orb/orbState.ts`, ORB REACTIVE V1).
 *
 * Só estado real da aplicação, sem áudio medido nem estados inventados:
 * - `listening` apenas quando o microfone está mesmo aberto (input `listening` no chat;
 *   `voice_phase === "listening"` no Node) — tem prioridade;
 * - `speaking` só com TTS `playing`;
 * - `thinking` para starting/transcribing do input, loading/synthesizing do TTS, tarefa em
 *   trabalho/voz `thinking` do Node;
 * - o resto é `idle`. `voice_phase === "error"` não é um estado de orb: volta a `idle` (o
 *   erro já é mostrado pelas vias próprias do Node/chat).
 */
export type OrbMode = "idle" | "listening" | "thinking" | "speaking";

export interface OrbSignals {
  /** `VoiceInputStateDto.state` (chat) ou `voice_phase` equivalente (Node). */
  inputState?: string | null;
  voicePhase?: "starting" | "listening" | "thinking" | "error" | null;
  /** `VoiceStateName` do TTS. */
  ttsState?: string | null;
  /** `chat.busy`/fase de tarefa em trabalho. */
  taskWorking?: boolean;
}

export function deriveOrbMode(signals: OrbSignals): OrbMode {
  const input = signals.inputState ?? null;
  const voice = signals.voicePhase ?? null;
  const tts = signals.ttsState ?? null;
  if (input === "listening" || voice === "listening") return "listening";
  if (tts === "playing") return "speaking";
  if (
    input === "starting" || input === "transcribing" ||
    voice === "starting" || voice === "thinking" ||
    tts === "loading" || tts === "synthesizing" ||
    signals.taskWorking
  ) return "thinking";
  return "idle";
}

import type { NodeStateDto, NodeStatus, VoiceInputHoldPhaseDto } from "../src/shared/types";

type Phase = NonNullable<NodeStateDto["voice_phase"]>;

/** Só estes terminais mostram a resposta existente no mini chat; cancelamento limpa o turno. */
const COMPLETION_STATUSES = new Set(["SUCCESS", "FAILED", "NOT_VERIFIED"]);

/**
 * O campo público de voz é apresentação: pendências de confirmação/resposta continuam a ser
 * `attention` e nunca podem ficar escondidas atrás de «A pensar». `show_activity=false` não
 * entra aqui — a atividade explícita do microfone continua visível.
 */
export function nodeVoicePhase(
  phase: Phase | null,
  status: NodeStatus | null
): NodeStateDto["voice_phase"] {
  if (!phase) return null;
  if (status === "needs_confirm" || status === "needs_input") return null;
  return phase;
}

type Turn = {
  session: string; press: number; minimumGeneration: number; generation: number | null;
  released: boolean; phase: Phase; ready: boolean; text: string | null;
  claimed: boolean; task: string | null; accepted: boolean; terminal: boolean; dismissed: boolean;
};

/** Presentation only: observes the existing sender; never starts capture or dispatches a task. */
export class BackgroundVoiceTurn {
  private turn: Turn | null = null;
  private origin: { session: string; press: number; background: boolean } | null = null;
  private inputGeneration = 0;

  get phase(): Phase | null { return this.turn?.dismissed ? null : this.turn?.phase ?? null; }
  capture(): Turn | null { return this.turn; }

  hold(event: VoiceInputHoldPhaseDto, background: boolean): void {
    if (event.phase === "pressed") {
      this.origin = { session: event.session_id, press: event.press_id, background };
      this.turn = background ? {
        session: event.session_id, press: event.press_id, minimumGeneration: this.inputGeneration,
        generation: null, released: false, phase: "starting", ready: false, text: null,
        claimed: false, task: null, accepted: false, terminal: false, dismissed: false,
      } : null;
      return;
    }
    const turn = this.turn;
    if (!turn || turn.session !== event.session_id || turn.press !== event.press_id) return;
    if (event.phase === "cancelled") { this.turn = null; return; }
    turn.released = true;
    if (turn.phase !== "error") turn.phase = "thinking";
  }

  backgroundRelease(event: VoiceInputHoldPhaseDto): boolean {
    return this.origin?.background === true && this.origin.session === event.session_id &&
      this.origin.press === event.press_id;
  }

  input(payload: Record<string, unknown>): void {
    const generation = payload.generation;
    if (!Number.isSafeInteger(generation) || (generation as number) < 0) return;
    const turn = this.turn;
    if (turn && payload.session_id !== turn.session) return;
    this.inputGeneration = Math.max(this.inputGeneration, generation as number);
    if (!turn || turn.phase === "error") return;
    if (turn.generation !== null && (generation as number) > turn.generation) { this.fail(turn); return; }
    if (turn.generation === null && (generation as number) > turn.minimumGeneration) {
      turn.generation = generation as number;
    }
    if (turn.generation !== generation) return;
    switch (payload.state) {
      case "starting": if (!turn.released) turn.phase = "starting"; break;
      case "listening": if (!turn.released) turn.phase = "listening"; break;
      case "transcribing": turn.phase = "thinking"; break;
      case "ready": turn.ready = true; turn.phase = "thinking"; break;
      case "idle": if (!turn.ready && !turn.claimed) this.fail(turn); break;
      case "error": case "unavailable": this.fail(turn); break;
    }
  }

  begin(type: string, payload: Record<string, unknown>, fromMain: boolean): Turn | null {
    if (type === "voice.input.cancel") { this.turn = null; return null; }
    const turn = this.turn;
    if (!turn || turn.phase === "error") return null;
    if (type === "voice.input.start") {
      if (payload.mode !== "hold") { this.dismiss(); return null; }
      return fromMain ? turn : null;
    }
    if (type === "voice.input.result" || type === "voice.input.finish") {
      return fromMain && payload.generation === turn.generation ? turn : null;
    }
    if (type === "chat.send" && fromMain && turn.released && !turn.claimed && turn.text !== null &&
        payload.text === turn.text) {
      turn.claimed = true;
      turn.text = null;
      return turn;
    }
    return null;
  }

  end(type: string, result: Record<string, unknown>, ticket: Turn | null): void {
    if (!ticket || ticket !== this.turn || ticket.phase === "error") return;
    if (type === "voice.input.start") {
      const gen = result.generation;
      if (result.accepted !== true || !Number.isSafeInteger(gen) || (gen as number) <= ticket.minimumGeneration ||
          (ticket.generation !== null && ticket.generation !== gen)) { this.fail(ticket); return; }
      ticket.generation = gen as number;
    } else if (type === "voice.input.finish") {
      if (result.accepted !== true) this.fail(ticket);
    } else if (type === "voice.input.result") {
      if (result.status !== "ready" || result.generation !== ticket.generation || result.auto_send !== true ||
          result.truncated === true || !ticket.released || typeof result.text !== "string" || !result.text.trim()) {
        this.fail(ticket); return;
      }
      ticket.text = result.text.trim();
    } else if (type === "chat.send") {
      if (typeof result.task_id !== "string" || (ticket.task !== null && ticket.task !== result.task_id)) {
        this.fail(ticket); return;
      }
      ticket.task = result.task_id;
      ticket.accepted = true;
    }
  }

  failed(ticket: Turn | null): void { if (ticket && ticket === this.turn) this.fail(ticket); }

  taskStarted(taskId: unknown): void {
    const turn = this.turn;
    if (turn?.claimed && turn.phase !== "error" && turn.task === null && typeof taskId === "string") turn.task = taskId;
  }

  finished(taskId: unknown, status: unknown): void {
    const turn = this.turn;
    if (!turn?.claimed || turn.phase === "error" || turn.task !== taskId) return;
    const normalized = typeof status === "string" ? status.trim().toUpperCase() : "";
    // Cancelamento/terminal sem resultado canónico não abre conversa nenhuma: limpa o turno.
    if (!COMPLETION_STATUSES.has(normalized)) {
      this.turn = null;
      return;
    }
    turn.terminal = true;
  }

  /** `task.cancelled` é terminal para este turno: não espera pelo `task.finished` seguinte. */
  cancelledTask(taskId: unknown): void {
    const turn = this.turn;
    if (!turn?.claimed || turn.phase === "error") return;
    if (turn.task === taskId || (turn.task === null && typeof taskId === "string")) this.turn = null;
  }

  takeCompleted(): boolean {
    const turn = this.turn;
    if (!turn?.accepted || !turn.terminal || turn.phase === "error") return false;
    this.turn = null;
    return !turn.dismissed;
  }

  dismiss(): void { if (this.turn) { this.turn.dismissed = true; this.turn.text = null; } }
  reset(): void { this.turn = null; this.origin = null; this.inputGeneration = 0; }
  private fail(turn: Turn): void { turn.phase = "error"; turn.text = null; turn.task = null; }
}

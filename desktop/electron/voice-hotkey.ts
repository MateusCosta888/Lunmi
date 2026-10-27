/**
 * VOICE REFINEMENTS V2 — helper Windows e fases hold (`desktop/electron/voice-hotkey.ts`).
 *
 * O Electron é o único dono do processo `app/voice/hotkey_worker.py`. Este módulo:
 * - arranca o helper com o Python já resolvido, stdio JSONL e ambiente mínimo (sem chaves);
 * - valida a sequência `pressed → released` e nunca transforma `cancelled`/`error` em release;
 * - aplica `configure`/`pause`/`resume` sem sintetizar teclas e sem polling em repouso;
 * - expõe um coordenador puro que publica `VoiceInputHoldPhaseDto` ao renderer único e cancela a
 *   captura no backend em lifecycle/hook loss, sem chamar `start`, `finish` ou `chat.send`.
 *
 * Nenhuma linha do helper carrega VK, scan code, texto, caminho, sessão ou geração. O renderer
 * decide `start`/`finish`/`chat.send`; o main só publica fases e cancela.
 */
import { spawn } from "child_process";
import * as path from "path";
import type {
  VoiceInputHoldPhase,
  VoiceInputHoldPhaseDto,
} from "../src/shared/types";
import { canonicalizeVoiceShortcut } from "./voice-shortcut";

export const HELPER_READY_TIMEOUT_MS = 2500;
export const HELPER_MAX_LINE_BYTES = 4096;
export const HELPER_MAX_BUFFER_BYTES = 8192;

const CANCELLED_REASONS = new Set<VoiceInputHoldPhaseDto["reason"]>([
  "extra_key",
  "injected",
  "pause",
  "reconfigured",
  "hook_lost",
  "blur",
  "shutdown",
  "lifecycle",
]);

/** Allowlist de ambiente do helper: nada de `LLM_*`, chaves, `.env` ou credenciais. */
const HELPER_ENV_ALLOWLIST = [
  "SystemRoot",
  "SYSTEMROOT",
  "PATH",
  "PATHEXT",
  "TEMP",
  "TMP",
  "WINDIR",
  "COMSPEC",
  "USERPROFILE",
  "LOCALAPPDATA",
  "APPDATA",
] as const;

export function helperEnvironment(base: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const key of HELPER_ENV_ALLOWLIST) {
    const value = base[key];
    if (value) {
      env[key] = value;
    }
  }
  env.PYTHONIOENCODING = "utf-8";
  env.PYTHONUNBUFFERED = "1";
  env.PYTHONDONTWRITEBYTECODE = "1";
  env.PYTHONUTF8 = "1";
  return env;
}

export interface VoiceHotkeyChild {
  stdin: {
    write(chunk: string): boolean;
    end(): void;
    on(event: "error", listener: (error: Error) => void): unknown;
  };
  stdout: {
    on(event: "data", listener: (chunk: Buffer) => void): unknown;
    on(event: "end" | "close", listener: () => void): unknown;
    on(event: "error", listener: (error: Error) => void): unknown;
  };
  stderr: {
    on(event: "data", listener: (chunk: Buffer) => void): unknown;
    on(event: "error", listener: (error: Error) => void): unknown;
  };
  on(event: "error", listener: (error: Error) => void): unknown;
  on(
    event: "exit" | "close",
    listener: (code: number | null, signal: string | null) => void
  ): unknown;
  kill(signal?: string): boolean;
  pid?: number;
  exitCode?: number | null;
}

export type VoiceHotkeySpawn = (
  command: string,
  args: string[],
  options: any
) => VoiceHotkeyChild;

export interface VoiceHotkeyObserverOptions {
  python: string;
  cwd: string;
  spawnImpl?: VoiceHotkeySpawn;
  environment?: NodeJS.ProcessEnv;
  readyTimeoutMs?: number;
  onPhase: (
    phase: VoiceInputHoldPhase,
    reason: VoiceInputHoldPhaseDto["reason"]
  ) => void;
  onFailure: () => void;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Supervisor do filho owned. Uma única instância; nunca faz restart em loop. `arm` só devolve
 * `true` depois de `ready`; falha, EOF, timeout e saída inesperada libertam o handle e resolvem
 * `false`, sem sintetizar `released`.
 */
export class VoiceHotkeyObserver {
  private child: VoiceHotkeyChild | null = null;
  private ready = false;
  private failed = false;
  private paused = false;
  private pressed = false;
  private current: string | null = null;
  private expectedExit = false;
  private disposed = false;
  private buffer = "";
  private readyWaiter: { resolve: (ok: boolean) => void; timer: NodeJS.Timeout } | null = null;
  private readyPromise: Promise<boolean> | null = null;

  private readonly spawnImpl: VoiceHotkeySpawn;
  private readonly readyTimeoutMs: number;

  constructor(private readonly options: VoiceHotkeyObserverOptions) {
    this.spawnImpl = options.spawnImpl ??
      ((command, args, spawnOptions) =>
        spawn(command, args, spawnOptions) as unknown as VoiceHotkeyChild);
    this.readyTimeoutMs = options.readyTimeoutMs ?? HELPER_READY_TIMEOUT_MS;
  }

  isReady(): boolean {
    return this.ready && this.child !== null && !this.failed;
  }

  async arm(accelerator: string, paused: boolean): Promise<boolean> {
    if (this.disposed) {
      return false;
    }
    const canonical = canonicalizeVoiceShortcut(accelerator);
    if (!canonical) {
      return false;
    }
    this.paused = paused;
    if (this.child) {
      if (!this.ready) {
        const ready = this.readyPromise ?? this.startReadyWaiter();
        if (!(await ready)) {
          return false;
        }
      }
      return this.applyCanonical(canonical, paused);
    }
    const ready = this.startReadyWaiter();
    try {
      this.spawnChild(canonical);
    } catch {
      this.fail();
      return false;
    }
    if (!(await ready)) {
      return false;
    }
    if (this.disposed) {
      this.disarm();
      return false;
    }
    if (paused && !this.paused) {
      this.paused = true;
    }
    if (this.paused && !this.write({ type: "pause" })) {
      this.fail();
      return false;
    }
    return true;
  }

  async reconfigure(accelerator: string, paused: boolean): Promise<boolean> {
    return this.arm(accelerator, paused);
  }

  pause(): boolean {
    if (this.paused) {
      return true;
    }
    if (!this.isReady()) {
      return false;
    }
    this.paused = true;
    if (!this.write({ type: "pause" })) {
      this.fail();
      return false;
    }
    if (this.pressed) {
      // O helper também emitirá `cancelled: pause`; este é o caminho determinístico do main.
      this.pressed = false;
      this.emit("cancelled", "pause");
    }
    return true;
  }

  resume(): boolean {
    if (!this.paused) {
      return true;
    }
    if (!this.isReady()) {
      return false;
    }
    if (!this.write({ type: "resume" })) {
      this.fail();
      return false;
    }
    this.paused = false;
    return true;
  }

  disarm(): void {
    const child = this.child;
    this.child = null;
    this.ready = false;
    this.pressed = false;
    this.current = null;
    this.paused = false;
    this.buffer = "";
    this.expectedExit = true;
    this.settleReady(false);
    if (!child) {
      return;
    }
    try {
      child.stdin.write('{"type":"shutdown"}\n');
      child.stdin.end();
    } catch {
      // pipe já fechado
    }
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        // já terminou
      }
    }, 250);
    timer.unref?.();
  }

  dispose(): void {
    this.disposed = true;
    this.disarm();
  }

  private async applyCanonical(accelerator: string, paused: boolean): Promise<boolean> {
    if (!this.isReady()) {
      return false;
    }
    if (this.current !== accelerator) {
      if (!this.write({ type: "configure", accelerator })) {
        this.fail();
        return false;
      }
      this.current = accelerator;
      this.pressed = false;
    }
    if (paused !== this.paused) {
      if (paused) {
        if (!this.pause()) {
          return false;
        }
      } else if (!this.resume()) {
        return false;
      }
    }
    return true;
  }

  private startReadyWaiter(): Promise<boolean> {
    if (this.readyPromise) {
      return this.readyPromise;
    }
    this.readyPromise = new Promise<boolean>((resolve) => {
      const timer = setTimeout(() => {
        this.readyWaiter = null;
        this.fail();
        resolve(false);
      }, this.readyTimeoutMs);
      timer.unref?.();
      this.readyWaiter = { resolve, timer };
    });
    return this.readyPromise;
  }

  private settleReady(ok: boolean): void {
    const waiter = this.readyWaiter;
    this.readyWaiter = null;
    this.readyPromise = null;
    if (!waiter) {
      return;
    }
    clearTimeout(waiter.timer);
    waiter.resolve(ok);
  }

  private spawnChild(accelerator: string): void {
    // Executa o ficheiro por caminho (não `-m app.voice.hotkey_worker`): o helper é stdlib puro
    // e não deve pagar/importar `app.voice.__init__` (manager, psutil, prefs) só para observar
    // teclas. O caminho existe tanto no checkout como em `resources/app` do pacote.
    const helperPath = path.resolve(this.options.cwd, "app", "voice", "hotkey_worker.py");
    const child = this.spawnImpl(
      this.options.python,
      ["-u", helperPath, "--accelerator", accelerator],
      {
        cwd: this.options.cwd,
        env: helperEnvironment(this.options.environment ?? process.env),
        stdio: ["pipe", "pipe", "pipe"],
        shell: false,
        windowsHide: true,
      }
    );
    this.child = child;
    this.ready = false;
    this.failed = false;
    this.current = accelerator;
    this.buffer = "";
    this.expectedExit = false;
    child.stdout.on("data", (chunk: Buffer) => this.onData(child, chunk));
    child.stdout.on("end", () => this.onUnexpectedExit(child));
    child.stdout.on("close", () => this.onUnexpectedExit(child));
    child.stdout.on("error", () => this.onUnexpectedExit(child));
    child.stderr.on("data", () => {
      // stderr é diagnóstico e é descartado: nunca vaza para eventos/logs.
    });
    child.stderr.on("error", () => {
      // stderr não invalida o hook.
    });
    // Um erro tardio do filho anterior não pode derrubar o filho atual.
    child.stdin.on("error", () => {
      if (this.child === child) this.fail();
    });
    child.on("error", () => {
      if (this.child === child) this.fail();
    });
    child.on("exit", () => {
      if (this.child !== child) {
        return;
      }
      if (this.expectedExit) {
        this.child = null;
        this.ready = false;
        return;
      }
      this.fail();
    });
  }

  private onUnexpectedExit(child: VoiceHotkeyChild): void {
    if (this.child !== child || this.expectedExit) {
      return;
    }
    this.fail();
  }

  private onData(child: VoiceHotkeyChild, chunk: Buffer): void {
    if (this.child !== child || this.failed) {
      return;
    }
    this.buffer += chunk.toString("utf8");
    if (this.buffer.length > HELPER_MAX_BUFFER_BYTES && !this.buffer.includes("\n")) {
      this.fail();
      return;
    }
    let index = this.buffer.indexOf("\n");
    while (index >= 0) {
      const line = this.buffer.slice(0, index);
      this.buffer = this.buffer.slice(index + 1);
      if (line.length > HELPER_MAX_LINE_BYTES) {
        this.fail();
        return;
      }
      if (line.trim()) {
        this.handleLine(line);
        if (this.failed) {
          return;
        }
      }
      index = this.buffer.indexOf("\n");
    }
  }

  private handleLine(raw: string): void {
    let value: unknown;
    try {
      value = JSON.parse(raw);
    } catch {
      this.fail();
      return;
    }
    if (!isPlainObject(value)) {
      this.fail();
      return;
    }
    const type = value.type;
    if (type === "ready") {
      if (value.protocol !== 1) {
        this.fail();
        return;
      }
      this.ready = true;
      this.failed = false;
      this.settleReady(true);
      if (this.paused && !this.write({ type: "pause" })) {
        this.fail();
      }
      return;
    }
    if (type === "pressed") {
      if (!this.isReady() || this.paused || this.pressed) {
        return;
      }
      this.pressed = true;
      this.emit("pressed", "");
      return;
    }
    if (type === "released") {
      if (!this.isReady() || this.paused || !this.pressed) {
        return;
      }
      this.pressed = false;
      this.emit("released", "");
      return;
    }
    if (type === "cancelled") {
      const reason = value.reason;
      if (typeof reason !== "string" || !CANCELLED_REASONS.has(reason as VoiceInputHoldPhaseDto["reason"])) {
        this.fail();
        return;
      }
      const hadPress = this.pressed;
      this.pressed = false;
      if (hadPress) {
        this.emit("cancelled", reason as VoiceInputHoldPhaseDto["reason"]);
      }
      return;
    }
    if (type === "error") {
      this.fail();
      return;
    }
    this.fail();
  }

  private emit(phase: VoiceInputHoldPhase, reason: VoiceInputHoldPhaseDto["reason"]): void {
    try {
      this.options.onPhase(phase, reason);
    } catch {
      // Um consumidor com erro não pode derrubar o supervisor.
    }
  }

  private write(message: Record<string, unknown>): boolean {
    const child = this.child;
    if (!child) {
      return false;
    }
    try {
      child.stdin.write(`${JSON.stringify(message)}\n`);
      return true;
    } catch {
      return false;
    }
  }

  private fail(): void {
    if (this.failed) {
      return;
    }
    this.failed = true;
    this.ready = false;
    const child = this.child;
    this.child = null;
    this.current = null;
    this.buffer = "";
    this.paused = false;
    this.expectedExit = true;
    const hadPress = this.pressed;
    this.pressed = false;
    this.settleReady(false);
    if (hadPress) {
      this.emit("cancelled", "hook_lost");
    }
    if (child) {
      try {
        child.kill();
      } catch {
        // já terminou
      }
    }
    try {
      this.options.onFailure();
    } catch {
      // o estado do store é atualizado pelo chamador; nunca derruba o main
    }
  }
}

export interface VoiceHoldCoordinatorOptions {
  getSessionId: () => string | null;
  isQuitting: () => boolean;
  publish: (phase: VoiceInputHoldPhaseDto) => void;
  cancelCapture: () => void | Promise<void>;
  showMain: (phase: VoiceInputHoldPhaseDto) => void;
}

/**
 * Converte fases do helper em DTOs sanitizados e mantém o `press_id` monotónico.
 * O release só existe depois de um pressed válido do mesmo gesto; um release sem resultado é
 * apenas uma fase, nunca um envio. O main não chama `start`/`finish`/`chat.send`.
 */
export class VoiceHoldCoordinator {
  private pressId = 0;
  private active: { pressId: number; sessionId: string } | null = null;

  constructor(private readonly options: VoiceHoldCoordinatorOptions) {}

  isActive(): boolean {
    return this.active !== null;
  }

  onPhase(phase: VoiceInputHoldPhase, reason: VoiceInputHoldPhaseDto["reason"]): void {
    if (phase === "pressed") {
      if (this.options.isQuitting() || this.active) {
        return;
      }
      const sessionId = this.options.getSessionId();
      if (!sessionId) {
        return;
      }
      this.pressId += 1;
      this.active = { pressId: this.pressId, sessionId };
      this.publish("pressed", "");
      return;
    }
    const active = this.active;
    if (!active) {
      return;
    }
    if (phase === "released") {
      this.active = null;
      this.publish("released", "", active);
      if (!this.options.isQuitting()) {
        this.options.showMain({ session_id: active.sessionId, press_id: active.pressId,
          phase: "released", reason: "" });
      }
      return;
    }
    this.active = null;
    this.publish("cancelled", reason, active);
    this.options.cancelCapture();
  }

  /** Lifecycle/config: cancela a fase ativa e a captura, sem sintetizar release nem envio. */
  cancel(reason: VoiceInputHoldPhaseDto["reason"]): void | Promise<void> {
    const active = this.active;
    if (!active) {
      return;
    }
    this.active = null;
    this.publish("cancelled", reason, active);
    return this.options.cancelCapture();
  }

  sessionChanged(): void {
    void this.cancel("lifecycle");
  }

  private publish(
    phase: VoiceInputHoldPhase,
    reason: VoiceInputHoldPhaseDto["reason"],
    active: { pressId: number; sessionId: string } | null = this.active
  ): void {
    if (!active) {
      return;
    }
    try {
      this.options.publish({
        session_id: active.sessionId,
        press_id: active.pressId,
        phase,
        reason,
      });
    } catch {
      // A janela pode estar a fechar; a fase seguinte volta a tentar.
    }
  }
}

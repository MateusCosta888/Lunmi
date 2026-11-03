import { spawn } from "node:child_process";
import { lstatSync } from "node:fs";
import * as path from "node:path";
import type { UpdateState } from "../src/shared/updates";
import { JsonlLineFramer } from "../src/shared/protocol";

const SIX_HOURS = 6 * 60 * 60 * 1000;
const VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-beta\.(0|[1-9]\d*))?$/;
const CLI_ERRORS = new Set(["offline", "source", "signature", "hash", "space", "locked", "extract", "busy", "internal"]);
type UpdateError = NonNullable<UpdateState["error"]>;
type Message = Record<string, unknown>;

class CliError extends Error {
  constructor(readonly code: UpdateError) { super(code); }
}

function newerVersion(candidate: string, installed: string): boolean {
  const a = VERSION.exec(candidate), b = VERSION.exec(installed);
  if (!a || !b || candidate.length > 64) return false;
  for (let i = 1; i <= 3; i++) {
    if (BigInt(a[i]) !== BigInt(b[i])) return BigInt(a[i]) > BigInt(b[i]);
  }
  if (a[4] === undefined || b[4] === undefined) return a[4] === undefined && b[4] !== undefined;
  return BigInt(a[4]) > BigInt(b[4]);
}

/** Development never controls an installed copy; Rust supplies the authoritative launcher. */
export function installedUpdateLauncher(packaged: boolean): string | null {
  if (!packaged || process.platform !== "win32" || process.arch !== "x64") return null;
  const executable = process.env.LUNMI_LAUNCHER || path.resolve(process.execPath, "..", "..", "..", "Lunmi.exe");
  if (!path.isAbsolute(executable) || path.basename(executable).toLowerCase() !== "lunmi.exe") return null;
  try {
    const file = lstatSync(executable);
    return file.isFile() && !file.isSymbolicLink() ? executable : null;
  } catch { return null; }
}

/** Rust owns verification and writes. Electron consumes bounded JSONL, never stderr or paths. */
function runCli(executable: string, args: string[], onMessage: (value: Message) => void,
                spawnCli: typeof spawn, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const framer = new JsonlLineFramer(64 * 1024);
    let settled = false;
    let child: ReturnType<typeof spawn> | undefined;
    let deadline: ReturnType<typeof setTimeout> | undefined;
    const finish = (error?: CliError) => {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      signal.removeEventListener("abort", abort);
      if (error) {
        try { child?.kill(); } catch { /* Already exited. */ }
        reject(error);
      } else resolve();
    };
    const abort = () => finish(new CliError("cancelled"));
    const read = (chunk: Buffer | string) => {
      if (settled) return;
      try {
        for (const line of framer.feed(chunk)) {
          const message: unknown = JSON.parse(line);
          if (!message || typeof message !== "object" || Array.isArray(message)) throw new Error();
          onMessage(message as Message);
        }
      } catch { finish(new CliError("internal")); }
    };
    if (signal.aborted) { abort(); return; }
    try {
      child = spawnCli(executable, args, { cwd: path.dirname(executable), shell: false, windowsHide: true,
        stdio: ["ignore", "pipe", "ignore"] });
      child.once("error", () => finish(new CliError("internal")));
      child.stdout?.on("data", read);
      child.once("close", code => {
        read("\n");
        finish(code === 0 ? undefined : new CliError("internal"));
      });
      signal.addEventListener("abort", abort, { once: true });
      deadline = setTimeout(() => finish(new CliError("offline")), args[0] === "--check" ? 60_000 : 30 * 60_000);
      deadline.unref();
    } catch { finish(new CliError("internal")); }
  });
}

function detached(executable: string, args: string[], spawnCli: typeof spawn): Promise<void> {
  return new Promise((resolve, reject) => {
    try {
      const child = spawnCli(executable, args, { cwd: path.dirname(executable), shell: false,
        windowsHide: true, detached: true, stdio: "ignore" });
      child.once("error", () => reject(new CliError("internal")));
      child.once("spawn", () => { child.unref(); resolve(); });
    } catch { reject(new CliError("internal")); }
  });
}

export class LunmiUpdates {
  private state: UpdateState;
  private flight: Promise<UpdateState> | null = null;
  private installFlight: Promise<UpdateState> | null = null;
  private readonly abort = new AbortController();
  private disposed = false;
  private healthySent = false;
  private startTimer?: ReturnType<typeof setTimeout>;
  private interval?: ReturnType<typeof setInterval>;

  constructor(private readonly options: {
    version: string;
    launcher: string | null;
    publish: (state: UpdateState) => void;
    blocked: () => boolean;
    finish: () => Promise<void>;
    confirmRestart?: () => Promise<boolean>;
    spawn?: typeof spawn;
  }) {
    this.state = { phase: options.launcher && VERSION.test(options.version) ? "idle" : "disabled", currentVersion: options.version };
  }

  get(): UpdateState { return { ...this.state }; }
  get installing(): boolean { return this.state.phase === "installing"; }
  private set(patch: Partial<UpdateState>): UpdateState {
    this.state = { ...this.state, ...patch };
    if (!this.disposed) this.options.publish(this.get());
    return this.get();
  }

  start(): void {
    if (this.state.phase === "disabled" || this.disposed || this.startTimer || this.interval) return;
    this.startTimer = setTimeout(() => { void this.check(); }, 3_000);
    this.interval = setInterval(() => { void this.check(); }, SIX_HOURS);
    this.startTimer.unref(); this.interval.unref();
  }

  dispose(): void {
    this.disposed = true;
    clearTimeout(this.startTimer); clearInterval(this.interval);
    this.abort.abort();
  }

  /** Only after session.ready; failure must never prevent the app from opening. */
  markHealthy(): void {
    if (this.state.phase === "disabled" || this.disposed || this.healthySent) return;
    this.healthySent = true;
    void detached(this.options.launcher!, ["--healthy", this.options.version], this.options.spawn ?? spawn).catch(() => {});
  }

  check(): Promise<UpdateState> {
    if (this.flight) return this.flight;
    if (this.disposed || ["disabled", "available", "downloading", "ready", "installing"].includes(this.state.phase)) return Promise.resolve(this.get());
    this.flight = this.runCheck().finally(() => { this.flight = null; });
    return this.flight;
  }

  private async command(args: string[], progress?: (percent: number) => void): Promise<Message> {
    let terminal: Message | undefined;
    let failure: CliError | undefined;
    try {
      await runCli(this.options.launcher!, args, message => {
        if (terminal || failure) throw new Error();
        if (message.phase === "error") {
          failure = new CliError(typeof message.error === "string" && CLI_ERRORS.has(message.error) ? message.error as UpdateError : "internal");
        } else if ((message.phase === "downloading" || message.phase === "installing") && progress) {
          if (typeof message.percent !== "number" || !Number.isFinite(message.percent) || message.percent < 0 || message.percent > 100) throw new Error();
          progress(Math.round(message.percent));
        } else if ((args[0] === "--check" && (message.phase === "available" || message.phase === "idle")) ||
                   (args[0] === "--stage" && message.phase === "ready")) {
          terminal = message;
        } else throw new Error();
      }, this.options.spawn ?? spawn, this.abort.signal);
    } catch (error) { throw failure ?? error; }
    if (failure) throw failure;
    if (!terminal) throw new CliError("internal");
    return terminal;
  }

  private async runCheck(): Promise<UpdateState> {
    this.set({ phase: "checking", error: undefined, percent: undefined, version: undefined, notes: undefined });
    try {
      const result = await this.command(["--check"]);
      if (result.phase === "idle") return this.set({ phase: "idle" });
      if (typeof result.version !== "string" || !newerVersion(result.version, this.options.version) ||
          (result.notes !== undefined && typeof result.notes !== "string")) throw new CliError("internal");
      return this.set({ phase: "available", version: result.version, notes: typeof result.notes === "string" ? result.notes.slice(0, 4000) : undefined });
    } catch (error) { return this.failed(error); }
  }

  install(): Promise<UpdateState> {
    if (this.installFlight) return this.installFlight;
    this.installFlight = this.runInstall().finally(() => { this.installFlight = null; });
    return this.installFlight;
  }

  private async runInstall(): Promise<UpdateState> {
    if (this.disposed || !this.options.launcher) return this.get();
    if (this.state.phase === "available" && this.state.version) {
      const version = this.state.version;
      this.set({ phase: "downloading", percent: 0, error: undefined });
      try {
        const result = await this.command(["--stage", version], percent => this.set({ percent }));
        if (result.version !== version) throw new CliError("internal");
        return this.set({ phase: "ready", percent: 100 });
      } catch (error) { return this.failed(error); }
    }
    if (this.state.phase !== "ready" || !this.state.version) return this.get();
    if (this.options.blocked()) return this.set({ error: "busy" });
    try {
      if (this.options.confirmRestart && !await this.options.confirmRestart()) return this.set({ error: "cancelled" });
      if (this.options.blocked()) return this.set({ error: "busy" });
      this.set({ phase: "installing", error: undefined });
      await detached(this.options.launcher, ["--activate", this.state.version, "--wait-pid", String(process.pid)], this.options.spawn ?? spawn);
      await this.options.finish();
      return this.get();
    } catch (error) { return this.failed(error); }
  }

  private failed(error: unknown): UpdateState {
    return this.set({ phase: "error", error: error instanceof CliError ? error.code : "internal" });
  }
}

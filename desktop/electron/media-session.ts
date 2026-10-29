/**
 * Windows media transport for the Node mini player (`desktop/electron/media-session.ts`).
 *
 * The helper is a fixed PowerShell 5.1 script, read through Node's fs (asar-safe) and passed
 * as an encoded command; no user value is interpolated into it. All requests are JSONL with a
 * bounded id/line size, one in flight at a time, and a finite timeout. The helper is lazy: no
 * process exists until the player asks for state or sends a command.
 *
 * Session identity: GSMTC has no stable session id. We bind the opaque token returned to the
 * renderer to the captured session object plus a source-app/title/artist/end-time fingerprint.
 * A command is accepted only for the current token, and only after the helper re-reads the
 * current session and finds the same fingerprint; it is then sent to the captured object. A
 * change, a helper restart, or an unknown token returns `stale_session` and never retargets.
 *
 * We do not use `Marshal.GetIUnknownForObject` pointer equality for authorization. That API
 * returns a counted IUnknown that would have to be released with `Marshal.Release`, but WinRT
 * agile/proxy objects can expose different identities for the same session and pointers can be
 * reused after release. A pointer check would therefore be both leak-prone and unsafe as a
 * session capability. The safer alternative is the token + fingerprint + captured object above.
 * Metadata stays in RAM; no cover URL, network, log or persistence is involved.
 */
import { spawn as spawnChild } from "node:child_process";
import { readFileSync } from "node:fs";
import * as path from "node:path";
import {
  EMPTY_MEDIA_STATE,
  type MediaCommand,
  type MediaCommandInput,
  type MediaCommandResultDto,
  type MediaPlayback,
  type MediaStateDto,
} from "../src/shared/media";

export const MEDIA_SESSION_MAX_SCRIPT_CHARS = 8000;
export const MEDIA_SESSION_MAX_ARG_CHARS = 30000;
const DEFAULT_MAX_LINE_BYTES = 8192;
const DEFAULT_REQUEST_TIMEOUT_MS = 5000;
const DEFAULT_START_TIMEOUT_MS = 5000;
const DEFAULT_DISPOSE_GRACE_MS = 500;
const TITLE_LIMIT = 160;
const ARTIST_LIMIT = 120;
const SOURCE_LIMIT = 120;
const SESSION_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;
const COMMANDS: ReadonlySet<MediaCommand> = new Set(["play", "pause", "previous", "next"]);

export interface MediaSessionSpawnOptions {
  env: NodeJS.ProcessEnv;
  shell: false;
  windowsHide: true;
  stdio: ["pipe", "pipe", "pipe"];
}

export type MediaSessionSpawn = (
  command: string,
  args: string[],
  options: MediaSessionSpawnOptions
) => MediaSessionProcess;

export interface MediaSessionProcess {
  stdin: {
    write(data: string): boolean;
    end(): void;
    on(event: string, listener: (...args: any[]) => void): unknown;
  };
  stdout: { on(event: string, listener: (...args: any[]) => void): unknown };
  stderr: { on(event: string, listener: (...args: any[]) => void): unknown };
  on(event: string, listener: (...args: any[]) => void): unknown;
  kill(signal?: NodeJS.Signals): boolean;
  exitCode?: number | null;
}

export interface WindowsMediaSessionOptions {
  scriptPath: string;
  requestTimeoutMs?: number;
  startTimeoutMs?: number;
  disposeGraceMs?: number;
  maxLineBytes?: number;
  maxScriptChars?: number;
  spawn?: MediaSessionSpawn;
  readScript?: (scriptPath: string) => string;
  powershellPath?: string;
  /** Pausa entre releituras depois de um comando; 0 desliga a releitura. */
  settleDelayMs?: number;
}

/**
 * A app que publica a sessão aplica o comando depois de o `Try*` responder (medido: ~400 ms num
 * publisher de teste). Relê até ~1 s e para logo que o efeito aparece.
 */
const SETTLE_READS = 10;
const DEFAULT_SETTLE_DELAY_MS = 100;

/** O estado já mostra o efeito do comando (play/pause) ou outra faixa (anterior/seguinte). */
export function commandSettled(command: MediaCommandInput, state: MediaStateDto): boolean {
  if (command.command === "play") return state.playback === "playing";
  if (command.command === "pause") return state.playback === "paused";
  return state.session_id !== command.session_id;
}

interface HelperResponse {
  v: 1;
  id: string;
  ok: boolean;
  error?: unknown;
  state?: unknown;
}

interface PendingRequest {
  resolve: (response: HelperResponse) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
}

interface ReadyWaiter {
  resolve: () => void;
  reject: (error: Error) => void;
}

export class MediaSessionError extends Error {
  constructor(public readonly code: string) {
    super(code);
    this.name = "MediaSessionError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isPlayback(value: unknown): value is MediaPlayback {
  return value === "playing" || value === "paused" || value === "stopped" || value === "unknown";
}

function cleanText(value: unknown, maximum: number): string {
  if (typeof value !== "string") {
    return "";
  }
  return value
    .replace(/[\u0000-\u001F\u007F]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, maximum);
}

function emptyState(): MediaStateDto {
  return { ...EMPTY_MEDIA_STATE };
}

function normalizeState(raw: unknown): MediaStateDto {
  if (!isRecord(raw) || raw.v !== 1) {
    return emptyState();
  }
  const available = raw.available === true;
  const sessionId =
    typeof raw.session_id === "string" && SESSION_ID_PATTERN.test(raw.session_id)
      ? raw.session_id
      : null;
  if ((available && !sessionId) || (!available && sessionId !== null)) {
    return emptyState();
  }
  const playback = isPlayback(raw.playback) ? raw.playback : "unknown";
  return {
    v: 1,
    available,
    session_id: available ? sessionId : null,
    title: available ? cleanText(raw.title, TITLE_LIMIT) : "",
    artist: available ? cleanText(raw.artist, ARTIST_LIMIT) : "",
    source_app: available ? cleanText(raw.source_app, SOURCE_LIMIT) : "",
    playback: available ? playback : "unknown",
    can_play: available && raw.can_play === true,
    can_pause: available && raw.can_pause === true,
    can_previous: available && raw.can_previous === true,
    can_next: available && raw.can_next === true,
  };
}

function safeError(value: unknown): string | null {
  return typeof value === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(value) ? value : null;
}

function errorCode(error: unknown): string {
  return error instanceof MediaSessionError ? error.code : "media_unavailable";
}

function isValidCommandInput(input: unknown): input is MediaCommandInput {
  return (
    isRecord(input) &&
    typeof input.command === "string" &&
    COMMANDS.has(input.command as MediaCommand) &&
    typeof input.session_id === "string" &&
    SESSION_ID_PATTERN.test(input.session_id)
  );
}

function defaultPowerShellPath(): string {
  const root = process.env.SystemRoot ?? process.env.WINDIR ?? "C:\\Windows";
  return path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
}

function minimalEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  for (const key of [
    "SystemRoot",
    "WINDIR",
    "PATH",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "HOMEDRIVE",
    "HOMEPATH",
    "COMSPEC",
  ]) {
    const value = process.env[key];
    if (typeof value === "string" && value.length > 0) {
      env[key] = value;
    }
  }
  env.PSModuleAnalysisCachePath = "nul";
  return env;
}

const defaultSpawn: MediaSessionSpawn = (command, args, options) =>
  spawnChild(command, args, options) as unknown as MediaSessionProcess;

export class WindowsMediaSession {
  private readonly scriptPath: string;
  private readonly requestTimeoutMs: number;
  private readonly startTimeoutMs: number;
  private readonly disposeGraceMs: number;
  private readonly maxLineBytes: number;
  private readonly maxScriptChars: number;
  private readonly settleDelayMs: number;
  private readonly spawnProcess: MediaSessionSpawn;
  private readonly readScript: (scriptPath: string) => string;
  private readonly powershellPath: string;

  private child: MediaSessionProcess | null = null;
  private startPromise: Promise<void> | null = null;
  private readyWaiter: ReadyWaiter | null = null;
  private readySeen = false;
  private readonly pending = new Map<string, PendingRequest>();
  private stdoutBuffer = "";
  private requestSeq = 0;
  private queue: Promise<void> = Promise.resolve();
  private disposed = false;

  constructor(options: WindowsMediaSessionOptions) {
    this.scriptPath = options.scriptPath;
    this.requestTimeoutMs = Math.max(1, options.requestTimeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS);
    this.startTimeoutMs = Math.max(1, options.startTimeoutMs ?? DEFAULT_START_TIMEOUT_MS);
    this.disposeGraceMs = Math.max(1, options.disposeGraceMs ?? DEFAULT_DISPOSE_GRACE_MS);
    this.maxLineBytes = Math.max(256, options.maxLineBytes ?? DEFAULT_MAX_LINE_BYTES);
    this.maxScriptChars = Math.max(1, options.maxScriptChars ?? MEDIA_SESSION_MAX_SCRIPT_CHARS);
    this.spawnProcess = options.spawn ?? defaultSpawn;
    this.readScript = options.readScript ?? ((scriptPath) => readFileSync(scriptPath, "utf8"));
    this.powershellPath = options.powershellPath ?? defaultPowerShellPath();
    this.settleDelayMs = Math.max(0, options.settleDelayMs ?? DEFAULT_SETTLE_DELAY_MS);
  }

  public async get(): Promise<MediaStateDto> {
    if (this.disposed) {
      return emptyState();
    }
    try {
      const response = await this.enqueue(() => this.request("get"));
      return response.ok ? normalizeState(response.state) : emptyState();
    } catch {
      return emptyState();
    }
  }

  public async command(input: MediaCommandInput): Promise<MediaCommandResultDto> {
    if (this.disposed) {
      return { ok: false, error: "disposed", state: emptyState() };
    }
    if (!isValidCommandInput(input)) {
      return { ok: false, error: "invalid_input", state: emptyState() };
    }
    try {
      const response = await this.enqueue(() =>
        this.request("command", input.command, input.session_id)
      );
      let state = normalizeState(response.state);
      if (response.ok) {
        // Só leituras: o comando nunca se repete. Devolve a última leitura, estabilizada ou não.
        for (let read = 0; read < SETTLE_READS && this.settleDelayMs > 0 && !commandSettled(input, state); read++) {
          await new Promise(resolve => setTimeout(resolve, this.settleDelayMs));
          if (this.disposed) break;
          // Uma releitura falhada nunca desfaz o comando que já foi aceite.
          const reread = await this.enqueue(() => this.request("get")).catch(() => null);
          if (!reread?.ok) break;
          state = normalizeState(reread.state);
        }
        return { ok: true, state };
      }
      return { ok: false, error: safeError(response.error) ?? "command_failed", state };
    } catch (error) {
      return { ok: false, error: errorCode(error), state: emptyState() };
    }
  }

  public async dispose(): Promise<void> {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    const child = this.child;
    this.child = null;
    this.readySeen = false;
    const error = new MediaSessionError("disposed");
    if (this.readyWaiter) {
      this.readyWaiter.reject(error);
      this.readyWaiter = null;
    }
    this.rejectAll(error);
    if (!child) {
      return;
    }

    let timer: NodeJS.Timeout | null = null;
    const exited = new Promise<void>((resolve) => {
      let settled = false;
      const finish = () => {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        resolve();
      };
      timer = setTimeout(finish, this.disposeGraceMs);
      child.on("exit", finish);
      if (child.exitCode !== null && child.exitCode !== undefined) finish();
    });
    try {
      child.stdin.end();
    } catch {
      // The process may already be gone; kill below is the fallback.
    }
    try {
      child.kill();
    } catch {
      // Cleanup is best effort; dispose must not throw into the caller.
    }
    await exited;
  }

  private enqueue<T>(task: () => Promise<T>): Promise<T> {
    const run = this.queue.then(task, task);
    this.queue = run.then(
      () => undefined,
      () => undefined
    );
    return run;
  }

  private async ensureStarted(): Promise<void> {
    if (this.disposed) {
      throw new MediaSessionError("disposed");
    }
    if (this.child) {
      return;
    }
    if (!this.startPromise) {
      this.startPromise = this.start().finally(() => {
        this.startPromise = null;
      });
    }
    await this.startPromise;
    if (!this.child) {
      throw new MediaSessionError("helper_exited");
    }
  }

  private async start(): Promise<void> {
    if (this.disposed) {
      throw new MediaSessionError("disposed");
    }
    let script: string;
    try {
      script = this.readScript(this.scriptPath).replace(/^\uFEFF/, "");
    } catch {
      throw new MediaSessionError("script_read_failed");
    }
    if (script.length > this.maxScriptChars) {
      throw new MediaSessionError("script_too_large");
    }
    const encoded = Buffer.from(script, "utf16le").toString("base64");
    if (encoded.length > MEDIA_SESSION_MAX_ARG_CHARS) {
      throw new MediaSessionError("script_too_large");
    }

    let child: MediaSessionProcess;
    try {
      child = this.spawnProcess(
        this.powershellPath,
        ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        {
          env: minimalEnv(),
          shell: false,
          windowsHide: true,
          stdio: ["pipe", "pipe", "pipe"],
        }
      );
    } catch {
      throw new MediaSessionError("spawn_failed");
    }
    if (this.disposed) {
      try {
        child.kill();
      } catch {
        // Already gone.
      }
      throw new MediaSessionError("disposed");
    }

    this.child = child;
    this.readySeen = false;
    this.stdoutBuffer = "";
    child.stdout.on("data", (chunk) => {
      if (this.child === child) this.onStdout(chunk);
    });
    child.stderr.on("data", () => undefined);
    child.stdin.on("error", () => {
      if (this.child === child) this.terminate("stdin_error");
    });
    child.on("error", () => {
      if (this.child === child) this.terminate("helper_error");
    });
    child.on("exit", () => this.onExit(child));
    await this.waitForReady();
  }

  private waitForReady(): Promise<void> {
    if (this.readySeen) {
      return Promise.resolve();
    }
    return new Promise<void>((resolve, reject) => {
      let settled = false;
      let timer: NodeJS.Timeout | null = null;
      const finish = (error?: Error) => {
        if (settled) return;
        settled = true;
        if (timer) clearTimeout(timer);
        if (this.readyWaiter === waiter) {
          this.readyWaiter = null;
        }
        if (error) reject(error);
        else resolve();
      };
      const waiter: ReadyWaiter = {
        resolve: () => finish(),
        reject: (error) => finish(error),
      };
      timer = setTimeout(() => {
        this.terminate("helper_start_timeout");
        finish(new MediaSessionError("helper_start_timeout"));
      }, this.startTimeoutMs);
      this.readyWaiter = waiter;
    });
  }

  private request(
    type: "get" | "command",
    command?: MediaCommand,
    sessionId?: string
  ): Promise<HelperResponse> {
    return (async () => {
      await this.ensureStarted();
      const child = this.child;
      if (!child) {
        throw new MediaSessionError("helper_exited");
      }
      const id = this.nextRequestId();
      const line =
        type === "get"
          ? { v: 1, id, type }
          : { v: 1, id, type, command, session_id: sessionId };

      return await new Promise<HelperResponse>((resolve, reject) => {
        const timer = setTimeout(() => {
          this.pending.delete(id);
          this.terminate("timeout");
          reject(new MediaSessionError("timeout"));
        }, this.requestTimeoutMs);
        this.pending.set(id, { resolve, reject, timer });
        try {
          child.stdin.write(`${JSON.stringify(line)}\n`);
        } catch (error) {
          clearTimeout(timer);
          this.pending.delete(id);
          reject(error instanceof Error ? error : new MediaSessionError("stdin_error"));
        }
      });
    })();
  }

  private nextRequestId(): string {
    this.requestSeq += 1;
    return `m-${Date.now().toString(36)}-${this.requestSeq.toString(36)}`;
  }

  private onStdout(chunk: Buffer | string): void {
    const text = typeof chunk === "string" ? chunk : chunk.toString("utf8");
    this.stdoutBuffer += text;

    let newline = this.stdoutBuffer.indexOf("\n");
    if (newline < 0 && Buffer.byteLength(this.stdoutBuffer, "utf8") > this.maxLineBytes) {
      this.terminate("protocol_error");
      return;
    }
    while (newline >= 0) {
      let line = this.stdoutBuffer.slice(0, newline);
      this.stdoutBuffer = this.stdoutBuffer.slice(newline + 1);
      if (line.endsWith("\r")) {
        line = line.slice(0, -1);
      }
      if (line.trim().length > 0) {
        if (Buffer.byteLength(line, "utf8") > this.maxLineBytes) {
          this.terminate("protocol_error");
          return;
        }
        this.handleLine(line);
        if (!this.child) {
          return;
        }
      }
      newline = this.stdoutBuffer.indexOf("\n");
    }
  }

  private handleLine(line: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      this.terminate("protocol_error");
      return;
    }
    if (!isRecord(parsed) || parsed.v !== 1) {
      this.terminate("protocol_error");
      return;
    }
    if (parsed.type === "ready") {
      this.readySeen = true;
      const waiter = this.readyWaiter;
      this.readyWaiter = null;
      waiter?.resolve();
      return;
    }
    const id = typeof parsed.id === "string" ? parsed.id : null;
    if (!id) {
      this.terminate("protocol_error");
      return;
    }
    const pending = this.pending.get(id);
    if (!pending) {
      return;
    }
    if (typeof parsed.ok !== "boolean" || !isRecord(parsed.state)) {
      this.terminate("protocol_error");
      return;
    }
    this.pending.delete(id);
    clearTimeout(pending.timer);
    pending.resolve({
      v: 1,
      id,
      ok: parsed.ok,
      error: parsed.error,
      state: parsed.state,
    });
  }

  private onExit(child: MediaSessionProcess): void {
    if (this.child !== child) {
      return;
    }
    this.child = null;
    this.readySeen = false;
    const error = new MediaSessionError("helper_exited");
    if (this.readyWaiter) {
      this.readyWaiter.reject(error);
      this.readyWaiter = null;
    }
    this.rejectAll(error);
  }

  private terminate(code: string): void {
    const child = this.child;
    this.child = null;
    this.readySeen = false;
    const error = new MediaSessionError(code);
    if (this.readyWaiter) {
      this.readyWaiter.reject(error);
      this.readyWaiter = null;
    }
    this.rejectAll(error);
    if (!child) {
      return;
    }
    try {
      child.stdin.end();
    } catch {
      // Already closed.
    }
    try {
      child.kill();
    } catch {
      // Already gone.
    }
  }

  private rejectAll(error: Error): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }
}

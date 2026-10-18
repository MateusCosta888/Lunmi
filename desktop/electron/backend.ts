/**
 * Supervisão do processo Python da ponte IPC v1 (`desktop/electron/backend.ts`).
 *
 * Responsabilidades (F1):
 * - Spawna `python -u -m app.bridge.server` por argv (sem shell).
 * - Mantém no máximo 1 processo Python ativo por instância.
 * - Realiza handshake `session.hello` -> `session.ready` enviando exclusivamente os `host_pids`
 *   coletados pela autoridade do processo principal Electron (nunca do renderer).
 * - Bloqueia `session.hello` e `app.shutdown` em `sendRendererCommand`, impedindo que o
 *   renderer injete `host_pids` ou altere autoridade.
 * - Em caso de crash ou encerramento inesperado do processo Python, rejeita todas as
 *   requisições pendentes imediatamente e emite um evento de erro de sessão/tarefa para o
 *   renderer, sem reexecutar nenhuma tarefa automaticamente.
 * - Suporta `restart()` explícito e `shutdown()` limpo (com fallback de `kill` para não deixar
 *   processos órfãos).
 */
import { ChildProcessWithoutNullStreams, spawn } from "child_process";
import * as fs from "fs";
import * as path from "path";
import { IntegrationAuthGate, isComposioAuthUrl } from "./integration-auth";
import {
  COMMAND_TYPES,
  Envelope,
  encodeEnvelope,
  isPlainObject,
  JsonlLineFramer,
  MAX_HOST_PIDS,
  MAX_ID_CHARS,
  parseEnvelope,
  PROTOCOL_VERSION,
  SessionReadyPayload,
  validateHostPids,
} from "../src/shared/protocol";

export interface PendingRequest {
  resolve: (value: Record<string, unknown>) => void;
  reject: (reason: Error) => void;
  timer: NodeJS.Timeout;
}

/** Estado público de falha de sessão do backend, sem segredos nem traceback. */
export interface SessionFailurePayload {
  code: string;
  message: string;
  detail: string | null;
  session_id: string;
  exit_code: number | null;
  signal: string | null;
  executable?: string;
}

export const RENDERER_FORBIDDEN_COMMANDS = new Set<string>([
  "session.hello",
  "app.shutdown",
  "fs.read",
  "integrations.browser.failed",
]);

export const DEFAULT_COMMAND_TIMEOUT_MS = 30000;

/**
 * Comandos que esperam o provider têm prazo maior que o padrão. Cada valor fica acima da
 * espera correspondente em `app/bridge/service.py` (30/45/95/30 s), para que o backend
 * responda — com resultado ou erro tipado — antes do main desistir. Com 30 s fixos, um
 * `brain.verify` ou `brain.probe` lento virava `timeout` aqui enquanto o Python ainda
 * trabalhava, e a resposta real chegava a uma promessa já descartada.
 */
export const COMMAND_TIMEOUT_MS: Readonly<Record<string, number>> = Object.freeze({
  "brain.list": 35000,
  "brain.verify": 50000,
  "brain.probe": 100000,
  "brain.set": 35000,
});

export function commandTimeoutMs(type: string): number {
  return COMMAND_TIMEOUT_MS[type] ?? DEFAULT_COMMAND_TIMEOUT_MS;
}

export function resolvePythonExecutable(repoRoot: string, resourcesRoot?: string | null): string {
  const envPy = process.env.LUNMI_PYTHON;
  if (envPy && fs.existsSync(envPy)) {
    return envPy;
  }
  // Runtime Python privado do pacote: nada de depender de um Python instalado na máquina
  // do testador. O instalador copia `resources/python` (CPython portável + dependências).
  if (resourcesRoot) {
    const bundled = path.join(resourcesRoot, "python", "python.exe");
    if (fs.existsSync(bundled)) {
      return bundled;
    }
    // Pacote sem o runtime privado é defeito de build: devolver o caminho ausente faz o
    // spawn falhar com o caminho nomeado (backend_spawn_failed) em vez de usar o Python da
    // máquina do testador por engano. Em desenvolvimento `resourcesRoot` é `null`.
    return bundled;
  }
  const winVenv = path.join(repoRoot, ".venv", "Scripts", "python.exe");
  if (fs.existsSync(winVenv)) {
    return winVenv;
  }
  const posixVenv = path.join(repoRoot, ".venv", "bin", "python");
  if (fs.existsSync(posixVenv)) {
    return posixVenv;
  }
  return "python";
}

export interface BackendSupervisorOptions {
  repoRoot: string;
  /** Raiz dos recursos do pacote (runtime Python privado). Ausente em desenvolvimento. */
  resourcesRoot?: string | null;
  /** Raiz de dados graváveis do utilizador; vira `LUNMI_USER_DATA` no processo Python. */
  userDataRoot?: string | null;
  clientVersion: string;
  sandboxDir?: string | null;
  handshakeOnly?: boolean;
  /** Aceite/sandbox: o backend lê a configuração real só em RAM (nada é gravado no pessoal). */
  settingsInRam?: boolean;
  /** Caminho fixo ou resolvedor tardio (permite testar retry depois de corrigir o executável). */
  pythonExecutable?: string | (() => string);
  pythonArgs?: string[];
  getHostPids: () => number[];
  onEvent: (envelope: Envelope) => void;
  /** Private OAuth URL, handled by main only; never forwarded to renderer. */
  onIntegrationAuthOpen?: (url: string) => Promise<void>;
}

export class PythonBackendSupervisor {
  private proc: ChildProcessWithoutNullStreams | null = null;
  private framer = new JsonlLineFramer();
  private readonly pending = new Map<string, PendingRequest>();
  private reqCounter = 0;
  private readyPayload: SessionReadyPayload | null = null;
  private startPromise: Promise<SessionReadyPayload> | null = null;
  private stopping = false;
  private failure: SessionFailurePayload | null = null;
  private readonly failedChildren = new WeakSet<ChildProcessWithoutNullStreams>();
  private readonly options: BackendSupervisorOptions;
  private readonly integrationAuth = new IntegrationAuthGate();

  constructor(options: BackendSupervisorOptions) {
    this.options = options;
  }

  public get pid(): number | null {
    return this.proc?.pid ?? null;
  }

  public get isRunning(): boolean {
    return Boolean(this.proc && this.proc.exitCode === null);
  }

  public get currentSession(): SessionReadyPayload | null {
    return this.readyPayload;
  }

  /** Último estado público de falha de sessão (spawn, pipe ou saída inesperada). */
  public get sessionFailure(): SessionFailurePayload | null {
    return this.failure;
  }

  public clearSessionFailure(): void {
    this.failure = null;
  }

  public collectSanitizedHostPids(): number[] {
    const raw = this.options.getHostPids();
    const unique = Array.from(
      new Set(
        [process.pid, ...raw].filter(
          (p) => typeof p === "number" && Number.isInteger(p) && p > 0 && p <= 2147483647
        )
      )
    ).slice(0, MAX_HOST_PIDS);
    return validateHostPids(unique);
  }

  public async startAndHandshake(): Promise<SessionReadyPayload> {
    if (this.readyPayload && this.isRunning) {
      await this.syncHostPids();
      return this.readyPayload;
    }
    if (this.startPromise) {
      return this.startPromise;
    }
    this.startPromise = this._doStartAndHandshake();
    try {
      return await this.startPromise;
    } finally {
      this.startPromise = null;
    }
  }

  private async _doStartAndHandshake(): Promise<SessionReadyPayload> {
    this.integrationAuth.clear();
    this.stopping = false;
    this.failure = null;
    this.spawnProcess();
    const hostPids = this.collectSanitizedHostPids();
    const payload = await this._sendInternalCommand("session.hello", {
      client: "electron",
      client_version: this.options.clientVersion,
      host_pids: hostPids,
    });
    this.readyPayload = payload as unknown as SessionReadyPayload;
    return this.readyPayload;
  }

  public async syncHostPids(): Promise<SessionReadyPayload | null> {
    if (!this.isRunning || !this.readyPayload) {
      return null;
    }
    const hostPids = this.collectSanitizedHostPids();
    const payload = await this._sendInternalCommand("session.hello", {
      client: "electron",
      client_version: this.options.clientVersion,
      host_pids: hostPids,
    });
    this.readyPayload = payload as unknown as SessionReadyPayload;
    return this.readyPayload;
  }

  public async restart(): Promise<SessionReadyPayload> {
    await this.shutdown();
    return this.startAndHandshake();
  }

  private spawnProcess(): void {
    if (this.proc && this.proc.exitCode === null) {
      return;
    }
    this.framer = new JsonlLineFramer();
    const configuredExe = this.options.pythonExecutable;
    const pythonExe =
      typeof configuredExe === "function"
        ? configuredExe()
        : configuredExe ?? resolvePythonExecutable(this.options.repoRoot, this.options.resourcesRoot ?? null);
    const args = this.options.pythonArgs
      ? [...this.options.pythonArgs]
      : ["-u", "-m", "app.bridge.server"];
    if (!this.options.pythonArgs) {
      if (this.options.sandboxDir) {
        args.push("--sandbox-dir", this.options.sandboxDir);
      }
      if (this.options.handshakeOnly) {
        args.push("--handshake-only");
      }
      if (this.options.settingsInRam) {
        args.push("--settings-in-ram");
      }
    }

    const childEnv: NodeJS.ProcessEnv = {
      ...process.env,
      PYTHONIOENCODING: "utf-8",
      // Nunca escrever `__pycache__` dentro dos recursos instalados (somente-leitura no
      // pacote por utilizador) e manter a saída de texto determinística.
      PYTHONDONTWRITEBYTECODE: "1",
      PYTHONUTF8: "1",
    };
    if (this.options.userDataRoot) {
      // O backend grava `.local/`, `logs/` e `output/` aqui, não na pasta instalada.
      childEnv.LUNMI_USER_DATA = this.options.userDataRoot;
    }

    let child: ChildProcessWithoutNullStreams;
    try {
      child = spawn(pythonExe, args, {
        cwd: this.options.repoRoot,
        stdio: ["pipe", "pipe", "pipe"],
        shell: false,
        windowsHide: true,
        env: childEnv,
      });
    } catch (err) {
      // `spawn` pode lançar de forma síncrona (argv inválido). Isso precisa rejeitar o
      // handshake pendente e publicar estado de falha — nunca virar uncaughtException.
      this._failSession(null, "backend_spawn_failed", {
        message: "Não foi possível iniciar o processo Python do backend.",
        detail: (err as Error).message,
        executable: pythonExe,
      });
      return;
    }
    this.proc = child;

    // `error` (ENOENT/EACCES do spawn) não garante `exit`: sem este handler o handshake
    // ficaria pendente para sempre e a exceção subiria como uncaughtException.
    child.on("error", (err) => {
      this._failSession(child, "backend_spawn_failed", {
        message: "Falha ao iniciar ou comunicar com o processo Python do backend.",
        detail: (err as Error).message,
        executable: pythonExe,
      });
    });

    // Erros de pipe (EPIPE ao escrever num backend morto) precisam falhar a sessão em vez
    // de derrubar o processo principal do Electron.
    child.stdin.on("error", (err) => {
      this._failSession(child, "backend_pipe_error", {
        message: "Falha ao escrever no stdin do backend Python.",
        detail: (err as Error).message,
      });
    });
    child.stdout.on("error", (err) => {
      this._failSession(child, "backend_pipe_error", {
        message: "Falha ao ler o stdout do backend Python.",
        detail: (err as Error).message,
      });
    });
    child.stderr.on("error", () => {
      // stderr é diagnóstico: um erro nele não invalida a sessão.
    });

    child.stdout.on("data", (chunk: Buffer) => {
      if (this.proc !== child || this.failedChildren.has(child)) return;
      const outcomes = this.framer.feedOutcomes(chunk);
      for (const item of outcomes) {
        if (item.error) {
          console.error("[bridge] erro de framing stdout:", item.error.code, item.error.message);
          continue;
        }
        if (item.line !== undefined) {
          this.handleLine(item.line);
        }
      }
    });

    child.stderr.on("data", (chunk: Buffer) => {
      const text = chunk.toString("utf8").trim();
      if (text) {
        process.stderr.write(`${text}\n`);
      }
    });

    child.on("exit", (code, signal) => {
      this._failSession(child, "backend_exited", {
        message:
          "O processo Python do backend encerrou inesperadamente; nenhuma tarefa foi reexecutada.",
        exitCode: code ?? null,
        signal: signal ?? null,
      });
    });
  }

  /**
   * Falha a sessão de forma idempotente: rejeita pendências, limpa o estado do handshake e
   * publica um estado público de falha para o renderer. Uma saída esperada (`stopping`) não
   * gera evento nem estado de erro.
   */
  private _failSession(
    child: ChildProcessWithoutNullStreams | null,
    code: string,
    info: {
      message: string;
      detail?: string | null;
      exitCode?: number | null;
      signal?: string | null;
      executable?: string;
    }
  ): void {
    if (child) {
      if (this.failedChildren.has(child)) {
        return;
      }
      this.failedChildren.add(child);
    }
    const expected = this.stopping;
    const prevSessionId = this.readyPayload?.session_id ?? "";
    const err = new Error(`${code}: ${info.message}`);
    for (const [, req] of this.pending) {
      clearTimeout(req.timer);
      req.reject(err);
    }
    this.pending.clear();
    this.framer = new JsonlLineFramer();
    this.readyPayload = null;
    if (this.proc && (child === null || this.proc === child)) {
      this.proc = null;
    }
    if (expected) {
      return;
    }
    const payload: SessionFailurePayload = {
      code,
      message: info.message,
      detail: info.detail ?? null,
      session_id: prevSessionId,
      exit_code: info.exitCode ?? null,
      signal: info.signal ?? null,
    };
    if (info.executable) {
      payload.executable = info.executable;
    }
    this.failure = payload;
    this.options.onEvent({
      v: PROTOCOL_VERSION,
      id: `evt-fail-${Date.now()}`,
      type: "task.error",
      payload: payload as unknown as Record<string, unknown>,
    });
  }

  private handleLine(rawLine: string): void {
    let env: Envelope;
    try {
      env = parseEnvelope(rawLine);
    } catch (err) {
      console.error("[bridge] envelope inválido descartado:", (err as Error).message);
      return;
    }

    if (env.type === "command.result") {
      const pending = this.pending.get(env.id);
      if (pending) {
        clearTimeout(pending.timer);
        this.pending.delete(env.id);
        pending.resolve(env.payload);
      }
      return;
    }

    if (env.type === "command.error") {
      const pending = this.pending.get(env.id);
      if (pending) {
        clearTimeout(pending.timer);
        this.pending.delete(env.id);
        const code = typeof env.payload.code === "string" ? env.payload.code : "command_error";
        const message =
          typeof env.payload.message === "string" ? env.payload.message : "Erro no comando IPC.";
        pending.reject(new Error(`${code}: ${message}`));
      }
      return;
    }

    if (env.type === "integrations.changed") this.integrationAuth.observe(env.payload);
    if (env.type === "integrations.browser.open") {
      const fail = () => {
        if (!this.integrationAuth.isCurrent(env.payload)) return;
        this.integrationAuth.clear();
        if (this.isRunning) {
          void this._sendInternalCommand("integrations.browser.failed", { flow_id: env.payload.flow_id }).catch(() => undefined);
        }
      };
      if (this.integrationAuth.isCurrent(env.payload) && !isComposioAuthUrl(env.payload.url)) {
        fail();
        return;
      }
      if (this.integrationAuth.accept(env.payload)) {
        void Promise.resolve().then(() => {
          if (!this.integrationAuth.isCurrent(env.payload)) return;
          if (!this.options.onIntegrationAuthOpen) throw new Error("Browser unavailable");
          return this.options.onIntegrationAuthOpen(env.payload.url as string);
        }).catch(fail);
      }
      return;
    }
    this.options.onEvent(env);
  }

  public sendRendererCommand(
    type: string,
    payload: Record<string, unknown> = {},
    timeoutMs = commandTimeoutMs(type)
  ): Promise<Record<string, unknown>> {
    if (RENDERER_FORBIDDEN_COMMANDS.has(type)) {
      return Promise.reject(
        new Error(`forbidden_command: O comando ${type} é exclusivo do processo principal.`)
      );
    }
    if (!isPlainObject(payload)) {
      return Promise.reject(new Error("invalid_payload: Payload deve ser um objeto."));
    }
    // Remove quaisquer campos internos reservados de caminhos confiáveis
    const sanitizedPayload: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(payload)) {
      if (!k.startsWith("_trusted_")) {
        sanitizedPayload[k] = v;
      }
    }
    if (type === "integrations.connect" && this.readyPayload) {
      this.integrationAuth.arm(this.readyPayload.session_id);
    } else if (type === "integrations.cancel" || type === "integrations.disconnect") {
      this.integrationAuth.clear();
    }
    return this._sendInternalCommand(type, sanitizedPayload, timeoutMs).catch((error: unknown) => {
      if (type === "integrations.connect") this.integrationAuth.clear();
      throw error;
    });
  }

  public _sendInternalCommand(
    type: string,
    payload: Record<string, unknown> = {},
    timeoutMs = commandTimeoutMs(type)
  ): Promise<Record<string, unknown>> {
    if (!this.proc || this.proc.exitCode !== null) {
      return Promise.reject(new Error("backend_not_running: Processo Python não está em execução."));
    }
    if (type !== "integrations.browser.failed" && !COMMAND_TYPES.includes(type as (typeof COMMAND_TYPES)[number])) {
      return Promise.reject(new Error(`unknown_command: Comando não permitido: ${type}`));
    }
    if (!isPlainObject(payload)) {
      return Promise.reject(new Error("invalid_payload: Payload deve ser um objeto."));
    }
    this.reqCounter += 1;
    const id = `req-${Date.now()}-${this.reqCounter}`.slice(0, MAX_ID_CHARS);
    const envelope: Envelope = {
      v: PROTOCOL_VERSION,
      id,
      type,
      payload,
    };

    return new Promise<Record<string, unknown>>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`timeout: Timeout aguardando resposta para ${type} (${id}).`));
      }, timeoutMs);

      this.pending.set(id, { resolve, reject, timer });
      try {
        const encoded = encodeEnvelope(envelope);
        this.proc!.stdin.write(encoded, "utf8");
      } catch (err) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(err as Error);
      }
    });
  }

  public async shutdown(): Promise<{ status: string; session_id: string }> {
    this.integrationAuth.clear();
    this.stopping = true;
    if (!this.proc || this.proc.exitCode !== null) {
      this.proc = null;
      this.readyPayload = null;
      return { status: "already_stopped", session_id: "" };
    }
    const child = this.proc;
    try {
      const res = await this._sendInternalCommand("app.shutdown", {}, 4000);
      await new Promise<void>((resolve) => {
        const killTimer = setTimeout(() => {
          if (child.exitCode === null) {
            child.kill();
          }
          resolve();
        }, 1500);
        child.once("exit", () => {
          clearTimeout(killTimer);
          resolve();
        });
      });
      return {
        status: String(res.status ?? "shutting_down"),
        session_id: String(res.session_id ?? ""),
      };
    } catch {
      if (child.exitCode === null) {
        child.kill();
      }
      await new Promise<void>((resolve) => {
        if (child.exitCode !== null) {
          resolve();
          return;
        }
        const t = setTimeout(resolve, 500);
        child.once("exit", () => {
          clearTimeout(t);
          resolve();
        });
      });
      return { status: "killed", session_id: "" };
    } finally {
      this.proc = null;
      this.readyPayload = null;
    }
  }
}

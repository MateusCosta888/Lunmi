/**
 * Regressões F1 do `PythonBackendSupervisor` (`desktop/electron/backend.ts`).
 *
 * Cobrem os reparos pontuais da revisão F1:
 * - falha de spawn (executável ausente) rejeita o handshake, limpa o estado e publica
 *   `backend_spawn_failed` sem virar uncaughtException;
 * - retry explícito no mesmo supervisor depois de corrigir o executável;
 * - encerramento inesperado publica `backend_exited` e `restart()` abre sessão nova sem
 *   reexecutar a tarefa anterior.
 */
import { describe, expect, it } from "vitest";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import { PythonBackendSupervisor } from "../electron/backend";
import type { Envelope } from "../src/shared/protocol";

const repoRoot = path.resolve(__dirname, "..", "..");

it("recusa leitura direta de arquivo pelo renderer antes de iniciar o backend", async () => {
  const supervisor = new PythonBackendSupervisor({
    repoRoot, clientVersion: "test", getHostPids: () => [], onEvent: () => undefined,
  });
  await expect(supervisor.sendRendererCommand("fs.read", { path: "qualquer-arquivo.txt" }))
    .rejects.toThrow(/forbidden_command/);
  expect(supervisor.pid).toBeNull();
});

function resolveRealPython(): string {
  const winPython = path.join(repoRoot, ".venv", "Scripts", "python.exe");
  if (fs.existsSync(winPython)) {
    return winPython;
  }
  const posixPython = path.join(repoRoot, ".venv", "bin", "python");
  if (fs.existsSync(posixPython)) {
    return posixPython;
  }
  return "python";
}

async function waitFor(
  predicate: () => boolean,
  timeoutMs = 8000,
  stepMs = 25
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, stepMs));
  }
  throw new Error("waitFor: condição não satisfeita no prazo.");
}

describe("PythonBackendSupervisor — falha de spawn/pipe e reinício explícito (F1)", () => {
  it("rejeita o handshake e publica estado de falha quando o executável não existe", async () => {
    const missing = path.join(
      os.tmpdir(),
      `lunmi-python-ausente-${process.pid}-${Date.now()}.exe`
    );
    expect(fs.existsSync(missing)).toBe(false);

    const events: Envelope[] = [];
    const sup = new PythonBackendSupervisor({
      repoRoot,
      clientVersion: "0.1.0",
      pythonExecutable: missing,
      getHostPids: () => [process.pid],
      onEvent: (env) => events.push(env),
    });

    await expect(sup.startAndHandshake()).rejects.toThrow(/backend_spawn_failed/);
    expect(sup.currentSession).toBeNull();
    expect(sup.isRunning).toBe(false);
    expect(sup.pid).toBeNull();
    expect(sup.sessionFailure?.code).toBe("backend_spawn_failed");
    expect(sup.sessionFailure?.executable).toBe(missing);

    // O estado de falha é publicado como evento público, sem traceback.
    const failureEvent = events.find((env) => env.type === "task.error");
    expect(failureEvent).toBeDefined();
    const payload = failureEvent!.payload as Record<string, unknown>;
    expect(payload.code).toBe("backend_spawn_failed");
    expect(payload.session_id).toBe("");

    // Uma segunda tentativa no mesmo executável ausente falha de novo (não fica presa).
    await expect(sup.startAndHandshake()).rejects.toThrow(/backend_spawn_failed/);
  });

  it("rejeita pedidos pendentes e permite retry bem-sucedido após corrigir o executável", async () => {
    const missing = path.join(
      os.tmpdir(),
      `lunmi-python-corrigivel-${process.pid}-${Date.now()}.exe`
    );
    const realPython = resolveRealPython();
    const sandboxDir = fs.mkdtempSync(path.join(os.tmpdir(), "lunmi-backend-retry-"));
    let currentExe = missing;

    const sup = new PythonBackendSupervisor({
      repoRoot,
      clientVersion: "0.1.0",
      sandboxDir,
      pythonExecutable: () => currentExe,
      getHostPids: () => [process.pid],
      onEvent: () => undefined,
    });

    try {
      await expect(sup.startAndHandshake()).rejects.toThrow(/backend_spawn_failed/);
      expect(sup.sessionFailure?.code).toBe("backend_spawn_failed");

      // Corrigido o executável, o mesmo supervisor reinicia e completa o handshake.
      currentExe = realPython;
      const ready = await sup.startAndHandshake();
      expect(ready.session_id).toBeTruthy();
      expect(ready.pid).toBeGreaterThan(0);
      expect(sup.sessionFailure).toBeNull();
      expect(sup.isRunning).toBe(true);

      const status = await sup.shutdown();
      expect(["shutting_down", "killed", "already_stopped"]).toContain(status.status);
      expect(sup.isRunning).toBe(false);
    } finally {
      await sup.shutdown().catch(() => undefined);
      fs.rmSync(sandboxDir, { recursive: true, force: true });
    }
  });

  it("publica backend_exited no crash e restart() abre sessão nova sem reexecutar tarefa", async () => {
    const sandboxDir = fs.mkdtempSync(path.join(os.tmpdir(), "lunmi-backend-crash-"));
    const events: Envelope[] = [];
    const sup = new PythonBackendSupervisor({
      repoRoot,
      clientVersion: "0.1.0",
      sandboxDir,
      getHostPids: () => [process.pid],
      onEvent: (env) => events.push(env),
    });

    try {
      const ready1 = await sup.startAndHandshake();
      const crashedPid = ready1.pid;
      expect(crashedPid).toBeGreaterThan(0);

      // Morte forçada do backend já pronto (não é shutdown normal).
      process.kill(crashedPid);

      await waitFor(() => sup.sessionFailure?.code === "backend_exited");
      expect(sup.currentSession).toBeNull();
      expect(sup.isRunning).toBe(false);

      const exitEvent = events.find(
        (env) =>
          env.type === "task.error" &&
          (env.payload as Record<string, unknown>).code === "backend_exited"
      );
      expect(exitEvent).toBeDefined();
      expect((exitEvent!.payload as Record<string, unknown>).session_id).toBe(ready1.session_id);

      const ready2 = await sup.restart();
      expect(ready2.session_id).toBeTruthy();
      expect(ready2.session_id).not.toBe(ready1.session_id);
      expect(ready2.pid).not.toBe(crashedPid);
      expect(sup.sessionFailure).toBeNull();
    } finally {
      await sup.shutdown().catch(() => undefined);
      fs.rmSync(sandboxDir, { recursive: true, force: true });
    }
  }, 30000);
});

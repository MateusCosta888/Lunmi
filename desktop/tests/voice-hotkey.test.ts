/**
 * VOICE REFINEMENTS V2 — supervisor do helper e fases hold (`desktop/electron/voice-hotkey.ts`).
 *
 * Filho falso, sem hook/Electron/GUI/áudio. Fixa: spawn canónico com ambiente mínimo, `ready`
 * antes de qualquer fase, pressed/released uma vez, cancelamento por extra/injected/pause,
 * release só depois de pressed, falha/EOF/timeout sem release, configure/pause/resume e o
 * coordenador que publica `VoiceInputHoldPhaseDto` com `press_id` monotónico sem chamar
 * start/finish/chat.send.
 */
import { EventEmitter } from "events";
import * as path from "path";
import { describe, expect, it, vi } from "vitest";
import {
  helperEnvironment,
  VoiceHoldCoordinator,
  VoiceHotkeyObserver,
  type VoiceHotkeyChild,
} from "../electron/voice-hotkey";
import type { VoiceInputHoldPhaseDto } from "../src/shared/types";

class FakeChild extends EventEmitter {
  stdin: EventEmitter & {
    writes: string[];
    write: ReturnType<typeof vi.fn>;
    end: ReturnType<typeof vi.fn>;
  };
  stdout = new EventEmitter();
  stderr = new EventEmitter();
  kill = vi.fn(() => true);
  pid = 123;
  exitCode: number | null = null;

  constructor() {
    super();
    const stdin = new EventEmitter() as FakeChild["stdin"];
    stdin.writes = [];
    stdin.write = vi.fn((chunk: string) => {
      stdin.writes.push(chunk);
      return true;
    });
    stdin.end = vi.fn();
    this.stdin = stdin;
  }
}

function harness(options: { readyTimeoutMs?: number } = {}) {
  const children: FakeChild[] = [];
  const spawnArgs: Array<{ command: string; args: string[]; options: any }> = [];
  const phases: Array<{ phase: string; reason: string }> = [];
  const failures: number[] = [];
  const observer = new VoiceHotkeyObserver({
    python: "C:/runtime/python.exe",
    cwd: "C:/repo/app",
    readyTimeoutMs: options.readyTimeoutMs ?? 1000,
    environment: { PATH: "C:/bin", LLM_API_KEY: "segredo" },
    spawnImpl: (command, args, spawnOptions) => {
      spawnArgs.push({ command, args, options: spawnOptions });
      const child = new FakeChild();
      children.push(child);
      return child as unknown as VoiceHotkeyChild;
    },
    onPhase: (phase, reason) => phases.push({ phase, reason }),
    onFailure: () => failures.push(1),
  });
  return { observer, children, spawnArgs, phases, failures };
}

function emit(child: FakeChild, value: Record<string, unknown>): void {
  child.stdout.emit("data", Buffer.from(`${JSON.stringify(value)}\n`, "utf8"));
}

async function ready(h: ReturnType<typeof harness>, accelerator = "Ctrl+Alt"): Promise<void> {
  const pending = h.observer.arm(accelerator, false);
  emit(h.children[0], { type: "ready", protocol: 1 });
  expect(await pending).toBe(true);
}

describe("VoiceHotkeyObserver — spawn e ready", () => {
  it("arranca o módulo canónico com ambiente mínimo, sem chaves", async () => {
    const h = harness();
    const pending = h.observer.arm("Alt+Ctrl", false);
    expect(h.spawnArgs).toHaveLength(1);
    const spawn = h.spawnArgs[0];
    expect(spawn.command).toBe("C:/runtime/python.exe");
    expect(spawn.args).toEqual([
      "-u",
      path.resolve("C:/repo/app", "app", "voice", "hotkey_worker.py"),
      "--accelerator",
      "Ctrl+Alt",
    ]);
    expect(spawn.options.cwd).toBe("C:/repo/app");
    expect(spawn.options.shell).toBe(false);
    expect(spawn.options.windowsHide).toBe(true);
    expect(spawn.options.env.PATH).toBe("C:/bin");
    expect(spawn.options.env.LLM_API_KEY).toBeUndefined();
    expect(spawn.options.env.PYTHONIOENCODING).toBe("utf-8");
    emit(h.children[0], { type: "ready", protocol: 1 });
    expect(await pending).toBe(true);
    expect(h.observer.isReady()).toBe(true);
  });

  it("helperEnvironment não transporta credenciais e define UTF-8", () => {
    const env = helperEnvironment({
      PATH: "C:/bin",
      SystemRoot: "C:/Windows",
      OPENAI_API_KEY: "sk-segredo",
      LLM_MODEL: "x",
      TEMP: "C:/tmp",
    });
    expect(env.PATH).toBe("C:/bin");
    expect(env.SystemRoot).toBe("C:/Windows");
    expect(env.TEMP).toBe("C:/tmp");
    expect(env.OPENAI_API_KEY).toBeUndefined();
    expect(env.LLM_MODEL).toBeUndefined();
    expect(env.PYTHONUTF8).toBe("1");
  });

  it("fases antes de ready são ignoradas", async () => {
    const h = harness();
    const pending = h.observer.arm("Ctrl+Alt", false);
    emit(h.children[0], { type: "pressed" });
    emit(h.children[0], { type: "released" });
    expect(h.phases).toEqual([]);
    emit(h.children[0], { type: "ready", protocol: 1 });
    await pending;
    emit(h.children[0], { type: "pressed" });
    expect(h.phases).toEqual([{ phase: "pressed", reason: "" }]);
  });

  it("protocolo errado/linha desconhecida falham fechado", async () => {
    const h = harness();
    const pending = h.observer.arm("Ctrl+Alt", false);
    emit(h.children[0], { type: "ready", protocol: 2 });
    expect(await pending).toBe(false);
    expect(h.failures).toHaveLength(1);
    expect(h.observer.isReady()).toBe(false);
  });
});

describe("VoiceHotkeyObserver — fases", () => {
  it("pressed/released uma vez; repetição e release sem pressed são ignorados", async () => {
    const h = harness();
    await ready(h);
    emit(h.children[0], { type: "pressed" });
    emit(h.children[0], { type: "pressed" });
    emit(h.children[0], { type: "released" });
    emit(h.children[0], { type: "released" });
    expect(h.phases).toEqual([
      { phase: "pressed", reason: "" },
      { phase: "released", reason: "" },
    ]);
  });

  it("cancelled por tecla extra/injected não vira release", async () => {
    const h = harness();
    await ready(h);
    emit(h.children[0], { type: "pressed" });
    emit(h.children[0], { type: "cancelled", reason: "extra_key" });
    emit(h.children[0], { type: "released" });
    expect(h.phases).toEqual([
      { phase: "pressed", reason: "" },
      { phase: "cancelled", reason: "extra_key" },
    ]);
  });

  it("cancelled com reason inválido falha sem publicar release", async () => {
    const h = harness();
    await ready(h);
    emit(h.children[0], { type: "pressed" });
    emit(h.children[0], { type: "cancelled", reason: "tecla" });
    expect(h.phases).toEqual([
      { phase: "pressed", reason: "" },
      { phase: "cancelled", reason: "hook_lost" },
    ]);
    expect(h.failures).toHaveLength(1);
  });

  it("cancelled sem reason (vazio) também falha fechado, nunca como release", async () => {
    const h = harness();
    await ready(h);
    emit(h.children[0], { type: "pressed" });
    emit(h.children[0], { type: "cancelled", reason: "" });
    expect(h.phases).toEqual([
      { phase: "pressed", reason: "" },
      { phase: "cancelled", reason: "hook_lost" },
    ]);
    expect(h.failures).toHaveLength(1);
  });

  it("pause/resume escrevem o protocolo e cancelam a fase ativa", async () => {
    const h = harness();
    await ready(h);
    emit(h.children[0], { type: "pressed" });
    expect(h.observer.pause()).toBe(true);
    expect(h.children[0].stdin.writes).toContain('{"type":"pause"}\n');
    expect(h.phases).toEqual([
      { phase: "pressed", reason: "" },
      { phase: "cancelled", reason: "pause" },
    ]);
    emit(h.children[0], { type: "cancelled", reason: "pause" });
    expect(h.observer.resume()).toBe(true);
    expect(h.children[0].stdin.writes).toContain('{"type":"resume"}\n');
    expect(h.phases).toHaveLength(2);
  });

  it("configure escreve o canónico sem reiniciar o filho", async () => {
    const h = harness();
    await ready(h);
    expect(await h.observer.reconfigure("Ctrl+V", false)).toBe(true);
    expect(h.children).toHaveLength(1);
    expect(h.children[0].stdin.writes).toContain(
      '{"type":"configure","accelerator":"Ctrl+V"}\n'
    );
    emit(h.children[0], { type: "pressed" });
    expect(h.phases).toEqual([{ phase: "pressed", reason: "" }]);
  });
});

describe("VoiceHotkeyObserver — falha, EOF e timeout", () => {
  it("saída inesperada com gesto ativo cancela hook_lost e não liberta release", async () => {
    const h = harness();
    await ready(h);
    emit(h.children[0], { type: "pressed" });
    h.children[0].emit("exit", 1, null);
    expect(h.phases).toEqual([
      { phase: "pressed", reason: "" },
      { phase: "cancelled", reason: "hook_lost" },
    ]);
    expect(h.failures).toHaveLength(1);
    expect(h.observer.isReady()).toBe(false);
  });

  it("EOF de stdout e linha error falham fechado", async () => {
    const h = harness();
    await ready(h);
    h.children[0].stdout.emit("end");
    expect(h.failures).toHaveLength(1);

    const h2 = harness();
    await ready(h2);
    emit(h2.children[0], { type: "error", code: "hook_unavailable" });
    expect(h2.failures).toHaveLength(1);
    expect(h2.phases).toEqual([]);
  });

  it("timeout de ready devolve false, mata o filho e não sintetiza released", async () => {
    const h = harness({ readyTimeoutMs: 10 });
    const pending = h.observer.arm("Ctrl+Alt", false);
    expect(await pending).toBe(false);
    expect(h.failures).toHaveLength(1);
    expect(h.children[0].kill).toHaveBeenCalled();
    expect(h.phases).toEqual([]);
  });

  it("disarm envia shutdown/EOF e não publica fase", async () => {
    const h = harness();
    await ready(h);
    h.observer.disarm();
    expect(h.children[0].stdin.writes).toContain('{"type":"shutdown"}\n');
    expect(h.children[0].stdin.end).toHaveBeenCalled();
    expect(h.phases).toEqual([]);
    expect(h.observer.isReady()).toBe(false);
  });

  it("erro tardio do filho anterior não derruba o filho atual", async () => {
    const h = harness();
    await ready(h);
    const previous = h.children[0];
    h.observer.disarm();
    const pending = h.observer.arm("Ctrl+V", false);
    const current = h.children[1];
    emit(current, { type: "ready", protocol: 1 });
    expect(await pending).toBe(true);
    previous.emit("error", new Error("pipe antigo"));
    expect(h.observer.isReady()).toBe(true);
    expect(h.failures).toEqual([]);
  });
});

describe("VoiceHoldCoordinator — fases sanitizadas", () => {
  function coordinator(session: string | null = "sess-1", quitting = false) {
    const published: VoiceInputHoldPhaseDto[] = [];
    const cancel = vi.fn();
    const show = vi.fn();
    const coordinator = new VoiceHoldCoordinator({
      getSessionId: () => session,
      isQuitting: () => quitting,
      publish: phase => published.push(phase),
      cancelCapture: cancel,
      showMain: show,
    });
    return { coordinator, published, cancel, show };
  }

  it("pressed/released com o mesmo press_id; release válido mostra a janela", () => {
    const c = coordinator();
    c.coordinator.onPhase("pressed", "");
    c.coordinator.onPhase("released", "");
    expect(c.published).toEqual([
      { session_id: "sess-1", press_id: 1, phase: "pressed", reason: "" },
      { session_id: "sess-1", press_id: 1, phase: "released", reason: "" },
    ]);
    expect(c.show).toHaveBeenCalledTimes(1);
    expect(c.cancel).not.toHaveBeenCalled();
  });

  it("release/cancel sem pressed e repeat de pressed são ignorados", () => {
    const c = coordinator();
    c.coordinator.onPhase("released", "");
    c.coordinator.onPhase("cancelled", "extra_key");
    expect(c.published).toEqual([]);
    expect(c.cancel).not.toHaveBeenCalled();

    c.coordinator.onPhase("pressed", "");
    c.coordinator.onPhase("pressed", "");
    expect(c.published).toHaveLength(1);
  });

  it("cancelled publica reason e cancela a captura imediatamente", () => {
    const c = coordinator();
    c.coordinator.onPhase("pressed", "");
    c.coordinator.onPhase("cancelled", "extra_key");
    expect(c.published[1]).toEqual({
      session_id: "sess-1",
      press_id: 1,
      phase: "cancelled",
      reason: "extra_key",
    });
    expect(c.cancel).toHaveBeenCalledTimes(1);
    expect(c.show).not.toHaveBeenCalled();
  });

  it("sem sessão ou em quit não publica pressed", () => {
    const noSession = coordinator(null);
    noSession.coordinator.onPhase("pressed", "");
    expect(noSession.published).toEqual([]);

    const quitting = coordinator("sess-1", true);
    quitting.coordinator.onPhase("pressed", "");
    expect(quitting.published).toEqual([]);
  });

  it("press_id cresce por gesto e sessionChanged cancela o gesto antigo", () => {
    const c = coordinator("sess-old");
    c.coordinator.onPhase("pressed", "");
    c.coordinator.onPhase("released", "");
    c.coordinator.onPhase("pressed", "");
    expect(c.published[2].press_id).toBe(2);
    c.coordinator.sessionChanged();
    expect(c.published[3]).toEqual({
      session_id: "sess-old",
      press_id: 2,
      phase: "cancelled",
      reason: "lifecycle",
    });
    expect(c.cancel).toHaveBeenCalledTimes(1);
  });

  it("cancel sem gesto ativo não cancela nem publica", () => {
    const c = coordinator();
    c.coordinator.cancel("pause");
    expect(c.published).toEqual([]);
    expect(c.cancel).not.toHaveBeenCalled();
  });
});

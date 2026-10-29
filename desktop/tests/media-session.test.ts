import { EventEmitter } from "node:events";
import { readFileSync } from "node:fs";
import { PassThrough } from "node:stream";
import { describe, expect, it } from "vitest";
import type { MediaCommandInput, MediaStateDto } from "../src/shared/media";
import {
  MEDIA_SESSION_MAX_SCRIPT_CHARS,
  WindowsMediaSession,
  type MediaSessionProcess,
  type MediaSessionSpawn,
} from "../electron/media-session";

class FakeChild implements MediaSessionProcess {
  readonly stdin = new PassThrough();
  readonly stdout = new PassThrough();
  readonly stderr = new PassThrough();
  readonly requests: Array<Record<string, unknown>> = [];
  killed = 0;
  exitCode: number | null = null;
  private readonly events = new EventEmitter();

  constructor() {
    this.stdin.on("data", (chunk: Buffer) => {
      for (const line of chunk.toString("utf8").split("\n")) {
        if (line.trim()) this.requests.push(JSON.parse(line) as Record<string, unknown>);
      }
    });
  }

  on(event: string, listener: (...args: any[]) => void): unknown {
    this.events.on(event, listener);
    return this;
  }

  kill(): boolean {
    this.killed += 1;
    this.exitCode = 0;
    this.events.emit("exit", 0, null);
    return true;
  }

  ready(): void {
    this.stdout.write(`${JSON.stringify({ v: 1, type: "ready" })}\n`);
  }

  send(value: unknown): void {
    this.stdout.write(`${typeof value === "string" ? value : JSON.stringify(value)}\n`);
  }

  exit(): void {
    this.exitCode = 0;
    this.events.emit("exit", 0, null);
  }

  fail(): void { this.events.emit("error", new Error("late helper error")); }
}

interface HarnessOptions {
  autoReady?: boolean;
  script?: string;
  requestTimeoutMs?: number;
  startTimeoutMs?: number;
  maxLineBytes?: number;
  readScript?: (path: string) => string;
}

function harness(options: HarnessOptions = {}) {
  let child: FakeChild | null = null;
  let spawns = 0;
  let lastArgs: string[] = [];
  let lastOptions: Parameters<MediaSessionSpawn>[2] | null = null;

  const spawn: MediaSessionSpawn = (_command, args, opts) => {
    spawns += 1;
    lastArgs = args;
    lastOptions = opts;
    child = new FakeChild();
    if (options.autoReady !== false) queueMicrotask(() => child?.ready());
    return child;
  };

  const session = new WindowsMediaSession({
    scriptPath: "/virtual/media-session.ps1",
    readScript: options.readScript ?? (() => options.script ?? "Write-Output ready"),
    spawn,
    requestTimeoutMs: options.requestTimeoutMs ?? 150,
    startTimeoutMs: options.startTimeoutMs ?? 150,
    maxLineBytes: options.maxLineBytes,
  });

  return {
    session,
    spawns: () => spawns,
    child: () => child,
    lastArgs: () => lastArgs,
    lastOptions: () => lastOptions,
  };
}

function state(overrides: Partial<MediaStateDto> = {}): MediaStateDto {
  return {
    v: 1,
    available: true,
    session_id: "m-abc",
    title: "Track",
    artist: "Artist",
    source_app: "fixture.exe",
    playback: "playing",
    can_play: true,
    can_pause: true,
    can_previous: true,
    can_next: true,
    ...overrides,
  };
}

function reply(id: unknown, value: MediaStateDto, ok = true, error: string | null = null) {
  return { v: 1, id, ok, error, state: value };
}

async function waitForRequest(
  getChild: () => FakeChild | null,
  count = 1
): Promise<Record<string, unknown>> {
  const deadline = Date.now() + 1000;
  while (Date.now() < deadline) {
    const child = getChild();
    if (child && child.requests.length >= count) return child.requests[count - 1];
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error("helper request not received");
}

describe("media-session.ps1", () => {
  it("cabe no limite do helper: acima dele o leitor ficaria vazio sem erro visível", () => {
    const script = readFileSync("electron/media-session.ps1", "utf8");
    expect(script.length).toBeLessThanOrEqual(MEDIA_SESSION_MAX_SCRIPT_CHARS);
  });
});

describe("WindowsMediaSession", () => {
  it.each(["stdout", "stdin", "process"])("ignora %s tardio do helper anterior depois de um timeout", async source => {
    const h = harness({ requestTimeoutMs: 50 });
    const first = h.session.get();
    await waitForRequest(h.child);
    const previous = h.child()!;
    await first;

    const second = h.session.get();
    const request = await waitForRequest(() => h.spawns() === 2 ? h.child() : null);
    const current = h.child()!;
    if (source === "stdout") previous.send("invalid old stdout");
    else if (source === "stdin") previous.stdin.emit("error", new Error("late EPIPE"));
    else previous.fail();
    current.send(reply(request.id, state()));

    await expect(second).resolves.toMatchObject({ available: true, title: "Track" });
    expect(current.killed).toBe(0);
    await h.session.dispose();
  });

  it("arranca lazy, usa EncodedCommand fixo e devolve estado real", async () => {
    const h = harness({ script: "$x = 1" });
    expect(h.spawns()).toBe(0);

    const pending = h.session.get();
    const request = await waitForRequest(h.child);
    expect(request).toMatchObject({ v: 1, type: "get" });
    h.child()!.send(reply(request.id, state()));

    await expect(pending).resolves.toMatchObject({
      available: true,
      session_id: "m-abc",
      title: "Track",
      can_next: true,
    });
    expect(h.spawns()).toBe(1);
    expect(h.lastArgs().slice(0, 4)).toEqual([
      "-NoLogo",
      "-NoProfile",
      "-NonInteractive",
      "-EncodedCommand",
    ]);
    expect(Buffer.from(h.lastArgs()[4], "base64").toString("utf16le")).toBe("$x = 1");
    expect(h.lastArgs()).not.toContain("-ExecutionPolicy");
    expect(h.lastOptions()).toMatchObject({
      shell: false,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
    });
  });

  it("não herda segredos do ambiente do Electron", async () => {
    const previous = process.env.LUNMI_TEST_SECRET;
    process.env.LUNMI_TEST_SECRET = "do-not-pass";
    try {
      const h = harness();
      const pending = h.session.get();
      const request = await waitForRequest(h.child);
      h.child()!.send(reply(request.id, state()));
      await pending;
      expect(h.lastOptions()!.env).not.toHaveProperty("LUNMI_TEST_SECRET");
    } finally {
      if (previous === undefined) delete process.env.LUNMI_TEST_SECRET;
      else process.env.LUNMI_TEST_SECRET = previous;
    }
  });

  it("recusa script acima do limite sem spawn", async () => {
    const h = harness({ script: "x".repeat(MEDIA_SESSION_MAX_SCRIPT_CHARS + 1) });
    await expect(h.session.get()).resolves.toMatchObject({ available: false });
    expect(h.spawns()).toBe(0);
  });

  it("devolve estado vazio quando o script não pode ser lido", async () => {
    const h = harness({
      readScript: () => {
        throw new Error("missing script");
      },
    });
    await expect(h.session.get()).resolves.toMatchObject({ available: false });
    expect(h.spawns()).toBe(0);
  });

  it("devolve estado vazio quando o helper não faz ready", async () => {
    const h = harness({ autoReady: false, startTimeoutMs: 30 });
    await expect(h.session.get()).resolves.toMatchObject({ available: false });
    expect(h.child()!.killed).toBeGreaterThan(0);
  });

  it("devolve estado vazio e mata o helper no timeout de pedido", async () => {
    const h = harness({ requestTimeoutMs: 30 });
    const pending = h.session.get();
    await waitForRequest(h.child);
    await expect(pending).resolves.toMatchObject({ available: false });
    expect(h.child()!.killed).toBeGreaterThan(0);
  });

  it("não resolve um pedido com id errado", async () => {
    const h = harness({ requestTimeoutMs: 30 });
    const pending = h.session.get();
    await waitForRequest(h.child);
    h.child()!.send(reply("other-request", state()));
    await expect(pending).resolves.toMatchObject({ available: false });
  });

  it("falha de forma fechada com JSON malformado", async () => {
    const h = harness();
    const pending = h.session.get();
    await waitForRequest(h.child);
    h.child()!.send("{");
    await expect(pending).resolves.toMatchObject({ available: false });
    expect(h.child()!.killed).toBeGreaterThan(0);
  });

  it("rejeita linha acima do limite", async () => {
    const h = harness({ maxLineBytes: 256 });
    const pending = h.session.get();
    const request = await waitForRequest(h.child);
    h.child()!.send({
      v: 1,
      id: request.id,
      ok: true,
      error: null,
      state: state(),
      pad: "x".repeat(300),
    });
    await expect(pending).resolves.toMatchObject({ available: false });
    expect(h.child()!.killed).toBeGreaterThan(0);
  });

  it("devolve estado vazio quando o helper termina com pedido pendente", async () => {
    const h = harness();
    const pending = h.session.get();
    await waitForRequest(h.child);
    h.child()!.exit();
    await expect(pending).resolves.toMatchObject({ available: false });
  });

  it("envia comando fechado com o token mostrado", async () => {
    const h = harness();
    const pending = h.session.command({ command: "next", session_id: "m-abc" });
    const request = await waitForRequest(h.child);
    expect(request).toMatchObject({
      v: 1,
      type: "command",
      command: "next",
      session_id: "m-abc",
    });
    h.child()!.send(reply(request.id, state({ title: "Next", session_id: "m-next" })));
    await expect(pending).resolves.toMatchObject({
      ok: true,
      state: { title: "Next" },
    });
  });

  it("relê depois do comando até o Windows mostrar o efeito, sem repetir o comando", async () => {
    const h = harness();
    const pending = h.session.command({ command: "pause", session_id: "m-abc" });
    const command = await waitForRequest(h.child);
    // O Windows ainda não aplicou a pausa quando o Try* responde.
    h.child()!.send(reply(command.id, state({ playback: "playing" })));
    const reread = await waitForRequest(h.child, 2);
    expect(reread).toMatchObject({ type: "get" });
    h.child()!.send(reply(reread.id, state({ playback: "paused" })));
    await expect(pending).resolves.toMatchObject({ ok: true, state: { playback: "paused" } });
    expect(h.child()!.requests.filter(r => r.type === "command")).toHaveLength(1);
  });

  it("uma releitura falhada mantém o comando aceite", async () => {
    const h = harness();
    const pending = h.session.command({ command: "play", session_id: "m-abc" });
    const command = await waitForRequest(h.child);
    h.child()!.send(reply(command.id, state({ playback: "paused" })));
    await expect(pending).resolves.toMatchObject({ ok: true });
  });

  it("propaga stale_session sem retarget silencioso", async () => {
    const h = harness();
    const pending = h.session.command({ command: "play", session_id: "m-abc" });
    const request = await waitForRequest(h.child);
    h.child()!.send(reply(request.id, state({ session_id: "m-new" }), false, "stale_session"));
    await expect(pending).resolves.toMatchObject({
      ok: false,
      error: "stale_session",
      state: { session_id: "m-new" },
    });
  });

  it("propaga capability_unavailable com o estado atual", async () => {
    const h = harness();
    const pending = h.session.command({ command: "next", session_id: "m-abc" });
    const request = await waitForRequest(h.child);
    h.child()!.send(reply(request.id, state({ can_next: false }), false, "capability_unavailable"));
    await expect(pending).resolves.toMatchObject({
      ok: false,
      error: "capability_unavailable",
      state: { can_next: false },
    });
  });

  it("limita metadados e descarta campos fora do contrato", async () => {
    const h = harness();
    const pending = h.session.get();
    const request = await waitForRequest(h.child);
    const raw = {
      ...state({
        title: "t".repeat(500),
        artist: "a\nb",
        source_app: "s".repeat(200),
      }),
      cover_url: "https://example.test/cover.jpg",
    };
    h.child()!.send({ v: 1, id: request.id, ok: true, error: null, state: raw });

    const result = await pending;
    expect(result.title).toHaveLength(160);
    expect(result.artist).toBe("a b");
    expect(result.source_app).toHaveLength(120);
    expect("cover_url" in result).toBe(false);
  });

  it("rejeita estado disponível sem token de sessão", async () => {
    const h = harness();
    const pending = h.session.get();
    const request = await waitForRequest(h.child);
    h.child()!.send(reply(request.id, state({ available: true, session_id: null })));
    await expect(pending).resolves.toMatchObject({ available: false, session_id: null });
  });

  it("recusa input de comando inválido antes de arrancar o helper", async () => {
    const h = harness();
    const result = await h.session.command({
      command: "seek" as unknown as MediaCommandInput["command"],
      session_id: "m-abc",
    });
    expect(result).toMatchObject({ ok: false, error: "invalid_input", state: { available: false } });
    expect(h.spawns()).toBe(0);
  });

  it("serializa pedidos: o segundo só sai depois da resposta do primeiro", async () => {
    const h = harness();
    const first = h.session.command({ command: "play", session_id: "m-abc" });
    const firstRequest = await waitForRequest(h.child);

    const second = h.session.command({ command: "pause", session_id: "m-abc" });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(h.child()!.requests).toHaveLength(1);

    h.child()!.send(reply(firstRequest.id, state()));
    await first;
    const secondRequest = await waitForRequest(h.child, 2);
    h.child()!.send(reply(secondRequest.id, state({ playback: "paused" })));
    await expect(second).resolves.toMatchObject({ ok: true, state: { playback: "paused" } });
  });

  it("dispose mata o processo, falha pendências e impede novo arranque", async () => {
    const h = harness();
    const pending = h.session.get();
    await waitForRequest(h.child);
    const child = h.child()!;

    const disposing = h.session.dispose();
    await expect(pending).resolves.toMatchObject({ available: false });
    await disposing;
    expect(child.killed).toBeGreaterThan(0);
    await expect(h.session.get()).resolves.toMatchObject({ available: false });
  });
});

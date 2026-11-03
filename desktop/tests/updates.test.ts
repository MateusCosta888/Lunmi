import { afterEach, describe, expect, it, vi } from "vitest";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import type { spawn as nodeSpawn } from "node:child_process";
import { LunmiUpdates } from "../electron/updates";

class Process extends EventEmitter {
  stdout = new PassThrough();
  kill = vi.fn(() => true);
  unref = vi.fn();
}
function fixture(options: Partial<ConstructorParameters<typeof LunmiUpdates>[0]> = {}) {
  const children: Process[] = [];
  const spawn = vi.fn((_command: string, _args: string[], _options: unknown) => {
    const child = new Process(); children.push(child);
    queueMicrotask(() => child.emit("spawn"));
    return child;
  });
  const blocked = vi.fn(() => false), publish = vi.fn(), finish = vi.fn(async () => {});
  const controller = new LunmiUpdates({ version: "1.0.0-beta.1", launcher: "C:\\Lunmi\\Lunmi.exe",
    spawn: spawn as unknown as typeof nodeSpawn, blocked, publish, finish, ...options });
  return { controller, spawn, children, blocked, publish, finish };
}
type Fixture = ReturnType<typeof fixture>;
const version = "1.0.0-beta.2";
function send(child: Process, message: unknown, exit = 0) {
  child.stdout.write(JSON.stringify(message) + "\n"); child.emit("close", exit);
}
async function offer(f: Fixture) {
  const promise = f.controller.check();
  send(f.children.at(-1)!, { phase: "available", version, notes: "Novidades <texto simples>" });
  return promise;
}
async function stage(f: Fixture) {
  await offer(f);
  const promise = f.controller.install();
  send(f.children.at(-1)!, { phase: "ready", version });
  return promise;
}
afterEach(() => { vi.useRealTimers(); });

describe("beta launcher updates", () => {
  it("offers an update by argv without shell, payload downloads or private CLI fields", async () => {
    const f = fixture(); const checking = f.controller.check();
    expect(f.spawn.mock.calls[0]).toEqual(["C:\\Lunmi\\Lunmi.exe", ["--check"], expect.objectContaining({
      windowsHide: true, shell: false, stdio: ["ignore", "pipe", "ignore"],
    })]);
    send(f.children[0], { phase: "available", version, notes: "<b>plain text</b>", url: "secret URL", path: "private path" });
    expect(await checking).toEqual({ phase: "available", currentVersion: "1.0.0-beta.1", version,
      notes: "<b>plain text</b>", error: undefined, percent: undefined });
    expect(f.spawn).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(f.publish.mock.calls)).not.toContain("secret URL");
    expect(f.finish).not.toHaveBeenCalled();
    expect((await f.controller.check()).phase).toBe("available");
    expect(f.spawn).toHaveBeenCalledTimes(1);
  });

  it("coalesces checks and handles idle plus a final record without newline", async () => {
    const f = fixture(); const a = f.controller.check(), b = f.controller.check();
    expect(a).toBe(b);
    f.children[0].stdout.write('{"phase":"idle"}'); f.children[0].emit("close", 0);
    expect(await a).toMatchObject({ phase: "idle" });
    expect(f.spawn).toHaveBeenCalledTimes(1);
  });

  it.each(["0.9.0", "1.0.0-beta.0", "1.0.0-beta.1", "1.0.0-alpha.2", "../evil", "01.0.0", "v1.0.1"])("rejects an invalid or non-new version %s", async candidate => {
    const f = fixture(); const promise = f.controller.check();
    send(f.children[0], { phase: "available", version: candidate });
    expect(await promise).toMatchObject({ phase: "error", error: "internal" });
    expect(f.finish).not.toHaveBeenCalled();
  });

  it("allows the stable release after a beta", async () => {
    const f = fixture(); const promise = f.controller.check();
    send(f.children[0], { phase: "available", version: "1.0.0" });
    expect(await promise).toMatchObject({ phase: "available", version: "1.0.0" });
  });

  it("stages immutable files while tasks run and never restarts on download completion", async () => {
    const f = fixture(); f.blocked.mockReturnValue(true); await offer(f);
    const a = f.controller.install(), b = f.controller.install();
    expect(a).toBe(b);
    expect(f.spawn.mock.calls[1][1]).toEqual(["--stage", version]);
    f.children[1].stdout.write('{"phase":"downloading","percent":42.4}\n');
    expect(f.controller.get()).toMatchObject({ phase: "downloading", percent: 42 });
    f.children[1].stdout.write('{"phase":"installing","percent":88}\n');
    expect(f.controller.get()).toMatchObject({ phase: "downloading", percent: 88 });
    f.children[1].stdout.write(JSON.stringify({ phase: "ready", version }) + "\n");
    expect(f.controller.get().phase).toBe("downloading");
    f.children[1].emit("close", 0);
    expect(await a).toMatchObject({ phase: "ready", percent: 100 });
    expect(f.finish).not.toHaveBeenCalled();
    expect(f.spawn).toHaveBeenCalledTimes(2);
    expect(await f.controller.install()).toMatchObject({ phase: "ready", error: "busy" });
    expect(f.spawn).toHaveBeenCalledTimes(2);
  });

  it("activates only on a second explicit call, waits for this Electron PID, then quits", async () => {
    const f = fixture(); await stage(f);
    const installing = f.controller.install();
    expect(f.finish).not.toHaveBeenCalled();
    expect(f.spawn.mock.calls[2]).toEqual(["C:\\Lunmi\\Lunmi.exe",
      ["--activate", version, "--wait-pid", String(process.pid)], expect.objectContaining({
        shell: false, windowsHide: true, detached: true, stdio: "ignore",
      })]);
    expect(await installing).toMatchObject({ phase: "installing" });
    expect(f.children[2].unref).toHaveBeenCalledTimes(1);
    expect(f.finish).toHaveBeenCalledTimes(1);
  });

  it("rechecks activity after optional confirmation", async () => {
    const confirmRestart = vi.fn(async () => true);
    const f = fixture({ confirmRestart }); await stage(f);
    confirmRestart.mockImplementationOnce(async () => { f.blocked.mockReturnValue(true); return true; });
    expect(await f.controller.install()).toMatchObject({ phase: "ready", error: "busy" });
    expect(f.finish).not.toHaveBeenCalled();
    expect(f.spawn).toHaveBeenCalledTimes(2);
  });

  it("keeps a staged update after Later and coalesces clicks", async () => {
    const confirmRestart = vi.fn(async () => false), f = fixture({ confirmRestart }); await stage(f);
    await Promise.all([f.controller.install(), f.controller.install()]);
    expect(f.controller.get()).toMatchObject({ phase: "ready", error: "cancelled" });
    expect(confirmRestart).toHaveBeenCalledTimes(1);
    expect(f.finish).not.toHaveBeenCalled();
  });

  it.each(["offline", "source", "signature", "hash", "space", "locked", "extract", "busy", "internal"])("projects only the known error code %s, with retry available", async error => {
    const f = fixture(); const promise = f.controller.check();
    send(f.children[0], { phase: "error", error, message: "private URL or path" }, 1);
    expect(await promise).toMatchObject({ phase: "error", error });
    expect(JSON.stringify(f.publish.mock.calls)).not.toContain("private URL");
    expect(await offer(f)).toMatchObject({ phase: "available" });
    expect(f.finish).not.toHaveBeenCalled();
  });

  it("hides unknown errors and never treats progress or nonzero exit as readiness", async () => {
    const f = fixture(); const first = f.controller.check();
    send(f.children[0], { phase: "error", error: "C:\\secret\\key" }, 1);
    expect(await first).toMatchObject({ error: "internal" });
    await offer(f); const preparing = f.controller.install();
    send(f.children.at(-1)!, { phase: "ready", version }, 1);
    expect(await preparing).toMatchObject({ phase: "error", error: "internal" });
    expect(f.finish).not.toHaveBeenCalled();
  });

  it.each([
    '{"phase":"ready","version":"9.9.9"}\n',
    '{"phase":"downloading","percent":101}\n',
    '{"phase":"downloading","percent":"42"}\n',
    '{"phase":"downloading","percent":null}\n',
    'not JSON\n',
    "{}\n",
    "x".repeat(65537),
  ])("refuses malformed staging output without activation", async output => {
    const f = fixture(); await offer(f); const preparing = f.controller.install();
    f.children[1].stdout.write(output); f.children[1].emit("close", 0);
    expect(await preparing).toMatchObject({ phase: "error", error: "internal" });
    expect(f.finish).not.toHaveBeenCalled();
  });

  it("decodes UTF-8 split across chunks, without splitting accented release notes", async () => {
    const f = fixture(); const checking = f.controller.check();
    const raw = Buffer.from(JSON.stringify({ phase: "available", version, notes: "corre\u00e7\u00e3o" }) + "\n");
    const cut = raw.indexOf(0xc3) + 1;
    f.children[0].stdout.write(raw.subarray(0, cut)); f.children[0].stdout.write(raw.subarray(cut));
    f.children[0].emit("close", 0);
    expect(await checking).toMatchObject({ notes: "corre\u00e7\u00e3o" });
  });

  it("ignores health-report failures and sends the detached report once after readiness", async () => {
    const f = fixture();
    expect(f.spawn).not.toHaveBeenCalled();
    f.spawn.mockImplementationOnce(() => {
      const child = new Process(); queueMicrotask(() => child.emit("error", Error("private failure"))); return child;
    });
    f.controller.markHealthy(); f.controller.markHealthy();
    await Promise.resolve(); await Promise.resolve();
    expect(f.spawn).toHaveBeenCalledTimes(1);
    expect(f.spawn.mock.calls[0][1]).toEqual(["--healthy", "1.0.0-beta.1"]);
    expect(f.controller.get().phase).toBe("idle");
  });

  it("never quits if the detached launcher cannot start", async () => {
    const f = fixture(); await stage(f);
    f.spawn.mockImplementationOnce(() => {
      const child = new Process(); queueMicrotask(() => child.emit("error", Error("cannot spawn"))); return child;
    });
    expect(await f.controller.install()).toMatchObject({ phase: "error", error: "internal" });
    expect(f.finish).not.toHaveBeenCalled();
  });

  it("kills a timed-out check and makes no retry or update effect", async () => {
    vi.useFakeTimers(); const f = fixture(); const checking = f.controller.check();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(await checking).toMatchObject({ phase: "error", error: "offline" });
    expect(f.children[0].kill).toHaveBeenCalledTimes(1);
    expect(f.spawn).toHaveBeenCalledTimes(1);
    expect(f.finish).not.toHaveBeenCalled();
    f.controller.dispose();
  });

  it("stops its pending CLI on dispose and emits no further UI events", async () => {
    const f = fixture(); const checking = f.controller.check(); const published = f.publish.mock.calls.length;
    f.controller.dispose(); await checking;
    expect(f.children[0].kill).toHaveBeenCalledTimes(1);
    expect(f.publish).toHaveBeenCalledTimes(published);
    await f.controller.check(); f.controller.markHealthy();
    expect(f.spawn).toHaveBeenCalledTimes(1);
  });

  it.each([null, "alpha"])("is disabled without a beta launcher (%s)", async mode => {
    const f = fixture(mode === null ? { launcher: null } : { version: "0.1.0-alpha.12" });
    expect(await f.controller.check()).toMatchObject({ phase: "disabled" });
    await f.controller.install(); f.controller.start(); f.controller.markHealthy(); f.controller.dispose();
    expect(f.spawn).not.toHaveBeenCalled(); expect(f.finish).not.toHaveBeenCalled();
  });

  it("checks on opening and later, but never starts downloading automatically", async () => {
    vi.useFakeTimers(); const f = fixture(); f.controller.start(); f.controller.start();
    await vi.advanceTimersByTimeAsync(3000);
    send(f.children[0], { phase: "idle" }); await Promise.resolve(); await Promise.resolve();
    await vi.advanceTimersByTimeAsync(6 * 60 * 60 * 1000);
    expect(f.spawn).toHaveBeenCalledTimes(2);
    expect(f.spawn.mock.calls.every(call => call[1][0] === "--check")).toBe(true);
    f.controller.dispose();
  });
});

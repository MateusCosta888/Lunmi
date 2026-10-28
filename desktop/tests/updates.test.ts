import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createHash, generateKeyPairSync, sign } from "node:crypto";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { EventEmitter } from "node:events";
import type { NsisUpdater } from "electron-updater";
import { LunmiUpdates, RELEASE_BASE } from "../electron/updates";
import { verifyDescriptor, verifyInstaller } from "../electron/update-signature";
import { fetchReleaseFile } from "../electron/update-download";

const keys = generateKeyPairSync("ed25519");
const publicKey = keys.publicKey.export({ format: "pem", type: "spki" }).toString();
const version = "0.1.0-alpha.5";
let directory: string;
let installer: string;
const payload = Buffer.from("test fixture installer; never executable");
const descriptor = { version, platform: "win32", arch: "x64", file: `Lunmi-${version}-Setup.exe`, size: payload.length, sha512: createHash("sha512").update(payload).digest("base64") };
let raw: Buffer;
let signature: Buffer;

function fixture(overrides: Partial<ConstructorParameters<typeof LunmiUpdates>[0]> = {}) {
  const updater = Object.assign(new EventEmitter(), {
    autoDownload: true, autoInstallOnAppQuit: true, allowDowngrade: true, allowPrerelease: false,
    checkForUpdates: vi.fn(async () => ({ isUpdateAvailable: true, updateInfo: { version, files: [{ url: descriptor.file, size: descriptor.size, sha512: descriptor.sha512 }] } })),
    downloadUpdate: vi.fn(async () => [installer]), quitAndInstall: vi.fn(),
  });
  const blocked = vi.fn(() => false);
  const confirmRestart = vi.fn(async () => true);
  const finish = vi.fn(async () => { updater.quitAndInstall(); });
  const fetch = vi.fn(async (url: string) => url.endsWith(".sig") ? signature : raw);
  const publish = vi.fn();
  const controller = new LunmiUpdates({ version: "0.1.0-alpha.4", updater: updater as unknown as NsisUpdater,
    publicKey, blocked, confirmRestart, finish, fetch, publish, ...overrides });
  return { controller, updater, blocked, confirmRestart, finish, fetch, publish };
}

beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), "lunmi-update-test-"));
  installer = join(directory, descriptor.file);
  await writeFile(installer, payload);
  raw = Buffer.from(JSON.stringify(descriptor));
  signature = sign(null, raw, keys.privateKey);
});
afterEach(async () => { await rm(directory, { recursive: true, force: true }); });

describe("signed updates", () => {
  it("verifies a valid descriptor and payload", async () => {
    expect(verifyDescriptor(raw, signature, publicKey, version)).toEqual(descriptor);
    await expect(verifyInstaller(installer, descriptor as never)).resolves.toBeUndefined();
  });
  it.each(["tampered", "wrong-key", "wrong-version", "short-signature", "oversized"])("rejects %s descriptors", kind => {
    const otherKey = generateKeyPairSync("ed25519").publicKey.export({ format: "pem", type: "spki" }).toString();
    expect(() => verifyDescriptor(kind === "tampered" ? Buffer.from("{}") : kind === "oversized" ? Buffer.alloc(20_000) : raw,
      kind === "short-signature" ? signature.subarray(1) : signature, kind === "wrong-key" ? otherKey : publicKey,
      kind === "wrong-version" ? "0.0.0" : version)).toThrow();
  });
  it("rejects signed data for another platform", () => {
    const value = Buffer.from(JSON.stringify({ ...descriptor, platform: "linux" }));
    expect(() => verifyDescriptor(value, sign(null, value, keys.privateKey), publicKey, version)).toThrow();
  });
  it("downloads only after verification and never installs on download", async () => {
    const f = fixture();
    expect(f.updater.autoDownload).toBe(false);
    expect(f.updater.autoInstallOnAppQuit).toBe(false);
    expect(f.updater.allowDowngrade).toBe(false);
    expect(await f.controller.check()).toMatchObject({ phase: "ready", version, percent: 100 });
    expect(f.fetch).toHaveBeenCalledWith(`${RELEASE_BASE}v${version}/lunmi-update.json`);
    expect(f.updater.downloadUpdate).toHaveBeenCalledTimes(1);
    expect(f.updater.quitAndInstall).not.toHaveBeenCalled();
  });
  it("does not download an unsigned update", async () => {
    signature = Buffer.alloc(64);
    const f = fixture();
    expect(await f.controller.check()).toMatchObject({ phase: "error", error: "verification" });
    expect(f.updater.downloadUpdate).not.toHaveBeenCalled();
  });
  it("rejects mismatched metadata or external download URLs", async () => {
    const f = fixture();
    f.updater.checkForUpdates.mockResolvedValueOnce({ isUpdateAvailable: true, updateInfo: { version, files: [{ url: "https://attacker.invalid/setup.exe", size: descriptor.size, sha512: descriptor.sha512 }] } });
    expect(await f.controller.check()).toMatchObject({ error: "verification" });
    expect(f.updater.downloadUpdate).not.toHaveBeenCalled();
  });
  it("coalesces concurrent checks", async () => {
    const f = fixture();
    await Promise.all([f.controller.check(), f.controller.check()]);
    expect(f.updater.checkForUpdates).toHaveBeenCalledTimes(1);
  });
  it("rejects unsigned metadata requesting administrator privileges", async () => {
    const f = fixture();
    const result = await f.updater.checkForUpdates();
    Object.assign(result.updateInfo.files[0], { isAdminRightsRequired: true });
    f.updater.checkForUpdates.mockResolvedValueOnce(result);
    expect(await f.controller.check()).toMatchObject({ phase: "error", error: "verification" });
    expect(f.updater.downloadUpdate).not.toHaveBeenCalled();
  });
  it("keeps chat usable on network error and permits retry", async () => {
    const f = fixture();
    f.fetch.mockRejectedValueOnce(new Error("secret URL should never reach renderer"));
    expect(await f.controller.check()).toMatchObject({ phase: "error", error: "unavailable" });
    expect(await f.controller.check()).toMatchObject({ phase: "ready" });
  });
  it("rejects a corrupt or interrupted download", async () => {
    await writeFile(installer, "incomplete");
    const f = fixture();
    expect(await f.controller.check()).toMatchObject({ phase: "error", error: "verification" });
    await f.controller.install();
    expect(f.finish).not.toHaveBeenCalled();
  });
  it("keeps installation disabled after disk failure and recovers on retry", async () => {
    const f = fixture();
    f.updater.downloadUpdate.mockRejectedValueOnce(Object.assign(new Error("disk full"), { code: "ENOSPC" }));
    expect(await f.controller.check()).toMatchObject({ phase: "error", error: "unavailable" });
    await f.controller.install();
    expect(f.finish).not.toHaveBeenCalled();
    expect(await f.controller.check()).toMatchObject({ phase: "ready" });
  });
  it("projects progress without marking it ready prematurely", async () => {
    const f = fixture();
    f.updater.downloadUpdate.mockImplementationOnce(async () => {
      f.updater.emit("download-progress", { percent: 42.4 });
      expect(f.controller.get()).toMatchObject({ phase: "downloading", percent: 42 });
      return [installer];
    });
    await f.controller.check();
  });
  it("revalidates cache before installation", async () => {
    const f = fixture();
    await f.controller.check();
    const changed = await readFile(installer); changed[0] ^= 1;
    await writeFile(installer, changed);
    expect(await f.controller.install()).toMatchObject({ phase: "error", error: "verification" });
    expect(f.finish).not.toHaveBeenCalled();
  });
  it("blocks active operations without showing a restart dialog", async () => {
    const f = fixture(); await f.controller.check(); f.blocked.mockReturnValue(true);
    expect(await f.controller.install()).toMatchObject({ phase: "ready", error: "busy" });
    expect(f.confirmRestart).not.toHaveBeenCalled();
  });
  it("rechecks activity after the dialog", async () => {
    const f = fixture(); await f.controller.check();
    f.confirmRestart.mockImplementationOnce(async () => { f.blocked.mockReturnValue(true); return true; });
    expect(await f.controller.install()).toMatchObject({ phase: "ready", error: "busy" });
    expect(f.finish).not.toHaveBeenCalled();
  });
  it("does not install after Later or simultaneous clicks", async () => {
    const f = fixture(); await f.controller.check(); f.confirmRestart.mockResolvedValue(false);
    await Promise.all([f.controller.install(), f.controller.install()]);
    expect(f.confirmRestart).toHaveBeenCalledTimes(1);
    expect(f.finish).not.toHaveBeenCalled();
    expect(f.controller.get()).toMatchObject({ phase: "ready", error: "cancelled" });
  });
  it("hands off only after explicit confirmation and verification", async () => {
    const f = fixture(); await f.controller.check(); await f.controller.install();
    expect(f.finish).toHaveBeenCalledTimes(1);
    expect(f.controller.get().phase).toBe("installing");
  });
  it("does nothing in development", async () => {
    const f = fixture({ updater: null });
    expect(await f.controller.check()).toMatchObject({ phase: "disabled" });
    await f.controller.install(); f.controller.start(); f.controller.dispose();
    expect(f.fetch).not.toHaveBeenCalled(); expect(f.finish).not.toHaveBeenCalled();
  });
  it.each(["http://github.com/a", "https://localhost/a", "https://github.com:444/a", "https://user:pass@github.com/a"])("rejects untrusted transport %s", async url => {
    await expect(fetchReleaseFile(url)).rejects.toThrow();
  });
});

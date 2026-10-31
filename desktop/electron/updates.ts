import type { NsisUpdater } from "electron-updater";
import type { UpdateState } from "../src/shared/updates";
import { verifyDescriptor, verifyInstaller, type ReleaseDescriptor } from "./update-signature";
import { fetchReleaseFile } from "./update-download";

export const RELEASE_BASE = "https://github.com/MateusCosta888/Lunmi/releases/download/";
const SIX_HOURS = 6 * 60 * 60 * 1000;

type Updater = Pick<NsisUpdater, "autoDownload" | "autoInstallOnAppQuit" | "allowDowngrade" | "allowPrerelease" |
  "on" | "checkForUpdates" | "downloadUpdate" | "quitAndInstall">;

export class LunmiUpdates {
  private state: UpdateState;
  private flight: Promise<UpdateState> | null = null;
  private installFlight: Promise<UpdateState> | null = null;
  private descriptor: ReleaseDescriptor | null = null;
  private installer: string | null = null;
  private startTimer?: ReturnType<typeof setTimeout>;
  private interval?: ReturnType<typeof setInterval>;

  constructor(private readonly options: {
    version: string;
    updater: Updater | null;
    publicKey: string;
    publish: (state: UpdateState) => void;
    blocked: () => boolean;
    /** Optional native confirmation; the renderer modal already asks before `install()`. */
    confirmRestart?: () => Promise<boolean>;
    finish: () => Promise<void>;
    fetch?: typeof fetchReleaseFile;
  }) {
    this.state = { phase: options.updater ? "idle" : "disabled", currentVersion: options.version };
    if (options.updater) {
      options.updater.autoDownload = false; // Signed descriptor must match before any payload download.
      options.updater.autoInstallOnAppQuit = false;
      options.updater.allowDowngrade = false;
      options.updater.allowPrerelease = options.version.includes("-");
      options.updater.on("error", () => { /* Errors are projected by the awaited operation, without URLs or local paths. */ });
      options.updater.on("download-progress", progress => {
        if (this.state.phase === "downloading") this.set({ percent: Math.max(0, Math.min(100, Math.round(progress.percent))) });
      });
    }
  }

  get(): UpdateState { return { ...this.state }; }
  get installing(): boolean { return this.state.phase === "installing"; }

  private set(patch: Partial<UpdateState>): UpdateState {
    this.state = { ...this.state, ...patch };
    this.options.publish(this.get());
    return this.get();
  }

  start(): void {
    if (!this.options.updater || this.startTimer || this.interval) return;
    // Shortly after opening, so the "what's new" prompt appears while the user is starting out.
    this.startTimer = setTimeout(() => { void this.check(); }, 3_000);
    this.startTimer.unref();
    this.interval = setInterval(() => { void this.check(); }, SIX_HOURS);
    this.interval.unref();
  }

  dispose(): void { clearTimeout(this.startTimer); clearInterval(this.interval); }

  check(): Promise<UpdateState> {
    if (this.flight) return this.flight;
    if (!this.options.updater || ["available", "downloading", "ready"].includes(this.state.phase) || this.installing) return Promise.resolve(this.get());
    this.flight = this.runCheck().finally(() => { this.flight = null; });
    return this.flight;
  }

  private async runCheck(): Promise<UpdateState> {
    this.descriptor = null;
    this.installer = null;
    this.set({ phase: "checking", error: undefined, percent: undefined, version: undefined, notes: undefined });
    try {
      const result = await this.options.updater!.checkForUpdates();
      if (!result || !result.isUpdateAvailable) return this.set({ phase: "idle" });
      const info = result.updateInfo;
      if (!/^\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?$/.test(info.version)) throw new Error("verification");
      const base = `${RELEASE_BASE}v${info.version}/`;
      const fetch = this.options.fetch ?? fetchReleaseFile;
      const [raw, signature] = await Promise.all([fetch(`${base}lunmi-update.json`), fetch(`${base}lunmi-update.sig`)]);
      let descriptor: ReleaseDescriptor;
      try {
        descriptor = verifyDescriptor(raw, signature, this.options.publicKey, info.version);
      } catch { throw new Error("verification"); }
      // NSIS full installer only. No URL override, web-installer package, or unsigned mirror.
      if (info.files.length !== 1 || info.files[0].url !== descriptor.file ||
          info.files[0].sha512 !== descriptor.sha512 || info.files[0].size !== descriptor.size ||
          ("isAdminRightsRequired" in info.files[0] && info.files[0].isAdminRightsRequired) ||
          ("packages" in info && info.packages)) throw new Error("verification");
      this.descriptor = descriptor;
      // Nothing is downloaded until the user accepts in the "what's new" prompt.
      return this.set({ phase: "available", version: info.version, notes: releaseNotesText(info.releaseNotes) });
    } catch (error) {
      return this.set({ phase: "error", error: error instanceof Error && error.message === "verification" ? "verification" : "unavailable" });
    }
  }

  private async download(): Promise<boolean> {
    try {
      this.set({ phase: "downloading", percent: 0, error: undefined });
      const paths = await this.options.updater!.downloadUpdate();
      if (paths.length !== 1) throw new Error("verification");
      try { await verifyInstaller(paths[0], this.descriptor!); } catch { throw new Error("verification"); }
      this.installer = paths[0];
      this.set({ phase: "ready", percent: 100 });
      return true;
    } catch (error) {
      this.descriptor = null;
      this.set({ phase: "error", error: error instanceof Error && error.message === "verification" ? "verification" : "unavailable" });
      return false;
    }
  }

  install(): Promise<UpdateState> {
    if (this.installFlight) return this.installFlight;
    this.installFlight = this.runInstall().finally(() => { this.installFlight = null; });
    return this.installFlight;
  }

  private async runInstall(): Promise<UpdateState> {
    if (this.state.phase === "available" && this.options.updater && this.descriptor) {
      if (this.options.blocked()) return this.set({ error: "busy" });
      if (!await this.download()) return this.get();
    }
    if (this.state.phase !== "ready" || !this.options.updater || !this.descriptor || !this.installer) return this.get();
    if (this.options.blocked()) return this.set({ error: "busy" });
    this.set({ phase: "installing", error: undefined });
    try {
      if (this.options.confirmRestart && !await this.options.confirmRestart()) return this.set({ phase: "ready", error: "cancelled" });
      if (this.options.blocked()) return this.set({ phase: "ready", error: "busy" });
      try { await verifyInstaller(this.installer, this.descriptor); } catch { throw new Error("verification"); }
      // Recheck after hashing: no task may slip in while the file is being read.
      if (this.options.blocked()) return this.set({ phase: "ready", error: "busy" });
      await this.options.finish();
      return this.get();
    } catch (error) {
      this.installer = null;
      this.descriptor = null;
      return this.set({ phase: "error", error: error instanceof Error && error.message === "verification" ? "verification" : "unavailable" });
    }
  }
}

/** GitHub release notes (HTML or a list) as plain text for the renderer; never markup. */
export function releaseNotesText(notes: unknown): string | undefined {
  const raw = Array.isArray(notes)
    ? notes.map(n => (n && typeof n === "object" && "note" in n ? String((n as { note: unknown }).note ?? "") : "")).join("\n")
    : typeof notes === "string" ? notes : "";
  const text = raw
    .replace(/<\s*(br|\/p|\/li|\/h\d|\/ul|\/ol|\/div)[^>]*>/gi, "\n")
    .replace(/<\s*li[^>]*>/gi, "• ")
    .replace(/<[^>]*>/g, "")
    .replace(/&nbsp;/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&")
    .split("\n").map(line => line.trim()).filter((line, i, all) => line || (i > 0 && all[i - 1])).join("\n")
    .trim();
  return text ? text.slice(0, 4000) : undefined;
}

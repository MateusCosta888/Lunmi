/**
 * VOICE REFINEMENTS V2 — atalho (`desktop/electron/voice-shortcut.ts`).
 *
 * Offline, sem Electron/globalShortcut/hook. Fixa a gramática V2 (modificadores sozinhos ou
 * modificador+tecla), a ordem visual persistida, o default `Ctrl+Alt` só por ausência, o `null`
 * explícito, a falha fechada de ficheiro corrompido, o rollback de helper/disco e os conflitos
 * internos com o Node por conjunto canónico.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import {
  canonicalizeVoiceShortcut,
  isValidVoiceShortcut,
  MAX_VOICE_SHORTCUT_BYTES,
  parseVoiceShortcut,
  parseVoiceShortcutPreferences,
  parseVoiceShortcutSetRequest,
  readVoiceShortcutPreference,
  sameVoiceShortcut,
  serializeVoiceShortcutPreferences,
  VOICE_SHORTCUT_FILE_NAME,
  VOICE_SHORTCUT_FILE_VERSION,
  VoiceInputShortcutStore,
  writeVoiceShortcutFile,
  type VoiceShortcutObserver,
  type VoiceShortcutStoreOptions,
} from "../electron/voice-shortcut";

const dirs: string[] = [];

function tempDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "lunmi-voice-shortcut-"));
  dirs.push(dir);
  return dir;
}

afterEach(() => {
  for (const dir of dirs.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

function fakeObserver() {
  const calls: string[] = [];
  const state = {
    ready: true,
    armed: null as string | null,
    armOk: true,
    reconfigureOk: true,
    failReconfigureFor: new Set<string>(),
    pauseOk: true,
    resumeOk: true,
    paused: false,
  };
  const observer: VoiceShortcutObserver = {
    arm: vi.fn(async (accelerator: string, paused: boolean) => {
      calls.push(`arm:${accelerator}:${paused}`);
      if (!state.armOk) return false;
      state.armed = accelerator;
      state.paused = paused;
      return true;
    }),
    reconfigure: vi.fn(async (accelerator: string, paused: boolean) => {
      calls.push(`reconfigure:${accelerator}:${paused}`);
      if (!state.reconfigureOk || state.failReconfigureFor.has(accelerator)) return false;
      state.armed = accelerator;
      state.paused = paused;
      return true;
    }),
    pause: vi.fn(() => {
      calls.push("pause");
      if (!state.pauseOk) return false;
      state.paused = true;
      return true;
    }),
    resume: vi.fn(() => {
      calls.push("resume");
      if (!state.resumeOk) return false;
      state.paused = false;
      return true;
    }),
    disarm: vi.fn(() => {
      calls.push("disarm");
      state.armed = null;
      state.paused = false;
    }),
    isReady: vi.fn(() => state.ready),
  };
  return { observer, calls, state };
}

function store(
  dir: string,
  observer: VoiceShortcutObserver,
  options: VoiceShortcutStoreOptions = {}
): VoiceInputShortcutStore {
  return new VoiceInputShortcutStore(dir, observer, options);
}

describe("gramática V2 e ordem visual", () => {
  it("aceita 2–4 modificadores sozinhos ou 1–4 modificadores + uma tecla", () => {
    for (const valid of [
      "Ctrl+Alt",
      "Alt+Ctrl",
      "Ctrl+Shift+F5",
      "Super+Space",
      "Ctrl+Alt+V",
      "V+Ctrl",
      "Ctrl+Alt+Shift+Super",
      "Ctrl+Alt+Shift+Super+F24",
      "Control+Meta+A",
      "Ctrl + Alt + V",
    ]) {
      expect(isValidVoiceShortcut(valid)).toBe(true);
    }
  });

  it("preserva a ordem visual e calcula o canónico interno", () => {
    expect(parseVoiceShortcut("Alt+Ctrl")).toMatchObject({
      visual: "Alt+Ctrl",
      canonical: "Ctrl+Alt",
    });
    expect(parseVoiceShortcut("V+Ctrl")).toMatchObject({
      visual: "V+Ctrl",
      canonical: "Ctrl+V",
    });
    expect(parseVoiceShortcut("Ctrl + Alt + V")).toMatchObject({
      visual: "Ctrl+Alt+V",
      canonical: "Ctrl+Alt+V",
    });
    expect(parseVoiceShortcut("Control+Meta+A")).toMatchObject({
      visual: "Control+Meta+A",
      canonical: "Ctrl+Super+A",
    });
    expect(canonicalizeVoiceShortcut("Alt+Ctrl+V")).toBe("Ctrl+Alt+V");
  });

  it("recusa duplicados, aliases duplicados, teclas fora da gramática e vazios", () => {
    for (const invalid of [
      "",
      "Ctrl",
      "Alt",
      "Ctrl+",
      "+Ctrl+Alt",
      "Ctrl++Alt",
      "ctrl+alt",
      "Ctrl+Alt+V+W",
      "Ctrl+Alt+Ctrl",
      "Ctrl+Control+A",
      "Alt+Alt+V",
      "Ctrl+Alt+Delete",
      "Ctrl+Alt+F25",
      "Ctrl+Alt+RightAlt",
      "A",
      null,
      undefined,
      7,
      {},
      [],
    ]) {
      expect(isValidVoiceShortcut(invalid)).toBe(false);
      expect(parseVoiceShortcut(invalid)).toBeNull();
    }
  });

  it("compara por conjunto canónico, não por ordem nem alias", () => {
    expect(sameVoiceShortcut("Alt+Ctrl", "Ctrl+Alt")).toBe(true);
    expect(sameVoiceShortcut("V+Ctrl", "Ctrl+V")).toBe(true);
    expect(sameVoiceShortcut("Control+Alt+V", "Ctrl+Alt+V")).toBe(true);
    expect(sameVoiceShortcut("Ctrl+Alt", "Ctrl+Alt+V")).toBe(false);
    expect(sameVoiceShortcut("Ctrl+Alt", "garbage")).toBe(false);
    expect(sameVoiceShortcut(null, null)).toBe(false);
  });
});

describe("preferências próprias do atalho", () => {
  it("distingue ausente, null explícito, valor visual e inválido", () => {
    expect(parseVoiceShortcutPreferences({})).toEqual({ kind: "absent" });
    expect(parseVoiceShortcutPreferences({ shortcut: null })).toEqual({ kind: "null" });
    expect(parseVoiceShortcutPreferences({ shortcut: "Alt+Ctrl" })).toMatchObject({
      kind: "value",
      parsed: { visual: "Alt+Ctrl", canonical: "Ctrl+Alt" },
    });
    expect(parseVoiceShortcutPreferences({ shortcut: 42 })).toEqual({ kind: "invalid" });
    expect(parseVoiceShortcutPreferences({ shortcut: "Ctrl+" })).toEqual({ kind: "invalid" });
    expect(parseVoiceShortcutPreferences(null)).toEqual({ kind: "invalid" });
  });

  it("grava versão 1 de forma atómica e preserva a ordem visual", () => {
    const dir = tempDir();
    expect(writeVoiceShortcutFile(dir, "Alt+Ctrl")).toBe(true);
    const raw = JSON.parse(fs.readFileSync(path.join(dir, VOICE_SHORTCUT_FILE_NAME), "utf8"));
    expect(raw).toEqual({ version: VOICE_SHORTCUT_FILE_VERSION, shortcut: "Alt+Ctrl" });
    expect(fs.readdirSync(dir).filter(name => name.endsWith(".tmp"))).toEqual([]);
    expect(readVoiceShortcutPreference(dir)).toMatchObject({
      kind: "value",
      parsed: { visual: "Alt+Ctrl", canonical: "Ctrl+Alt" },
    });
  });

  it("leitura tolerante: ausente/null/corrompida/gigante", () => {
    const dir = tempDir();
    expect(readVoiceShortcutPreference(dir)).toEqual({ kind: "absent" });

    writeVoiceShortcutFile(dir, null);
    expect(readVoiceShortcutPreference(dir)).toEqual({ kind: "null" });

    const target = path.join(dir, VOICE_SHORTCUT_FILE_NAME);
    fs.writeFileSync(target, "{nao-json", "utf8");
    expect(readVoiceShortcutPreference(dir)).toEqual({ kind: "invalid" });

    fs.writeFileSync(target, JSON.stringify({ shortcut: 42 }), "utf8");
    expect(readVoiceShortcutPreference(dir)).toEqual({ kind: "invalid" });

    fs.writeFileSync(target, " ".repeat(MAX_VOICE_SHORTCUT_BYTES + 1), "utf8");
    expect(readVoiceShortcutPreference(dir)).toEqual({ kind: "invalid" });
  });

  it("serializa null como ausência explícita", () => {
    expect(JSON.parse(serializeVoiceShortcutPreferences(null))).toEqual({
      version: VOICE_SHORTCUT_FILE_VERSION,
      shortcut: null,
    });
  });

  it("pedido IPC aceita exatamente uma chave accelerator", () => {
    expect(parseVoiceShortcutSetRequest({ accelerator: null })).toEqual({
      ok: true,
      accelerator: null,
    });
    expect(parseVoiceShortcutSetRequest({ accelerator: "Alt+Ctrl" })).toEqual({
      ok: true,
      accelerator: "Alt+Ctrl",
    });
    for (const invalid of [
      {},
      { accelerator: "Ctrl+J", extra: 1 },
      { accel: "Ctrl+J" },
      null,
      [],
      "Ctrl+J",
    ]) {
      expect(parseVoiceShortcutSetRequest(invalid)).toEqual({ ok: false });
    }
  });
});

describe("VoiceInputShortcutStore — load", () => {
  it("sem ficheiro usa o default Ctrl+Alt e arma-o", async () => {
    const dir = tempDir();
    const f = fakeObserver();
    const s = store(dir, f.observer);
    expect(await s.get()).toEqual({ accelerator: "Ctrl+Alt", error: "" });
    expect(f.calls).toEqual(["arm:Ctrl+Alt:false"]);
    expect(fs.existsSync(path.join(dir, VOICE_SHORTCUT_FILE_NAME))).toBe(false);
  });

  it("null explícito não arma e não aplica o default", async () => {
    const dir = tempDir();
    writeVoiceShortcutFile(dir, null);
    const f = fakeObserver();
    const s = store(dir, f.observer);
    expect(await s.get()).toEqual({ accelerator: null, error: "" });
    expect(f.calls).toEqual([]);
  });

  it("ficheiro corrompido falha fechado, sem armar e sem escrever", async () => {
    const dir = tempDir();
    const target = path.join(dir, VOICE_SHORTCUT_FILE_NAME);
    fs.writeFileSync(target, "{nao-json", "utf8");
    const f = fakeObserver();
    const s = store(dir, f.observer);
    expect(await s.get()).toEqual({ accelerator: null, error: "" });
    expect(f.calls).toEqual([]);
    expect(fs.readFileSync(target, "utf8")).toBe("{nao-json");
  });

  it("preserva um atalho V1 com tecla final e a ordem visual guardada", async () => {
    const dir = tempDir();
    writeVoiceShortcutFile(dir, "Ctrl+Shift+V");
    const f = fakeObserver();
    const s = store(dir, f.observer);
    expect(await s.get()).toEqual({ accelerator: "Ctrl+Shift+V", error: "" });
    expect(f.calls).toEqual(["arm:Ctrl+Shift+V:false"]);

    const visualDir = tempDir();
    writeVoiceShortcutFile(visualDir, "Alt+Ctrl+V");
    const visual = fakeObserver();
    const visualStore = store(visualDir, visual.observer);
    expect(await visualStore.get()).toEqual({ accelerator: "Alt+Ctrl+V", error: "" });
    expect(visual.calls).toEqual(["arm:Ctrl+Alt+V:false"]);
  });

  it("falha do helper fica honesta: null + hold_unavailable", async () => {
    const dir = tempDir();
    const f = fakeObserver();
    f.state.armOk = false;
    const s = store(dir, f.observer);
    expect(await s.get()).toEqual({ accelerator: null, error: "hold_unavailable" });
  });

  it("conflito com o Node por conjunto canónico não arma", async () => {
    const dir = tempDir();
    writeVoiceShortcutFile(dir, "Alt+Ctrl+V");
    const f = fakeObserver();
    const s = store(dir, f.observer, {
      nodeConflict: canonical => sameVoiceShortcut(canonical, "Ctrl+Alt+V"),
    });
    expect(await s.get()).toEqual({ accelerator: null, error: "shortcut_conflict" });
    expect(f.calls).toEqual([]);
  });
});

describe("VoiceInputShortcutStore — set", () => {
  it("arma o canónico, persiste a ordem visual e get devolve a ordem", async () => {
    const dir = tempDir();
    const f = fakeObserver();
    const s = store(dir, f.observer);
    await s.load();
    expect(await s.set("Alt+Ctrl")).toEqual({ accelerator: "Alt+Ctrl", error: "" });
    expect(f.calls).toEqual(["arm:Ctrl+Alt:false"]);
    expect(readVoiceShortcutPreference(dir)).toMatchObject({
      kind: "value",
      parsed: { visual: "Alt+Ctrl", canonical: "Ctrl+Alt" },
    });
  });

  it("mesmo conjunto com outra ordem só atualiza a apresentação, sem rearmar", async () => {
    const dir = tempDir();
    writeVoiceShortcutFile(dir, "Ctrl+Alt");
    const f = fakeObserver();
    const s = store(dir, f.observer);
    await s.load();
    expect(await s.set("Alt+Ctrl")).toEqual({ accelerator: "Alt+Ctrl", error: "" });
    expect(f.calls).toEqual(["arm:Ctrl+Alt:false"]);
    expect(readVoiceShortcutPreference(dir)).toMatchObject({
      kind: "value",
      parsed: { visual: "Alt+Ctrl" },
    });
  });

  it("valor inválido/conflito não toca no helper", async () => {
    const dir = tempDir();
    const f = fakeObserver();
    const s = store(dir, f.observer, {
      nodeConflict: canonical => canonical === "Ctrl+V",
    });
    await s.load();
    f.calls.length = 0;
    expect((await s.set("Ctrl+")).error).toBe("invalid_shortcut");
    expect((await s.set("V+Ctrl")).error).toBe("shortcut_conflict");
    expect(f.calls).toEqual([]);
    expect(await s.get()).toEqual({ accelerator: "Ctrl+Alt", error: "" });
  });

  it("falha do reconfigure não anuncia o candidato e repõe o anterior", async () => {
    const dir = tempDir();
    writeVoiceShortcutFile(dir, "Ctrl+Alt");
    const f = fakeObserver();
    const s = store(dir, f.observer);
    await s.load();
    f.calls.length = 0;
    f.state.reconfigureOk = false;
    expect(await s.set("Ctrl+Shift+J")).toEqual({
      accelerator: "Ctrl+Alt",
      error: "hold_unavailable",
    });
    expect(f.calls).toEqual(["reconfigure:Ctrl+Shift+J:false", "arm:Ctrl+Alt:false"]);
    expect(await s.get()).toEqual({ accelerator: "Ctrl+Alt", error: "hold_unavailable" });
    expect(readVoiceShortcutPreference(dir)).toMatchObject({
      kind: "value",
      parsed: { visual: "Ctrl+Alt" },
    });
  });

  it("falha de disco desfaz o candidato e rearma o anterior", async () => {
    const dir = tempDir();
    writeVoiceShortcutFile(dir, "Ctrl+Alt");
    const f = fakeObserver();
    const s = store(dir, f.observer);
    await s.load();
    fs.rmSync(path.join(dir, VOICE_SHORTCUT_FILE_NAME), { force: true });
    fs.mkdirSync(path.join(dir, VOICE_SHORTCUT_FILE_NAME));
    f.calls.length = 0;

    expect(await s.set("Ctrl+Shift+J")).toEqual({
      accelerator: "Ctrl+Alt",
      error: "io_error",
    });
    expect(f.calls).toEqual([
      "reconfigure:Ctrl+Shift+J:false",
      "reconfigure:Ctrl+Alt:false",
    ]);
    expect(await s.get()).toEqual({ accelerator: "Ctrl+Alt", error: "" });
  });

  it("falha de disco + falha do rollback liberta o helper e não anuncia binding", async () => {
    const dir = tempDir();
    writeVoiceShortcutFile(dir, "Ctrl+Alt");
    const f = fakeObserver();
    const s = store(dir, f.observer);
    await s.load();
    fs.rmSync(path.join(dir, VOICE_SHORTCUT_FILE_NAME), { force: true });
    fs.mkdirSync(path.join(dir, VOICE_SHORTCUT_FILE_NAME));
    f.calls.length = 0;
    f.state.failReconfigureFor.add("Ctrl+Alt"); // o rollback por reconfigure falha
    f.state.armOk = false; // e a reposição por um helper novo também
    expect(await s.set("Ctrl+Shift+J")).toEqual({
      accelerator: null,
      error: "io_error",
    });
    expect(f.calls).toEqual([
      "reconfigure:Ctrl+Shift+J:false",
      "reconfigure:Ctrl+Alt:false",
      "arm:Ctrl+Alt:false",
      "disarm",
    ]);
    expect((f.observer.disarm as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThanOrEqual(1);
    expect(await s.get()).toEqual({ accelerator: null, error: "" });
  });

  it("set(null) liberta o helper e persiste null explícito", async () => {
    const dir = tempDir();
    writeVoiceShortcutFile(dir, "Ctrl+Alt");
    const f = fakeObserver();
    const s = store(dir, f.observer);
    await s.load();
    expect(await s.set(null)).toEqual({ accelerator: null, error: "" });
    expect(f.state.armed).toBeNull();
    expect(readVoiceShortcutPreference(dir)).toEqual({ kind: "null" });
    expect(await s.get()).toEqual({ accelerator: null, error: "" });
  });

  it("falha ao persistir null rearma o anterior", async () => {
    const dir = tempDir();
    writeVoiceShortcutFile(dir, "Ctrl+Alt");
    const f = fakeObserver();
    const s = store(dir, f.observer);
    await s.load();
    fs.rmSync(path.join(dir, VOICE_SHORTCUT_FILE_NAME), { force: true });
    fs.mkdirSync(path.join(dir, VOICE_SHORTCUT_FILE_NAME));
    expect(await s.set(null)).toEqual({ accelerator: "Ctrl+Alt", error: "io_error" });
    expect(f.state.armed).toBe("Ctrl+Alt");
  });

  it("falha do helper depois do load não deixa binding anunciado", async () => {
    const dir = tempDir();
    const f = fakeObserver();
    const s = store(dir, f.observer);
    await s.load();
    s.handleObserverFailure();
    expect(await s.get()).toEqual({ accelerator: null, error: "hold_unavailable" });
  });
});

describe("VoiceInputShortcutStore — pausa do picker", () => {
  it("sem binding pausa localmente e arma já pausado", async () => {
    const dir = tempDir();
    writeVoiceShortcutFile(dir, null);
    const f = fakeObserver();
    const s = store(dir, f.observer);
    expect(await s.pause()).toEqual({ ok: true, error: "" });
    expect(await s.set("Ctrl+V")).toEqual({ accelerator: "Ctrl+V", error: "" });
    expect(f.calls).toEqual(["arm:Ctrl+V:true"]);
    expect(await s.resume()).toEqual({ ok: true, error: "" });
  });

  it("com binding usa o observer; resume devolve ok", async () => {
    const dir = tempDir();
    const f = fakeObserver();
    const s = store(dir, f.observer);
    await s.load();
    expect(await s.pause()).toEqual({ ok: true, error: "" });
    expect(await s.resume()).toEqual({ ok: true, error: "" });
    expect(f.calls).toEqual(["arm:Ctrl+Alt:false", "pause", "resume"]);
  });

  it("helper não pronto devolve hold_unavailable", async () => {
    const dir = tempDir();
    const f = fakeObserver();
    f.state.ready = false;
    const s = store(dir, f.observer);
    await s.load();
    expect(await s.pause()).toEqual({ ok: false, error: "hold_unavailable" });
    expect(await s.resume()).toEqual({ ok: false, error: "hold_unavailable" });
  });

  it("dispose liberta o helper sem apagar a preferência", async () => {
    const dir = tempDir();
    writeVoiceShortcutFile(dir, "Ctrl+Alt");
    const f = fakeObserver();
    const s = store(dir, f.observer);
    await s.load();
    s.dispose();
    expect(f.state.armed).toBeNull();
    expect(readVoiceShortcutPreference(dir)).toMatchObject({
      kind: "value",
      parsed: { visual: "Ctrl+Alt" },
    });
  });
});

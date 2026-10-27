/**
 * VOICE REFINEMENTS V2 — atalho de ditado (`desktop/electron/voice-shortcut.ts`).
 *
 * Fronteira: o renderer nunca lê nem escreve ficheiros; o preload só transporta `get`/`set`,
 * `pause`/`resume` e a subscrição de fases. O main valida remetente, frame e documento antes de
 * chegar aqui (ver `setupVoiceInputShortcutIpc` em `main.ts`).
 *
 * Regras do contrato `.agents/VOICE-REFINEMENTS-V2-CONTRACT.md`:
 * - a preferência preserva a **ordem visual** escolhida (`Alt+Ctrl` continua `Alt+Ctrl` depois de
 *   reabrir); a deteção e o helper usam o accelerator **canónico** interno (`Ctrl+Alt`);
 * - sem ficheiro/campo → default `Ctrl+Alt`; `null` explícito → sem binding; ficheiro corrompido →
 *   falha fechada (`null`, sem escrever);
 * - falha de helper/registo/persistência mantém o anterior vivo; nunca se anuncia um binding morto;
 * - conflitos internos com o Node são comparados por conjunto canónico e o Node é preservado.
 */

import * as fs from "fs";
import * as path from "path";
import { DEFAULT_VOICE_INPUT_SHORTCUT } from "../src/shared/types";
import type {
  VoiceInputShortcutControlDto,
  VoiceInputShortcutResultDto,
} from "../src/shared/types";

export const VOICE_SHORTCUT_FILE_NAME = "desktop-voice-input.json";
export const VOICE_SHORTCUT_FILE_VERSION = 1;
export const MAX_VOICE_SHORTCUT_BYTES = 4 * 1024;
export const MAX_VOICE_SHORTCUT_CHARS = 64;

const MODIFIER_ORDER = ["Ctrl", "Alt", "Shift", "Super"] as const;
const MODIFIER_ALIASES: Record<string, (typeof MODIFIER_ORDER)[number]> = {
  Ctrl: "Ctrl",
  Control: "Ctrl",
  Alt: "Alt",
  Shift: "Shift",
  Super: "Super",
  Meta: "Super",
};
const TERMINAL_KEYS = new Set<string>([
  ...Array.from({ length: 26 }, (_, index) => String.fromCharCode(65 + index)),
  ...Array.from({ length: 10 }, (_, index) => String(index)),
  ...Array.from({ length: 24 }, (_, index) => `F${index + 1}`),
  "Space",
  "Tab",
  "Enter",
  "Escape",
  "Up",
  "Down",
  "Left",
  "Right",
]);

export interface ParsedVoiceShortcut {
  /** Ordem exata em que as teclas foram escolhidas, normalizada sem espaços. */
  visual: string;
  /** Ordem estável usada para deteção, helper e conflitos: Ctrl, Alt, Shift, Super, tecla. */
  canonical: string;
  modifiers: string[];
  key: string | null;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Gramática V2: 2–4 modificadores sozinhos ou 1–4 modificadores + uma tecla final.
 * A ordem visual é preservada; aliases V1 (`Control`, `Meta`) e espaços em volta de `+` são
 * aceites, mas duplicados/aliases duplicados e teclas desconhecidas são recusados.
 */
export function parseVoiceShortcut(raw: unknown): ParsedVoiceShortcut | null {
  if (typeof raw !== "string" || raw.length === 0 || raw.length > MAX_VOICE_SHORTCUT_CHARS) {
    return null;
  }
  const tokens = raw.split("+").map((token) => token.trim());
  if (tokens.some((token) => token.length === 0)) {
    return null;
  }
  const modifiers: string[] = [];
  let key: string | null = null;
  for (const token of tokens) {
    const canonicalModifier = MODIFIER_ALIASES[token];
    if (canonicalModifier) {
      if (modifiers.includes(canonicalModifier)) {
        return null;
      }
      modifiers.push(canonicalModifier);
      continue;
    }
    if (TERMINAL_KEYS.has(token)) {
      if (key !== null) {
        return null;
      }
      key = token;
      continue;
    }
    return null;
  }
  if (key === null) {
    if (modifiers.length < 2 || modifiers.length > 4) {
      return null;
    }
  } else if (modifiers.length < 1 || modifiers.length > 4) {
    return null;
  }
  const ordered = MODIFIER_ORDER.filter((modifier) => modifiers.includes(modifier));
  return {
    visual: tokens.join("+"),
    canonical: [...ordered, ...(key ? [key] : [])].join("+"),
    modifiers: [...ordered],
    key,
  };
}

export function isValidVoiceShortcut(raw: unknown): raw is string {
  return parseVoiceShortcut(raw) !== null;
}

export function canonicalizeVoiceShortcut(raw: unknown): string | null {
  return parseVoiceShortcut(raw)?.canonical ?? null;
}

/** Igualdade por conjunto canónico: `Alt+Ctrl` e `Ctrl+Alt` são o mesmo atalho. */
export function sameVoiceShortcut(left: unknown, right: unknown): boolean {
  const a = parseVoiceShortcut(left);
  const b = parseVoiceShortcut(right);
  return a !== null && b !== null && a.canonical === b.canonical;
}

// ---------------------------------------------------------------------------
// Preferências próprias (version 1, atómicas, tolerantes)
// ---------------------------------------------------------------------------

export type VoiceShortcutPreference =
  | { kind: "absent" }
  | { kind: "null" }
  | { kind: "value"; parsed: ParsedVoiceShortcut }
  | { kind: "invalid" };

export function parseVoiceShortcutPreferences(raw: unknown): VoiceShortcutPreference {
  if (!isPlainObject(raw)) {
    return { kind: "invalid" };
  }
  if (!("shortcut" in raw)) {
    return { kind: "absent" };
  }
  const value = raw.shortcut;
  if (value === null) {
    return { kind: "null" };
  }
  const parsed = parseVoiceShortcut(value);
  return parsed ? { kind: "value", parsed } : { kind: "invalid" };
}

export function readVoiceShortcutPreference(localDir: string): VoiceShortcutPreference {
  if (!localDir) {
    return { kind: "invalid" };
  }
  try {
    const target = path.join(localDir, VOICE_SHORTCUT_FILE_NAME);
    const stat = fs.statSync(target);
    if (stat.size > MAX_VOICE_SHORTCUT_BYTES) {
      return { kind: "invalid" };
    }
    return parseVoiceShortcutPreferences(JSON.parse(fs.readFileSync(target, "utf8")));
  } catch (error) {
    // Só a ausência do ficheiro conta como "sem preferência" (default). Ilegível/corrompido
    // continua a ser falha fechada.
    if ((error as NodeJS.ErrnoException)?.code === "ENOENT") {
      return { kind: "absent" };
    }
    return { kind: "invalid" };
  }
}

export function serializeVoiceShortcutPreferences(shortcut: string | null): string {
  return JSON.stringify({ version: VOICE_SHORTCUT_FILE_VERSION, shortcut });
}

/** Escreve um temporário no mesmo diretório e renomeia; falha devolve `false`, nunca lança. */
export function writeVoiceShortcutFile(localDir: string, shortcut: string | null): boolean {
  if (!localDir) {
    return false;
  }
  const target = path.join(localDir, VOICE_SHORTCUT_FILE_NAME);
  const temporary = `${target}.${process.pid}.${Date.now()}.tmp`;
  try {
    fs.mkdirSync(localDir, { recursive: true });
    fs.writeFileSync(temporary, serializeVoiceShortcutPreferences(shortcut), "utf8");
    fs.renameSync(temporary, target);
    return true;
  } catch {
    try {
      fs.rmSync(temporary, { force: true });
    } catch {
      // o temporário é descartável; a preferência em RAM continua válida
    }
    return false;
  }
}

/** Pedido IPC fechado: objeto com exatamente a chave `accelerator` (string ou null). */
export type VoiceShortcutSetRequest = { ok: true; accelerator: unknown } | { ok: false };

export function parseVoiceShortcutSetRequest(raw: unknown): VoiceShortcutSetRequest {
  if (!isPlainObject(raw)) {
    return { ok: false };
  }
  const keys = Object.keys(raw);
  if (keys.length !== 1 || keys[0] !== "accelerator") {
    return { ok: false };
  }
  return { ok: true, accelerator: raw.accelerator };
}

// ---------------------------------------------------------------------------
// Observador do helper injetado (implementado em `voice-hotkey.ts`)
// ---------------------------------------------------------------------------

export interface VoiceShortcutObserver {
  /** Arma o accelerator canónico; `paused` evita aceitar fases antes do primeiro pause. */
  arm(accelerator: string, paused: boolean): Promise<boolean>;
  /** Troca o conjunto sem libertar o anterior até o candidato estar aplicado. */
  reconfigure(accelerator: string, paused: boolean): Promise<boolean>;
  pause(): boolean;
  resume(): boolean;
  disarm(): void;
  isReady(): boolean;
}

export interface VoiceShortcutStoreOptions {
  defaultShortcut?: string;
  /** Compara o candidato canónico com o atalho atual do Node (por conjunto). */
  nodeConflict?: (canonical: string) => boolean;
}

/**
 * Estado efetivo do atalho = preferência (visual) + helper (canónico). `get` nunca toca no disco
 * nem no sistema além do estado em RAM; `load` é o único caminho de arranque.
 */
export class VoiceInputShortcutStore {
  private loading: Promise<void> | null = null;
  private effective: ParsedVoiceShortcut | null = null;
  private error: VoiceInputShortcutResultDto["error"] = "";
  private paused = false;
  private storedNull = false;
  private disposed = false;

  constructor(
    private readonly localDir: string,
    private readonly observer: VoiceShortcutObserver,
    private readonly options: VoiceShortcutStoreOptions = {}
  ) {}

  private get defaultShortcut(): string {
    return this.options.defaultShortcut ?? DEFAULT_VOICE_INPUT_SHORTCUT;
  }

  private currentResult(): VoiceInputShortcutResultDto {
    return { accelerator: this.effective?.visual ?? null, error: this.error };
  }

  private isNodeConflict(canonical: string): boolean {
    return this.options.nodeConflict?.(canonical) === true;
  }

  load(): Promise<void> {
    if (this.disposed) {
      return Promise.resolve();
    }
    if (!this.loading) {
      this.loading = this.applyLoad();
    }
    return this.loading;
  }

  private async applyLoad(): Promise<void> {
    const preference = readVoiceShortcutPreference(this.localDir);
    this.effective = null;
    this.error = "";
    this.storedNull = false;
    if (preference.kind === "null") {
      this.storedNull = true;
      return;
    }
    if (preference.kind === "invalid") {
      // Falha fechada: não liga captura por acidente e não escreve no disco neste arranque.
      return;
    }
    const candidate = preference.kind === "value"
      ? preference.parsed
      : parseVoiceShortcut(this.defaultShortcut);
    if (!candidate) {
      return;
    }
    if (this.isNodeConflict(candidate.canonical)) {
      this.error = "shortcut_conflict";
      return;
    }
    const armed = await this.observer.arm(candidate.canonical, false);
    if (this.disposed) {
      this.observer.disarm();
      return;
    }
    if (!armed) {
      this.error = "hold_unavailable";
      return;
    }
    this.effective = candidate;
  }

  async get(): Promise<VoiceInputShortcutResultDto> {
    if (!this.disposed) {
      await this.load();
    }
    return this.currentResult();
  }

  async set(requested: unknown): Promise<VoiceInputShortcutResultDto> {
    await this.load();
    if (this.disposed) {
      return this.currentResult();
    }
    if (requested === null) {
      return this.setNull();
    }
    const candidate = parseVoiceShortcut(requested);
    if (!candidate) {
      return { ...this.currentResult(), error: "invalid_shortcut" };
    }
    if (this.isNodeConflict(candidate.canonical)) {
      return { ...this.currentResult(), error: "shortcut_conflict" };
    }
    // Mesmo conjunto: só a apresentação (ordem/alias) muda; não re-regista.
    if (this.effective && candidate.canonical === this.effective.canonical) {
      if (candidate.visual === this.effective.visual) {
        return this.currentResult();
      }
      if (!writeVoiceShortcutFile(this.localDir, candidate.visual)) {
        return { ...this.currentResult(), error: "io_error" };
      }
      this.effective = candidate;
      this.error = "";
      this.storedNull = false;
      return this.currentResult();
    }

    const previous = this.effective;
    const applied = previous
      ? await this.observer.reconfigure(candidate.canonical, this.paused)
      : await this.observer.arm(candidate.canonical, this.paused);
    if (this.disposed) {
      this.observer.disarm();
      return this.currentResult();
    }
    if (!applied) {
      // O helper não aplicou o candidato. Se havia um atalho anterior, tenta repô-lo antes de
      // anunciar falha: a preferência anterior continua a ser a única que pode ficar viva.
      if (previous) {
        const restored = await this.observer.arm(previous.canonical, this.paused);
        if (restored && !this.disposed) {
          this.effective = previous;
          this.error = "hold_unavailable";
          return { accelerator: previous.visual, error: "hold_unavailable" };
        }
      }
      this.observer.disarm();
      this.effective = null;
      this.error = "hold_unavailable";
      return { accelerator: null, error: "hold_unavailable" };
    }
    if (!writeVoiceShortcutFile(this.localDir, candidate.visual)) {
      // Disco recusou: desfaz o candidato e tenta recuperar o anterior.
      if (previous) {
        // `reconfigure` pode falhar porque o filho morreu; nesse caso `arm` volta a criar o
        // helper com o conjunto anterior antes de desistir.
        const restored = (await this.observer.reconfigure(previous.canonical, this.paused)) ||
          (await this.observer.arm(previous.canonical, this.paused));
        if (restored && !this.disposed) {
          this.effective = previous;
          this.error = "";
          return { accelerator: previous.visual, error: "io_error" };
        }
      }
      this.observer.disarm();
      this.effective = null;
      this.error = "";
      return { accelerator: null, error: "io_error" };
    }
    this.effective = candidate;
    this.error = "";
    this.storedNull = false;
    return this.currentResult();
  }

  private async setNull(): Promise<VoiceInputShortcutResultDto> {
    if (!this.effective) {
      if (this.storedNull && this.error === "") {
        return this.currentResult();
      }
      this.observer.disarm();
      if (!writeVoiceShortcutFile(this.localDir, null)) {
        return { accelerator: null, error: "io_error" };
      }
      this.storedNull = true;
      this.error = "";
      this.paused = false;
      return this.currentResult();
    }
    const previous = this.effective;
    this.observer.disarm();
    this.effective = null;
    if (!writeVoiceShortcutFile(this.localDir, null)) {
      // Disco recusou: o anterior só volta a ser anunciado se o helper o registar de novo.
      const restored = await this.observer.arm(previous.canonical, this.paused);
      if (restored) {
        this.effective = previous;
        this.error = "";
      } else {
        this.error = "hold_unavailable";
      }
      return { accelerator: this.effective?.visual ?? null, error: "io_error" };
    }
    this.storedNull = true;
    this.error = "";
    this.paused = false;
    return this.currentResult();
  }

  async pause(): Promise<VoiceInputShortcutControlDto> {
    await this.load();
    if (this.disposed) {
      return { ok: false, error: "hold_unavailable" };
    }
    if (!this.effective) {
      this.paused = true;
      return { ok: true, error: "" };
    }
    if (!this.observer.isReady()) {
      return { ok: false, error: "hold_unavailable" };
    }
    if (!this.observer.pause()) {
      return { ok: false, error: "hold_unavailable" };
    }
    this.paused = true;
    return { ok: true, error: "" };
  }

  async resume(): Promise<VoiceInputShortcutControlDto> {
    await this.load();
    if (this.disposed) {
      return { ok: false, error: "hold_unavailable" };
    }
    if (!this.effective) {
      this.paused = false;
      return { ok: true, error: "" };
    }
    if (!this.observer.isReady()) {
      return { ok: false, error: "hold_unavailable" };
    }
    if (!this.observer.resume()) {
      return { ok: false, error: "hold_unavailable" };
    }
    this.paused = false;
    return { ok: true, error: "" };
  }

  /** Chamado pelo main quando o helper morre: não se anuncia um binding morto. */
  handleObserverFailure(): void {
    if (this.effective) {
      this.effective = null;
      this.error = "hold_unavailable";
    }
    this.paused = false;
  }

  /** Quit/encerramento: liberta o helper sem escrever na preferência. */
  dispose(): void {
    this.disposed = true;
    this.observer.disarm();
    this.effective = null;
    this.error = "";
    this.paused = false;
    this.loading = null;
  }
}

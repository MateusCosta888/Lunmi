/**
 * Preferências de apresentação lidas pela camada confiável (`desktop/electron/preferences.ts`).
 *
 * Espelha as decisões de `app/ui/ui_preferences.py`: leitura **tolerante** (arquivo ausente,
 * inválido, gigante ou com tipo errado devolve o padrão e nunca levanta) e um único teto de
 * bytes antes de qualquer parse.
 *
 * Por que isto está no main e não no renderer: `scale_pct` e a geometria da janela são da
 * **janela**, não da página. O renderer não aplica zoom nem move a própria janela — e não pode
 * enviar `window` arbitrário. O main lê o arquivo, aplica `zoomFactor`/bounds e é o único que
 * grava a geometria de volta.
 */
import * as fs from "fs";
import * as path from "path";
import { getTheme } from "../src/shared/theme";

/** Escalas legadas aceitas no arquivo; o shell atual usa DPI nativo sem zoom adicional. */
export const SCALE_CHOICES = [100, 125, 150, 200] as const;

export const MIN_WINDOW_EDGE = 400;
export const MAX_WINDOW_WIDTH = 16384;
export const MAX_WINDOW_HEIGHT = 10240;
export const MAX_WINDOW_X = 16384;
export const MAX_WINDOW_Y = 10240;
/** Pixels que precisam estar na tela para a restauração valer a pena (`MIN_VISIBLE`). */
export const MIN_VISIBLE = 40;
export const MAX_PREFS_BYTES = 64 * 1024;

export const DEFAULT_WINDOW_WIDTH = 1120;
export const DEFAULT_WINDOW_HEIGHT = 760;

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface UiPreferences {
  scalePct: number;
  window: Rect | null;
  theme: string;
  reduceMotion: boolean;
}

export const DEFAULT_PREFERENCES: UiPreferences = { scalePct: 100, window: null, theme: "celestial", reduceMotion: false };

/** 100 -> 1, 125 -> 1,25, 150 -> 1,5, 200 -> 2. Qualquer outro valor é 1 (sem escala). */
export function zoomFactorFor(scalePct: unknown): number {
  return typeof scalePct === "number" && (SCALE_CHOICES as readonly number[]).includes(scalePct)
    ? scalePct / 100
    : 1;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isInt(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value);
}

function parseRect(raw: unknown): Rect | null {
  if (!Array.isArray(raw) || raw.length !== 4 || !raw.every(isInt)) {
    return null;
  }
  const [x, y, width, height] = raw as number[];
  if (
    width < MIN_WINDOW_EDGE ||
    width > MAX_WINDOW_WIDTH ||
    height < MIN_WINDOW_EDGE ||
    height > MAX_WINDOW_HEIGHT ||
    x < -MAX_WINDOW_X ||
    x > MAX_WINDOW_X ||
    y < -MAX_WINDOW_Y ||
    y > MAX_WINDOW_Y
  ) {
    return null;
  }
  return { x, y, width, height };
}

/** Converte o que veio do disco no que a janela usa, descartando o que não é confiável. */
export function parsePreferences(raw: unknown): UiPreferences {
  if (!isPlainObject(raw)) {
    return DEFAULT_PREFERENCES;
  }
  const scale = raw.scale_pct;
  const scalePct = isInt(scale) && (SCALE_CHOICES as readonly number[]).includes(scale) ? scale : 100;
  return { scalePct, window: parseRect(raw.window), theme: getTheme(raw.theme).id, reduceMotion: raw.reduce_motion === true };
}

/** Lê sem nunca falhar: arquivo ruim é ausente, e ausente é o padrão. */
export function readPreferencesFile(localDir: string): UiPreferences {
  try {
    const target = path.join(localDir, "lunmi-ui.json");
    const stat = fs.statSync(target);
    if (stat.size > MAX_PREFS_BYTES) {
      return DEFAULT_PREFERENCES;
    }
    return parsePreferences(JSON.parse(fs.readFileSync(target, "utf8")));
  } catch {
    return DEFAULT_PREFERENCES;
  }
}

/**
 * O retângulo que volta cabendo **inteiro** numa tela, ou `null` quando não há como.
 *
 * Monitor removido é o caso do `null`: uma janela salva fora da área útil não tem sobreposição
 * nenhuma e abrir no lugar padrão é melhor que abrir fora do alcance do mouse. Janela que apenas
 * transborda a borda é trazida para dentro, preservando as dimensões quando elas caibam.
 */
export function usableBounds(saved: Rect | null, display: Rect): Rect | null {
  if (!saved) {
    return null;
  }
  if (display.width < MIN_WINDOW_EDGE || display.height < MIN_WINDOW_EDGE) {
    return null;
  }
  const width = Math.min(saved.width, display.width);
  const height = Math.min(saved.height, display.height);
  if (width < MIN_WINDOW_EDGE || height < MIN_WINDOW_EDGE) {
    return null;
  }
  // Sobreposição real, eixo a eixo, ANTES de mover: sem esta medida qualquer retângulo esquecido
  // viraria um retângulo na origem, decidindo por engano onde o usuário quer a janela.
  const overlapX = Math.min(saved.x + width, display.x + display.width) - Math.max(saved.x, display.x);
  if (overlapX < MIN_VISIBLE) {
    return null;
  }
  const overlapY = Math.min(saved.y + height, display.y + display.height) - Math.max(saved.y, display.y);
  if (overlapY < MIN_VISIBLE) {
    return null;
  }
  return {
    x: Math.max(display.x, Math.min(saved.x, display.x + display.width - width)),
    y: Math.max(display.y, Math.min(saved.y, display.y + display.height - height)),
    width,
    height,
  };
}

/** Rótulo humano da escala — usado pela interface para dizer o que está valendo. */
export function describeScale(scalePct: number): string {
  return `${zoomFactorFor(scalePct) === 1 ? 100 : scalePct}%`;
}

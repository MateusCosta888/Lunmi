/**
 * Geometria pura do companheiro (orb/mini chat) — `desktop/src/shared/presence.ts`.
 *
 * Sem Electron e sem DOM: o processo principal usa para validar/limitar a janela auxiliar e o
 * renderer usa as mesmas medidas/limiares, para não existirem dois números para o mesmo orb.
 * Coordenadas em DIP, sempre dentro da área útil do monitor atual.
 */
import type { PresenceMode } from "./types";

export interface PresenceBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface PresenceAnchor {
  x: number;
  y: number;
}

/** Candidato inicial do plano: orb de 112 px, mini ~380x480 DIP. */
export const ORB_SIZE = 112;
export const MINI_WIDTH = 380;
export const MINI_HEIGHT = 480;
/** Margem entre o orb e a borda da área útil. */
export const COMPANION_MARGIN = 24;
/** Movimento acima disto é arrasto; abaixo, é clique no orb. */
export const DRAG_CLICK_THRESHOLD_PX = 4;
/** Passo máximo aceito por chamada de arrasto; o main recusa valores maiores. */
export const MAX_DRAG_STEP_PX = 96;
/** Rascunho partilhado: mesmo teto do compositor, nunca persistido. */
export const MAX_DRAFT_CHARS = 4000;
/** O main nunca aceita um arrasto que deixe o orb fora da área útil. */
export function defaultCompanionAnchor(workArea: PresenceBounds): PresenceAnchor {
  return clampCompanionAnchor(
    {
      x: workArea.x + workArea.width - ORB_SIZE - COMPANION_MARGIN,
      y: workArea.y + workArea.height - ORB_SIZE - COMPANION_MARGIN,
    },
    workArea
  );
}

function clamp(value: number, low: number, high: number): number {
  if (high < low) return low;
  return Math.min(high, Math.max(low, value));
}

export function clampCompanionAnchor(anchor: PresenceAnchor, workArea: PresenceBounds): PresenceAnchor {
  return {
    x: clamp(Math.round(anchor.x), workArea.x, workArea.x + workArea.width - ORB_SIZE),
    y: clamp(Math.round(anchor.y), workArea.y, workArea.y + workArea.height - ORB_SIZE),
  };
}

/**
 * Retângulo da janela auxiliar para cada modo. O mini cresce para cima/para a esquerda a partir
 * do orb, para o canto inferior direito continuar estável na troca orb↔mini.
 */
export function companionBounds(
  mode: Exclude<PresenceMode, "hidden">,
  anchor: PresenceAnchor,
  workArea: PresenceBounds
): PresenceBounds {
  const safe = clampCompanionAnchor(anchor, workArea);
  if (mode === "orb") {
    return { x: safe.x, y: safe.y, width: ORB_SIZE, height: ORB_SIZE };
  }
  const width = Math.min(MINI_WIDTH, workArea.width);
  const height = Math.min(MINI_HEIGHT, workArea.height);
  return {
    x: clamp(safe.x + ORB_SIZE - width, workArea.x, workArea.x + workArea.width - width),
    y: clamp(safe.y + ORB_SIZE - height, workArea.y, workArea.y + workArea.height - height),
    width,
    height,
  };
}

/** Âncora do orb a partir do retângulo atual, para gravar a posição depois do arrasto. */
export function companionAnchorFromBounds(
  mode: Exclude<PresenceMode, "hidden">,
  bounds: PresenceBounds
): PresenceAnchor {
  if (mode === "orb") {
    return { x: Math.round(bounds.x), y: Math.round(bounds.y) };
  }
  return {
    x: Math.round(bounds.x + bounds.width - ORB_SIZE),
    y: Math.round(bounds.y + bounds.height - ORB_SIZE),
  };
}

/** Só um par de inteiros é uma âncora válida vinda do disco. */
export function parsePresenceAnchor(raw: unknown): PresenceAnchor | null {
  if (!Array.isArray(raw) || raw.length !== 2) return null;
  const [x, y] = raw;
  if (typeof x !== "number" || typeof y !== "number") return null;
  if (!Number.isInteger(x) || !Number.isInteger(y)) return null;
  if (Math.abs(x) > 16384 || Math.abs(y) > 16384) return null;
  return { x, y };
}

/** Passo de arrasto aceito pelo main: inteiro, pequeno e não nulo em ambos os eixos. */
export function isValidDragStep(dx: unknown, dy: unknown): dx is number {
  return (
    typeof dx === "number" &&
    typeof dy === "number" &&
    Number.isInteger(dx) &&
    Number.isInteger(dy) &&
    Math.abs(dx) <= MAX_DRAG_STEP_PX &&
    Math.abs(dy) <= MAX_DRAG_STEP_PX
  );
}

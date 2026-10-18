/**
 * Mecanismo "bento light" (`desktop/src/shared/bento.tsx`) — LUNMI-GLASS-BENTO-COMPOSER.
 *
 * Adaptação mínima, em React/CSS puro, do mecanismo do MagicBento do React Bits
 * (David Haz; MIT + Commons Clause — aviso em `desktop/THIRD-PARTY-MOTION.md` e
 * `desktop/licenses/react-bits-MagicBento.txt`). Nada do demo original entra aqui:
 * sem estrelas/canvas (enableStars/particleCount inertes), sem tilt, sem magnetismo,
 * sem auto-hide de texto e sem GSAP. Ficam apenas as três ideias verificáveis:
 *
 * - seguimento exponencial da luz (`--bento-x/--bento-y`), fade de proximidade da
 *   borda (`--bento-near`) e do spotlight (`--bento-hot`);
 * - onda de clique finita desenhada pela máscara da borda (`--bento-wx/wy/wr/wa`);
 * - um único laço de rAF que nasce com o ponteiro e para quando tudo assenta —
 *   jamais fica girando com o ponteiro parado.
 *
 * O motor é um singleton de módulo: N superfícies registradas compartilham um par
 * de listeners de janela. Cor/raio/tema ficam no CSS (variáveis `--bento-*`), então
 * trocar de tema não exige tocar em JavaScript. Reduced-motion e visibilidade são
 * lidas em runtime: com motion reduzido o motor cancela o laço, limpa as variáveis
 * e ignora novos eventos; ao voltar, o próximo evento de ponteiro reacende. Cada
 * `register` devolve um unregister que remove listeners, timers e variáveis.
 */
import { useEffect, type RefObject } from "react";

export interface BentoOptions {
  /** Dispara a onda decorativa finita em pointerdown dentro da superfície. */
  wave?: boolean;
  /** Uma única onda curta de entrada ao registrar a superfície. */
  intro?: boolean;
}

const FOLLOW_TAU = 0.05;
const HOT_IN = 0.14, HOT_OUT = 0.32;
const NEAR_IN = 0.12, NEAR_OUT = 0.4;
const WAVE_SECONDS = 1.1;
const INTRO_GAIN = 0.55;
const VAR_NAMES = ["--bento-x", "--bento-y", "--bento-hot", "--bento-near", "--bento-wx", "--bento-wy", "--bento-wr", "--bento-wa"] as const;

interface Rect { left: number; top: number; width: number; height: number }
interface Wave { x: number; y: number; t: number; reach: number; gain: number }
interface Surface {
  el: HTMLElement;
  waveEnabled: boolean;
  rect: Rect | null;
  dirty: boolean;
  x: number;
  y: number;
  hot: number;
  near: number;
  wave: Wave | null;
  written: Record<string, string>;
  introTimer: number | undefined;
}

const surfaces = new Set<Surface>();
const pointer = { x: 0, y: 0, inside: false };
let raf = 0;
let last = 0;
let listening = false;
let introCursor = 0;
let motionMedia: MediaQueryList | null = null;

const reducedNow = () => motionMedia?.matches ?? false;
const motionAllowed = () => !document.hidden && !reducedNow();

function ensureMedia(): void {
  if (motionMedia || typeof window.matchMedia !== "function") return;
  motionMedia = window.matchMedia("(prefers-reduced-motion: reduce)");
}

function readRect(el: HTMLElement): Rect {
  const r = el.getBoundingClientRect();
  return { left: r.left, top: r.top, width: r.width, height: r.height };
}

const contains = (rect: Rect, x: number, y: number) =>
  x >= rect.left && x <= rect.left + rect.width && y >= rect.top && y <= rect.top + rect.height;

function write(s: Surface, name: string, value: string): void {
  if (s.written[name] === value) return;
  s.written[name] = value;
  s.el.style.setProperty(name, value);
}

function clearSurface(s: Surface): void {
  s.hot = 0;
  s.near = 0;
  s.wave = null;
  s.written = {};
  for (const name of VAR_NAMES) s.el.style.removeProperty(name);
}

function stopLoop(): void {
  if (!raf) return;
  cancelAnimationFrame(raf);
  raf = 0;
}

function resetAll(): void {
  stopLoop();
  for (const s of surfaces) clearSurface(s);
}

function markDirty(): void {
  for (const s of surfaces) s.dirty = true;
}

function wake(): void {
  ensureMedia();
  if (raf || surfaces.size === 0 || !motionAllowed()) return;
  // Nova geração: com o motor ocioso o layout pode ter mudado sem evento de resize
  // (lateral que colapsa/expande, troca de vista, painel que encolhe). A geometria de
  // cada superfície é relida no primeiro quadro da geração — nunca fica presa em cache.
  for (const s of surfaces) s.dirty = true;
  last = performance.now();
  raf = requestAnimationFrame(frame);
}

/** Um passo do motor para todas as superfícies; devolve true se ainda há movimento. */
function step(dt: number): boolean {
  let active = false;
  for (const s of surfaces) {
    if (s.dirty || !s.rect) {
      s.rect = readRect(s.el);
      s.dirty = false;
    }
    const over = pointer.inside && contains(s.rect, pointer.x, pointer.y);
    const hotTarget = over ? 1 : 0;
    if (over) {
      const k = 1 - Math.exp(-dt / FOLLOW_TAU);
      s.x += (pointer.x - s.rect.left - s.x) * k;
      s.y += (pointer.y - s.rect.top - s.y) * k;
    }
    s.hot += (hotTarget - s.hot) * (1 - Math.exp(-dt / (hotTarget > s.hot ? HOT_IN : HOT_OUT)));
    if (Math.abs(hotTarget - s.hot) < 0.002) s.hot = hotTarget;
    const nearTarget = over ? 1 : 0;
    s.near += (nearTarget - s.near) * (1 - Math.exp(-dt / (nearTarget > s.near ? NEAR_IN : NEAR_OUT)));
    if (Math.abs(nearTarget - s.near) < 0.002) s.near = nearTarget;

    let waveRadius = 0, waveAlpha = 0;
    if (s.wave) {
      s.wave.t += dt;
      const progress = Math.min(1, s.wave.t / WAVE_SECONDS);
      waveRadius = s.wave.reach * (1 - Math.pow(1 - progress, 2.2));
      waveAlpha = Math.pow(1 - progress, 1.3) * s.wave.gain;
      if (progress >= 1) s.wave = null;
      active = true;
    }

    write(s, "--bento-x", `${s.x.toFixed(1)}px`);
    write(s, "--bento-y", `${s.y.toFixed(1)}px`);
    write(s, "--bento-hot", s.hot.toFixed(3));
    write(s, "--bento-near", s.near.toFixed(3));
    if (s.wave || waveRadius > 0) {
      write(s, "--bento-wx", `${s.wave?.x.toFixed(1) ?? "0"}px`);
      write(s, "--bento-wy", `${s.wave?.y.toFixed(1) ?? "0"}px`);
      write(s, "--bento-wr", `${waveRadius.toFixed(1)}px`);
      write(s, "--bento-wa", waveAlpha.toFixed(3));
    } else {
      write(s, "--bento-wa", "0.000");
      write(s, "--bento-wr", "0.0px");
    }

    if (s.hot !== hotTarget || s.near !== nearTarget || s.wave) active = true;
  }
  return active;
}

function frame(now: number): void {
  raf = 0;
  const dt = Math.max(0, Math.min(0.064, (now - last) / 1000));
  last = now;
  const active = step(dt);
  if (active && motionAllowed()) raf = requestAnimationFrame(frame);
}

function onPointerMove(event: PointerEvent): void {
  if (event.pointerType === "touch") return;
  pointer.x = event.clientX;
  pointer.y = event.clientY;
  pointer.inside = true;
  // O layout pode mudar sem evento de resize (troca de vista, colapso de painel, troca de
  // tema, chip que reflui): a cada movimento a geometria de todas as superfícies é relida
  // no quadro seguinte — o motor nunca aponta para um retângulo velho.
  for (const s of surfaces) s.dirty = true;
  wake();
}

function onPointerOut(event: PointerEvent): void {
  if (event.relatedTarget) return;
  pointer.inside = false;
  wake();
}

function onPointerDown(event: PointerEvent): void {
  if (event.pointerType === "touch" || !motionAllowed()) return;
  pointer.x = event.clientX;
  pointer.y = event.clientY;
  pointer.inside = true;
  for (const s of surfaces) s.dirty = true;
  for (const s of surfaces) {
    if (!s.waveEnabled) continue;
    if (s.dirty || !s.rect) { s.rect = readRect(s.el); s.dirty = false; }
    if (!contains(s.rect, event.clientX, event.clientY)) continue;
    const x = event.clientX - s.rect.left, y = event.clientY - s.rect.top;
    const reach = Math.hypot(Math.max(x, s.rect.width - x), Math.max(y, s.rect.height - y)) + 140;
    s.wave = { x, y, t: 0, reach, gain: 1 };
  }
  wake();
}

function onVisibility(): void {
  if (document.hidden) stopLoop();
}

function onMotionChange(): void {
  if (reducedNow()) resetAll();
  else wake();
}

function attachListeners(): void {
  if (listening) return;
  listening = true;
  window.addEventListener("pointermove", onPointerMove as EventListener, { passive: true });
  window.addEventListener("pointerdown", onPointerDown as EventListener, { passive: true });
  window.addEventListener("resize", markDirty);
  window.addEventListener("scroll", markDirty, { capture: true, passive: true });
  document.addEventListener("pointerout", onPointerOut as EventListener);
  document.addEventListener("visibilitychange", onVisibility);
  ensureMedia();
  motionMedia?.addEventListener("change", onMotionChange);
}

function detachListeners(): void {
  if (!listening) return;
  listening = false;
  window.removeEventListener("pointermove", onPointerMove as EventListener);
  window.removeEventListener("pointerdown", onPointerDown as EventListener);
  window.removeEventListener("resize", markDirty);
  window.removeEventListener("scroll", markDirty, { capture: true } as EventListenerOptions);
  document.removeEventListener("pointerout", onPointerOut as EventListener);
  document.removeEventListener("visibilitychange", onVisibility);
  motionMedia?.removeEventListener("change", onMotionChange);
}

function registerSurface(el: HTMLElement, options: BentoOptions): () => void {
  const initialRect = readRect(el);
  const surface: Surface = {
    el, waveEnabled: options.wave !== false,
    rect: initialRect, dirty: false,
    x: 0, y: 0, hot: 0, near: 0, wave: null, written: {},
    introTimer: undefined,
  };
  surface.x = initialRect.width / 2;
  surface.y = initialRect.height / 2;
  surfaces.add(surface);
  attachListeners();
  if (options.intro !== false && motionAllowed()) {
    const delay = 90 * introCursor++;
    surface.introTimer = window.setTimeout(() => {
      surface.introTimer = undefined;
      if (!motionAllowed()) return;
      if (surface.dirty || !surface.rect) surface.rect = readRect(surface.el);
      const x = surface.rect.width * 0.3, y = surface.rect.height * 0.22;
      const reach = Math.hypot(Math.max(x, surface.rect.width - x), Math.max(y, surface.rect.height - y)) + 140;
      surface.wave = { x, y, t: 0, reach, gain: INTRO_GAIN };
      wake();
    }, delay);
  }
  return () => {
    if (surface.introTimer !== undefined) window.clearTimeout(surface.introTimer);
    surfaces.delete(surface);
    clearSurface(surface);
    if (surfaces.size === 0) {
      stopLoop();
      detachListeners();
    }
  };
}

/** Registra uma superfície decorativa e devolve o unregister (para o useEffect). */
export function registerBentoSurface(el: HTMLElement, options: BentoOptions = {}): () => void {
  return registerSurface(el, options);
}

/** Ativa o mecanismo numa referência de elemento enquanto o componente viver. */
export function useBentoSurface(ref: RefObject<HTMLElement | null>, options: BentoOptions = {}): void {
  const { wave = true, intro = true } = options;
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    return registerSurface(el, { wave, intro });
  }, [ref, wave, intro]);
}

/** Internos do motor para testes e instrumentação; não fazem parte da UI do produto. */
export const __bentoInternals = {
  surfaceCount: () => surfaces.size,
  hasPendingFrame: () => raf !== 0,
  /** Avança o motor em passos de 1/60 s sem depender do rAF do ambiente. */
  advance: (seconds: number) => {
    stopLoop();
    if (!motionAllowed()) return;
    const dt = 1 / 60;
    for (let t = 0; t < seconds; t += dt) step(dt);
  },
  /** Avança até tudo assentar (fade + onda terminados). */
  settle: () => {
    stopLoop();
    if (!motionAllowed()) return;
    const dt = 1 / 60;
    for (let t = 0; t < 4; t += dt) step(dt);
  },
  /** Devolve o módulo ao estado inicial (para suítes de teste e instrumentos). */
  reset: () => {
    for (const surface of [...surfaces]) {
      if (surface.introTimer !== undefined) window.clearTimeout(surface.introTimer);
      surfaces.delete(surface);
      clearSurface(surface);
    }
    pointer.x = 0; pointer.y = 0; pointer.inside = false;
    stopLoop();
    detachListeners();
    motionMedia = null;
    introCursor = 0;
  },
};

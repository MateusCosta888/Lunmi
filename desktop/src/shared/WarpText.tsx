/**
 * WarpText — React Bits, adapted for the Lunmi header (`desktop/src/shared/WarpText.tsx`).
 *
 * Origin: the official registry item `@react-bits/WarpText-JS-CSS`
 * (https://reactbits.dev/text-animations/warp-text, source
 * https://github.com/DavidHDev/react-bits/tree/main/src/content/TextAnimations/WarpText),
 * license MIT + Commons Clause, © 2026 David Haz. Full notice in
 * `desktop/licenses/react-bits-WarpText.txt` and `desktop/THIRD-PARTY-MOTION.md`.
 *
 * Faithful to the upstream mechanism: the same WebGL2 fragment shader (fbm ambient warp,
 * pointer lens, ripple, chromatic dispersion, click shock) driven by `ogl`, the same uniform
 * names and defaults. Every prop in the surface below is declared by the official revision
 * inspected on 2026-10-16 (`main`); none of them is inert. This vendored copy is adapted to this
 * project only:
 * - TypeScript types and an explicit prop surface (the upstream file is untyped JSX);
 * - a real DOM text label that is always present for accessibility and is shown as the static
 *   fallback whenever WebGL is unavailable, the context is lost, or the system asks for reduced
 *   transparency — the upstream only rendered a fallback on failure and used `role="img"`;
 * - the canvas is `aria-hidden` so the label is never doubled for assistive tech;
 * - a null WebGL context is handled explicitly (upstream would throw after its try/catch), and a
 *   lost context removes the canvas instead of leaving it above the label (the browser paints a
 *   broken-image plate over the mark otherwise);
 * - the effect re-runs only when the reduced-transparency preference flips, so a theme change
 *   never multiplies the RAF loop;
 * - the pointer displacement keeps a floor of about 1.2 physical pixels (see `pointerWarp`), so
 *   the effect stays real in a ~30 px header instead of falling below one pixel. At the upstream
 *   showcase size the floor is smaller than the original UV term and the drawing is unchanged;
 * - with reduced motion the text is completely static: no ambient drift, no pointer warp and no
 *   click ripple (upstream still rippled under reduced motion). A wave already in flight is zeroed
 *   the moment the preference flips (upstream let it keep ageing and painting), the shader
 *   multiplies the click shock by `uMotion` as a second guarantee, and the RAF loop parks instead
 *   of repainting an unchanged text — it wakes on motion return, geometry (ResizeObserver) and
 *   theme ink (MutationObserver on `<html>`), which also replaced the old in-loop colour poll.
 *
 * Not a generic CSS animation: the requested effect is the real upstream shader.
 */
import React, { useEffect, useRef, useState } from "react";
import { Renderer, Program, Mesh, Triangle, Texture } from "ogl";
import "./WarpText.css";

export type WarpTextLens = "bulge" | "pinch" | "swirl";

export interface WarpTextProps {
  text?: string;
  color?: string;
  warpStrength?: number;
  warpScale?: number;
  speed?: number;
  pointerInfluence?: number;
  pointerStrength?: number;
  refraction?: number;
  chromatic?: number;
  ripple?: boolean;
  clickRipple?: boolean;
  idleDrift?: number;
  lens?: WarpTextLens;
  intro?: boolean;
  fontSize?: number | string;
  fontWeight?: number | string;
  fontFamily?: string;
  letterSpacing?: number | string;
  lineHeight?: number | string;
  className?: string;
  style?: React.CSSProperties;
  testId?: string;
}

const LENS_MODES: Record<WarpTextLens, number> = { bulge: 0, pinch: 1, swirl: 2 };
const INTRO_MS = 1600;
const RIPPLE_LIFE = 1.6;
const MAX_CLICKS = 4;

const vertex = `#version 300 es
in vec2 position;
in vec2 uv;
out vec2 vUv;
void main() {
  vUv = uv;
  gl_Position = vec4(position, 0.0, 1.0);
}
`;

const fragment = `#version 300 es
precision highp float;

uniform sampler2D uTextTexture;
uniform vec2 uResolution;
uniform vec2 uPointer;
uniform float uPointerActive;
uniform float uTime;
uniform float uWarpStrength;
uniform float uWarpScale;
uniform float uSpeed;
uniform float uPointerInfluence;
uniform float uPointerStrength;
uniform float uRefraction;
uniform float uChromatic;
uniform float uRipple;
uniform float uMotion;
uniform float uLens;
uniform float uIntro;
uniform float uReveal;
uniform float uBackdrop;
uniform vec4 uClicks[4];

in vec2 vUv;
out vec4 fragColor;

float hash(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}

float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);

  float a = hash(i);
  float b = hash(i + vec2(1.0, 0.0));
  float c = hash(i + vec2(0.0, 1.0));
  float d = hash(i + vec2(1.0, 1.0));

  return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
}

float fbm(vec2 p) {
  float value = 0.0;
  float amplitude = 0.5;
  for (int i = 0; i < 4; i++) {
    value += amplitude * noise(p);
    p *= 2.02;
    amplitude *= 0.5;
  }
  return value;
}

vec4 sampleText(vec2 uv) {
  if (uv.x < 0.0 || uv.x > 1.0 || uv.y < 0.0 || uv.y > 1.0) {
    return vec4(0.0);
  }
  return texture(uTextTexture, uv);
}

void main() {
  vec2 uv = vUv;
  float aspect = uResolution.x / max(uResolution.y, 1.0);
  // Um pixel físico em UV: piso da ondulação do ponteiro (ver pointerWarp abaixo).
  vec2 pixel = vec2(1.0 / max(uResolution.x, 1.0), 1.0 / max(uResolution.y, 1.0));
  float time = uTime * uSpeed;
  float scale = max(uWarpScale, 0.001);

  vec2 drift = vec2(time * 0.055, -time * 0.045);
  float n1 = fbm(uv * scale * 3.1 + drift);
  float n2 = fbm((uv + 19.17) * scale * 3.4 - drift.yx);
  vec2 ambient = (vec2(n1, n2) - 0.5) * (uWarpStrength * 0.045 * uMotion + uIntro * 0.18);

  vec2 pointerDelta = uv - uPointer;
  vec2 aspectDelta = vec2(pointerDelta.x * aspect, pointerDelta.y);
  float dist = length(aspectDelta);
  float radius = max(uPointerInfluence, 0.001);
  float t = clamp(dist / radius, 0.0, 1.0);
  float lens = smoothstep(radius, 0.0, dist) * uPointerActive;
  float bulge = t * (1.0 - t) * (1.0 - t) * 6.75 * uPointerActive;
  vec2 dir = dist > 0.0001 ? vec2(aspectDelta.x / aspect, aspectDelta.y) / dist : vec2(0.0);

  float rippleWave = sin(dist * 28.0 - time * 4.2) * 0.5 + 0.5;
  float rippleRing = (rippleWave - 0.5) * uRipple;
  vec2 pointerWarp = dir * rippleRing * bulge * uPointerStrength * 0.016;
  // Calibração para o cabeçalho (a pedido: efeito sutil, porém real no tamanho final). Numa
  // cápsula de ~30 px de altura os deslocamentos em UV acima caem abaixo de um pixel e o
  // ponteiro não muda nada. Este termo dá ~1,2 px físicos de ondulação no anel do ponteiro
  // (o bulge máximo é 1, em t = 1/3) em qualquer tamanho, sem deformar as letras. No tamanho
  // de vitrine do upstream ele fica menor que o termo em UV e não altera o desenho original.
  pointerWarp += dir * bulge * uPointerStrength * pixel * 3.2;

  if (uLens > 1.5) {
    float twist = (1.0 - t) * (1.0 - t) * uPointerActive * uPointerStrength * 2.4;
    float s = sin(twist);
    float c = cos(twist);
    vec2 turned = vec2(aspectDelta.x * c - aspectDelta.y * s, aspectDelta.x * s + aspectDelta.y * c);
    pointerWarp += vec2(turned.x / aspect, turned.y) - pointerDelta;
  } else {
    float side = uLens > 0.5 ? -1.0 : 1.0;
    pointerWarp -= dir * side * bulge * uPointerStrength * 0.045;
  }

  vec2 shock = vec2(0.0);
  float shockLight = 0.0;
  for (int i = 0; i < 4; i++) {
    vec4 click = uClicks[i];
    if (click.w <= 0.0) continue;
    // Multiplicar por uMotion é a garantia no shader: com movimento reduzido a onda não desenha
    // nada, mesmo que um valor residual chegue aos uniformes (o estado em JS também é zerado).
    vec2 clickDelta = uv - click.xy;
    vec2 clickAspect = vec2(clickDelta.x * aspect, clickDelta.y);
    float clickDist = length(clickAspect);
    float age = click.z;
    float offset = (clickDist - age * 0.9) / (0.06 + age * 0.06);
    float fade = 1.0 - smoothstep(0.0, 1.6, age);
    float energy = exp(-offset * offset) * fade * smoothstep(0.0, 0.08, age) * click.w;
    vec2 clickDir = clickDist > 0.0001 ? vec2(clickAspect.x / aspect, clickAspect.y) / clickDist : vec2(0.0);
    shock -= clickDir * offset * energy * 0.09 * uMotion;
    shockLight += energy * uMotion;
  }

  vec2 bend = pointerWarp + shock;
  vec2 rest = uv + ambient;
  vec2 splitDir = ambient + bend;
  float splitLen = length(splitDir);
  splitDir = splitLen > 0.00001 ? splitDir / splitLen : vec2(0.7071, 0.7071);
  vec2 split = splitDir * uRefraction * 0.16 * (0.35 + lens * 1.65 + uIntro * 3.0);
  float dispersion = uChromatic * 0.5;

  vec3 ink = vec3(0.0);
  vec3 cover = vec3(0.0);
  vec3 weight = vec3(0.0);
  for (int i = 0; i < 7; i++) {
    float k = 1.0 - float(i) / 3.0;
    vec4 tap = sampleText(rest + bend * (1.0 + dispersion * k) + split * k);
    vec3 band = vec3(max(k, 0.0), 1.0 - abs(k), max(-k, 0.0));
    ink += tap.rgb * band;
    cover += tap.a * band;
    weight += band;
  }
  ink /= weight;
  cover /= weight;
  float a = max(max(cover.r, cover.g), cover.b);
  vec3 color = ink + (a - cover) * uBackdrop;
  color = min(color + (lens + shockLight * 0.5) * cover.g * 0.055, vec3(a));
  fragColor = vec4(color, a) * uReveal;
}
`;

const getFontValue = (value: number | string): string => (typeof value === "number" ? `${value}px` : value);
const smooth = (value: number): number => value * value * (3 - 2 * value);

const getLuminance = (color: string): number => {
  const probe = document.createElement("canvas");
  probe.width = 1;
  probe.height = 1;
  const ctx = probe.getContext("2d", { willReadFrequently: true });
  if (!ctx) return 1;
  ctx.fillStyle = color;
  ctx.fillRect(0, 0, 1, 1);
  const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
  return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
};

const measureLine = (ctx: CanvasRenderingContext2D, line: string, letterSpacing: number): number => {
  const chars = Array.from(line);
  const textWidth = chars.reduce((width, char) => width + ctx.measureText(char).width, 0);
  return textWidth + Math.max(0, chars.length - 1) * letterSpacing;
};

const drawLine = (ctx: CanvasRenderingContext2D, line: string, x: number, y: number, letterSpacing: number): void => {
  const chars = Array.from(line);
  let cursor = x - measureLine(ctx, line, letterSpacing) / 2;
  chars.forEach((char, index) => {
    ctx.fillText(char, cursor, y);
    cursor += ctx.measureText(char).width + (index === chars.length - 1 ? 0 : letterSpacing);
  });
};

interface RasterProps {
  text: string;
  color: string;
  fontFamily: string;
  fontSize: number | string;
  fontWeight: number | string;
  letterSpacing: number | string;
  lineHeight: number | string;
}

const buildTextCanvas = ({
  container,
  width,
  height,
  dpr,
  props,
}: {
  container: HTMLElement;
  width: number;
  height: number;
  dpr: number;
  props: RasterProps;
}): { canvas: HTMLCanvasElement; ink: string } => {
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.floor(width * dpr));
  canvas.height = Math.max(1, Math.floor(height * dpr));
  const ctx = canvas.getContext("2d");
  if (!ctx) return { canvas, ink: props.color };

  const probe = document.createElement("span");
  probe.textContent = props.text;
  Object.assign(probe.style, {
    position: "absolute",
    visibility: "hidden",
    pointerEvents: "none",
    whiteSpace: "pre",
    inset: "0 auto auto 0",
    color: props.color,
    fontFamily: props.fontFamily,
    fontSize: getFontValue(props.fontSize),
    fontWeight: String(props.fontWeight),
    letterSpacing: getFontValue(props.letterSpacing),
    lineHeight: typeof props.lineHeight === "number" ? String(props.lineHeight) : props.lineHeight,
  });
  container.appendChild(probe);
  const computed = window.getComputedStyle(probe);
  let fontSizePx = parseFloat(computed.fontSize) || 96;
  const ink = computed.color || props.color;
  const fontFamily = computed.fontFamily || "sans-serif";
  const fontWeight = computed.fontWeight || String(props.fontWeight);
  let letterSpacing = computed.letterSpacing === "normal" ? 0 : parseFloat(computed.letterSpacing) || 0;
  let lineHeight = parseFloat(computed.lineHeight);
  if (!Number.isFinite(lineHeight)) {
    lineHeight = fontSizePx * (typeof props.lineHeight === "number" ? props.lineHeight : 0.92);
  }
  probe.remove();

  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillStyle = ink;
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";

  const lines = String(props.text || "").split("\n");
  const applyFont = () => {
    ctx.font = `${fontWeight} ${fontSizePx}px ${fontFamily}`;
  };
  applyFont();

  const maxWidth = width * 0.86;
  const maxHeight = height * 0.78;
  const widest = Math.max(...lines.map(line => measureLine(ctx, line, letterSpacing)), 1);
  const blockHeight = Math.max(lineHeight * lines.length, 1);
  const fit = Math.min(1, maxWidth / widest, maxHeight / blockHeight);

  if (fit < 1) {
    fontSizePx *= fit;
    letterSpacing *= fit;
    lineHeight *= fit;
    applyFont();
  }

  const startY = height / 2 - (lineHeight * (lines.length - 1)) / 2;
  lines.forEach((line, index) => drawLine(ctx, line, width / 2, startY + index * lineHeight, letterSpacing));

  return { canvas, ink };
};

interface SyncProps {
  warpStrength: number;
  warpScale: number;
  speed: number;
  pointerInfluence: number;
  pointerStrength: number;
  refraction: number;
  chromatic: number;
  ripple: boolean;
  lens: WarpTextLens;
}

const syncUniforms = (program: Program, props: SyncProps): void => {
  const uniforms = program.uniforms;
  uniforms.uWarpStrength.value = props.warpStrength;
  uniforms.uWarpScale.value = props.warpScale;
  uniforms.uSpeed.value = props.speed;
  uniforms.uPointerInfluence.value = props.pointerInfluence;
  uniforms.uPointerStrength.value = props.pointerStrength;
  uniforms.uRefraction.value = props.refraction;
  uniforms.uChromatic.value = props.chromatic;
  uniforms.uRipple.value = props.ripple ? 1 : 0;
  uniforms.uLens.value = LENS_MODES[props.lens] ?? 0;
};

const prefersReducedTransparency = (): boolean =>
  window.matchMedia?.("(prefers-reduced-transparency: reduce)").matches ?? false;

export function WarpText({
  text = "Lunmi",
  color = "currentColor",
  warpStrength = 0.08,
  warpScale = 1.7,
  speed = 0.55,
  pointerInfluence = 0.42,
  pointerStrength = 0.38,
  refraction = 0.018,
  chromatic = 0.35,
  ripple = true,
  clickRipple = true,
  idleDrift = 0.2,
  lens = "bulge",
  intro = true,
  fontSize = 22,
  fontWeight = 800,
  fontFamily = "inherit",
  letterSpacing = "-0.06em",
  lineHeight = 0.9,
  className = "",
  style,
  testId,
}: WarpTextProps): React.ReactElement {
  const containerRef = useRef<HTMLSpanElement>(null);
  const propsRef = useRef({
    text, color, fontSize, fontWeight, fontFamily, letterSpacing, lineHeight,
    warpStrength, warpScale, speed, pointerInfluence, pointerStrength, refraction, chromatic, ripple, clickRipple, idleDrift, lens, intro,
  });
  const contextRef = useRef<{ program: Program; rasterize: () => Promise<void> } | null>(null);
  // "text" keeps the readable DOM label visible until the WebGL texture is painted.
  const [mode, setMode] = useState<"text" | "canvas">("text");
  const [staticMode, setStaticMode] = useState<boolean>(prefersReducedTransparency);

  useEffect(() => {
    const media = window.matchMedia?.("(prefers-reduced-transparency: reduce)");
    if (!media) return;
    const sync = (event: MediaQueryListEvent) => setStaticMode(event.matches);
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    propsRef.current = {
      text, color, fontSize, fontWeight, fontFamily, letterSpacing, lineHeight,
      warpStrength, warpScale, speed, pointerInfluence, pointerStrength, refraction, chromatic, ripple, clickRipple, idleDrift, lens, intro,
    };
    if (contextRef.current) syncUniforms(contextRef.current.program, propsRef.current);
  }, [
    text, color, fontSize, fontWeight, fontFamily, letterSpacing, lineHeight,
    warpStrength, warpScale, speed, pointerInfluence, pointerStrength, refraction, chromatic, ripple, clickRipple, idleDrift, lens, intro,
  ]);

  useEffect(() => {
    contextRef.current?.rasterize().catch(() => undefined);
  }, [text, color, fontSize, fontWeight, fontFamily, letterSpacing, lineHeight]);

  useEffect(() => {
    if (staticMode) { setMode("text"); return undefined; }
    const container = containerRef.current;
    if (!container || typeof window === "undefined") return undefined;

    let renderer: Renderer | undefined;
    let gl: Renderer["gl"] | undefined;
    let program: Program | undefined;
    let geometry: Triangle | undefined;
    let mesh: Mesh | undefined;
    let texture: Texture | undefined;
    let resizeObserver: ResizeObserver | undefined;
    let intersectionObserver: IntersectionObserver | undefined;
    let raf = 0;
    let disposed = false;
    let contextLost = false;
    let visible = true;
    let pageVisible = !document.hidden;
    let reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    let rasterVersion = 0;
    let textReady = false;
    let introProgress = propsRef.current.intro && !reduceMotion ? 0 : 1;
    let lastFrame = 0;

    const pointer = { x: 0.5, y: 0.5, tx: 0.5, ty: 0.5, active: 0, activeTarget: 0 };
    const clicks = Array.from({ length: MAX_CLICKS }, () => [0.5, 0.5, 0, 0]);
    let clickSlot = 0;
    const startTime = performance.now();

    try {
      renderer = new Renderer({ webgl: 2, alpha: true, premultipliedAlpha: true, antialias: true, dpr: Math.min(window.devicePixelRatio || 1, 2) });
      gl = renderer.gl;
    } catch (error) {
      console.warn("WarpText: WebGL could not be initialized.", error);
      setMode("text");
      return undefined;
    }
    if (!renderer || !gl) {
      console.warn("WarpText: WebGL could not be initialized.");
      setMode("text");
      return undefined;
    }

    gl.clearColor(0, 0, 0, 0);
    const canvas = gl.canvas;
    canvas.style.position = "absolute";
    canvas.style.inset = "0";
    canvas.style.width = "100%";
    canvas.style.height = "100%";
    canvas.style.display = "block";
    canvas.setAttribute("aria-hidden", "true");
    container.appendChild(canvas);

    texture = new Texture(gl, {
      generateMipmaps: false,
      premultiplyAlpha: true,
      minFilter: gl.LINEAR,
      magFilter: gl.LINEAR,
      wrapS: gl.CLAMP_TO_EDGE,
      wrapT: gl.CLAMP_TO_EDGE,
    });

    geometry = new Triangle(gl);
    program = new Program(gl, {
      vertex,
      fragment,
      transparent: true,
      depthTest: false,
      depthWrite: false,
      uniforms: {
        uTextTexture: { value: texture },
        uResolution: { value: new Float32Array([1, 1]) },
        uPointer: { value: new Float32Array([0.5, 0.5]) },
        uPointerActive: { value: 0 },
        uTime: { value: 0 },
        uWarpStrength: { value: propsRef.current.warpStrength },
        uWarpScale: { value: propsRef.current.warpScale },
        uSpeed: { value: propsRef.current.speed },
        uPointerInfluence: { value: propsRef.current.pointerInfluence },
        uPointerStrength: { value: propsRef.current.pointerStrength },
        uRefraction: { value: propsRef.current.refraction },
        uChromatic: { value: propsRef.current.chromatic },
        uRipple: { value: propsRef.current.ripple ? 1 : 0 },
        uMotion: { value: reduceMotion ? 0 : 1 },
        uLens: { value: LENS_MODES[propsRef.current.lens] ?? 0 },
        uIntro: { value: 0 },
        uReveal: { value: 0 },
        uBackdrop: { value: 0 },
        uClicks: { value: clicks },
      },
    });
    mesh = new Mesh(gl, { geometry, program });

    const renderOnce = () => {
      if (disposed || contextLost || !renderer || !mesh) return;
      renderer.render({ scene: mesh });
    };

    const rasterize = async () => {
      const version = ++rasterVersion;
      if (document.fonts?.load) {
        try { await document.fonts.load(`${String(propsRef.current.fontWeight)} ${getFontValue(propsRef.current.fontSize)} "Plus Jakarta Sans"`); }
        catch { /* fallback to the installed sans */ }
      }
      if (document.fonts?.ready) {
        try { await document.fonts.ready; } catch { /* ignore */ }
      }
      if (disposed || contextLost || version !== rasterVersion) return;
      const rect = container.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;

      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const raster = buildTextCanvas({ container, width: rect.width, height: rect.height, dpr, props: propsRef.current });
      texture!.image = raster.canvas;
      texture!.needsUpdate = true;
      program!.uniforms.uBackdrop.value = getLuminance(raster.ink) < 0.5 ? 1 : 0;
      textReady = true;
      renderOnce();
      if (!disposed && !contextLost) setMode("canvas");
    };

    const resize = () => {
      if (disposed || contextLost || !renderer || !program) return;
      const rect = container.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      renderer.dpr = Math.min(window.devicePixelRatio || 1, 2);
      renderer.setSize(rect.width, rect.height);
      program.uniforms.uResolution.value[0] = gl!.drawingBufferWidth;
      program.uniforms.uResolution.value[1] = gl!.drawingBufferHeight;
      void rasterize();
    };

    const onPointerMove = (event: PointerEvent) => {
      if (event.pointerType === "touch") return;
      const rect = canvas.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      pointer.tx = (event.clientX - rect.left) / rect.width;
      pointer.ty = 1 - (event.clientY - rect.top) / rect.height;
      pointer.activeTarget = 1;
    };
    const onPointerLeave = () => { pointer.activeTarget = 0; };
    const onPointerDown = (event: PointerEvent) => {
      if (!propsRef.current.clickRipple || reduceMotion) return;
      const rect = canvas.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      const click = clicks[clickSlot];
      clickSlot = (clickSlot + 1) % clicks.length;
      click[0] = (event.clientX - rect.left) / rect.width;
      click[1] = 1 - (event.clientY - rect.top) / rect.height;
      click[2] = 0;
      click[3] = 1;
    };
    const onContextLost = (event: Event) => {
      event.preventDefault();
      contextLost = true;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      // O canvas perdido é RETIRADO, não só escondido: o Chromium repinta um canvas com contexto
      // perdido como uma placa de imagem quebrada, e ele está por cima do rótulo DOM — o modo
      // "text" sozinho deixava a marca coberta. `remove()` é idempotente e o cleanup confere o
      // `parentNode` antes de remover de novo.
      canvas.remove();
      setMode("text");
    };
    const onVisibility = () => {
      pageVisible = !document.hidden;
      if (pageVisible && visible && !reduceMotion && !raf) raf = requestAnimationFrame(loop);
      if (!pageVisible && raf) { cancelAnimationFrame(raf); raf = 0; }
    };
    /**
     * Estado estático exigido por `prefers-reduced-motion`. Zera TODO transiente (ondas de clique
     * em voo inclusive: antes elas continuavam envelhecendo e o shader seguia desenhando
     * shock/shockLight, então o texto continuava se mexendo depois da preferência ligar) e escreve
     * os uniformes que definem o quadro parado. É a única fonte do quadro reduzido, usada pelo
     * listener de mídia e pelo próprio laço, para os dois caminhos concordarem.
     */
    const applyStaticFrame = () => {
      if (!program) return;
      clicks.forEach(click => { click[2] = 0; click[3] = 0; });
      program.uniforms.uMotion.value = 0;
      program.uniforms.uTime.value = 0;
      program.uniforms.uPointerActive.value = 0;
      program.uniforms.uIntro.value = 0;
      program.uniforms.uReveal.value = textReady ? 1 : 0;
      renderOnce();
    };
    const mediaQuery = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    const onReducedMotion = (event: MediaQueryListEvent) => {
      reduceMotion = event.matches;
      if (reduceMotion) {
        introProgress = 1;
        // Nada se move: um quadro estático e nenhum agendamento pendente.
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
        applyStaticFrame();
        return;
      }
      if (program) program.uniforms.uMotion.value = 1;
      renderOnce();
      if (visible && pageVisible && !raf) raf = requestAnimationFrame(loop);
    };
    // A cor vem de `currentColor` (o tema troca a cor computada sem mudar props). Um observador de
    // atributo no <html> repinta a textura uma vez, o que hoje também substitui a sondagem por
    // contagem de quadros dentro do laço — sem ela o laço podia ficar parado no movimento reduzido.
    const themeObserver = new MutationObserver(() => { void rasterize(); });
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "style", "class"] });

    const loop = (now: number) => {
      raf = 0;
      if (disposed || contextLost || !renderer || !program) return;
      // Movimento reduzido: pinta o quadro estático uma vez e NÃO reagenda. Nada se move, então
      // continuar o laço só repintaria o mesmo texto. Os caminhos que precisam acordar de novo são
      // o listener de movimento (voltar a animar), o ResizeObserver (geometria) e o observador de
      // tema (tinta) — todos fora do laço.
      if (reduceMotion) {
        applyStaticFrame();
        return;
      }
      const elapsed = (now - startTime) * 0.001;
      const delta = lastFrame ? Math.min(now - lastFrame, 50) : 16;
      lastFrame = now;

      if (textReady && introProgress < 1) introProgress = Math.min(1, introProgress + delta / INTRO_MS);
      const settle = 1 - introProgress;

      const drift = Math.min(Math.max(propsRef.current.idleDrift, 0), 1);
      const engaged = pointer.activeTarget > 0;
      const idleX = 0.5 + Math.sin(elapsed * 0.33) * (0.08 + drift * 0.2);
      const idleY = 0.5 + Math.cos(elapsed * 0.27) * (0.06 + drift * 0.2);
      const follow = 1 - Math.exp(-delta / (engaged ? 130 : 470));

      pointer.x += ((engaged ? pointer.tx : idleX) - pointer.x) * follow;
      pointer.y += ((engaged ? pointer.ty : idleY) - pointer.y) * follow;
      pointer.active += ((engaged ? 1 : drift) - pointer.active) * (1 - Math.exp(-delta / 270));

      clicks.forEach(click => {
        if (click[3] <= 0) return;
        click[2] += delta / 1000;
        if (click[2] > RIPPLE_LIFE) click[3] = 0;
      });

      program.uniforms.uPointer.value[0] = pointer.x;
      program.uniforms.uPointer.value[1] = pointer.y;
      program.uniforms.uPointerActive.value = pointer.active;
      program.uniforms.uTime.value = elapsed;
      program.uniforms.uIntro.value = settle * settle * settle;
      program.uniforms.uReveal.value = textReady ? smooth(Math.min(1, introProgress / 0.45)) : 0;

      renderOnce();
      raf = requestAnimationFrame(loop);
    };

    resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(container);
    intersectionObserver = new IntersectionObserver(
      ([entry]) => {
        visible = entry.isIntersecting;
        if (visible && pageVisible && !reduceMotion && !raf) raf = requestAnimationFrame(loop);
        if (!visible && raf) { cancelAnimationFrame(raf); raf = 0; }
      },
      { threshold: 0 }
    );
    intersectionObserver.observe(container);

    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerleave", onPointerLeave);
    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("webglcontextlost", onContextLost, false);
    document.addEventListener("visibilitychange", onVisibility);
    mediaQuery?.addEventListener("change", onReducedMotion);

    syncUniforms(program, propsRef.current);
    contextRef.current = { program, rasterize };
    resize();
    raf = requestAnimationFrame(loop);

    return () => {
      disposed = true;
      contextRef.current = null;
      if (raf) cancelAnimationFrame(raf);
      resizeObserver?.disconnect();
      intersectionObserver?.disconnect();
      themeObserver.disconnect();
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerleave", onPointerLeave);
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("webglcontextlost", onContextLost);
      document.removeEventListener("visibilitychange", onVisibility);
      mediaQuery?.removeEventListener("change", onReducedMotion);
      if (!contextLost) {
        try {
          if (texture?.texture) gl!.deleteTexture(texture.texture);
          geometry?.remove?.();
          program?.remove?.();
          gl!.getExtension("WEBGL_lose_context")?.loseContext();
        } catch { /* nothing left to release */ }
      }
      if (canvas.parentNode === container) container.removeChild(canvas);
    };
  }, [staticMode]);

  return (
    <span
      ref={containerRef}
      className={`lunmi-warp ${className}`.trim()}
      style={style}
      data-testid={testId}
      data-mode={mode}
      data-warp-engine="react-bits-warptext"
    >
      <span
        className="lunmi-warp-label"
        style={{
          color,
          fontFamily,
          fontWeight,
          fontSize: getFontValue(fontSize),
          letterSpacing: getFontValue(letterSpacing),
          lineHeight,
        }}
      >
        {text}
      </span>
    </span>
  );
}

export default WarpText;

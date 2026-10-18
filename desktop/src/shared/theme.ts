/**
 * Design tokens e utilitários WCAG 2.1 da interface Lunmi (`desktop/src/shared/theme.ts`).
 *
 * Paleta celestial aprovada pelo usuário em 2026-10-14; o Tk legado mantém seus tokens.
 * - Cálculo de luminância relativa sRGB e razão de contraste WCAG 2.1.
 * - Lista `CONTRAST_PAIRS` verificada em testes automatizados.
 */

export const CELESTIAL_COLORS = {
  bgWindow: "#030508",
  bgSidebar: "#080A0F",
  bgChat: "#030508",
  bgCard: "#0C111B",
  bgCardSubtle: "#081224",
  bgUser: "#111D31",
  bgInput: "#0B1019",
  borderSubtle: "#26334A",
  borderHairline: "#1B2536",
  borderOnUser: "#30425E",
  textPrimary: "#F3F6FC",
  textSecondary: "#B4C3D7",
  textMuted: "#91A3BD",
  textMutedOnUser: "#8FA3C0",
  accent: "#7DAFFF",
  accentHover: "#95BFFF",
  accentPressed: "#6B99E6",
  onAccent: "#06101E",
  success: "#76CFA9",
  warning: "#E7BB75",
  error: "#E58D99",
  onError: "#2A0B10",
  disabled: "#647A96",
  surfaceHover: "#162A45",
} as const;

export type Palette = Record<keyof typeof CELESTIAL_COLORS, string>;
export interface ThemeDefinition {
  id: string;
  label: string;
  description: string;
  mode: "dark" | "light";
  colors: Palette;
  depth: {
    raised: string; inset: string; control: string; pressed: string; rim: string; spotlight: string; skyOpacity: string;
    canvas: string; glassSurface: string; glassEdge: string; glassLight: string; glassShadow: string; readyDot: string;
    /** Opacidade da camada de refração do vidro (warp do pacote) — reflexo real do fundo. */
    glassRefraction: string;
    /** Triplet "R, G, B" do brilho do mecanismo bento (spotlight/contorno/onda). */
    glow: string;
  };
}

export const THEMES = {
  celestial: {
    id: "celestial", label: "Celestial", description: "Mineral escuro e azul lunar", mode: "dark",
    colors: CELESTIAL_COLORS,
    depth: {
      raised: "8px 8px 22px #00000080, -5px -5px 16px #23324a35",
      inset: "inset 4px 4px 10px #00000080, inset -3px -3px 8px #24334a40",
      control: "3px 3px 8px #00000080, -2px -2px 7px #26365045",
      pressed: "inset 2px 2px 5px #00000099, inset -2px -2px 5px #27385355",
      rim: "#7183a030", spotlight: "#7dafff12", skyOpacity: "0.55",
      canvas: "radial-gradient(560px 620px at 6% 42%, #12305540, transparent 70%), radial-gradient(500px 560px at 94% 58%, #0d245040, transparent 72%), linear-gradient(172deg, #071021 0%, #04080f 48%, #030508 100%)",
      glassSurface: "linear-gradient(173deg, color-mix(in srgb, var(--lunmi-bg-user) 52%, transparent) 0%, color-mix(in srgb, var(--lunmi-bg-card) 26%, transparent) 46%, color-mix(in srgb, var(--lunmi-bg-window) 70%, transparent) 100%)",
      glassEdge: "#7e93bb33",
      glassLight: "inset 0 1px 0 #c8dbff1c, inset 0 -20px 34px -26px #000000e0",
      glassShadow: "0 18px 36px -22px #000000e6, 0 6px 16px -10px #00000073",
      readyDot: "#76CFA9",
      glassRefraction: "0.8",
      glow: "125, 175, 255",
    },
  },
  "soft-light": {
    id: "soft-light", label: "Claro suave", description: "Relevo acetinado e índigo", mode: "light",
    colors: {
      bgWindow: "#E8EDF5", bgSidebar: "#F3F6FB", bgChat: "#E8EDF5", bgCard: "#FFFFFF",
      bgCardSubtle: "#EEF1F9", bgUser: "#E2E8F8", bgInput: "#FFFFFF",
      borderSubtle: "#C4CDE2", borderHairline: "#D8DFEE", borderOnUser: "#BFC9E6",
      textPrimary: "#0F172A", textSecondary: "#5E6B80", textMuted: "#5F6B7E", textMutedOnUser: "#586783",
      accent: "#5F62EE", accentHover: "#5356E4", accentPressed: "#4A4DD8", onAccent: "#FFFFFF",
      success: "#16654D", warning: "#85500D", error: "#B42342", onError: "#FFFFFF", disabled: "#727787", surfaceHover: "#DDE3F4",
    },
    depth: {
      raised: "0 10px 25px -5px rgba(99, 102, 241, 0.08), -8px -8px 18px #fffffff2",
      inset: "inset 3px 3px 7px rgba(99, 102, 241, 0.12), inset -3px -3px 7px #ffffffe6",
      control: "4px 4px 10px rgba(99, 102, 241, 0.12), -4px -4px 10px #fffffff2",
      pressed: "inset 2px 2px 5px rgba(99, 102, 241, 0.14), inset -2px -2px 5px #ffffffd9",
      rim: "#ffffffd9", spotlight: "rgba(99, 102, 241, 0.10)", skyOpacity: "0.42",
      canvas: "radial-gradient(560px 620px at 6% 42%, rgba(99, 102, 241, 0.10), transparent 70%), radial-gradient(500px 560px at 94% 58%, rgba(99, 102, 241, 0.08), transparent 72%), linear-gradient(170deg, #F1F4FA 0%, #E8EDF5 100%)",
      // Light themes: a slightly more transparent panel plus a tinted 1px edge and a real specular
      // highlight make the liquid glass legible on a pale backdrop (warp + blur of the real cosmos).
      glassSurface: "linear-gradient(173deg, color-mix(in srgb, var(--lunmi-bg-card) 90%, transparent) 0%, color-mix(in srgb, var(--lunmi-bg-card) 82%, transparent) 46%, color-mix(in srgb, var(--lunmi-bg-card) 88%, transparent) 100%)",
      glassEdge: "#AEB4E8",
      glassLight: "inset 0 1px 0 #ffffff, inset 1px 0 0 #ffffffb3, inset 0 -1px 0 rgba(95, 98, 238, 0.22), inset 0 -16px 28px -22px rgba(95, 98, 238, 0.30)",
      glassShadow: "0 18px 36px -22px rgba(95, 98, 238, 0.36), 0 4px 12px -8px rgba(24, 30, 66, 0.12)",
      readyDot: "#10B981",
      glassRefraction: "0.68",
      glow: "95, 98, 238",
    },
  },
  "red-velvet": {
    id: "red-velvet", label: "Red Velvet", description: "Creme quente, maroon e rosa", mode: "light",
    colors: {
      bgWindow: "#FDEEEB", bgSidebar: "#FFF0EE", bgChat: "#FFF8F6", bgCard: "#FFFFFF",
      bgCardSubtle: "#FFF0EE", bgUser: "#F7DDD8", bgInput: "#FFFFFF",
      borderSubtle: "#F1D6D2", borderHairline: "#F7E4E0", borderOnUser: "#E9C9C4",
      textPrimary: "#1C0F0E", textSecondary: "#7A5C58", textMuted: "#746662", textMutedOnUser: "#6E5F5C",
      accent: "#800000", accentHover: "#9E2A2B", accentPressed: "#6E0B14", onAccent: "#FFFFFF",
      success: "#16654D", warning: "#85500D", error: "#BA1A1A", onError: "#FFFFFF", disabled: "#8A7A76", surfaceHover: "#F8DED9",
    },
    depth: {
      raised: "8px 8px 18px rgba(130, 80, 75, 0.28), -8px -8px 18px rgba(255, 255, 255, 0.95)",
      inset: "inset 3px 3px 7px rgba(130, 80, 75, 0.22), inset -3px -3px 7px rgba(255, 255, 255, 0.90)",
      control: "4px 4px 10px rgba(130, 80, 75, 0.25), -4px -4px 10px rgba(255, 255, 255, 0.95)",
      pressed: "inset 2px 2px 5px rgba(130, 80, 75, 0.22), inset -2px -2px 5px rgba(255, 255, 255, 0.85)",
      rim: "#F1D6D2", spotlight: "rgba(128, 0, 0, 0.07)", skyOpacity: "0.42",
      canvas: "radial-gradient(560px 620px at 6% 42%, rgba(128, 0, 0, 0.08), transparent 70%), radial-gradient(500px 560px at 94% 58%, rgba(158, 42, 43, 0.07), transparent 72%), linear-gradient(170deg, #FFF8F6 0%, #FDEEEB 100%)",
      glassSurface: "linear-gradient(173deg, color-mix(in srgb, var(--lunmi-bg-card) 90%, transparent) 0%, color-mix(in srgb, var(--lunmi-bg-sidebar) 82%, transparent) 46%, color-mix(in srgb, var(--lunmi-bg-card-subtle) 88%, transparent) 100%)",
      glassEdge: "#E0AAA3",
      glassLight: "inset 0 1px 0 #ffffff, inset 1px 0 0 #ffffffb3, inset 0 -1px 0 rgba(128, 0, 0, 0.22), inset 0 -16px 28px -22px rgba(128, 0, 0, 0.28)",
      glassShadow: "0 18px 34px -20px rgba(128, 0, 0, 0.32), 0 4px 12px -8px rgba(70, 20, 18, 0.14)",
      readyDot: "#10B981",
      glassRefraction: "0.68",
      glow: "158, 42, 43",
    },
  },
} satisfies Record<string, ThemeDefinition>;

export type ThemeId = keyof typeof THEMES;
export function getTheme(id: unknown): ThemeDefinition {
  return typeof id === "string" && Object.hasOwn(THEMES, id) ? THEMES[id as ThemeId] : THEMES.celestial;
}
const cssName = (key: string) => key.replace(/[A-Z]/g, letter => `-${letter.toLowerCase()}`);
// CSS variables also cover module-level inline styles; a theme switch needs no component remount.
export const TOKENS = Object.fromEntries(Object.keys(CELESTIAL_COLORS).map(key => [key, `var(--lunmi-${cssName(key)})`])) as Palette;

export const TEXT_FLOOR = 4.5;
export const LARGE_TEXT_FLOOR = 3.0;
export const GRAPHIC_FLOOR = 3.0;

export function channelLinear(value: number): number {
  return value <= 10 ? value / 255 / 12.92 : Math.pow((value / 255 + 0.055) / 1.055, 2.4);
}

export function luminance(hexColor: string): number {
  const clean = hexColor.replace("#", "").trim();
  const r = parseInt(clean.slice(0, 2), 16);
  const g = parseInt(clean.slice(2, 4), 16);
  const b = parseInt(clean.slice(4, 6), 16);
  return 0.2126 * channelLinear(r) + 0.7152 * channelLinear(g) + 0.0722 * channelLinear(b);
}

export function contrastRatio(first: string, second: string): number {
  const l1 = luminance(first);
  const l2 = luminance(second);
  const high = Math.max(l1, l2);
  const low = Math.min(l1, l2);
  return (high + 0.05) / (low + 0.05);
}

export interface ContrastCheckPair {
  fgLabel: string;
  bgLabel: string;
  fg: string;
  bg: string;
  floor: number;
}

export function contrastPairs(colors: Palette): readonly ContrastCheckPair[] {
  return [
  { fgLabel: "texto principal", bgLabel: "janela", fg: colors.textPrimary, bg: colors.bgWindow, floor: TEXT_FLOOR },
  { fgLabel: "texto principal", bgLabel: "chat", fg: colors.textPrimary, bg: colors.bgChat, floor: TEXT_FLOOR },
  { fgLabel: "texto principal", bgLabel: "cartão", fg: colors.textPrimary, bg: colors.bgCard, floor: TEXT_FLOOR },
  { fgLabel: "texto principal", bgLabel: "bolha do usuário", fg: colors.textPrimary, bg: colors.bgUser, floor: TEXT_FLOOR },
  { fgLabel: "texto secundário", bgLabel: "cartão", fg: colors.textSecondary, bg: colors.bgCard, floor: TEXT_FLOOR },
  { fgLabel: "texto secundário", bgLabel: "sidebar", fg: colors.textSecondary, bg: colors.bgSidebar, floor: TEXT_FLOOR },
  { fgLabel: "texto discreto", bgLabel: "janela", fg: colors.textMuted, bg: colors.bgWindow, floor: TEXT_FLOOR },
  { fgLabel: "texto discreto", bgLabel: "cartão", fg: colors.textMuted, bg: colors.bgCard, floor: TEXT_FLOOR },
  { fgLabel: "texto discreto", bgLabel: "painel sutil", fg: colors.textMuted, bg: colors.bgCardSubtle, floor: TEXT_FLOOR },
  { fgLabel: "texto discreto", bgLabel: "bolha do usuário", fg: colors.textMutedOnUser, bg: colors.bgUser, floor: TEXT_FLOOR },
  { fgLabel: "placeholder do campo", bgLabel: "campo de escrita", fg: colors.textMuted, bg: colors.bgInput, floor: TEXT_FLOOR },
  { fgLabel: "texto no campo", bgLabel: "campo de escrita", fg: colors.textPrimary, bg: colors.bgInput, floor: TEXT_FLOOR },
  { fgLabel: "texto selecionado", bgLabel: "marcação do campo", fg: colors.onAccent, bg: colors.accent, floor: TEXT_FLOOR },
  { fgLabel: "rótulo da rota ativa", bgLabel: "preenchimento de acento", fg: colors.onAccent, bg: colors.accent, floor: TEXT_FLOOR },
  { fgLabel: "rótulo da rota ativa (hover)", bgLabel: "preenchimento de acento", fg: colors.onAccent, bg: colors.accentHover, floor: TEXT_FLOOR },
  { fgLabel: "rótulo da rota ativa (pressionado)", bgLabel: "preenchimento de acento", fg: colors.onAccent, bg: colors.accentPressed, floor: TEXT_FLOOR },
  { fgLabel: "link em cartão", bgLabel: "cartão", fg: colors.accent, bg: colors.bgCard, floor: TEXT_FLOOR },
  { fgLabel: "seta de enviar", bgLabel: "botão azul", fg: colors.onAccent, bg: colors.accent, floor: GRAPHIC_FLOOR },
  { fgLabel: "sucesso", bgLabel: "cartão", fg: colors.success, bg: colors.bgCard, floor: TEXT_FLOOR },
  { fgLabel: "atenção", bgLabel: "cartão", fg: colors.warning, bg: colors.bgCard, floor: TEXT_FLOOR },
  { fgLabel: "falha", bgLabel: "cartão", fg: colors.error, bg: colors.bgCard, floor: TEXT_FLOOR },
  { fgLabel: "ícone de fechar (hover)", bgLabel: "material de perigo", fg: colors.onError, bg: colors.error, floor: GRAPHIC_FLOOR },
];
}

export const CONTRAST_PAIRS = contrastPairs(CELESTIAL_COLORS);

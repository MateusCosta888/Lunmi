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
  depth: { raised: string; inset: string; control: string; pressed: string; rim: string; spotlight: string; skyOpacity: string };
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
      rim: "#7183a030", spotlight: "#7dafff12", skyOpacity: "0.18",
    },
  },
  "soft-light": {
    id: "soft-light", label: "Claro suave", description: "Relevo acetinado e índigo", mode: "light",
    colors: {
      bgWindow: "#E8EAF0", bgSidebar: "#E5E7ED", bgChat: "#E8EAF0", bgCard: "#E8EAF0",
      bgCardSubtle: "#E0E2EB", bgUser: "#DDDFF1", bgInput: "#E8EAF0",
      borderSubtle: "#C1C5D1", borderHairline: "#CDD0DB", borderOnUser: "#BEC2DE",
      textPrimary: "#2E3040", textSecondary: "#505261", textMuted: "#606371", textMutedOnUser: "#56596C",
      accent: "#5145CD", accentHover: "#4338B8", accentPressed: "#392E9C", onAccent: "#FFFFFF",
      success: "#16654D", warning: "#85500D", error: "#B42342", disabled: "#727787", surfaceHover: "#D6DAED",
    },
    depth: {
      raised: "8px 8px 18px #a6afc373, -8px -8px 18px #fffffff2",
      inset: "inset 3px 3px 7px #a6afc35c, inset -3px -3px 7px #ffffffe6",
      control: "4px 4px 10px #a6afc366, -4px -4px 10px #fffffff2",
      pressed: "inset 2px 2px 5px #a6afc359, inset -2px -2px 5px #ffffffd9",
      rim: "#ffffffbf", spotlight: "#6366f112", skyOpacity: "0.12",
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
  { fgLabel: "rótulo da rota ativa", bgLabel: "sidebar", fg: colors.accent, bg: colors.bgSidebar, floor: TEXT_FLOOR },
  { fgLabel: "seta de enviar", bgLabel: "botão azul", fg: colors.onAccent, bg: colors.accent, floor: GRAPHIC_FLOOR },
  { fgLabel: "sucesso", bgLabel: "cartão", fg: colors.success, bg: colors.bgCard, floor: TEXT_FLOOR },
  { fgLabel: "atenção", bgLabel: "cartão", fg: colors.warning, bg: colors.bgCard, floor: TEXT_FLOOR },
  { fgLabel: "falha", bgLabel: "cartão", fg: colors.error, bg: colors.bgCard, floor: TEXT_FLOOR },
];
}

export const CONTRAST_PAIRS = contrastPairs(CELESTIAL_COLORS);

import { getTheme } from "./theme";
const cssName = (key: string) => key.replace(/[A-Z]/g, letter => `-${letter.toLowerCase()}`);
export function applyTheme(id: unknown): void {
  const theme = getTheme(id), element = document.documentElement;
  for (const [key, value] of Object.entries(theme.colors)) element.style.setProperty(`--lunmi-${cssName(key)}`, value);
  for (const [key, value] of Object.entries(theme.depth)) element.style.setProperty(`--lunmi-depth-${cssName(key)}`, value);
  // Full-height refraction must not darken a light sidebar beneath its text.
  element.style.setProperty("--lunmi-glass-refraction-opacity", theme.mode === "light" ? "0.04" : "1");
  element.dataset.theme = theme.id;
  element.style.colorScheme = theme.mode;
}

import { getTheme } from "./theme";
const cssName = (key: string) => key.replace(/[A-Z]/g, letter => `-${letter.toLowerCase()}`);
export function applyTheme(id: unknown): void {
  const theme = getTheme(id), element = document.documentElement;
  for (const [key, value] of Object.entries(theme.colors)) element.style.setProperty(`--lunmi-${cssName(key)}`, value);
  for (const [key, value] of Object.entries(theme.depth)) element.style.setProperty(`--lunmi-depth-${cssName(key)}`, value);
  element.dataset.theme = theme.id;
  element.style.colorScheme = theme.mode;
}

import { expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { contrastPairs, contrastRatio, getTheme, THEMES, TOKENS } from "../src/shared/theme";
import { applyTheme } from "../src/shared/themeDom";
import { PreferencesPanel } from "../src/features/preferences/PreferencesPanel";
import type { SendCommandFn } from "../src/shared/ipc/client";
import type { SessionReadyPayload } from "../src/shared/protocol";
import { parsePreferences } from "../electron/preferences";

it("todos os temas cobrem os mesmos tokens e passam os pares de contraste", () => {
  for (const theme of Object.values(THEMES)) {
    expect(Object.keys(theme.colors)).toEqual(Object.keys(THEMES.celestial.colors));
    for (const pair of contrastPairs(theme.colors)) expect(contrastRatio(pair.fg, pair.bg), `${theme.id}: ${pair.fgLabel}/${pair.bgLabel}`).toBeGreaterThanOrEqual(pair.floor);
  }
});

it("troca variáveis, esquema e materiais sem reconstruir os componentes", () => {
  applyTheme("soft-light");
  expect(document.documentElement.dataset.theme).toBe("soft-light");
  expect(document.documentElement.style.colorScheme).toBe("light");
  expect(document.documentElement.style.getPropertyValue("--lunmi-bg-window")).toBe(THEMES["soft-light"].colors.bgWindow);
  expect(TOKENS.bgWindow).toBe("var(--lunmi-bg-window)");
  applyTheme("unknown"); expect(document.documentElement.dataset.theme).toBe("celestial");
  expect(getTheme("__proto__").id).toBe("celestial");
});

it("a camada nativa resolve só temas registrados e ignora IDs desconhecidos", () => {
  expect(parsePreferences({ theme: "soft-light" }).theme).toBe("soft-light");
  expect(parsePreferences({ theme: "../../file" }).theme).toBe("celestial");
});

function setup(fail = false) {
  let theme = "celestial";
  const changed = vi.fn();
  const send = vi.fn(async (type: string, payload: Record<string, unknown>) => {
    if (type === "approval.get") return { auto_approve: false, label: "Manual" };
    if (type === "ui.preferences.set") {
      if (fail) throw new Error("io_error: Sem acesso ao arquivo");
      theme = payload.theme as string;
    }
    return { theme, actions: [] };
  });
  render(<PreferencesPanel sendCommand={send as SendCommandFn} session={{ session_id: "test" } as SessionReadyPayload} onThemeChanged={changed} />);
  return { send, changed };
}

it("aplica apenas depois da persistência confirmada e sem enviar ações/paths/Brain", async () => {
  const fake = setup();
  await waitFor(() => expect(screen.getByTestId("theme-celestial").getAttribute("aria-pressed")).toBe("true"));
  fireEvent.click(screen.getByTestId("theme-soft-light"));
  await waitFor(() => expect(fake.changed).toHaveBeenCalledWith("soft-light"));
  expect(fake.send).toHaveBeenCalledWith("ui.preferences.set", { theme: "soft-light" });
  expect(fake.send.mock.calls.some(([type]) => type.startsWith("brain.") || type === "chat.send")).toBe(false);
});

it("falha de gravação preserva o tema anterior e apresenta erro", async () => {
  const fake = setup(true);
  await act(async () => {});
  fireEvent.click(screen.getByTestId("theme-soft-light"));
  await screen.findByTestId("preferences-error");
  expect(fake.changed).not.toHaveBeenCalled();
  expect(screen.getByTestId("theme-celestial").getAttribute("aria-pressed")).toBe("true");
});

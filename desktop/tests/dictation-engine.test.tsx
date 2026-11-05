/** O Groq só liga depois de aceitar o aviso, e nunca sem chave guardada. */
import { afterEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { DictationEngineChoice } from "../src/features/voice/DictationEngineChoice";

afterEach(() => {
  delete (window as unknown as { lunmi?: unknown }).lunmi;
  vi.restoreAllMocks();
});

function bridge(key_ready: boolean) {
  const calls: Array<Record<string, unknown>> = [];
  (window as unknown as { lunmi: unknown }).lunmi = {
    onEvent: () => () => undefined,
    sendCommand: vi.fn(async (_type: string, payload: Record<string, unknown> = {}) => {
      calls.push(payload);
      return { engine: payload.engine ?? "local", key_ready };
    }),
  };
  return calls;
}

it("asks before the audio leaves the PC", async () => {
  const calls = bridge(true);
  const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
  render(<DictationEngineChoice />);
  const groq = await screen.findByTestId("dictation-engine-groq");
  fireEvent.click(groq);
  expect(calls).toEqual([{}]);
  fireEvent.click(groq);
  await waitFor(() => expect(calls.at(-1)).toEqual({ engine: "groq" }));
  expect(String(confirm.mock.calls[0][0])).toContain("vai para o Groq");
});

it("cannot choose Groq without a saved key", async () => {
  bridge(false);
  render(<DictationEngineChoice />);
  expect((await screen.findByTestId("dictation-engine-groq") as HTMLInputElement).disabled).toBe(true);
  expect(screen.getByText("Guarda primeiro a chave do Groq em Cérebro.")).toBeTruthy();
});

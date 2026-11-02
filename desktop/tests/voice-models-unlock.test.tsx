/**
 * Review 21a75b0 P1-4: after the models are installed, the real voice hooks must unlock without a
 * remount or restart. The backend republishes voice.state / voice.input.state when a group becomes
 * usable (tested on the Python side); here the hooks, mounted once, take those events.
 */
import { afterEach, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { useVoice } from "../src/features/voice/useVoice";
import { useVoiceInput } from "../src/features/voice/useVoiceInput";
import { VoiceModelsPanel } from "../src/features/voice/VoiceModelsPanel";

afterEach(() => {
  delete (window as unknown as { lunmi?: unknown }).lunmi;
});

function Probe() {
  const { voice } = useVoice("s");
  const input = useVoiceInput("s");
  return (
    <>
      <span data-testid="speech">{String(voice?.available ?? "none")}</span>
      <span data-testid="dictation">{String(input.state?.available ?? "none")}</span>
      <VoiceModelsPanel sessionId="s" />
    </>
  );
}

it("unlocks voice and dictation in place when their models become usable", async () => {
  type Listener = (event: { v: 1; type: string; payload: unknown }) => void;
  const listeners = new Set<Listener>();
  const voiceDto = { enabled: true, auto_speak: true, available: false, state: "error", generation: 3,
    can_speak_last: false, capture_supported: false, message: "Modelos em falta." };
  const inputDto = { available: false, state: "unavailable", generation: 2, message: "Ditado indisponível." };
  const missing = { message: "", groups: {
    speech: { state: "missing", bytes_total: 399e6, bytes_done: 0, message: "" },
    dictation: { state: "missing", bytes_total: 1.6e9, bytes_done: 0, message: "" } } };
  const sendCommand = vi.fn(async (type: string) => {
    if (type === "voice.get") return voiceDto;
    if (type === "voice.input.get") return inputDto;
    if (type === "voice.models.get") return missing;
    throw new Error(`unexpected_command: ${type}`);
  });
  (window as unknown as { lunmi: unknown }).lunmi = {
    onEvent: (cb: Listener) => { listeners.add(cb); return () => { listeners.delete(cb); }; },
    sendCommand,
  };
  render(<Probe />);
  await waitFor(() => expect(screen.getByTestId("speech").textContent).toBe("false"));
  expect(screen.getByTestId("dictation").textContent).toBe("false");
  const emit = (type: string, payload: unknown) => act(() => listeners.forEach(cb => cb({ v: 1, type, payload })));
  // What the bridge now sends when both groups finish: models, then the canonical states.
  emit("voice.models", { session_id: "s", message: "", groups: {
    speech: { state: "installed", bytes_total: 399e6, bytes_done: 399e6, message: "" },
    dictation: { state: "installed", bytes_total: 1.6e9, bytes_done: 1.6e9, message: "" } } });
  emit("voice.state", { ...voiceDto, available: true, state: "ready", message: "", session_id: "s" });
  emit("voice.input.state", { ...inputDto, available: true, state: "idle", message: "", session_id: "s" });
  expect(screen.getByTestId("speech").textContent).toBe("true");
  expect(screen.getByTestId("dictation").textContent).toBe("true");
  expect(screen.getAllByText("Instalada e verificada.")).toHaveLength(2);
  // A stale event of another session never unlocks anything.
  emit("voice.state", { ...voiceDto, available: false, generation: 9, session_id: "other" });
  expect(screen.getByTestId("speech").textContent).toBe("true");
});

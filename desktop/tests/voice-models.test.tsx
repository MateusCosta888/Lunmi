/**
 * Modelos de voz transferidos depois da instalação (01/11): o painel só transfere quando o
 * utilizador carrega, mostra progresso pelos eventos e nunca aparece com uma ponte antiga.
 */
import { afterEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { VoiceModelsPanel, formatSize, type VoiceModelsDto } from "../src/features/voice/VoiceModelsPanel";

afterEach(() => {
  delete (window as unknown as { lunmi?: unknown }).lunmi;
});

const missing: VoiceModelsDto = {
  message: "",
  groups: {
    speech: { state: "missing", bytes_total: 399_000_000, bytes_done: 0, message: "" },
    dictation: { state: "installed", bytes_total: 1_624_000_000, bytes_done: 1_624_000_000, message: "" },
  },
};

function bridge(initial: VoiceModelsDto | Error) {
  const calls: Array<{ type: string; payload: Record<string, unknown> }> = [];
  let listener: ((event: { v: 1; type: string; payload: unknown }) => void) | undefined;
  const sendCommand = vi.fn(async (type: string, payload: Record<string, unknown> = {}) => {
    calls.push({ type, payload });
    if (initial instanceof Error) throw initial;
    return initial;
  });
  (window as unknown as { lunmi: unknown }).lunmi = {
    onEvent: (cb: typeof listener) => { listener = cb; return () => { listener = undefined; }; },
    sendCommand,
  };
  return { calls, emit: (payload: VoiceModelsDto) => listener?.({ v: 1, type: "voice.models", payload }) };
}

it("shows each group separately and downloads only when the user asks", async () => {
  const { calls, emit } = bridge(missing);
  render(<VoiceModelsPanel />);
  await screen.findByText(/Por transferir · 399 MB/);
  expect(screen.getByTestId("voice-model-dictation").textContent).toContain("Instalada e verificada.");
  expect(calls.map(call => call.type)).toEqual(["voice.models.get"]);  // nothing downloaded yet
  fireEvent.click(screen.getByRole("button", { name: "Transferir" }));
  await waitFor(() => expect(calls[1]).toEqual({ type: "voice.models.download", payload: { group: "speech" } }));
  act(() => emit({ ...missing, groups: { ...missing.groups,
    speech: { state: "downloading", bytes_total: 399_000_000, bytes_done: 199_500_000, message: "" } } }));
  expect(await screen.findByText(/A transferir · 50% de 399 MB/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Pausar" }));
  await waitFor(() => expect(calls[2]).toEqual({ type: "voice.models.cancel", payload: { group: "speech" } }));
});

it("explains a failure honestly and offers to resume", async () => {
  bridge({ ...missing, groups: { speech: { state: "error", bytes_total: 399_000_000, bytes_done: 10_000_000,
    message: "Espaço em disco insuficiente para os modelos de voz." } } });
  render(<VoiceModelsPanel />);
  expect(await screen.findByText("Espaço em disco insuficiente para os modelos de voz.")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Retomar" })).toBeTruthy();
});

it("does not appear with a backend that has no voice models command", async () => {
  bridge(new Error("unexpected_command: voice.models.get"));
  const { container } = render(<VoiceModelsPanel />);
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(container.innerHTML).toBe("");
});

it("formats sizes in Portuguese", () => {
  expect(formatSize(399_000_000)).toBe("399 MB");
  expect(formatSize(1_624_000_000)).toBe("1,6 GB");
});

it("offers the light dictation on a weak PC and removes a model only after confirming", async () => {
  const size = { bytes_total: 486_000_000, bytes_done: 486_000_000, message: "" };
  const { calls } = bridge({ ...missing, light: true, groups: { ...missing.groups,
    dictation_light: { state: "installed", ...size } } });
  const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
  render(<VoiceModelsPanel />);
  const remove = await screen.findByRole("button", { name: "Remover e libertar 486 MB" });
  fireEvent.click(remove);
  expect(calls.some(call => call.type === "voice.models.remove")).toBe(false);
  fireEvent.click(remove);
  await waitFor(() => expect(calls.at(-1)).toEqual({ type: "voice.models.remove", payload: { group: "dictation_light" } }));
  confirm.mockRestore();
});

it("hides the light dictation on a normal PC and the missing large one on a weak PC", async () => {
  const groups = { ...missing.groups, dictation: { ...missing.groups.dictation!, state: "missing" as const },
    dictation_light: { state: "missing" as const, bytes_total: 486_000_000, bytes_done: 0, message: "" } };
  bridge({ ...missing, groups });
  const normal = render(<VoiceModelsPanel />);
  await screen.findByTestId("voice-model-dictation");
  expect(screen.queryByTestId("voice-model-dictation_light")).toBeNull();
  normal.unmount();
  bridge({ ...missing, light: true, groups });
  render(<VoiceModelsPanel />);
  await screen.findByTestId("voice-model-dictation_light");
  expect(screen.queryByTestId("voice-model-dictation")).toBeNull();
});

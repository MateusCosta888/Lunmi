/** A escolha só mostra o que o modelo tem, e guarda logo. */
import { expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CodexSpeedOptions } from "../src/features/brain/CodexSpeedOptions";
import { ModelPicker } from "../src/features/chat/ModelPicker";
import type { SendCommandFn } from "../src/shared/ipc/client";
import type { BrainGetDto } from "../src/shared/types";

const model = { id: "gpt-6-luna", label: "Luna", default: false, efforts: ["low", "high"], fast: true };

function send(result = { effort: "", fast: false }) {
  return vi.fn(async (_type: string, payload: Record<string, unknown> = {}) =>
    (Object.keys(payload).length ? payload : result)) as unknown as SendCommandFn & ReturnType<typeof vi.fn>;
}

it("lists only this model's levels and saves a change", async () => {
  const command = send();
  render(<CodexSpeedOptions sendCommand={command} model={model} />);
  await screen.findByTestId("codex-speed");
  expect(screen.getAllByRole("radio").map(item => item.textContent)).toEqual(["Padrão", "Baixo", "Alto"]);
  fireEvent.click(screen.getByTestId("codex-effort-low"));
  await waitFor(() => expect(command).toHaveBeenLastCalledWith("brain.codex.options", { effort: "low", fast: false }));
  await waitFor(() => expect(screen.getByTestId("codex-effort-low").getAttribute("aria-checked")).toBe("true"));
  fireEvent.click(screen.getByTestId("codex-fast"));
  await waitFor(() => expect(command).toHaveBeenLastCalledWith("brain.codex.options", { effort: "low", fast: true }));
});

it("shows the default when the saved level is not in this model, and hides fast when there is none", async () => {
  render(<CodexSpeedOptions sendCommand={send({ effort: "ultra", fast: false })} model={{ ...model, fast: false }} />);
  expect((await screen.findByTestId("codex-effort-default")).getAttribute("aria-checked")).toBe("true");
  expect(screen.queryByTestId("codex-fast")).toBeNull();
});

it("shows the level and fast in the chat model bar, and a change in the menu updates it", async () => {
  const brain = { current: { provider: "codex", model: "gpt-6-luna", provider_label: "Codex" } } as unknown as BrainGetDto;
  const base = send({ effort: "low", fast: true });
  const command = vi.fn(async (type: string, payload: Record<string, unknown> = {}) => type === "brain.list"
    ? { provider: "codex", ok: true, models: [model] } : base(type, payload)) as unknown as SendCommandFn;
  render(<ModelPicker brain={brain} sendCommand={command} disabled={false} changing={false}
    onBusyChange={() => undefined} onChanged={() => undefined} onProviderSettings={() => undefined} />);
  expect((await screen.findByTestId("composer-model-speed")).textContent).toBe("Baixo");
  expect(screen.getByLabelText("Modo rápido")).toBeTruthy();
  fireEvent.click(screen.getByTestId("composer-model"));
  fireEvent.click(await screen.findByTestId("codex-effort-high"));
  await waitFor(() => expect(screen.getByTestId("composer-model-speed").textContent).toBe("Alto"));
});

it("keeps the model bar of other brains unchanged", async () => {
  const brain = { current: { provider: "groq", model: "m" } } as unknown as BrainGetDto;
  const command = send();
  render(<ModelPicker brain={brain} sendCommand={command} disabled={false} changing={false}
    onBusyChange={() => undefined} onChanged={() => undefined} onProviderSettings={() => undefined} />);
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(screen.queryByTestId("composer-model-speed")).toBeNull();
  expect(command).not.toHaveBeenCalled();
});

it("stays out of the way without a model catalogue", async () => {
  const { container } = render(<CodexSpeedOptions sendCommand={send()} model={undefined} />);
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(container.textContent).toBe("");
});

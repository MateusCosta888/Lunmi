import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { UpdatesCard, UpdateNotice } from "../src/features/preferences/UpdatesCard";
import type { UpdateState } from "../src/shared/updates";
import type { LunmiBridgeApi } from "../src/shared/ipc/client";

afterEach(() => { delete window.lunmi; });
function bridge(phase: UpdateState["phase"]) {
  const state: UpdateState = { phase, currentVersion: "0.1.0-alpha.4", version: "0.1.0-alpha.5", percent: 32 };
  const api = { get: vi.fn(async () => state), check: vi.fn(async () => state), install: vi.fn(async () => state), onState: vi.fn(() => vi.fn()) };
  window.lunmi = { updates: api } as unknown as LunmiBridgeApi;
  return api;
}
describe("update presentation", () => {
  it("hides gracefully when preload has no updater", () => {
    render(<UpdatesCard />); expect(screen.queryByTestId("updates-card")).toBeNull();
  });
  it("explains development mode", async () => {
    bridge("disabled"); render(<UpdatesCard />);
    expect(await screen.findByText(/Windows/)).toBeTruthy(); expect(screen.queryByRole("button")).toBeNull();
  });
  it("uses existing card and button classes, with explicit restart only", async () => {
    const api = bridge("ready"); render(<UpdatesCard />);
    const button = await screen.findByRole("button", { name: "Reiniciar e atualizar" });
    expect(button.className).toContain("lunmi-action-button");
    expect(screen.getByTestId("updates-card").className).toContain("lunmi-settings-card");
    expect(api.install).not.toHaveBeenCalled(); fireEvent.click(button);
    await waitFor(() => expect(api.install).toHaveBeenCalledTimes(1));
  });
  it("shows accessible progress and disables redundant checks", async () => {
    bridge("downloading"); render(<UpdatesCard />);
    expect(await screen.findByRole("progressbar")).toBeTruthy();
    expect((screen.getByRole("button") as HTMLButtonElement).disabled).toBe(true);
  });
  it("announces readiness without invoking installation", async () => {
    const api = bridge("ready"); const open = vi.fn(); render(<UpdateNotice onOpen={open} />);
    fireEvent.click(await screen.findByRole("button", { name: "Ver atualização" }));
    expect(open).toHaveBeenCalledTimes(1); expect(api.install).not.toHaveBeenCalled();
  });
});

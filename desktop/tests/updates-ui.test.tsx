import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { UpdateModal, UpdatesCard, UpdateNotice } from "../src/features/preferences/UpdatesCard";
import type { UpdateState } from "../src/shared/updates";
import type { LunmiBridgeApi } from "../src/shared/ipc/client";

afterEach(() => { delete window.lunmi; });
function bridge(phase: UpdateState["phase"], error?: UpdateState["error"]) {
  const state: UpdateState = { phase, currentVersion: "1.0.0-beta.1", version: "1.0.0-beta.2", percent: 32, notes: "Instalador novo", error };
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
  it("offers the new version with its notes and installs only on request", async () => {
    const api = bridge("available"); render(<UpdateModal />);
    expect(await screen.findByText("Instalador novo")).toBeTruthy();
    expect(screen.getByRole("dialog").textContent).toContain("1.0.0-beta.2");
    expect(api.install).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Descarregar agora" }));
    await waitFor(() => expect(api.install).toHaveBeenCalledTimes(1));
  });
  it("closes on Later without installing", async () => {
    const api = bridge("available"); render(<UpdateModal />);
    fireEvent.click(await screen.findByRole("button", { name: "Mais tarde" }));
    expect(screen.queryByTestId("update-modal")).toBeNull();
    expect(api.install).not.toHaveBeenCalled();
  });
  it("keeps a finished download open with an explicit restart button and a Later choice", async () => {
    const api = bridge("available");
    api.install.mockResolvedValueOnce({ phase: "ready", currentVersion: "1.0.0-beta.1", version: "1.0.0-beta.2" });
    render(<UpdateModal />);
    fireEvent.click(await screen.findByRole("button", { name: "Descarregar agora" }));
    expect(await screen.findByRole("button", { name: "Reiniciar e atualizar" })).toBeTruthy();
    expect(api.install).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Mais tarde" }));
    expect(screen.queryByTestId("update-modal")).toBeNull();
    expect(api.install).toHaveBeenCalledTimes(1);
  });
  it.each(["offline", "source", "signature", "hash", "space", "locked", "extract", "busy", "internal"] as const)("explains the launcher error %s", async code => {
    bridge("error", code); render(<UpdatesCard />);
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.getByRole("alert").textContent!.length).toBeGreaterThan(20);
  });
  it("stays hidden when there is nothing new", async () => {
    bridge("idle"); render(<UpdateModal />);
    await waitFor(() => expect(screen.queryByTestId("update-modal")).toBeNull());
  });
});

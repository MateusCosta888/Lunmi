import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SpotifyCard from "../src/features/integrations/SpotifyCard";
import type { SendCommandFn } from "../src/shared/ipc/client";
import type { SessionReadyPayload } from "../src/shared/protocol";
import type { SpotifyStateDto } from "../src/shared/spotify";

const session = { session_id: "manual-policy-test", capabilities: { spotify: true } } as unknown as SessionReadyPayload;
const state = (setup: SpotifyStateDto["setup"]): SpotifyStateDto => ({
  setup, connection: "connected", device: "unknown", revision: 1, epoch: 1, job: null, error: null,
  detection: { app: "win32", version: "supported", architecture: "x64", cli: "pinned", link: "present", app_running: true },
});

beforeEach(() => { window.lunmi = { onEvent: () => () => {} } as unknown as typeof window.lunmi; });

describe("Spotify: automatic installer withheld after failed real removal", () => {
  it.each(["not_configured", "configured", "needs_repair"] as const)("blocks automatic mutations for %s by default", async setup => {
    const send = vi.fn(async () => ({ spotify: state(setup) }));
    render(<SpotifyCard session={session} sendCommand={send as unknown as SendCommandFn} />);
    const labels = setup === "not_configured" ? ["Configurar Spotify"] : ["Reparar ligação", "Remover ligação"];
    for (const label of labels) {
      const buttons = await screen.findAllByRole("button", { name: label, hidden: true });
      for (const button of buttons) {
        expect((button as HTMLButtonElement).disabled).toBe(true);
        fireEvent.click(button);
      }
    }
    expect(send.mock.calls).toEqual([["spotify.get", {}]]);
    expect(screen.getByText(/configuração e remoção automáticas estão bloqueadas/i)).toBeTruthy();
  });

  it("keeps manual connection management without setup commands", async () => {
    const send = vi.fn(async (command: string) => ({ spotify: command === "spotify.disconnect"
      ? { ...state("configured"), revision: 2, connection: "disabled" }
      : state("configured") }));
    render(<SpotifyCard session={session} sendCommand={send as unknown as SendCommandFn} />);
    const disconnect = await screen.findByRole("button", { name: "Desligar da Lunmi" });
    expect((disconnect as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(disconnect);
    await screen.findByText("Desligado da Lunmi");
    expect(send.mock.calls.map(([name]) => name)).toEqual(["spotify.get", "spotify.disconnect"]);
  });
});

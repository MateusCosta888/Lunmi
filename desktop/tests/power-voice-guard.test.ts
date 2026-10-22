/**
 * Guarda de energia da voz (`desktop/electron/main.ts`).
 *
 * Offline, com um `powerMonitor` emitido/mockado: bloquear o ecrã ou suspender chama uma única
 * vez o mesmo parar-voz já existente (que invalida a geração e os buffers no worker). Nenhum
 * evento de regresso é ligado, porque retomar não pode abrir microfone, reiniciar TTS nem repetir
 * a tarefa. Sem áudio real, sem runtime, sem novos estados de voz.
 */
import { expect, it, vi } from "vitest";
import { registerPowerVoiceGuards } from "../electron/main";

type PowerMonitorLike = Parameters<typeof registerPowerVoiceGuards>[0];

function fakeMonitor() {
  const listeners = new Map<string, () => void>();
  const monitor = {
    on: (event: string, listener: () => void) => {
      listeners.set(event, listener);
    },
  } as unknown as PowerMonitorLike;
  return {
    monitor,
    listeners,
    emit: (event: string) => listeners.get(event)?.(),
  };
}

it("lock-screen e suspend param a fala pelo stop existente", () => {
  const fake = fakeMonitor();
  const stop = vi.fn();
  registerPowerVoiceGuards(fake.monitor, stop);

  expect([...fake.listeners.keys()].sort()).toEqual(["lock-screen", "suspend"]);
  fake.emit("lock-screen");
  fake.emit("suspend");

  expect(stop).toHaveBeenCalledTimes(2);
});

it("desbloquear/retomar não retoma áudio nem repete nada", () => {
  const fake = fakeMonitor();
  const stop = vi.fn();
  registerPowerVoiceGuards(fake.monitor, stop);

  // Só há dois caminhos ligados: qualquer evento de regresso continua sem handler.
  expect(fake.listeners.has("unlock-screen")).toBe(false);
  expect(fake.listeners.has("resume")).toBe(false);
  fake.emit("unlock-screen");
  fake.emit("resume");

  expect(stop).toHaveBeenCalledTimes(0);
});

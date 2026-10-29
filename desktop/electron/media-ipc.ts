import type { IpcMain, IpcMainInvokeEvent } from "electron";
import { EMPTY_MEDIA_STATE, type MediaCommandInput, type MediaCommandResultDto, type MediaStateDto, type NodeMediaApi } from "../src/shared/media";

interface NodeMediaIpcOptions {
  ipc: Pick<IpcMain, "handle">;
  trusted: (event: IpcMainInvokeEvent) => boolean;
  active: () => boolean;
  passive: () => boolean;
  media: () => NodeMediaApi;
}

function validInput(value: unknown): value is MediaCommandInput {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const input = value as Record<string, unknown>;
  const keys = Object.keys(input);
  return keys.length === 2 && keys.includes("command") && keys.includes("session_id") &&
    ["play", "pause", "previous", "next"].includes(input.command as string) &&
    typeof input.session_id === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(input.session_id);
}

/** The companion can control the displayed media session, never a path, shell or arbitrary API. */
export function registerNodeMediaIpc(options: NodeMediaIpcOptions): void {
  let reading: Promise<MediaStateDto> | null = null;
  let commanding = false;
  const empty = (): MediaStateDto => ({ ...EMPTY_MEDIA_STATE });
  const denied = (error: string): MediaCommandResultDto => ({ ok: false, error, state: empty() });
  const checkSender = (event: IpcMainInvokeEvent) => {
    if (!options.trusted(event)) throw new Error("Sender não autorizado.");
  };

  options.ipc.handle("lunmi:node-media-get", async (event, ...args: unknown[]) => {
    checkSender(event);
    if (args.length) throw new Error("invalid_payload");
    if (!options.active() || options.passive()) return empty();
    if (!reading) {
      // Volta a confirmar dentro do microtask: um Node que ficou inativo entretanto não arranca o helper.
      reading = Promise.resolve()
        .then(() => (options.active() && !options.passive() ? options.media().get() : empty()))
        .catch(empty).finally(() => { reading = null; });
    }
    const state = await reading;
    return options.active() && !options.passive() ? state : empty();
  });

  options.ipc.handle("lunmi:node-media-command", async (event, input: unknown, ...extra: unknown[]) => {
    checkSender(event);
    if (extra.length || !validInput(input)) return denied("invalid_payload");
    if (!options.active()) return denied("node_inactive");
    if (options.passive()) return denied("passive_running");
    if (commanding) return denied("busy");
    commanding = true;
    try {
      return await options.media().command(input);
    } catch {
      // A command with an uncertain outcome is never retried.
      return denied("media_unavailable");
    } finally {
      commanding = false;
    }
  });
}

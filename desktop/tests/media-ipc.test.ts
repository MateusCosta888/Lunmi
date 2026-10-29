import type { IpcMain, IpcMainInvokeEvent } from "electron";
import { describe, expect, it, vi } from "vitest";
import { registerNodeMediaIpc } from "../electron/media-ipc";
import { EMPTY_MEDIA_STATE, type MediaCommand, type MediaCommandResultDto, type MediaStateDto } from "../src/shared/media";

const GET = "lunmi:node-media-get";
const COMMAND = "lunmi:node-media-command";
const input = { command: "pause" as const, session_id: "fixture_session-1" };
const state: MediaStateDto = {
  v: 1, available: true, session_id: input.session_id, title: "Fixture track", artist: "Fixture artist",
  source_app: "fixture.player", playback: "playing", can_play: false, can_pause: true,
  can_previous: false, can_next: true,
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function harness() {
  const listeners = new Map<string, (event: IpcMainInvokeEvent, ...args: unknown[]) => unknown>();
  const handle = vi.fn<IpcMain["handle"]>((name, listener) => { listeners.set(name, listener); });
  const mainFrame = { url: "file:///fixture/companion.html" };
  const sender = { id: 42, mainFrame };
  const event = { sender, senderFrame: mainFrame } as unknown as IpcMainInvokeEvent;
  // The production main owns the URL/frame policy; this seam must honour its predicate.
  const trusted = vi.fn((candidate: IpcMainInvokeEvent) => candidate.sender === event.sender &&
    candidate.senderFrame === event.senderFrame);
  let active = true;
  let passive = false;
  const media = {
    get: vi.fn(async (): Promise<MediaStateDto> => state),
    command: vi.fn(async (): Promise<MediaCommandResultDto> => ({ ok: true, state })),
  };
  const factory = vi.fn(() => media);
  registerNodeMediaIpc({ ipc: { handle }, trusted, active: () => active, passive: () => passive, media: factory });
  return {
    handle, event, trusted, media, factory,
    setActive(value: boolean) { active = value; },
    setPassive(value: boolean) { passive = value; },
    async invoke(name: string, args: unknown[] = [], senderEvent = event) {
      const listener = listeners.get(name);
      if (!listener) throw new Error(`Missing handler: ${name}`);
      return listener(senderEvent, ...args);
    },
  };
}

describe("Node media IPC boundary", () => {
  it("registers only the two requested channels without creating a media helper", () => {
    const h = harness();
    expect(h.handle.mock.calls.map(([name]) => name)).toEqual([GET, COMMAND]);
    expect(h.factory).not.toHaveBeenCalled();
  });

  it.each([GET, COMMAND])("rejects another sender or a subframe before dispatching %s", async channel => {
    const h = harness();
    for (const event of [
      { ...h.event, sender: { id: 99 } },
      { ...h.event, senderFrame: { url: "file:///fixture/companion.html" } },
      { ...h.event, senderFrame: null },
    ]) {
      await expect(h.invoke(channel, channel === COMMAND ? [input] : [], event as IpcMainInvokeEvent))
        .rejects.toThrow("Sender não autorizado.");
    }
    expect(h.trusted).toHaveBeenCalledTimes(3);
    expect(h.factory).not.toHaveBeenCalled();
    expect(h.media.get).not.toHaveBeenCalled();
    expect(h.media.command).not.toHaveBeenCalled();
  });

  it.each([GET, COMMAND])("keeps sender rejection ahead of payload and activity checks for %s", async channel => {
    const h = harness();
    h.setActive(false); h.setPassive(true); h.trusted.mockReturnValue(false);
    await expect(h.invoke(channel, ["invalid", "extra"])).rejects.toThrow("Sender não autorizado.");
    expect(h.factory).not.toHaveBeenCalled();
  });

  it.each([[undefined], [null], [{}], ["unexpected"], [input, input]])(
    "rejects extra get arguments before creating a helper: %j", async (...args) => {
      const h = harness();
      await expect(h.invoke(GET, args)).rejects.toThrow("invalid_payload");
      expect(h.factory).not.toHaveBeenCalled();
    });

  it.each([
    undefined, null, [], "pause", 1, {}, { command: "pause" }, { session_id: input.session_id },
    { ...input, extra: true }, { ...input, command: "quit" }, { ...input, command: "PAUSE" },
    { ...input, command: 1 }, { ...input, session_id: null }, { ...input, session_id: 1 },
    { ...input, session_id: "" }, { ...input, session_id: "a".repeat(65) },
    { ...input, session_id: "a b" }, { ...input, session_id: "../session" },
    { ...input, session_id: "a\nb" }, { ...input, session_id: "a\n" }, { ...input, session_id: "$(command)" },
  ])("rejects malformed command payload without creating a helper: %j", async payload => {
    const h = harness();
    expect(await h.invoke(COMMAND, [payload])).toEqual({ ok: false, error: "invalid_payload", state: EMPTY_MEDIA_STATE });
    expect(h.factory).not.toHaveBeenCalled();
    expect(h.media.command).not.toHaveBeenCalled();
  });

  it("rejects extra command arguments without creating a helper", async () => {
    const h = harness();
    expect(await h.invoke(COMMAND, [input, undefined])).toEqual({ ok: false, error: "invalid_payload", state: EMPTY_MEDIA_STATE });
    expect(h.factory).not.toHaveBeenCalled();
  });

  it.each<MediaCommand>(["play", "pause", "previous", "next"])("forwards the exact %s request and its captured session once", async command => {
    const h = harness();
    const payload = { ...input, command };
    expect(await h.invoke(COMMAND, [payload])).toEqual({ ok: true, state });
    expect(h.media.command).toHaveBeenCalledExactlyOnceWith(payload);
    expect(h.factory).toHaveBeenCalledTimes(1);
    expect(h.media.get).not.toHaveBeenCalled();
  });

  it.each(["a", "A0_-".repeat(16)])("accepts valid session-token length boundaries: %s", async session_id => {
    const h = harness();
    await h.invoke(COMMAND, [{ ...input, session_id }]);
    expect(h.media.command).toHaveBeenCalledExactlyOnceWith({ ...input, session_id });
  });

  it.each(["inactive", "passive"])("never calls the media factory when the Node is %s", async reason => {
    const h = harness();
    if (reason === "inactive") h.setActive(false); else h.setPassive(true);
    expect(await h.invoke(GET)).toEqual(EMPTY_MEDIA_STATE);
    expect(await h.invoke(COMMAND, [input])).toEqual({
      ok: false, error: reason === "inactive" ? "node_inactive" : "passive_running", state: EMPTY_MEDIA_STATE,
    });
    expect(h.factory).not.toHaveBeenCalled();
  });

  it("coalesces concurrent reads but never retains metadata as a last-state cache", async () => {
    const h = harness();
    const pending = deferred<MediaStateDto>();
    h.media.get.mockReturnValueOnce(pending.promise);
    const first = h.invoke(GET);
    const second = h.invoke(GET);
    await Promise.resolve();
    expect(h.factory).toHaveBeenCalledTimes(1);
    expect(h.media.get).toHaveBeenCalledTimes(1);
    pending.resolve(state);
    expect(await Promise.all([first, second])).toEqual([state, state]);
    h.media.get.mockResolvedValueOnce({ ...EMPTY_MEDIA_STATE });
    expect(await h.invoke(GET)).toEqual(EMPTY_MEDIA_STATE);
    expect(h.media.get).toHaveBeenCalledTimes(2);
  });

  it.each(["inactive", "passive"])("discards an awaited read when the Node becomes %s", async reason => {
    const h = harness();
    const pending = deferred<MediaStateDto>();
    h.media.get.mockReturnValueOnce(pending.promise);
    const first = h.invoke(GET);
    await Promise.resolve();
    if (reason === "inactive") h.setActive(false); else h.setPassive(true);
    expect(await h.invoke(GET)).toEqual(EMPTY_MEDIA_STATE);
    pending.resolve(state);
    expect(await first).toEqual(EMPTY_MEDIA_STATE);
    expect(h.factory).toHaveBeenCalledTimes(1);
    expect(h.media.get).toHaveBeenCalledTimes(1);
  });

  it("allows one command while a read is waiting and coalesces reads during that command", async () => {
    const h = harness();
    const reading = deferred<MediaStateDto>();
    const commanding = deferred<MediaCommandResultDto>();
    h.media.get.mockReturnValueOnce(reading.promise);
    h.media.command.mockReturnValueOnce(commanding.promise);
    const firstRead = h.invoke(GET);
    await Promise.resolve();
    const command = h.invoke(COMMAND, [input]);
    const secondRead = h.invoke(GET);
    await Promise.resolve();
    expect(h.media.get).toHaveBeenCalledTimes(1);
    expect(h.media.command).toHaveBeenCalledExactlyOnceWith(input);
    commanding.resolve({ ok: true, state }); reading.resolve(state);
    expect(await command).toEqual({ ok: true, state });
    expect(await Promise.all([firstRead, secondRead])).toEqual([state, state]);
    expect(h.factory).toHaveBeenCalledTimes(2);
  });

  it("rejects a concurrent second command as busy without constructing or retrying a helper", async () => {
    const h = harness();
    const pending = deferred<MediaCommandResultDto>();
    h.media.command.mockReturnValueOnce(pending.promise);
    const first = h.invoke(COMMAND, [input]);
    expect(await h.invoke(COMMAND, [{ ...input, command: "next" }])).toEqual({ ok: false, error: "busy", state: EMPTY_MEDIA_STATE });
    expect(h.factory).toHaveBeenCalledTimes(1);
    expect(h.media.command).toHaveBeenCalledExactlyOnceWith(input);
    pending.resolve({ ok: true, state });
    await first;
    expect(await h.invoke(COMMAND, [{ ...input, command: "next" }])).toEqual({ ok: true, state });
    expect(h.media.command).toHaveBeenCalledTimes(2);
  });

  it("does not lose the honest acknowledgement of a sent command when the Node closes", async () => {
    const h = harness();
    const pending = deferred<MediaCommandResultDto>();
    h.media.command.mockReturnValueOnce(pending.promise);
    const command = h.invoke(COMMAND, [input]);
    h.setActive(false);
    pending.resolve({ ok: true, state: { ...state, playback: "paused" } });
    expect(await command).toEqual({ ok: true, state: { ...state, playback: "paused" } });
    expect(h.media.command).toHaveBeenCalledTimes(1);
    expect(h.media.get).not.toHaveBeenCalled();
  });

  it("sanitizes read errors and resets the coalescer for a later explicit request", async () => {
    const h = harness();
    h.media.get.mockRejectedValueOnce(new Error("private helper details"));
    expect(await h.invoke(GET)).toEqual(EMPTY_MEDIA_STATE);
    expect(h.media.get).toHaveBeenCalledTimes(1);
    expect(await h.invoke(GET)).toEqual(state);
    expect(h.media.get).toHaveBeenCalledTimes(2);
  });

  it("never retries a failed command and releases busy in finally", async () => {
    const h = harness();
    h.media.command.mockRejectedValueOnce(new Error("private helper details"));
    expect(await h.invoke(COMMAND, [input])).toEqual({ ok: false, error: "media_unavailable", state: EMPTY_MEDIA_STATE });
    expect(h.media.command).toHaveBeenCalledTimes(1);
    expect(h.media.get).not.toHaveBeenCalled();
    expect(await h.invoke(COMMAND, [input])).toEqual({ ok: true, state });
    expect(h.media.command).toHaveBeenCalledTimes(2);
  });

  it.each([GET, COMMAND])("sanitizes factory exceptions on %s without retrying", async channel => {
    const h = harness();
    h.factory.mockImplementationOnce(() => { throw new Error("private startup details"); });
    expect(await h.invoke(channel, channel === COMMAND ? [input] : []))
      .toEqual(channel === COMMAND ? { ok: false, error: "media_unavailable", state: EMPTY_MEDIA_STATE } : EMPTY_MEDIA_STATE);
    expect(h.factory).toHaveBeenCalledTimes(1);
    expect(h.media.get).not.toHaveBeenCalled();
    expect(h.media.command).not.toHaveBeenCalled();
    expect(await h.invoke(channel, channel === COMMAND ? [input] : []))
      .toEqual(channel === COMMAND ? { ok: true, state } : state);
    expect(h.factory).toHaveBeenCalledTimes(2);
  });

  it("passes a stale-session rejection through without retargeting or retrying", async () => {
    const h = harness();
    const stale: MediaCommandResultDto = { ok: false, error: "stale_session", state: EMPTY_MEDIA_STATE };
    h.media.command.mockResolvedValueOnce(stale);
    expect(await h.invoke(COMMAND, [input])).toEqual(stale);
    expect(h.media.command).toHaveBeenCalledExactlyOnceWith(input);
    expect(h.media.get).not.toHaveBeenCalled();
  });
});

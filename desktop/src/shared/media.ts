/** Local Windows media state; metadata stays in RAM and never enters chat/history. */
export type MediaCommand = "play" | "pause" | "previous" | "next";
export type MediaPlayback = "playing" | "paused" | "stopped" | "unknown";

export interface MediaStateDto {
  v: 1;
  available: boolean;
  session_id: string | null;
  title: string;
  artist: string;
  source_app: string;
  playback: MediaPlayback;
  can_play: boolean;
  can_pause: boolean;
  can_previous: boolean;
  can_next: boolean;
}

export interface MediaCommandInput { command: MediaCommand; session_id: string; }
export interface MediaCommandResultDto { ok: boolean; error?: string; state: MediaStateDto; }
export interface NodeMediaApi {
  get(): Promise<MediaStateDto>;
  command(input: MediaCommandInput): Promise<MediaCommandResultDto>;
}

export const EMPTY_MEDIA_STATE: Readonly<MediaStateDto> = Object.freeze({
  v: 1, available: false, session_id: null, title: "", artist: "", source_app: "", playback: "unknown",
  can_play: false, can_pause: false, can_previous: false, can_next: false,
});

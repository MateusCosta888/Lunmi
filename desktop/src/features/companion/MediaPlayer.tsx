/** Controls reflect the reported session and capabilities; errors survive unavailable sessions. */
import React from "react";
import { Pause, Play, SkipBack, SkipForward } from "lucide-react";
import { useNodeMedia } from "./useNodeMedia";

const PLAYBACK_TEXT: Record<string, string> = {
  playing: "Em reprodução",
  paused: "Em pausa",
  stopped: "Parado",
  unknown: "",
};

export interface MediaPlayerProps {
  /** `false` durante o morph/camada oculta/passivo: nada é consultado nem operável. */
  active: boolean;
}

export function MediaPlayer({ active }: MediaPlayerProps): React.ReactElement | null {
  const { state, busy, loading, error, command } = useNodeMedia(active);
  const notice = error ? <p className="lunmi-mini-media-error" role="status" data-testid="mini-media-error">{error}</p> : null;
  if (!active) return null;
  if (!state || !state.available || !state.session_id) return notice;
  const playing = state.playback === "playing";
  const canToggle = playing ? state.can_pause : state.can_play;
  const toggleLabel = playing ? "Pausar" : "Reproduzir";
  const known = [state.title, state.artist, state.source_app].filter(part => part.trim().length > 0);
  const title = state.title.trim() || "Música do sistema";
  const subtitle = [state.artist.trim(), state.source_app.trim()].filter(Boolean).join(" · ");
  return (
    <section className="lunmi-mini-media" data-testid="mini-media" aria-label="Música do sistema" role="group" aria-busy={loading || busy !== null}>
      <div className="lunmi-mini-media-info">
        <span className="lunmi-mini-media-title" data-testid="mini-media-title" title={known.join(" · ") || title}>{title}</span>
        <span className="lunmi-mini-media-sub" data-testid="mini-media-sub">
          {[subtitle, PLAYBACK_TEXT[state.playback] ?? ""].filter(Boolean).join(" · ") || "Estado do leitor indisponível"}
        </span>
      </div>
      <div className="lunmi-mini-media-actions">
        <button type="button" data-testid="mini-media-prev" className="lunmi-mini-icon-button"
          aria-label="Faixa anterior" title="Faixa anterior"
          disabled={loading || !state.can_previous || busy !== null}
          tabIndex={active ? 0 : -1}
          onClick={() => command("previous")}>
          <SkipBack size={16} aria-hidden="true" />
        </button>
        <button type="button" data-testid="mini-media-toggle" className="lunmi-mini-icon-button"
          aria-label={toggleLabel} title={toggleLabel}
          disabled={loading || !canToggle || busy !== null}
          tabIndex={active ? 0 : -1}
          onClick={() => command(playing ? "pause" : "play")}>
          {playing ? <Pause size={16} aria-hidden="true" /> : <Play size={16} aria-hidden="true" />}
        </button>
        <button type="button" data-testid="mini-media-next" className="lunmi-mini-icon-button"
          aria-label="Faixa seguinte" title="Faixa seguinte"
          disabled={loading || !state.can_next || busy !== null}
          tabIndex={active ? 0 : -1}
          onClick={() => command("next")}>
          <SkipForward size={16} aria-hidden="true" />
        </button>
      </div>
      {notice}
    </section>
  );
}

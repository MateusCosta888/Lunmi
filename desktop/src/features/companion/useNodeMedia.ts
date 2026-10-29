/** Media metadata stays local; only current, visible activations may apply asynchronous results. */
import { useCallback, useEffect, useRef, useState } from "react";
import { EMPTY_MEDIA_STATE, type MediaCommand, type MediaStateDto } from "../../shared/media";
import { getPresenceApi } from "../../shared/ipc/presence";

/** Intervalo entre leituras enquanto o player está visível/operável. */
export const MEDIA_POLL_MS = 4000;

const ERROR_TEXT: Record<string, string> = {
  stale_session: "A sessão do leitor mudou.",
  no_session: "Não há música disponível.",
  not_supported: "Este controlo não está disponível.",
  busy: "O leitor está ocupado.",
  timeout: "O leitor não respondeu a tempo.",
  media_unavailable: "O leitor não está disponível.",
  node_inactive: "O leitor está fechado.",
  passive_running: "Espera que a tarefa termine.",
};

function commandError(code: string | undefined): string {
  return code && Object.hasOwn(ERROR_TEXT, code) ? ERROR_TEXT[code] : "Não foi possível controlar o leitor.";
}

export interface UseNodeMediaApi {
  /** `null` enquanto não houver estado real (sem API, inativo ou ainda sem leitura). */
  state: MediaStateDto | null;
  /** Comando em voo (para desativar botões sem duplicar cliques). */
  busy: MediaCommand | null;
  loading: boolean;
  /** Erro do último comando/leitura; limpo numa leitura ou comando seguinte. */
  error: string | null;
  command: (command: MediaCommand) => void;
}

export function useNodeMedia(active: boolean): UseNodeMediaApi {
  const media = getPresenceApi()?.media;
  const [state, setState] = useState<MediaStateDto | null>(null);
  const [busy, setBusy] = useState<MediaCommand | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const currentState = useRef<MediaStateDto | null>(null);
  // A single synchronous lock covers reads and commands, including across activation changes.
  const inFlight = useRef(false);
  const resume = useRef<(() => void) | null>(null);
  const dispatch = useRef<(command: MediaCommand) => void>(() => undefined);

  useEffect(() => {
    let cancelled = false;
    let revision = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const clear = () => { if (timer !== undefined) { clearTimeout(timer); timer = undefined; } };
    const update = (next: MediaStateDto | null) => { currentState.current = next; setState(next); };
    const current = (stamp: number) => !cancelled && stamp === revision && !document.hidden;
    const schedule = () => {
      clear();
      if (!cancelled && !document.hidden) timer = setTimeout(() => void tick(), MEDIA_POLL_MS);
    };
    const finish = (stamp: number) => {
      inFlight.current = false;
      if (cancelled || stamp !== revision) resume.current?.();
      else schedule();
    };
    const tick = async () => {
      clear();
      if (cancelled || !active || !media || document.hidden || inFlight.current) return;
      inFlight.current = true;
      setLoading(true);
      const stamp = revision;
      try {
        const next = await media.get();
        if (current(stamp)) { update(next ?? EMPTY_MEDIA_STATE); setError(null); }
      } catch {
        if (current(stamp)) { update(EMPTY_MEDIA_STATE); setError("Não foi possível consultar o leitor."); }
      } finally {
        if (!cancelled) setLoading(false);
        finish(stamp);
      }
    };

    update(null); setBusy(null); setLoading(false); setError(null);
    if (!active || !media) return () => { cancelled = true; };

    resume.current = () => void tick();
    dispatch.current = next => {
      const sessionId = currentState.current?.session_id;
      if (cancelled || document.hidden || inFlight.current || !currentState.current?.available || !sessionId) return;
      inFlight.current = true;
      clear(); setBusy(next); setError(null);
      const stamp = revision;
      void (async () => {
        try {
          const result = await media.command({ command: next, session_id: sessionId });
          if (current(stamp)) {
            update(result.state ?? EMPTY_MEDIA_STATE);
            setError(result.ok ? null : commandError(result.error));
          }
        } catch {
          if (current(stamp)) { update(EMPTY_MEDIA_STATE); setError("O leitor não respondeu."); }
        } finally {
          if (!cancelled) setBusy(null);
          finish(stamp);
        }
      })();
    };

    const onVisibility = () => {
      revision += 1;
      clear(); update(null); setBusy(null); setLoading(false); setError(null);
      if (!document.hidden) void tick();
    };
    void tick();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelled = true;
      clear();
      resume.current = null;
      dispatch.current = () => undefined;
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [active, media]);

  const command = useCallback((next: MediaCommand) => dispatch.current(next), []);
  return { state, busy, loading, error, command };
}

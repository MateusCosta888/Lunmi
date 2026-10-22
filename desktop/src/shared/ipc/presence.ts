/**
 * Cliente da presença residente no renderer (`desktop/src/shared/ipc/presence.ts`).
 *
 * O preload expõe `window.lunmiPresence` nas duas janelas; o processo principal valida o
 * remetente, a allowlist de comandos e os limites. Este módulo só lê snapshot/eventos e oferece
 * os hooks que a janela principal e o mini chat partilham (rascunho, modo, sessão, residência).
 *
 * Se a API não existir (preload antigo), os hooks degradam para estado local: nada quebra e o
 * comportamento anterior de X = sair continua válido.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from "react";
import type {
  LunmiPresenceApi,
  NodePreferencesDto,
  NodePreferencesResultDto,
  NodeStateDto,
  PresenceAction,
  PresenceActionResultDto,
  PresenceEventDto,
  PresenceMode,
  PresencePreferencesDto,
  PresenceSnapshotDto,
  SessionReadyPayload,
  VoiceStateEventDto,
} from "../types";

declare global {
  interface Window {
    lunmiPresence?: LunmiPresenceApi;
  }
}

export function getPresenceApi(): LunmiPresenceApi | undefined {
  return typeof window === "undefined" ? undefined : window.lunmiPresence;
}

/**
 * Último snapshot recebido, para uma vista que monta tarde (mini chat) já abrir com a conversa
 * em vez de um frame vazio. É a mesma projeção pública do main, não um segundo estado.
 */
let cachedSnapshot: PresenceSnapshotDto | null = null;

export function getCachedPresenceSnapshot(): PresenceSnapshotDto | null {
  return cachedSnapshot;
}

export function rememberPresenceSnapshot(snapshot: PresenceSnapshotDto): void {
  cachedSnapshot = snapshot;
}

export function presenceAvailable(): boolean {
  return typeof getPresenceApi()?.snapshot === "function";
}

export function presenceAction(action: PresenceAction): Promise<PresenceActionResultDto> {
  const api = getPresenceApi();
  if (!api) {
    return Promise.resolve({ ok: false, mode: "hidden", resident: false, error: "presence_unavailable" });
  }
  return api.action(action);
}

/**
 * Rascunho partilhado: a principal e o mini mostram o mesmo texto; nunca é persistido.
 *
 * O setter preserva `Dispatch<SetStateAction<string>>` para substituir o `useState` atual de
 * `App.tsx`/`ChatPanel` sem adaptadores: aceita texto ou função e o valor resolvido (string) é
 * o que segue para o main. Um snapshot tardio não reverte uma edição/evento mais novo.
 */
export function usePresenceDraft(): [string, Dispatch<SetStateAction<string>>] {
  const api = getPresenceApi();
  const [draft, setDraftState] = useState("");
  const draftRef = useRef("");
  const sawNewerEvent = useRef(false);
  const sawLocalEdit = useRef(false);

  useEffect(() => {
    if (!api) return;
    let alive = true;
    sawNewerEvent.current = false;
    sawLocalEdit.current = false;
    const apply = (text: string) => {
      draftRef.current = text;
      setDraftState(text);
    };
    const off = api.onEvent((event) => {
      if (!alive || event.type !== "draft") return;
      sawNewerEvent.current = true;
      apply(event.text);
    });
    void api
      .snapshot()
      .then((snapshot) => {
        // O snapshot é de antes da subscrição: não desfaz um evento nem uma edição local.
        rememberPresenceSnapshot(snapshot);
        if (alive && !sawNewerEvent.current && !sawLocalEdit.current) apply(snapshot.draft);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
      off();
    };
  }, [api]);

  const setDraft = useCallback<Dispatch<SetStateAction<string>>>(
    (action) => {
      const next = typeof action === "function" ? action(draftRef.current) : action;
      sawLocalEdit.current = true;
      draftRef.current = next;
      // Eco local imediato; o main publica o texto canónico às duas janelas.
      setDraftState(next);
      if (api) void api.setDraft(next).catch(() => undefined);
    },
    [api]
  );

  return [draft, setDraft];
}

export function usePresenceMode(): PresenceMode | null {
  const api = getPresenceApi();
  const [mode, setMode] = useState<PresenceMode | null>(null);

  useEffect(() => {
    if (!api) return;
    let alive = true;
    let sawNewerEvent = false;
    const off = api.onEvent((event) => {
      if (!alive || event.type !== "mode") return;
      sawNewerEvent = true;
      setMode(event.mode);
    });
    void api
      .snapshot()
      .then((snapshot) => {
        rememberPresenceSnapshot(snapshot);
        if (alive && !sawNewerEvent) setMode(snapshot.mode);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
      off();
    };
  }, [api]);

  return mode;
}

/**
 * Tema efetivo da principal. O companheiro monta sem `App`; sem isto aplicaria sempre o tema
 * inicial e não acompanharia uma troca feita nas definições.
 */
export function usePresenceTheme(): string | null {
  const api = getPresenceApi();
  const [theme, setTheme] = useState<string | null>(null);

  useEffect(() => {
    if (!api) return;
    let alive = true;
    let sawNewerEvent = false;
    const off = api.onEvent((event) => {
      if (!alive || event.type !== "theme") return;
      sawNewerEvent = true;
      setTheme(event.theme);
    });
    void api
      .snapshot()
      .then((snapshot) => {
        rememberPresenceSnapshot(snapshot);
        if (alive && !sawNewerEvent) setTheme(snapshot.theme);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
      off();
    };
  }, [api]);

  return theme;
}

export interface PresenceSessionState {
  session: SessionReadyPayload | null;
  sessionVersion: number;
  loading: boolean;
}

/** Sessão pública para o mini chat: mesma sessão do backend, sem handshake próprio. */
export function usePresenceSession(): PresenceSessionState {
  const api = getPresenceApi();
  const [session, setSession] = useState<SessionReadyPayload | null>(null);
  const [sessionVersion, setSessionVersion] = useState(0);
  const [loading, setLoading] = useState(Boolean(api));

  useEffect(() => {
    if (!api) {
      setLoading(false);
      return;
    }
    let alive = true;
    let currentId = "";
    let sawNewerEvent = false;
    const accept = (next: SessionReadyPayload | null) => {
      if (!alive) return;
      setSession(next);
      const nextId = next?.session_id ?? "";
      if (nextId && nextId !== currentId) {
        currentId = nextId;
        setSessionVersion((version) => version + 1);
      }
    };
    const off = api.onEvent((event) => {
      if (!alive || event.type !== "session") return;
      sawNewerEvent = true;
      accept(event.session);
    });
    void api
      .snapshot()
      .then((snapshot) => {
        if (!alive) return;
        rememberPresenceSnapshot(snapshot);
        if (!sawNewerEvent) accept(snapshot.session);
        setLoading(false);
      })
      .catch(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
      off();
    };
  }, [api]);

  return { session, sessionVersion, loading };
}

export interface ResidentOptionState {
  resident: boolean;
  available: boolean;
  busy: boolean;
  setResident: (resident: boolean) => Promise<PresencePreferencesDto | null>;
}

/** Opção «Continuar em segundo plano ao fechar» — só a janela principal a pode alterar. */
export function useResidentOption(): ResidentOptionState {
  const api = getPresenceApi();
  const [resident, setResidentState] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!api) return;
    let alive = true;
    let sawNewerEvent = false;
    const off = api.onEvent((event) => {
      if (!alive || event.type !== "resident") return;
      sawNewerEvent = true;
      setResidentState(event.resident);
    });
    void api
      .snapshot()
      .then((snapshot) => {
        rememberPresenceSnapshot(snapshot);
        if (alive && !sawNewerEvent) setResidentState(snapshot.resident);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
      off();
    };
  }, [api]);

  const setResident = useCallback(
    async (next: boolean): Promise<PresencePreferencesDto | null> => {
      if (!api?.setResident) return null;
      setBusy(true);
      try {
        const saved = await api.setResident(next);
        setResidentState(saved.resident);
        return saved;
      } catch {
        return null;
      } finally {
        setBusy(false);
      }
    },
    [api]
  );

  return {
    resident,
    available: typeof api?.setResident === "function",
    busy,
    setResident,
  };
}

export interface NodeStateHook {
  state: NodeStateDto | null;
  available: boolean;
}

/**
 * Estado do Node (N1): superfície, fase, transição e lado. O `transition_id` é monotónico —
 * um evento mais novo vence o snapshot tardio, e um snapshot antigo não desfaz um evento.
 */
export function useNodeState(): NodeStateHook {
  const api = getPresenceApi();
  const [state, setState] = useState<NodeStateDto | null>(null);

  useEffect(() => {
    if (!api) return;
    let alive = true;
    let sawNewerEvent = false;
    const accept = (next: NodeStateDto) => {
      if (!alive) return;
      setState((previous) =>
        previous && next.transition_id < previous.transition_id ? previous : next
      );
    };
    const off = api.onEvent((event) => {
      if (!alive || event.type !== "node") return;
      sawNewerEvent = true;
      accept(event.state);
    });
    void api
      .snapshot()
      .then((snapshot) => {
        rememberPresenceSnapshot(snapshot);
        if (alive && !sawNewerEvent && snapshot.node) accept(snapshot.node);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
      off();
    };
  }, [api]);

  return { state, available: typeof api?.snapshot === "function" };
}

export interface NodePreferencesHook {
  preferences: NodePreferencesDto | null;
  available: boolean;
  busy: boolean;
  error: NodePreferencesResultDto["error"] | null;
  setNodePreferences: (patch: Partial<NodePreferencesDto>) => Promise<NodePreferencesResultDto | null>;
}

/**
 * Preferências do Node para a janela principal. Sem estado otimista: o valor só muda quando o
 * main confirma a gravação; uma falha mantém o anterior e publica um erro honesto.
 */
export function useNodePreferences(): NodePreferencesHook {
  const api = getPresenceApi();
  const [preferences, setPreferences] = useState<NodePreferencesDto | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<NodePreferencesResultDto["error"] | null>(null);
  const available = typeof api?.setNodePreferences === "function";

  useEffect(() => {
    if (!api) return;
    let alive = true;
    let sawNewerEvent = false;
    const off = api.onEvent((event) => {
      if (!alive || event.type !== "node-prefs") return;
      sawNewerEvent = true;
      setPreferences(event.preferences);
    });
    void api
      .snapshot()
      .then((snapshot) => {
        rememberPresenceSnapshot(snapshot);
        if (alive && !sawNewerEvent) setPreferences(snapshot.node_preferences ?? null);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
      off();
    };
  }, [api]);

  const setNodePreferences = useCallback(
    async (patch: Partial<NodePreferencesDto>): Promise<NodePreferencesResultDto | null> => {
      if (!api?.setNodePreferences) return null;
      setBusy(true);
      setError(null);
      try {
        const result = await api.setNodePreferences(patch);
        if (result.ok) {
          setPreferences(result.preferences);
        } else {
          setError(result.error ?? "invalid_payload");
          setPreferences(result.preferences);
        }
        return result;
      } catch {
        // Falha de IPC não é sucesso: mantém o valor conhecido e marca erro.
        setError("io_error");
        return null;
      } finally {
        setBusy(false);
      }
    },
    [api]
  );

  return { preferences, available, busy, error, setNodePreferences };
}

/**
 * Estado de voz recebido pelo companheiro (o mini nunca sintetiza; só mostra/para).
 *
 * O snapshot traz o último `voice.state` (abrir o mini durante `playing` não perde o estado nem
 * o «Parar voz»). Guardas: evento mais novo que o snapshot vence; geração menor que a já vista é
 * ignorada (gen4 depois de gen5); sessão nova limpa o estado e voz de sessão antiga é descartada.
 * `generation` é autoridade do main; esta vista nunca a inventa.
 */
export function usePresenceVoiceState(): VoiceStateEventDto | null {
  const api = getPresenceApi();
  const [state, setState] = useState<VoiceStateEventDto | null>(null);

  useEffect(() => {
    if (!api) return;
    let alive = true;
    let sawNewerEvent = false;
    let sessionId = "";
    let generation = -1;

    const accept = (voice: VoiceStateEventDto | null) => {
      if (!alive || !voice) return;
      if (sessionId && voice.session_id !== sessionId) return;
      if (Number.isInteger(voice.generation) && voice.generation < generation) return;
      sessionId = voice.session_id;
      if (Number.isInteger(voice.generation)) generation = voice.generation;
      setState(voice);
    };

    const off = api.onEvent((event) => {
      if (!alive) return;
      if (event.type === "voice") {
        sawNewerEvent = true;
        accept(event.state);
        return;
      }
      if (event.type === "session") {
        sawNewerEvent = true;
        const next = event.session?.session_id ?? "";
        if (next !== sessionId) {
          sessionId = next;
          generation = -1;
          setState(null);
        }
      }
    });
    void api
      .snapshot()
      .then((snapshot) => {
        if (!alive || sawNewerEvent) return;
        // Mesmo sem voz no snapshot, a sessão dele já fixa a fronteira: uma voz da sessão
        // anterior (gen alta) não pode entrar depois de a sessão nova começar muda.
        const snapshotSession = snapshot.session?.session_id ?? "";
        if (snapshotSession) sessionId = snapshotSession;
        accept(snapshot.voice);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
      off();
    };
  }, [api]);

  return state;
}

/** Assinatura baixa de eventos de presença, com o filtro do chamador. */
export function usePresenceEvents(
  handler: (event: PresenceEventDto) => void,
  deps: unknown[] = []
): void {
  const api = getPresenceApi();
  const stable = useCallback(handler, deps);
  useEffect(() => {
    if (!api) return;
    return api.onEvent(stable);
  }, [api, stable]);
}

/** Conveniência para o mini chat: snapshot completo ao montar (sem polling). */
export function usePresenceSnapshot(): {
  snapshot: PresenceSnapshotDto | null;
  error: boolean;
  refresh: () => void;
} {
  const api = getPresenceApi();
  const [snapshot, setSnapshot] = useState<PresenceSnapshotDto | null>(null);
  const [error, setError] = useState(false);
  const [tick, setTick] = useState(0);
  const refresh = useMemo(() => () => setTick((value) => value + 1), []);

  useEffect(() => {
    if (!api) return;
    let alive = true;
    void api
      .snapshot()
      .then((next) => {
        if (alive) {
          rememberPresenceSnapshot(next);
          setSnapshot(next);
          setError(false);
        }
      })
      .catch(() => {
        if (alive) setError(true);
      });
    return () => {
      alive = false;
    };
  }, [api, tick]);

  return { snapshot, error, refresh };
}

/**
 * Sessão da ponte IPC no renderer — `desktop/src/shared/ipc/useSession.ts` (F1 reparo 3 / F2).
 *
 * Responsabilidades:
 * - fazer o `session.hello` inicial e publicar `ready`;
 * - detectar falha do backend já pronto (`backend_exited`, `backend_spawn_failed`,
 *   `backend_pipe_error`) e trocar o estado visível para `failed` — uma sessão morta nunca pode
 *   continuar parecendo "Ready";
 * - oferecer `restart()` explícito, que refaz apenas o handshake numa sessão nova. Reiniciar o
 *   backend **não** repete a instrução anterior: repetir tarefa é decisão explícita do usuário;
 * O shell avisa o main após carregar seus dados e montar a interface, não só após o handshake.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { SessionReadyPayload } from "../protocol";
import type { SessionFailureDto } from "../types";
import { describeError, getBridgeApi } from "./client";

export type SessionStatus = "connecting" | "ready" | "failed" | "restarting";

const BACKEND_FAILURE_CODES = new Set<string>([
  "backend_exited",
  "backend_spawn_failed",
  "backend_pipe_error",
]);

export interface UseSessionResult {
  status: SessionStatus;
  session: SessionReadyPayload | null;
  failure: SessionFailureDto | null;
  errorMessage: string;
  /** Sobe a cada sessão nova (inclusive após restart) para o consumidor limpar estado antigo. */
  sessionVersion: number;
  restart: () => Promise<void>;
}

export function isBackendFailureEvent(eventType: string, code: unknown): boolean {
  return eventType === "task.error" && typeof code === "string" && BACKEND_FAILURE_CODES.has(code);
}

export function useSession(): UseSessionResult {
  const [status, setStatus] = useState<SessionStatus>("connecting");
  const [session, setSession] = useState<SessionReadyPayload | null>(null);
  const [failure, setFailure] = useState<SessionFailureDto | null>(null);
  const [errorMessage, setErrorMessage] = useState("");
  const [sessionVersion, setSessionVersion] = useState(0);
  const sessionIdRef = useRef<string>("");
  const requestEpochRef = useRef(0);

  const acceptSession = useCallback((payload: SessionReadyPayload | null | undefined) => {
    if (!payload || typeof payload.session_id !== "string") {
      return;
    }
    setSession(payload);
    setFailure(null);
    setErrorMessage("");
    setStatus("ready");
    if (payload.session_id !== sessionIdRef.current) {
      sessionIdRef.current = payload.session_id;
      setSessionVersion((v) => v + 1);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    const epoch = ++requestEpochRef.current;
    const api = getBridgeApi();
    if (!api) {
      setStatus("failed");
      setErrorMessage("Ponte preload indisponível.");
      return;
    }

    const unsubscribe = api.onEvent((event) => {
      if (!alive) return;
      if (event.type === "session.ready") {
        requestEpochRef.current += 1;
        acceptSession(event.payload as unknown as SessionReadyPayload);
        return;
      }
      if (isBackendFailureEvent(event.type, (event.payload as Record<string, unknown>).code)) {
        requestEpochRef.current += 1;
        const payload = event.payload as unknown as SessionFailureDto;
        setFailure(payload);
        setErrorMessage(payload.message || "O backend Python encerrou.");
        setSession(null);
        setStatus("failed");
      }
    });

    api
      .hello()
      .then((payload) => { if (alive && requestEpochRef.current === epoch) acceptSession(payload); })
      .catch((err: unknown) => {
        if (!alive || requestEpochRef.current !== epoch) return;
        setStatus("failed");
        setErrorMessage(describeError(err));
      });

    return () => {
      alive = false;
      requestEpochRef.current += 1;
      unsubscribe();
    };
  }, [acceptSession]);

  const restart = useCallback(async () => {
    const epoch = ++requestEpochRef.current;
    const api = getBridgeApi();
    if (!api || typeof api.restartBackend !== "function") {
      setStatus("failed");
      setErrorMessage("Reinício do backend indisponível nesta ponte.");
      return;
    }
    setStatus("restarting");
    setErrorMessage("");
    try {
      const payload = await api.restartBackend();
      if (requestEpochRef.current !== epoch) return;
      acceptSession(payload);
    } catch (err: unknown) {
      if (requestEpochRef.current !== epoch) return;
      setFailure(null);
      setStatus("failed");
      setErrorMessage(describeError(err));
    }
  }, [acceptSession]);

  return { status, session, failure, errorMessage, sessionVersion, restart };
}

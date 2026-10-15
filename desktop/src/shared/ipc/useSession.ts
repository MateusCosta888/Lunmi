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
    const api = getBridgeApi();
    if (!api) {
      setStatus("failed");
      setErrorMessage("Ponte preload indisponível.");
      return;
    }

    const unsubscribe = api.onEvent((event) => {
      if (event.type === "session.ready") {
        acceptSession(event.payload as unknown as SessionReadyPayload);
        return;
      }
      if (isBackendFailureEvent(event.type, (event.payload as Record<string, unknown>).code)) {
        const payload = event.payload as unknown as SessionFailureDto;
        setFailure(payload);
        setErrorMessage(payload.message || "O backend Python encerrou.");
        setSession(null);
        setStatus("failed");
      }
    });

    api
      .hello()
      .then((payload) => acceptSession(payload))
      .catch((err: unknown) => {
        setStatus("failed");
        setErrorMessage(describeError(err));
      });

    return () => {
      unsubscribe();
    };
  }, [acceptSession]);

  const restart = useCallback(async () => {
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
      acceptSession(payload);
    } catch (err: unknown) {
      setFailure(null);
      setStatus("failed");
      setErrorMessage(describeError(err));
    }
  }, [acceptSession]);

  return { status, session, failure, errorMessage, sessionVersion, restart };
}

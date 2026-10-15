/**
 * Estado da sessão no shell (`desktop/src/features/session/SessionState.tsx`).
 *
 * - No estado saudável há apenas uma cápsula compacta no topo ("Ready"); nenhuma faixa
 * permanente substitui o cabeçalho. Os detalhes da sessão vivem em Configurações.
 * - Quando o backend já pronto morre, a falha continua explícita: faixa com `role="alert"`,
 *   mensagem e código reais, e **reiniciar backend** — que abre sessão nova sem repetir a
 *   instrução anterior. Uma sessão morta nunca pode continuar parecendo "Ready": o chip
 *   compacta só existe para os estados de conexão vivos.
 */
import React from "react";
import { TOKENS } from "../../shared/theme";
import type { SessionFailureDto } from "../../shared/types";
import type { SessionStatus } from "../../shared/ipc/useSession";

export const SESSION_STATUS_LABEL: Record<SessionStatus, string> = {
  connecting: "Conectando…",
  ready: "Ready",
  failed: "Sessão com falha",
  restarting: "Reiniciando…",
};

export function SessionStatusChip({ status }: { status: SessionStatus }): React.ReactElement | null {
  if (status === "failed") {
    return null;
  }
  return (
    <span className="lunmi-session-chip" data-status={status} role="status" aria-label="Estado da sessão">
      <i aria-hidden="true" />
      <span data-testid="session-status">{SESSION_STATUS_LABEL[status]}</span>
    </span>
  );
}

export interface SessionFailureStripProps {
  failure: SessionFailureDto | null;
  errorMessage: string;
  onRestart: () => void;
}

export function SessionFailureStrip({
  failure,
  errorMessage,
  onRestart,
}: SessionFailureStripProps): React.ReactElement {
  return (
    <section className="lunmi-failure-strip" role="alert" aria-label="Sessão com falha" data-testid="session-failure-strip">
      <strong data-testid="session-status">{SESSION_STATUS_LABEL.failed}</strong>
      <span style={{ color: TOKENS.textSecondary }} data-testid="session-failure-message">
        {errorMessage || "O backend Python não está disponível."}
      </span>
      {failure?.code && (
        <code data-testid="session-failure-code">{failure.code}</code>
      )}
      <p className="lunmi-failure-note">Reiniciar abre uma sessão nova e não repete a última instrução.</p>
      <button type="button" data-testid="restart-backend" onClick={onRestart}>
        Reiniciar backend
      </button>
    </section>
  );
}

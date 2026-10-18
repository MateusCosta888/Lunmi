/**
 * Estado da sessão no shell (`desktop/src/features/session/SessionState.tsx`).
 *
 * O chip saudável "Ready" foi removido por pedido do usuário (2026-10-16): nenhum indicador
 * permanente ocupa o topo no estado saudável, e nenhum nó invisível existe só para satisfazer
 * teste. Os detalhes reais da sessão (id, pid, capacidades) continuam em Configurações.
 *
 * A falha real continua explícita e recuperável: quando o backend já pronto morre, a faixa
 * `role="alert"` mostra mensagem e código reais e oferece **reiniciar backend** — que abre sessão
 * nova sem repetir a instrução anterior. Uma sessão morta nunca pode continuar parecendo saudável.
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

import React, { useEffect, useState } from "react";
import { getBridgeApi } from "../../shared/ipc/client";
import type { UpdateState } from "../../shared/updates";

function useUpdates() {
  const api = getBridgeApi()?.updates;
  const [state, setState] = useState<UpdateState | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!api) return;
    let alive = true;
    let changed = false;
    const unsubscribe = api.onState(next => { changed = true; if (alive) { setState(next); setFailed(false); } });
    void api.get().then(next => { if (alive && !changed) setState(next); }).catch(() => { if (alive) setFailed(true); });
    return () => { alive = false; unsubscribe(); };
  }, [api]);
  const act = async (action: "check" | "install") => {
    try { setFailed(false); setState(await api![action]()); } catch { setFailed(true); }
  };
  return { state, failed, act };
}

const ERRORS = {
  verification: "Não foi possível verificar a autenticidade da atualização. Nada foi instalado. Tenta procurar novamente.",
  unavailable: "Não foi possível obter a atualização. A versão atual continua disponível; tenta novamente mais tarde.",
  busy: "Conclui ou para a tarefa, a ligação de conta ou a operação de voz antes de reiniciar.",
  cancelled: "Atualização guardada para mais tarde.",
};

export function UpdatesCard(): React.ReactElement | null {
  const { state, failed, act } = useUpdates();
  if (!state && !failed) return null;
  const working = state && ["checking", "downloading", "installing"].includes(state.phase);
  return <section className="lunmi-settings-card" aria-labelledby="updates-heading" data-testid="updates-card">
    <h3 id="updates-heading">Atualizações</h3>
    {state && <p>Versão instalada · {state.currentVersion}</p>}
    <p role="status" aria-live="polite">
      {failed ? "Não foi possível consultar o atualizador. Tenta novamente mais tarde." :
        state?.phase === "disabled" ? "As atualizações estão disponíveis na Lunmi instalada para Windows." :
        state?.phase === "checking" ? "A procurar atualizações…" :
        state?.phase === "downloading" ? `A baixar a versão ${state.version} · ${state.percent ?? 0}%` :
        state?.phase === "ready" ? `A versão ${state.version} está pronta para instalar.` :
        state?.phase === "installing" ? "A preparar o reinício…" :
        state?.phase === "error" ? "A atualização não foi concluída." : "A Lunmi procura e baixa novas versões automaticamente."}
    </p>
    {state?.phase === "downloading" && <progress aria-label="Download da atualização" max={100} value={state.percent ?? 0} style={{ width: "100%", accentColor: "var(--lunmi-accent)" }} />}
    {state?.error && <p role={state.error === "cancelled" ? "status" : "alert"}>{ERRORS[state.error]}</p>}
    {state?.phase !== "disabled" && <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
      {state?.phase === "ready" ? <button className="lunmi-action-button lunmi-action-primary" type="button" onClick={() => void act("install")}>Reiniciar e atualizar</button> :
        <button className="lunmi-action-button" type="button" disabled={Boolean(working)} onClick={() => void act("check")}>Procurar atualizações</button>}
    </div>}
    {state?.phase !== "disabled" && <p style={{ color: "var(--lunmi-text-muted)", fontSize: 12 }}>A instalação espera pelo teu reinício. Fechar a janela não instala a atualização.</p>}
  </section>;
}

export function UpdateNotice({ onOpen }: { onOpen: () => void }): React.ReactElement | null {
  const { state } = useUpdates();
  if (state?.phase !== "ready") return null;
  return <div className="lunmi-history-banner" role="status" style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
    <span>A Lunmi {state.version} está pronta para atualizar.</span>
    <button type="button" className="lunmi-action-button" style={{ minHeight: 44 }} onClick={onOpen}>Ver atualização</button>
  </div>;
}

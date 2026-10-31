import React, { useEffect, useRef, useState } from "react";
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
        state?.phase === "available" ? `A versão ${state.version} está disponível.` :
        state?.phase === "downloading" ? `A baixar a versão ${state.version} · ${state.percent ?? 0}%` :
        state?.phase === "ready" ? `A versão ${state.version} está pronta para instalar.` :
        state?.phase === "installing" ? "A fechar a Lunmi para instalar. O instalador vai mostrar o progresso…" :
        state?.phase === "error" ? "A atualização não foi concluída." : "A Lunmi procura e baixa novas versões automaticamente."}
    </p>
    {state?.phase === "downloading" && <progress aria-label="Download da atualização" max={100} value={state.percent ?? 0} style={{ width: "100%", accentColor: "var(--lunmi-accent)" }} />}
    {state?.error && <p role={state.error === "cancelled" ? "status" : "alert"}>{ERRORS[state.error]}</p>}
    {state?.phase !== "disabled" && <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
      {state?.phase === "ready" || state?.phase === "available" ? <button className="lunmi-action-button lunmi-action-primary" type="button" onClick={() => void act("install")}>{state.phase === "ready" ? "Reiniciar e atualizar" : "Instalar atualização"}</button> :
        <button className="lunmi-action-button" type="button" disabled={Boolean(working)} onClick={() => void act("check")}>Procurar atualizações</button>}
    </div>}
    {state?.phase !== "disabled" && <p style={{ color: "var(--lunmi-text-muted)", fontSize: 12 }}>A Lunmi procura novas versões ao abrir e pergunta antes de instalar. No fim, abre sozinha.</p>}
  </section>;
}

/** Ao abrir: se houver versão nova, mostra o que mudou e pergunta antes de instalar. */
export function UpdateModal(): React.ReactElement | null {
  const { state, act } = useUpdates();
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [accepted, setAccepted] = useState(false);
  const primary = useRef<HTMLButtonElement>(null);
  const phase = state?.phase;
  const offered = phase === "available" && dismissed !== state?.version;
  const following = accepted && (phase === "downloading" || phase === "ready" || phase === "installing" || phase === "error");
  const open = Boolean(state && (offered || following));
  useEffect(() => { if (open) primary.current?.focus(); }, [open, phase]);
  if (!open || !state) return null;
  const later = () => { setDismissed(state.version ?? null); setAccepted(false); };
  const install = () => { setAccepted(true); void act("install"); };
  const busy = phase === "downloading" || phase === "installing" || (phase === "ready" && !state.error);
  return <div className="lunmi-modal-backdrop" data-testid="update-modal-backdrop"
    onKeyDown={event => { if (event.key === "Escape" && !busy) later(); }}>
    <div role="dialog" aria-modal="true" aria-labelledby="update-modal-title" className="lunmi-modal" data-testid="update-modal">
      <header className="lunmi-modal-head">
        <h2 id="update-modal-title">{phase === "error" ? "A atualização não foi concluída" : "Nova versão da Lunmi"}</h2>
      </header>
      <p style={{ margin: "0 0 12px", color: "var(--lunmi-text-muted)" }}>Versão {state.version} · tens a {state.currentVersion}</p>
      {phase === "available" && <>
        <h3 style={{ fontSize: 14, margin: "0 0 6px" }}>O que há de novo</h3>
        <div data-testid="update-notes" style={{ whiteSpace: "pre-wrap", maxHeight: 260, overflowY: "auto", padding: "10px 12px", borderRadius: 10,
          background: "var(--lunmi-bg-input, #0B1019)", border: "1px solid var(--lunmi-border-subtle, #26334A)", fontSize: 13, lineHeight: 1.55 }}>
          {state.notes || "Correções e melhorias."}
        </div>
        <p className="lunmi-modal-hint" style={{ marginTop: 12 }}>Ao instalar, a Lunmi descarrega a nova versão, fecha, instala e volta a abrir sozinha. Mensagens por enviar perdem-se; o histórico e as definições ficam.</p>
      </>}
      {(phase === "downloading" || (phase === "ready" && !state.error) || phase === "installing") && <div role="status" aria-live="polite">
        <p style={{ margin: "4px 0 8px" }}>{phase === "downloading" ? `A descarregar a atualização · ${state.percent ?? 0}%` : "A fechar a Lunmi para instalar…"}</p>
        <progress aria-label="Download da atualização" max={100} value={phase === "downloading" ? state.percent ?? 0 : 100} style={{ width: "100%", accentColor: "var(--lunmi-accent)" }} />
        {phase === "downloading" && <p className="lunmi-modal-hint">Podes continuar a usar a Lunmi. Quando o download terminar, ela fecha para instalar.</p>}
      </div>}
      {state.error && <p role={state.error === "cancelled" ? "status" : "alert"} className="lunmi-modal-error">{ERRORS[state.error]}</p>}
      <div className="lunmi-modal-actions">
        {phase === "available" && <button type="button" className="lunmi-modal-quiet" onClick={later}>Mais tarde</button>}
        {phase === "available" && <button ref={primary} type="button" className="lunmi-modal-primary" onClick={install}>Instalar agora</button>}
        {phase === "downloading" && <button ref={primary} type="button" className="lunmi-modal-quiet" onClick={() => setAccepted(false)}>Esconder</button>}
        {phase === "ready" && state.error && <>
          <button type="button" className="lunmi-modal-quiet" onClick={later}>Mais tarde</button>
          <button ref={primary} type="button" className="lunmi-modal-primary" onClick={install}>Tentar de novo</button>
        </>}
        {phase === "error" && <button ref={primary} type="button" className="lunmi-modal-primary" onClick={later}>Fechar</button>}
      </div>
    </div>
  </div>;
}

export function UpdateNotice({ onOpen }: { onOpen: () => void }): React.ReactElement | null {
  const { state } = useUpdates();
  if (state?.phase !== "ready") return null;
  return <div className="lunmi-history-banner" role="status" style={{ display: "flex", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
    <span>A Lunmi {state.version} está pronta para atualizar.</span>
    <button type="button" className="lunmi-action-button" style={{ minHeight: 44 }} onClick={onOpen}>Ver atualização</button>
  </div>;
}

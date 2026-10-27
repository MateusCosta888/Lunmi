import React, { useEffect, useRef, useState } from "react";
import { getBridgeApi, type SendCommandFn } from "../../shared/ipc/client";
import type { SessionReadyPayload } from "../../shared/protocol";
import type { GmailConnectionState, IntegrationsDto } from "../../shared/types";
import { Icon } from "../../shared/ui";
import SpotifyCard from "./SpotifyCard";
import gmailLogo from "./assets/gmail.svg";
import "./apps.css";

const LABELS: Record<GmailConnectionState, string> = {
  disconnected: "Não ligado", connecting: "A ligar ao Composio…",
  composio_connected: "Composio ligado", gmail_connecting: "A autorizar o Gmail…",
  select_account: "Escolhe a tua conta", connected: "Ligado", reauth_required: "Reconexão necessária",
  unavailable: "Gmail ainda indisponível nesta ligação", error: "Não foi possível ligar",
};
const FLOW_STATES: GmailConnectionState[] = ["connecting", "composio_connected", "gmail_connecting", "select_account"];

const PRIMARY = "lunmi-action-button lunmi-action-primary";
const SECONDARY = "lunmi-action-button";

export default function AppsPanel({ sendCommand, session }: {
  sendCommand: SendCommandFn; session: SessionReadyPayload | null;
}): React.ReactElement {
  const supported = session?.capabilities.integrations === true;
  const sessionId = session?.session_id ?? "";
  const identity = useRef(sessionId); identity.current = sessionId;
  const [data, setData] = useState<IntegrationsDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState("");
  const [error, setError] = useState("");
  const [selected, setSelected] = useState("");
  const latest = useRef<IntegrationsDto | null>(null);
  const events = useRef(0);
  const alive = useRef(false);
  const operation = useRef(0);

  const accept = (next: IntegrationsDto, eventVersion: number) => {
    if (next.gmail.epoch < (latest.current?.gmail.epoch ?? -1)) return;
    // A reply captured before a push must not roll the same flow back to an earlier state.
    if (eventVersion !== events.current && next.gmail.epoch === latest.current?.gmail.epoch) return;
    latest.current = next; setData(next); setLoading(false);
  };

  useEffect(() => {
    alive.current = true; latest.current = null; events.current = 0;
    operation.current++; setData(null); setSelected(""); setAction(""); setError(""); setLoading(supported);
    if (!supported) return () => { alive.current = false; };
    let current = true;
    const unsubscribe = getBridgeApi()?.onEvent(event => {
      const eventSession = (event.payload as { session_id?: string }).session_id;
      if (!current || event.type !== "integrations.changed" || (eventSession && eventSession !== sessionId)) return;
      const next = event.payload as unknown as IntegrationsDto;
      if (next.gmail.epoch < (latest.current?.gmail.epoch ?? -1)) return;
      events.current++; accept(next, events.current); setError(""); setSelected("");
    });
    const version = events.current;
    void sendCommand<IntegrationsDto>("integrations.get", {}).then(next => {
      if (current) accept(next, version);
    }).catch(() => {
      if (current) { setLoading(false); setError("Não foi possível consultar a ligação. Tenta novamente."); }
    });
    return () => { current = false; alive.current = false; operation.current++; unsubscribe?.(); };
  }, [sendCommand, sessionId, supported]);

  const run = async (command: string, payload: Record<string, unknown> = {}) => {
    if (!supported || action) return;
    const id = ++operation.current, origin = sessionId, version = events.current;
    setAction(command); setError("");
    try {
      const next = await sendCommand<IntegrationsDto>(command, payload);
      if (alive.current && identity.current === origin && operation.current === id) accept(next, version);
    } catch {
      if (alive.current && identity.current === origin && operation.current === id) {
        setError(command === "integrations.cancel" ? "Não foi possível confirmar o cancelamento. A ligação pode continuar em curso."
          : command === "integrations.disconnect" ? "Não foi possível confirmar que a ligação foi removida."
            : "Não foi possível concluir esta ação. Tenta novamente.");
      }
    } finally {
      if (alive.current && identity.current === origin && operation.current === id) setAction("");
    }
  };

  const gmail = data?.gmail;
  const inFlow = gmail && FLOW_STATES.includes(gmail.state);
  const connected = gmail?.state === "connected" && Boolean(gmail.account);
  const tone = connected ? "ok"
    : gmail?.state === "unavailable" || gmail?.state === "error" ? "error"
      : gmail?.state === "reauth_required" ? "warn" : "idle";

  return <section className="lunmi-settings-page lunmi-apps-page" aria-labelledby="apps-heading" data-testid="apps-panel">
    <div className="lunmi-settings-content">
      <header className="lunmi-settings-heading">
        <h2 id="apps-heading">Apps</h2>
        <p>Liga as tuas contas para a Lunmi te ajudar no chat.</p>
      </header>

      <div className="lunmi-settings-grid lunmi-apps-grid">
        <article className="lunmi-settings-card lunmi-apps-card lunmi-app-card"
          aria-labelledby="gmail-heading" data-testid="gmail-card" aria-busy={loading || Boolean(action)}>
          <header className="lunmi-app-card-head">
            <span className="lunmi-app-logo" data-brand="gmail" data-testid="gmail-logo">
              <img src={gmailLogo} alt="" width={28} height={28} draggable={false} />
            </span>
            <div className="lunmi-app-card-heading">
              <h3 id="gmail-heading">Gmail</h3>
              <p>Pesquisar, ler e preparar emails. Enviar sempre com a tua confirmação.</p>
            </div>
          </header>

          <div className="lunmi-app-card-body">
            {!supported ? <p className="lunmi-app-copy" role="status">Esta versão do backend não disponibiliza a ligação ao Gmail.</p>
              : loading ? <p className="lunmi-app-copy" role="status">A consultar a ligação…</p>
                : <>
                  {gmail && <p className="lunmi-app-status lunmi-apps-state" role="status" data-testid="gmail-state"
                    data-connected={connected} data-tone={tone}>
                    {connected ? <><Icon name="check" size={15} /><span>Ligado</span>
                      <span className="lunmi-app-status-sep" aria-hidden="true">·</span>
                      <span className="lunmi-app-account">{gmail.account!.email}</span></>
                      : gmail.state === "connected" ? "Conta não confirmada" : LABELS[gmail.state]}
                  </p>}
                  {inFlow && <p className="lunmi-app-copy">{gmail.state === "select_account" ? "Composio ligado. Escolhe qual conta Gmail queres usar."
                    : "Conclui a autorização no browser. Usa a tua própria conta Composio e autoriza o Gmail."}</p>}
                  {gmail?.state === "select_account" && <fieldset className="lunmi-app-accounts">
                    <legend>Qual conta Gmail queres usar?</legend>
                    {gmail.accounts.map(account => <label key={account.id}>
                      <input type="radio" name="gmail-account" value={account.id} checked={selected === account.id}
                        disabled={Boolean(action)} onChange={() => setSelected(account.id)} />
                      <span>{account.email}</span>
                    </label>)}
                    <button type="button" className={PRIMARY} disabled={!selected || Boolean(action)}
                      onClick={() => void run("integrations.select", { account_id: selected })}>Usar esta conta</button>
                  </fieldset>}
                  {gmail?.state === "unavailable" && <p className="lunmi-app-copy">Não foi possível confirmar uma conta Gmail e as ferramentas necessárias. Nenhuma conta alternativa foi usada.</p>}
                  {gmail?.state === "reauth_required" && <p className="lunmi-app-copy">Volta a ligar ao Composio para continuar a usar o Gmail.</p>}

                  <div className="lunmi-app-actions">
                    {inFlow ? <button type="button" className={SECONDARY} disabled={Boolean(action)}
                      onClick={() => void run("integrations.cancel")}>Cancelar ligação</button>
                      : connected ? <><button type="button" className={SECONDARY} disabled={Boolean(action)}
                        onClick={() => void run("integrations.connect")}>Trocar conta</button>
                        <button type="button" className="lunmi-action-button lunmi-action-danger" disabled={Boolean(action)}
                          onClick={() => void run("integrations.disconnect")}>Desligar da Lunmi</button></>
                        : <button type="button" className={PRIMARY} disabled={Boolean(action)}
                          onClick={() => void run(gmail ? "integrations.connect" : "integrations.get")}>
                          {action ? "A aguardar…" : !gmail ? "Tentar novamente" : gmail.state === "reauth_required" ? "Reconectar" : "Ligar Gmail"}
                        </button>}
                  </div>
                </>}
            {error && <p className="lunmi-app-error" role="alert">{error}</p>}
          </div>

          {supported && <p className="lunmi-app-card-note">A ligação usa o teu Composio. O conteúdo dos emails consultados pode ser enviado ao provider do Brain.
            Desligar da Lunmi remove o acesso local; a revogação global é feita no Composio.</p>}
        </article>

        <SpotifyCard sendCommand={sendCommand} session={session} />
      </div>
    </div>
  </section>;
}

/**
 * Introdução da primeira abertura: boas-vindas e um cérebro a funcionar antes do chat.
 *
 * Plug and play: no Groq a pessoa só cria a chave na página que abrimos e copia-a; a Lunmi
 * apanha-a da área de transferência (o main só devolve algo com formato de chave Groq), testa e
 * aplica. No Kiro, a Lunmi instala o CLI e lança o login já com o fornecedor escolhido.
 * "Configurar depois" está sempre disponível: ninguém fica preso sem rede ou sem conta.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { describeError, getBridgeApi, type SendCommandFn } from "../../shared/ipc/client";
import type { BrainGetDto, BrainStateDto, ModelListingDto, UiPreferencesDto } from "../../shared/types";
import "./onboarding.css";

type Step = "welcome" | "choose" | "groq" | "kiro" | "done";

export interface KiroStateDto {
  phase: "idle" | "downloading" | "installing" | "installed" | "waiting_login" | "logged_in" | "error";
  progress: number | null;
  error: string | null;
  url: string | null;
  supported?: boolean;
  installed?: boolean;
  version?: string | null;
  logged_in?: boolean;
  account?: string | null;
}

/**
 * Mostrar só a quem nunca concluiu a introdução e ainda não tem um cérebro configurado. Só um
 * `false` explícito conta: um backend que não conhece o campo nunca abre a introdução.
 */
export function needsOnboarding(prefs: UiPreferencesDto | null, brain: BrainGetDto | null): boolean {
  if (!prefs || !brain || prefs.onboarding_done !== false) return false;
  const info = brain.providers.find(p => p.id === brain.current.provider);
  if (!info) return true;
  if (info.requires_api_key) return !brain.current.has_api_key;
  return info.installed === false;
}

const KIRO_LOGINS = [
  { method: "google", label: "Entrar com Google" },
  { method: "github", label: "Entrar com GitHub" },
  { method: "builder", label: "AWS Builder ID" },
] as const;

export function Onboarding({ sendCommand, onDone }: {
  sendCommand: SendCommandFn;
  /** `view` diz para onde ir depois: o chat, ou o painel Brain quando a pessoa adia. */
  onDone: (view: "chat" | "brain") => void;
}): React.ReactElement {
  const [step, setStep] = useState<Step>("welcome");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [groqKey, setGroqKey] = useState("");
  const [kiro, setKiro] = useState<KiroStateDto | null>(null);
  const [connected, setConnected] = useState("");
  const triedKey = useRef("");

  const finish = useCallback(async (view: "chat" | "brain") => {
    // Gravar a conclusão nunca bloqueia a entrada: no pior caso a introdução volta a aparecer.
    await sendCommand("ui.preferences.set", { onboarding_done: true }).catch(() => undefined);
    onDone(view);
  }, [onDone, sendCommand]);

  const apply = useCallback(async (payload: Record<string, unknown>, label: string) => {
    const result = await sendCommand<BrainStateDto>("brain.set", {
      base_url: "", app_provider: "", app_model: "", remember_credentials: true, ...payload,
    });
    if (result.ok === false) throw new Error(result.error?.message ?? "Não foi possível aplicar o cérebro.");
    setConnected(label);
    setStatus("");
    setStep("done");
  }, [sendCommand]);

  // ---------------------------------------------------------------- Groq --
  const connectGroq = useCallback(async (key: string) => {
    const trimmed = key.trim();
    if (!trimmed || busy) return;
    triedKey.current = trimmed;
    setBusy(true); setError(null); setStatus("A testar a chave no Groq…");
    try {
      // `brain.list` com a chave nova: o catálogo só carrega se o Groq aceitar a chave.
      const listing = await sendCommand<ModelListingDto>("brain.list", {
        provider: "groq", model: "", api_key: trimmed, base_url: "", force: true,
      });
      const models = listing.models;
      if (!listing.ok || !models.length) throw new Error(listing.detail || "O Groq recusou esta chave.");
      const model = (models.find(m => m.default) ?? models[0])?.id;
      if (!model) throw new Error("O Groq não devolveu nenhum modelo para esta chave.");
      setStatus("A ligar o cérebro…");
      await apply({ provider: "groq", model, api_key: trimmed }, "Groq");
      setGroqKey("");
    } catch (err) {
      setError(describeError(err)); setStatus("");
    } finally {
      setBusy(false);
    }
  }, [apply, busy, sendCommand]);

  // A chave copiada no browser chega sozinha quando a pessoa volta à Lunmi.
  useEffect(() => {
    if (step !== "groq") return;
    const api = getBridgeApi()?.onboarding;
    if (!api) return;
    let alive = true;
    const check = () => {
      void api.groqKey().then(({ key }) => {
        if (alive && key && key !== triedKey.current) { setGroqKey(key); void connectGroq(key); }
      }).catch(() => undefined);
    };
    window.addEventListener("focus", check);
    const timer = window.setInterval(check, 1500);
    return () => { alive = false; window.removeEventListener("focus", check); window.clearInterval(timer); };
  }, [step, connectGroq]);

  // ---------------------------------------------------------------- Kiro --
  const refreshKiro = useCallback(async () => {
    setKiro(await sendCommand<KiroStateDto>("brain.kiro.status", {}));
  }, [sendCommand]);

  useEffect(() => {
    if (step !== "kiro") return;
    setError(null);
    void refreshKiro().catch(err => setError(describeError(err)));
    const api = getBridgeApi();
    if (!api) return;
    return api.onEvent(event => {
      if (event.type !== "brain.kiro.state") return;
      const next = event.payload as unknown as KiroStateDto;
      setKiro(current => ({ ...current, ...next }));
      if (next.error) setError(next.error);
      // Instalado ou com login feito: o `status` confirma com o próprio CLI.
      if (next.phase === "installed" || next.phase === "logged_in") void refreshKiro().catch(() => undefined);
    });
  }, [step, refreshKiro]);

  useEffect(() => {
    if (step !== "kiro" || !kiro?.logged_in || busy || connected) return;
    setBusy(true); setStatus("A ligar o cérebro…");
    void apply({ provider: "kiro", model: "auto" }, `Kiro (${kiro.account ?? "conta"})`)
      .catch(err => { setError(describeError(err)); setStatus(""); })
      .finally(() => setBusy(false));
  }, [step, kiro, busy, connected, apply]);

  const kiroAction = (type: string, payload: Record<string, unknown> = {}) => {
    setError(null);
    void sendCommand(type, payload).catch(err => setError(describeError(err)));
  };

  const working = kiro?.phase === "downloading" || kiro?.phase === "installing" || kiro?.phase === "waiting_login";

  return (
    <div className="lunmi-onboarding" data-testid="onboarding">
      <section className="lunmi-onboarding-card" aria-live="polite">
        {step === "welcome" && <>
          <p className="lunmi-onboarding-eyebrow">Bem-vindo</p>
          <h1>Olá, eu sou a Lunmi.</h1>
          <p>Converso contigo, faço tarefas no teu computador e lembro-me do que me pedires para lembrar.
            As tuas conversas e memórias ficam neste computador.</p>
          <p>Para pensar, preciso de um cérebro. Vamos ligar um agora; demora cerca de um minuto.</p>
          <div className="lunmi-onboarding-actions">
            <button type="button" className="lunmi-onboarding-primary" data-testid="onboarding-start"
              onClick={() => setStep("choose")}>Começar</button>
          </div>
        </>}

        {step === "choose" && <>
          <p className="lunmi-onboarding-eyebrow">Passo 1 de 2</p>
          <h1>Escolhe o cérebro da Lunmi</h1>
          <div className="lunmi-onboarding-options">
            <button type="button" className="lunmi-onboarding-option" data-testid="onboarding-groq"
              onClick={() => { setError(null); setStep("groq"); }}>
              <strong>Groq <span className="lunmi-onboarding-badge">Recomendado</span></strong>
              <span>Gratuito e muito rápido. Só precisas de criar uma chave na conta Groq.</span>
            </button>
            <button type="button" className="lunmi-onboarding-option" data-testid="onboarding-kiro"
              onClick={() => { setError(null); setStep("kiro"); }}>
              <strong>Kiro</strong>
              <span>Entras com Google, GitHub ou AWS Builder ID. A Lunmi instala o Kiro CLI por ti.</span>
            </button>
          </div>
        </>}

        {step === "groq" && <>
          <p className="lunmi-onboarding-eyebrow">Passo 2 de 2 · Groq</p>
          <h1>Liga a tua conta Groq</h1>
          <ol className="lunmi-onboarding-steps">
            <li>Abre o Groq e entra (ou cria conta, é grátis).</li>
            <li>Carrega em <b>Create API Key</b> e copia a chave.</li>
            <li>Volta aqui: a Lunmi apanha a chave sozinha.</li>
          </ol>
          <div className="lunmi-onboarding-actions">
            <button type="button" className="lunmi-onboarding-primary" data-testid="onboarding-groq-open" disabled={busy}
              onClick={() => void getBridgeApi()?.onboarding?.open("groq-keys")}>Abrir Groq</button>
          </div>
          <form className="lunmi-onboarding-key" onSubmit={event => { event.preventDefault(); void connectGroq(groqKey); }}>
            <label htmlFor="onboarding-groq-key">Ou cola a chave aqui</label>
            <div>
              <input id="onboarding-groq-key" type="password" autoComplete="off" spellCheck={false}
                placeholder="gsk_…" value={groqKey} disabled={busy} data-testid="onboarding-groq-key"
                onChange={event => setGroqKey(event.target.value)} />
              <button type="submit" disabled={busy || !groqKey.trim()}>Ligar</button>
            </div>
          </form>
        </>}

        {step === "kiro" && <>
          <p className="lunmi-onboarding-eyebrow">Passo 2 de 2 · Kiro</p>
          <h1>Liga a tua conta Kiro</h1>
          {!kiro && <p role="status">A verificar o Kiro CLI…</p>}
          {kiro && kiro.supported === false && <p>O Kiro CLI precisa do Windows 11. Escolhe o Groq para continuar.</p>}
          {kiro && kiro.supported !== false && !kiro.installed && <>
            <p>Primeiro a Lunmi instala o Kiro CLI (cerca de 270 MB, sem pedir permissões de administrador).</p>
            {kiro.phase === "downloading" && <progress max={1} value={kiro.progress ?? 0} aria-label="A descarregar o Kiro CLI" />}
            {kiro.phase === "installing" && <p role="status">A instalar… o Windows mostra uma pequena barra de progresso.</p>}
            <div className="lunmi-onboarding-actions">
              <button type="button" className="lunmi-onboarding-primary" data-testid="onboarding-kiro-install"
                disabled={working} onClick={() => kiroAction("brain.kiro.install")}>Instalar Kiro CLI</button>
            </div>
          </>}
          {kiro?.installed && !kiro.logged_in && <>
            <p>Escolhe como entrar. O browser abre na página certa; quando terminares, volta aqui.</p>
            <div className="lunmi-onboarding-actions">
              {KIRO_LOGINS.map(item => <button key={item.method} type="button" className="lunmi-onboarding-secondary"
                data-testid={`onboarding-kiro-${item.method}`} disabled={working}
                onClick={() => kiroAction("brain.kiro.login", { method: item.method })}>{item.label}</button>)}
            </div>
            {kiro.phase === "waiting_login" && <>
              <p role="status">À espera do login no browser…</p>
              {kiro.url && <p className="lunmi-onboarding-url">Se o browser não abriu, visita: <code>{kiro.url}</code></p>}
            </>}
          </>}
          {working && <button type="button" className="lunmi-link" onClick={() => kiroAction("brain.kiro.cancel")}>Cancelar</button>}
        </>}

        {step === "done" && <>
          <p className="lunmi-onboarding-eyebrow">Pronto</p>
          <h1>A Lunmi já tem cérebro.</h1>
          <p>Ligada a {connected}. Podes mudar de cérebro quando quiseres, em Brain.</p>
          <div className="lunmi-onboarding-actions">
            <button type="button" className="lunmi-onboarding-primary" data-testid="onboarding-finish"
              onClick={() => void finish("chat")}>Entrar na Lunmi</button>
          </div>
        </>}

        {status && <p role="status" className="lunmi-onboarding-status">{status}</p>}
        {error && <p role="alert" className="lunmi-onboarding-error" data-testid="onboarding-error">{error}</p>}

        <footer className="lunmi-onboarding-footer">
          {(step === "groq" || step === "kiro") && !busy && !working &&
            <button type="button" className="lunmi-link" onClick={() => { setError(null); setStep("choose"); }}>Voltar</button>}
          {step !== "done" && <button type="button" className="lunmi-link" data-testid="onboarding-skip"
            onClick={() => void finish("brain")}>Configurar depois</button>}
        </footer>
      </section>
    </div>
  );
}

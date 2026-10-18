/**
 * Painel do Brain (`desktop/src/features/brain/BrainPanel.tsx`, redesign BYOK 2026-10-17).
 *
 * Fluxo: escolher provider → chave (sempre que existir rascunho) → catálogo pesquisável do
 * provider consultado → verificar (opcional) → aplicar. Modelo é SEMPRE escolhido do catálogo;
 * não há campo de ID. Composição inspirada no fluxo BYOK do Open Design (fonte pinada em
 * output/brain-provider-redesign/reference), implementação própria.
 *
 * Regras do contrato `.agents/BRAIN-PROVIDERS-OPENDESIGN-CONTRACT.md`:
 * - `brain.list` vai com `{provider-alvo, role, force, base_url, model, api_key?}`; `api_key` só
 *   quando existe rascunho em RAM; `provider` do role `app` é o provider do AppAgent, não o principal.
 * - A chave NÃO desaparece entre listar/verificar/aplicar; limpa ao aplicar, ao descartar
 *   explicitamente, ao trocar provider e ao desmontar. Nunca é devolvida nem renderizada de volta.
 * - Respostas obsoletas (list/verify/probe) são descartadas por identidade completa
 *   provider+role+endpoint+modelo+impressão-da-chave — a impressão nunca vai para DOM/log.
 * - Catálogo, verificação e aplicação são selos separados: listar não é verificar; `cached` é
 *   rotulado; o evento `brain.updated` é a autoridade do aplicado.
 * - `brain.probe` continua só com clique explícito e `consent: true`.
 * - AppAgent avançado preservado; sem campo de chave próprio (backend não tem caminho canónico).
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { describeError, getBridgeApi, toBridgeError, type SendCommandFn } from "../../shared/ipc/client";
import type {
  BrainGetDto,
  BrainProbeDto,
  BrainStateDto,
  BrainVerifyDto,
  ModelListingDto,
} from "../../shared/types";
import type { SessionReadyPayload } from "../../shared/protocol";
import { ModelCatalogSelect, type CatalogState } from "./ModelCatalogSelect";
import "./brain.css";

export interface BrainPanelProps {
  sendCommand: SendCommandFn;
  session: SessionReadyPayload | null;
  onConfigured?: () => void;
}

type ListingRole = "main" | "app";

/** Impressão local da chave (FNV-1a + comprimento): identifica a credencial sem guardar texto. */
function keyFingerprint(value: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return `${value.length}:${(hash >>> 0).toString(36)}`;
}

function hasEnvOverride(state: BrainStateDto | null): boolean {
  if (!state) return false;
  const overrides = state.env_overrides;
  return Boolean(overrides && (overrides.provider || overrides.model || overrides.app_provider || overrides.app_model));
}

const INHERITANCE_LABEL: Record<string, string> = {
  full: "AppAgent herda tudo do principal",
  provider: "AppAgent herda o provider; modelo próprio",
  model: "AppAgent com provider próprio; modelo herdado do principal",
  none: "AppAgent com provider e modelo próprios",
};

interface ListingSlot {
  state: CatalogState;
  data: ModelListingDto | null;
  identity: string;
}

const IDLE_SLOT: ListingSlot = { state: "idle", data: null, identity: "" };

export function BrainPanel({ sendCommand, session, onConfigured }: BrainPanelProps): React.ReactElement {
  const [brain, setBrain] = useState<BrainGetDto | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [statusMessage, setStatusMessage] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [appProvider, setAppProvider] = useState("");
  const [appModel, setAppModel] = useState("");
  const [remember, setRemember] = useState(false);
  const [mainSlot, setMainSlot] = useState<ListingSlot>(IDLE_SLOT);
  const [appSlot, setAppSlot] = useState<ListingSlot>(IDLE_SLOT);
  const [verifyResult, setVerifyResult] = useState<BrainVerifyDto | null>(null);
  const [probeResult, setProbeResult] = useState<BrainProbeDto | null>(null);
  const [busy, setBusy] = useState(false);

  const applyState = useCallback((state: BrainStateDto) => {
    setProvider(state.provider);
    setModel(state.model);
    setBaseUrl(state.base_url);
    setAppProvider(state.app_provider);
    setAppModel(state.app_model);
  }, []);

  /**
   * O commit de `brain.set` pode terminar depois da resposta do comando; o backend publica o
   * estado final em `brain.updated`. O evento é a fonte da verdade do "aplicado".
   */
  useEffect(() => {
    const api = getBridgeApi();
    if (!api) return;
    const unsubscribe = api.onEvent((event) => {
      if (event.type !== "brain.updated") return;
      const published = event.payload as unknown as BrainStateDto;
      applyState(published);
      setBrain(prev => (prev ? { ...prev, current: published } : prev));
      setBusy(false);
      setApiKey(""); // aplicar terminou (evento): a chave rascunho sai do estado do renderer.
      if (published.ok === false) {
        const suffix = published.error?.stage ? ` (${published.error.stage})` : "";
        setStatusMessage("");
        setActionError(`${published.error?.message ?? "Não foi possível aplicar o cérebro."}${suffix}`);
      } else if (published.persisted === false) {
        setActionError(null);
        setStatusMessage(`Cérebro aplicado nesta sessão, mas a seleção não foi gravada${published.error?.message ? `: ${published.error.message}` : "."}`);
      } else {
        setActionError(null);
        setStatusMessage("Cérebro aplicado.");
      }
    });
    return () => unsubscribe();
  }, [applyState, session]);

  useEffect(() => {
    let alive = true;
    setLoadError(null);
    sendCommand<BrainGetDto>("brain.get", {})
      .then(result => { if (alive) { setBrain(result); applyState(result.current); } })
      .catch(err => { if (alive) setLoadError(describeError(err)); });
    return () => { alive = false; };
  }, [sendCommand, session, applyState]);

  const sessionReady = Boolean(session);
  const providers = brain?.providers ?? [];
  const currentProvider = useMemo(() => providers.find(item => item.id === provider) ?? null, [providers, provider]);
  const appProviderInfo = useMemo(() => providers.find(item => item.id === appProvider) ?? null, [providers, appProvider]);

  const groups = useMemo(() => {
    const cloud = providers.filter(p => p.location !== "local_endpoint" && p.kind === "api");
    const local = providers.filter(p => p.location === "local_endpoint");
    const cli = providers.filter(p => p.kind === "cli");
    const other = providers.filter(p => !cloud.includes(p) && !local.includes(p) && !cli.includes(p));
    return { cloud, local, cli, other };
  }, [providers]);

  /**
   * Identidade do CATÁLOGO: provider-alvo + role + endpoint + credencial (impressão local, nunca
   * o texto). NÃO inclui o modelo — escolher uma opção do catálogo não pode invalidar o próprio
   * catálogo. A comparação de respostas obsoletas é feita contra este ref, atualizado a cada
   * render, porque depois do `await` o closure da requisição veria estado velho.
   */
  const catalogIdentityOf = useCallback((role: ListingRole): string => JSON.stringify(
    role === "app"
      ? ["app", appProvider, baseUrl, appProvider === provider ? keyFingerprint(apiKey) : ""]
      : ["main", provider, baseUrl, keyFingerprint(apiKey)]
  ), [apiKey, appProvider, baseUrl, provider]);
  const catalogIdentRef = useRef({ main: catalogIdentityOf("main"), app: catalogIdentityOf("app") });
  catalogIdentRef.current = { main: catalogIdentityOf("main"), app: catalogIdentityOf("app") };

  /**
   * Identidade da SELEÇÃO para vereditos de verify/probe: inclui o modelo escolhido. A impressão
   * da chave entra na comparação mas nunca é renderizada nem logada.
   */
  const selectionKey = useMemo(() => JSON.stringify({
    provider, model, base_url: baseUrl, app_provider: appProvider, app_model: appModel, key: keyFingerprint(apiKey),
  }), [apiKey, appModel, appProvider, baseUrl, model, provider]);
  const selectionKeyRef = useRef(selectionKey);
  useEffect(() => { selectionKeyRef.current = selectionKey; }, [selectionKey]);
  const listSeqRef = useRef({ main: 0, app: 0 });

  const selectionPayload = useCallback((): Record<string, unknown> => {
    const payload: Record<string, unknown> = { provider, model };
    if (apiKey.trim()) payload.api_key = apiKey.trim();
    // `base_url` vai SEMPRE, inclusive vazio: o handler Python distingue chave ausente de string
    // vazia; omitir faria "apagar o endpoint" parecer aplicado sem alterar nada.
    payload.base_url = baseUrl.trim();
    payload.app_provider = appProvider.trim();
    payload.app_model = appProvider.trim() ? appModel : "";
    return payload;
  }, [apiKey, appModel, appProvider, baseUrl, model, provider]);

  const guard = useCallback((candidate: string): boolean => {
    if (!sessionReady) {
      setActionError("Sessão indisponível: reinicie o backend antes de alterar o Brain.");
      return false;
    }
    if (!candidate.trim()) {
      setActionError("Informe um provider antes de continuar.");
      return false;
    }
    setActionError(null);
    return true;
  }, [sessionReady]);

  const loadModels = useCallback(async (role: ListingRole, force: boolean) => {
    const target = role === "app" ? appProvider : provider;
    if (!guard(target)) return;
    const identity = catalogIdentRef.current[role];
    const sequence = ++listSeqRef.current[role];
    const slot = role === "app" ? setAppSlot : setMainSlot;
    slot({ state: "loading", data: null, identity });
    setStatusMessage("Consultando catálogo…");
    const payload: Record<string, unknown> = {
      provider: target,
      role,
      force,
      base_url: baseUrl.trim(),
      // rascunho da seleção viaja junto: descobrir catálogo não exige modelo escolhido.
      model: role === "app" ? appModel : model,
    };
    // A chave rascunho só acompanha a consulta do MESMO alvo. Para o AppAgent com provider
    // diferente, o backend usa a fonte canônica daquela credencial; enviar a chave principal
    // seria enganoso (e seria ignorado).
    const draftKey = apiKey.trim();
    const keyAppliesToTarget = role === "main" || appProvider === provider;
    if (draftKey && keyAppliesToTarget) payload.api_key = draftKey;
    try {
      const result = await sendCommand<ModelListingDto>("brain.list", payload);
      if (catalogIdentRef.current[role] !== identity || listSeqRef.current[role] !== sequence) {
        setStatusMessage("A seleção mudou durante a consulta; o catálogo foi descartado.");
        return;
      }
      slot({ state: result.ok ? (result.models.length ? "ready" : "empty") : "error", data: result, identity });
      setStatusMessage(result.ok ? "" : result.detail || result.status);
    } catch (err) {
      if (catalogIdentRef.current[role] !== identity || listSeqRef.current[role] !== sequence) return;
      slot({ state: "error", data: null, identity });
      setStatusMessage("");
      setActionError(describeError(err));
    }
  }, [apiKey, appModel, appProvider, baseUrl, guard, model, provider, sendCommand]);

  const verify = useCallback(async () => {
    if (!guard(provider)) return;
    const requestedKey = selectionKeyRef.current;
    setBusy(true);
    setStatusMessage("Verificando conexão…");
    try {
      const result = await sendCommand<BrainVerifyDto>("brain.verify", selectionPayload());
      if (selectionKeyRef.current !== requestedKey) {
        setVerifyResult(null);
        setStatusMessage("A seleção mudou durante a verificação; o resultado foi descartado.");
        return;
      }
      setVerifyResult(result);
      setStatusMessage(result.ok ? "Conexão verificada." : result.detail || result.status);
    } catch (err) {
      if (selectionKeyRef.current !== requestedKey) {
        setStatusMessage("A seleção mudou durante a verificação; o erro foi descartado.");
        return;
      }
      setVerifyResult(null);
      setStatusMessage("");
      setActionError(describeError(err));
    } finally {
      setBusy(false);
      // A chave FICA em RAM entre listar/verificar/aplicar; sai só em aplicar/descartar/trocar/desmontar.
    }
  }, [guard, provider, selectionPayload, sendCommand]);

  const probe = useCallback(async () => {
    if (!guard(provider)) return;
    const requestedKey = selectionKeyRef.current;
    setBusy(true);
    setStatusMessage("Testando o modelo (chamada real de inferência)…");
    try {
      const result = await sendCommand<BrainProbeDto>("brain.probe", { ...selectionPayload(), consent: true });
      if (selectionKeyRef.current !== requestedKey) {
        setProbeResult(null);
        setStatusMessage("A seleção mudou durante o teste; o resultado foi descartado.");
        return;
      }
      setProbeResult(result);
      setStatusMessage(result.summary || "O modelo não respondeu.");
    } catch (err) {
      if (selectionKeyRef.current !== requestedKey) {
        setStatusMessage("A seleção mudou durante o teste; o erro foi descartado.");
        return;
      }
      setProbeResult(null);
      setStatusMessage("");
      setActionError(describeError(err));
    } finally {
      setBusy(false);
    }
  }, [guard, provider, selectionPayload, sendCommand]);

  const applySelection = useCallback(async () => {
    if (!guard(provider)) return;
    setBusy(true);
    setStatusMessage("Aplicando cérebro…");
    try {
      const result = await sendCommand<BrainStateDto>("brain.set", { ...selectionPayload(), remember_credentials: remember });
      applyState(result);
      setBrain(prev => (prev ? { ...prev, current: result } : prev));
      if (result.ok === false) {
        setStatusMessage("");
        setActionError(result.error?.message ?? "Não foi possível aplicar o cérebro.");
      } else if (result.persisted === false) {
        setStatusMessage(`Cérebro aplicado nesta sessão, mas a seleção não foi gravada${result.error?.message ? `: ${result.error.message}` : "."}`);
      } else {
        setStatusMessage("Cérebro aplicado.");
      }
      onConfigured?.();
    } catch (err) {
      // `reconfiguring` não é falha: o commit continua e o estado final sai em `brain.updated`.
      if (toBridgeError(err).code === "reconfiguring") {
        setActionError(null);
        setStatusMessage("A troca continua em andamento; o estado final sai no evento brain.updated.");
      } else {
        setStatusMessage("");
        setActionError(describeError(err));
      }
    } finally {
      setApiKey(""); // fronteira de limpeza: aplicar (concluído ou falhado) descarta o rascunho.
      setBusy(false);
    }
  }, [applyState, guard, onConfigured, provider, remember, selectionPayload, sendCommand]);

  const changeProvider = (next: string) => {
    setProvider(next);
    setModel("");
    setMainSlot(IDLE_SLOT);
    setVerifyResult(null);
    setProbeResult(null);
    setApiKey(""); // troca explícita de provider: a chave do anterior sai do estado.
  };

  const mainModels = mainSlot.data && mainSlot.identity === catalogIdentRef.current.main ? mainSlot.data.models : [];
  const appModels = appSlot.data && appSlot.identity === catalogIdentRef.current.app ? appSlot.data.models : [];
  const mainListing = mainSlot.data && mainSlot.identity === catalogIdentRef.current.main ? mainSlot.data : null;
  const verifyForSelection = verifyResult && verifyResult.provider === provider;

  return (
    <section aria-label="Brain" data-testid="brain-panel" className="lunmi-brain-page">
      <header className="lunmi-brain-head">
        <div>
          <h2>Cérebro</h2>
          <p className="lunmi-brain-sub">
            Estado atual:{" "}
            <strong data-testid="current-brain">{brain ? `${brain.current.provider_label} · ${brain.current.model || "(padrão)"}` : "…"}</strong>
          </p>
          {brain?.current.effective ? (
            <p className="lunmi-brain-hint" data-testid="effective-brain">
              Aplicado efetivo — HostAgent: {brain.current.effective.host.provider} ·{" "}
              {brain.current.effective.host.model || "(padrão)"}; AppAgent:{" "}
              {brain.current.effective.app.provider} · {brain.current.effective.app.model || "(padrão)"}
              {" "}({INHERITANCE_LABEL[brain.current.effective.app_inheritance] ?? brain.current.effective.app_inheritance}).
            </p>
          ) : (
            <p className="lunmi-brain-hint" data-testid="effective-brain-none">
              Seleção efetiva Host/App: não reportada por esta versão do backend.
            </p>
          )}
        </div>
        <span className="lunmi-brain-seals" data-testid="brain-seals">
          <span data-testid="catalog-seal" data-on={mainListing ? "true" : "false"}>
            catálogo: {mainSlot.state === "loading" ? "consultando…" : mainListing ? `${mainListing.models.length} modelos${mainListing.cached ? " · cache" : ""}` : "não consultado"}
          </span>
          <span data-testid="verified-seal" data-on={verifyForSelection && verifyResult?.ok ? "true" : "false"}>
            verificado: {verifyForSelection && verifyResult ? (verifyResult.ok ? "sim" : "não") : "—"}
          </span>
          <span data-testid="applied-seal" data-on={brain ? "true" : "false"}>aplicado: {brain ? brain.current.provider_label : "…"}</span>
        </span>
      </header>

      {hasEnvOverride(brain?.current ?? null) && (
        <p role="status" data-testid="env-override-warning" className="lunmi-brain-warn">
          Uma variável de ambiente (LLM_PROVIDER/LLM_MODEL/LLM_APP_*) está definida e prevalece
          sobre a seleção salva.
        </p>
      )}
      {loadError && <p role="alert" data-testid="brain-load-error" className="lunmi-brain-error-text">{loadError}</p>}
      {(statusMessage || actionError) && (
        <p role={actionError ? "alert" : "status"} data-testid="brain-status"
          className={actionError ? "lunmi-brain-error-text" : "lunmi-brain-ok-text"}>
          {actionError ?? statusMessage}
        </p>
      )}

      <div className="lunmi-brain-card">
        <h3 className="lunmi-brain-card-title">Ligação principal</h3>

        <label htmlFor="brain-provider" className="lunmi-brain-label">Provedor</label>
        <select id="brain-provider" data-testid="provider-select" value={provider} disabled={!sessionReady}
          onChange={event => changeProvider(event.target.value)} className="lunmi-brain-control">
          {!provider && <option value="">selecione…</option>}
          {groups.cloud.length > 0 && (
            <optgroup label="API em nuvem">
              {groups.cloud.map(item => <option key={item.id} value={item.id}>{item.label}{item.requires_api_key ? " · chave" : ""}</option>)}
            </optgroup>
          )}
          {groups.local.length > 0 && (
            <optgroup label="Endpoint local">
              {groups.local.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
            </optgroup>
          )}
          {groups.cli.length > 0 && (
            <optgroup label="CLI">
              {groups.cli.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
            </optgroup>
          )}
          {groups.other.length > 0 && (
            <optgroup label="Outros">
              {groups.other.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
            </optgroup>
          )}
        </select>

        <label htmlFor="brain-api-key" className="lunmi-brain-label">
          Chave de API
        </label>
        <div className="lunmi-brain-key-row">
          <input id="brain-api-key" data-testid="api-key" type="password" autoComplete="off"
            placeholder={brain?.current.provider === provider && brain.current.has_api_key
              ? "Chave configurada — cola outra para substituir" : "Cola a chave de API"}
            value={apiKey} disabled={!sessionReady}
            onChange={event => setApiKey(event.target.value)} className="lunmi-brain-control" />
          <button type="button" data-testid="api-key-clear" className="lunmi-brain-quiet"
            disabled={!apiKey} onClick={() => setApiKey("")}>Limpar campo</button>
        </div>
        <p className="lunmi-brain-hint">
          Após aplicar, a chave deixa de ser mostrada. Podes guardá-la neste computador em Avançado.
        </p>

        {currentProvider?.location === "local_endpoint" && (
          <>
            <label htmlFor="brain-base-url" className="lunmi-brain-label">Endpoint local</label>
            <input id="brain-base-url" data-testid="base-url" value={baseUrl} disabled={!sessionReady}
              onChange={event => setBaseUrl(event.target.value)} className="lunmi-brain-control" />
          </>
        )}

        <ModelCatalogSelect
          label="Modelo"
          testId="model-select"
          value={model}
          models={mainModels}
          state={mainSlot.identity === catalogIdentRef.current.main ? mainSlot.state : "idle"}
          detail={mainSlot.data?.detail}
          cached={mainSlot.data?.cached}
          disabled={!sessionReady || !provider}
          allowDefault={currentProvider?.kind === "cli"}
          unverified={Boolean(model) && !mainListing}
          currentUnavailable={brain?.current.model && brain.current.provider === provider && brain.current.model !== model && !mainModels.some(m => m.id === brain.current.model) ? brain.current.model : undefined}
          onSelect={setModel}
          onRefresh={() => void loadModels("main", true)}
          onOpenEmpty={() => void loadModels("main", false)}
        />

        {mainListing && (
          <p data-testid="model-listing" className="lunmi-brain-muted">
            {mainListing.label} · {mainListing.status}{mainListing.cached ? " · catálogo do cache (não revalidado)" : ""}
            {mainListing.detail ? ` — ${mainListing.detail}` : ""}
            {mainListing.selection_problem ? <span className="lunmi-brain-warn"> · {mainListing.selection_problem}</span> : null}
          </p>
        )}

        <div className="lunmi-brain-actions">
          <button type="button" data-testid="list-models"
            disabled={!sessionReady || !provider || (mainSlot.state === "loading" && mainSlot.identity === catalogIdentRef.current.main)}
            onClick={() => void loadModels("main", false)} className="lunmi-brain-quiet">
            Listar modelos
          </button>
          <button type="button" data-testid="verify-button" disabled={!sessionReady || busy}
            onClick={() => void verify()} className="lunmi-brain-secondary">
            Verificar conexão
          </button>
          <button type="button" data-testid="apply-brain" disabled={!sessionReady || busy}
            onClick={() => void applySelection()} className="lunmi-brain-primary">
            Aplicar
          </button>
        </div>

        {verifyResult && verifyForSelection && (
          <p data-testid="verify-result" className={verifyResult.ok ? "lunmi-brain-ok-text" : "lunmi-brain-error-text"}>
            {verifyResult.ok ? "Verificado" : "Falha na verificação"} · {verifyResult.status}
            {verifyResult.detail ? ` — ${verifyResult.detail}` : ""}
          </p>
        )}
      </div>

      <details className="lunmi-brain-card lunmi-brain-advanced" data-testid="brain-advanced">
        <summary className="lunmi-brain-card-title">Avançado — AppAgent, credenciais e teste real</summary>

        <div data-testid="app-brain-card">
          <p className="lunmi-brain-hint">Deixe vazio para usar o mesmo cérebro principal.</p>
          <label htmlFor="app-brain-provider" className="lunmi-brain-label">Provider do AppAgent</label>
          <select id="app-brain-provider" data-testid="app-provider-select" value={appProvider} disabled={!sessionReady}
            onChange={event => { setAppProvider(event.target.value); setAppModel(""); setAppSlot(IDLE_SLOT); }}
            className="lunmi-brain-control">
            <option value="">(mesmo do principal)</option>
            {providers.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>

          <ModelCatalogSelect
            label="Modelo do AppAgent (do catálogo)"
            testId="app-model-select"
            value={appModel}
            models={appModels}
            state={appSlot.identity === catalogIdentRef.current.app ? appSlot.state : "idle"}
            detail={appSlot.data?.detail}
            cached={appSlot.data?.cached}
            disabled={!sessionReady || !appProvider}
            allowDefault={appProviderInfo?.kind === "cli"}
            unverified={Boolean(appModel) && !(appSlot.data && appSlot.identity === catalogIdentRef.current.app)}
            onSelect={setAppModel}
            onRefresh={() => void loadModels("app", true)}
            onOpenEmpty={() => void loadModels("app", false)}
          />
          {appProvider && appProvider !== provider ? (
            <p className="lunmi-brain-hint" data-testid="app-credential-note">
              AppAgent em provider diferente: a consulta e a aplicação usam a credencial canônica
              já guardada desse provider; a chave principal em memória não é enviada.
            </p>
          ) : null}

          <div className="lunmi-brain-actions">
            <button type="button" data-testid="list-app-models" disabled={!sessionReady || busy || !appProvider || (appSlot.state === "loading" && appSlot.identity === catalogIdentRef.current.app)}
              onClick={() => void loadModels("app", false)} className="lunmi-brain-secondary">
              Listar modelos do AppAgent
            </button>
          </div>
        </div>

        <label htmlFor="remember-credentials" className="lunmi-brain-check">
          <input id="remember-credentials" data-testid="remember-credentials" type="checkbox"
            checked={remember} disabled={!sessionReady}
            onChange={event => setRemember(event.target.checked)} />
          Lembrar credenciais de forma protegida (DPAPI) neste computador
        </label>

        <div className="lunmi-brain-actions">
          <button type="button" data-testid="probe-button" disabled={!sessionReady || busy}
            onClick={() => void probe()} className="lunmi-brain-warn-button">
            Testar modelo (gasta 1 chamada real)
          </button>
        </div>
        {probeResult && (
          <p data-testid="probe-result" className={probeResult.ok ? "lunmi-brain-ok-text" : "lunmi-brain-error-text"}>
            {probeResult.summary} · {Math.round(probeResult.latency_ms)} ms
            {probeResult.error_kind ? ` · ${probeResult.error_kind}` : ""}
          </p>
        )}
        {appProviderInfo?.supports_model_discovery === false ? (
          <p className="lunmi-brain-muted">Este provider não expõe catálogo; o AppAgent usará o padrão anunciado.</p>
        ) : null}
      </details>
    </section>
  );
}

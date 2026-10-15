/**
 * Painel do Brain (`desktop/src/features/brain/BrainPanel.tsx`, F2b).
 *
 * Reutiliza o registro de providers, a descoberta de modelos e o probe já existentes na ponte:
 * - `brain.get`    -> estado atual + catálogo de providers;
 * - `brain.list`   -> catálogo de modelos por provider, com `role: "main" | "app"` e `force`;
 * - `brain.verify` -> valida a seleção contra o provider (sem gastar inferência);
 * - `brain.probe`  -> **exige ação explícita** e envia `consent: true`; é uma chamada real;
 * - `brain.set`    -> aplica a seleção principal e a do AppAgent; `remember_credentials` opcional.
 *
 * A chave de API é somente de escrita: nunca vem do backend, nunca é renderizada de volta e é
 * limpa do estado local imediatamente depois de qualquer chamada que a use.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { TOKENS } from "../../shared/theme";
import { describeError, type SendCommandFn } from "../../shared/ipc/client";
import type {
  BrainGetDto,
  BrainProbeDto,
  BrainStateDto,
  BrainVerifyDto,
  ModelListingDto,
} from "../../shared/types";
import type { SessionReadyPayload } from "../../shared/protocol";

export interface BrainPanelProps {
  sendCommand: SendCommandFn;
  session: SessionReadyPayload | null;
  onConfigured?: () => void;
}

type ListingRole = "main" | "app";

function hasEnvOverride(state: BrainStateDto | null): boolean {
  if (!state) {
    return false;
  }
  const overrides = state.env_overrides;
  return Boolean(
    overrides &&
      (overrides.provider || overrides.model || overrides.app_provider || overrides.app_model)
  );
}

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
  const [listing, setListing] = useState<ModelListingDto | null>(null);
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

  useEffect(() => {
    let alive = true;
    setLoadError(null);
    sendCommand<BrainGetDto>("brain.get", {})
      .then((result) => {
        if (!alive) {
          return;
        }
        setBrain(result);
        applyState(result.current);
      })
      .catch((err) => {
        if (alive) {
          setLoadError(describeError(err));
        }
      });
    return () => {
      alive = false;
    };
  }, [sendCommand, session, applyState]);

  const sessionReady = Boolean(session);
  const providers = brain?.providers ?? [];
  const currentProvider = useMemo(
    () => providers.find((item) => item.id === provider) ?? null,
    [providers, provider]
  );

  const modelOptions = useMemo(() => {
    const ids = new Set<string>();
    if (model) {
      ids.add(model);
    }
    if (listing && listing.provider === provider) {
      for (const item of listing.models) {
        ids.add(item.id);
      }
    }
    return Array.from(ids);
  }, [listing, provider, model]);

  const guard = useCallback(
    (candidate: string): boolean => {
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
    },
    [sessionReady]
  );

  /**
   * Identidade da seleção atual. Muda quando provider/model/endpoint/AppAgent mudam, e é o que
   * decide se uma resposta de `verify`/`probe` ainda pertence à seleção que a pediu.
   */
  const selectionKey = useMemo(
    () =>
      JSON.stringify({
        provider,
        model,
        base_url: baseUrl,
        app_provider: appProvider,
        app_model: appModel,
      }),
    [provider, model, baseUrl, appProvider, appModel]
  );
  const selectionKeyRef = useRef(selectionKey);
  useEffect(() => {
    selectionKeyRef.current = selectionKey;
  }, [selectionKey]);

  const selectionPayload = useCallback((): Record<string, unknown> => {
    const payload: Record<string, unknown> = { provider, model };
    if (apiKey.trim()) {
      payload.api_key = apiKey.trim();
    }
    // `base_url` vai SEMPRE, inclusive vazio: o handler Python distingue chave ausente de string
    // vazia e conserva o valor antigo quando a chave falta — omitir aqui faria apagar o endpoint
    // na tela parecer aplicado sem alterar nada. Vazio inválido para o provider vira recusa
    // visível na validação do backend, não mudança silenciosa.
    payload.base_url = baseUrl.trim();
    payload.app_provider = appProvider.trim();
    payload.app_model = appProvider.trim() ? appModel : "";
    return payload;
  }, [provider, model, apiKey, baseUrl, appProvider, appModel]);

  const loadModels = useCallback(
    async (nextRole: ListingRole, force: boolean) => {
      if (!guard(provider)) {
        return;
      }
      setBusy(true);
      setStatusMessage("Consultando modelos…");
      try {
        const result = await sendCommand<ModelListingDto>("brain.list", {
          provider,
          role: nextRole,
          force,
        });
        setListing(result);
        setStatusMessage(result.ok ? `Modelos de ${result.label}` : result.detail || result.status);
      } catch (err) {
        setListing(null);
        setStatusMessage("");
        setActionError(describeError(err));
      } finally {
        setBusy(false);
      }
    },
    [guard, provider, sendCommand]
  );

  const verify = useCallback(async () => {
    if (!guard(provider)) {
      return;
    }
    const requestedKey = selectionKeyRef.current;
    setBusy(true);
    setStatusMessage("Verificando conexão…");
    try {
      const result = await sendCommand<BrainVerifyDto>("brain.verify", selectionPayload());
      if (selectionKeyRef.current !== requestedKey) {
        // A seleção mudou durante a operação: o veredito é do provider antigo e não pode
        // aparecer como se pertencesse ao novo.
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
      // A chave nunca permanece no estado do renderer depois de usada.
      setApiKey("");
      setBusy(false);
    }
  }, [guard, provider, selectionPayload, sendCommand]);

  const probe = useCallback(async () => {
    if (!guard(provider)) {
      return;
    }
    const requestedKey = selectionKeyRef.current;
    setBusy(true);
    setStatusMessage("Testando o modelo (chamada real de inferência)…");
    try {
      const result = await sendCommand<BrainProbeDto>("brain.probe", {
        ...selectionPayload(),
        consent: true,
      });
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
      setApiKey("");
      setBusy(false);
    }
  }, [guard, provider, selectionPayload, sendCommand]);

  const applySelection = useCallback(async () => {
    if (!guard(provider)) {
      return;
    }
    setBusy(true);
    setStatusMessage("Aplicando cérebro…");
    try {
      const result = await sendCommand<BrainStateDto>("brain.set", {
        ...selectionPayload(),
        remember_credentials: remember,
      });
      applyState(result);
      setBrain((prev) => (prev ? { ...prev, current: result } : prev));
      setStatusMessage("Cérebro aplicado.");
      onConfigured?.();
    } catch (err) {
      setStatusMessage("");
      setActionError(describeError(err));
    } finally {
      setApiKey("");
      setBusy(false);
    }
  }, [applyState, guard, provider, remember, selectionPayload, sendCommand, onConfigured]);

  return (
    <section
      aria-label="Brain"
      data-testid="brain-panel"
      style={{
        flex: "1 1 auto",
        overflowY: "auto",
        padding: "20px",
        display: "grid",
        gap: "16px",
        alignContent: "start",
      }}
    >
      <header>
        <h2 style={headingStyle}>Cérebro</h2>
        <p style={{ margin: 0, color: TOKENS.textMuted, fontSize: "13px" }}>
          Estado atual:{" "}
          <strong data-testid="current-brain" style={{ color: TOKENS.textPrimary }}>
            {brain ? `${brain.current.provider_label} · ${brain.current.model || "(padrão)"}` : "…"}
          </strong>
        </p>
        {hasEnvOverride(brain?.current ?? null) && (
          <p
            role="status"
            data-testid="env-override-warning"
            style={{
              margin: "8px 0 0",
              padding: "8px 10px",
              borderRadius: "8px",
              border: `1px solid ${TOKENS.warning}`,
              color: TOKENS.warning,
              fontSize: "12px",
            }}
          >
            Uma variável de ambiente (LLM_PROVIDER/LLM_MODEL/LLM_APP_*) está definida e prevalece
            sobre a seleção salva.
          </p>
        )}
        {loadError && (
          <p
            role="alert"
            data-testid="brain-load-error"
            style={{ margin: "8px 0 0", color: TOKENS.error, fontSize: "13px" }}
          >
            {loadError}
          </p>
        )}
        {(statusMessage || actionError) && (
          <p
            role={actionError ? "alert" : "status"}
            data-testid="brain-status"
            style={{
              margin: "8px 0 0",
              color: actionError ? TOKENS.error : TOKENS.success,
              fontSize: "13px",
            }}
          >
            {actionError ?? statusMessage}
          </p>
        )}
      </header>

      <div style={cardStyle}>
        <h3 style={subHeadingStyle}>Provider e modelo principal</h3>

        <label htmlFor="brain-provider" style={labelStyle}>
          Provider
        </label>
        <select
          id="brain-provider"
          data-testid="provider-select"
          value={provider}
          disabled={!sessionReady}
          onChange={(event) => {
            setProvider(event.target.value);
            setModel("");
            setListing(null);
            setVerifyResult(null);
            setProbeResult(null);
          }}
          style={controlStyle}
        >
          {!provider && <option value="">selecione…</option>}
          {providers.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>

        <label htmlFor="brain-model" style={labelStyle}>
          Modelo
        </label>
        <select
          id="brain-model"
          data-testid="model-select"
          value={model}
          disabled={!sessionReady}
          onChange={(event) => setModel(event.target.value)}
          style={controlStyle}
        >
          <option value="">(padrão do provider)</option>
          {modelOptions
            .filter((id) => id.length > 0)
            .map((id) => (
              <option key={id} value={id}>
                {id}
              </option>
            ))}
        </select>

        <label htmlFor="brain-api-key" style={labelStyle}>
          Chave de API (somente escrita; não é devolvida pelo backend)
        </label>
        <input
          id="brain-api-key"
          data-testid="api-key"
          type="password"
          autoComplete="off"
          value={apiKey}
          disabled={!sessionReady}
          onChange={(event) => setApiKey(event.target.value)}
          style={controlStyle}
        />

        {currentProvider?.location === "local_endpoint" && (
          <>
            <label htmlFor="brain-base-url" style={labelStyle}>
              Endpoint local
            </label>
            <input
              id="brain-base-url"
              data-testid="base-url"
              value={baseUrl}
              disabled={!sessionReady}
              onChange={(event) => setBaseUrl(event.target.value)}
              style={controlStyle}
            />
          </>
        )}

        <div style={rowStyle}>
          <button
            type="button"
            data-testid="list-models"
            disabled={!sessionReady || busy}
            onClick={() => void loadModels("main", false)}
            style={primaryButtonStyle}
          >
            Listar modelos
          </button>
          <button
            type="button"
            data-testid="refresh-models"
            disabled={!sessionReady || busy}
            onClick={() => void loadModels("main", true)}
            style={secondaryButtonStyle}
          >
            Atualizar
          </button>
          <button
            type="button"
            data-testid="verify-button"
            disabled={!sessionReady || busy}
            onClick={() => void verify()}
            style={secondaryButtonStyle}
          >
            Verificar conexão
          </button>
          <button
            type="button"
            data-testid="probe-button"
            disabled={!sessionReady || busy}
            onClick={() => void probe()}
            style={{ ...secondaryButtonStyle, borderColor: TOKENS.warning, color: TOKENS.warning }}
          >
            Testar modelo (gasta 1 chamada real)
          </button>
        </div>

        {listing && (
          <div data-testid="model-listing" style={{ marginTop: "14px", fontSize: "12px" }}>
            <p style={{ margin: 0, color: TOKENS.textSecondary }}>
              {listing.label} · {listing.status}
              {listing.cached ? " · cache" : ""}
            </p>
            {listing.detail && (
              <p style={{ margin: "4px 0 0", color: TOKENS.textMuted }}>{listing.detail}</p>
            )}
            {listing.selection_problem && (
              <p style={{ margin: "4px 0 0", color: TOKENS.warning }}>
                {listing.selection_problem}
              </p>
            )}
            <ul
              data-testid="model-list"
              style={{ margin: "8px 0 0", paddingLeft: "18px", color: TOKENS.textSecondary }}
            >
              {listing.models.map((item) => (
                <li key={item.id}>
                  {item.label || item.id}
                  {item.default ? " (padrão)" : ""}
                </li>
              ))}
            </ul>
          </div>
        )}

        {verifyResult && (
          <p
            data-testid="verify-result"
            style={{
              margin: "12px 0 0",
              fontSize: "12px",
              color: verifyResult.ok ? TOKENS.success : TOKENS.error,
            }}
          >
            {verifyResult.ok ? "Verificado" : "Falha na verificação"} · {verifyResult.status}
            {verifyResult.detail ? ` — ${verifyResult.detail}` : ""}
          </p>
        )}

        {probeResult && (
          <p
            data-testid="probe-result"
            style={{
              margin: "8px 0 0",
              fontSize: "12px",
              color: probeResult.ok ? TOKENS.success : TOKENS.error,
            }}
          >
            {probeResult.summary} · {Math.round(probeResult.latency_ms)} ms
            {probeResult.error_kind ? ` · ${probeResult.error_kind}` : ""}
          </p>
        )}
      </div>

      <div style={cardStyle} data-testid="app-brain-card">
        <h3 style={subHeadingStyle}>Cérebro do AppAgent</h3>
        <p style={{ margin: "0 0 8px", color: TOKENS.textMuted, fontSize: "12px" }}>
          Deixe vazio para usar o mesmo cérebro principal.
        </p>

        <label htmlFor="app-brain-provider" style={labelStyle}>
          Provider do AppAgent
        </label>
        <select
          id="app-brain-provider"
          data-testid="app-provider-select"
          value={appProvider}
          disabled={!sessionReady}
          onChange={(event) => {
            setAppProvider(event.target.value);
            setAppModel("");
          }}
          style={controlStyle}
        >
          <option value="">(mesmo do principal)</option>
          {providers.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>

        <label htmlFor="app-brain-model" style={labelStyle}>
          Modelo do AppAgent
        </label>
        <input
          id="app-brain-model"
          data-testid="app-model-input"
          value={appModel}
          disabled={!sessionReady || !appProvider}
          onChange={(event) => setAppModel(event.target.value)}
          style={controlStyle}
        />

        <div style={rowStyle}>
          <button
            type="button"
            data-testid="list-app-models"
            disabled={!sessionReady || busy || !appProvider}
            onClick={() => void loadModels("app", false)}
            style={secondaryButtonStyle}
          >
            Listar modelos do AppAgent
          </button>
        </div>
      </div>

      <div style={cardStyle}>
        <label
          htmlFor="remember-credentials"
          style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "13px" }}
        >
          <input
            id="remember-credentials"
            data-testid="remember-credentials"
            type="checkbox"
            checked={remember}
            disabled={!sessionReady}
            onChange={(event) => setRemember(event.target.checked)}
          />
          Lembrar credenciais de forma protegida (DPAPI) neste computador
        </label>

        <div style={{ marginTop: "14px" }}>
          <button
            type="button"
            data-testid="apply-brain"
            disabled={!sessionReady || busy}
            onClick={() => void applySelection()}
            style={primaryButtonStyle}
          >
            Aplicar
          </button>
        </div>
      </div>
    </section>
  );
}

const headingStyle: React.CSSProperties = {
  margin: "0 0 6px",
  fontSize: "15px",
  color: TOKENS.textPrimary,
};
const subHeadingStyle: React.CSSProperties = {
  margin: "0 0 10px",
  fontSize: "13px",
  color: TOKENS.textSecondary,
};
const labelStyle: React.CSSProperties = {
  display: "block",
  margin: "10px 0 4px",
  fontSize: "12px",
  color: TOKENS.textMuted,
};
const controlStyle: React.CSSProperties = {
  width: "100%",
  padding: "8px 10px",
  borderRadius: "8px",
  border: `1px solid ${TOKENS.borderSubtle}`,
  backgroundColor: TOKENS.bgInput,
  color: TOKENS.textPrimary,
  fontSize: "13px",
};
const cardStyle: React.CSSProperties = {
  padding: "16px",
  borderRadius: "12px",
  border: `1px solid ${TOKENS.borderSubtle}`,
  backgroundColor: TOKENS.bgCard,
  maxWidth: "660px",
};
const rowStyle: React.CSSProperties = {
  display: "flex",
  gap: "8px",
  flexWrap: "wrap",
  marginTop: "14px",
};
const primaryButtonStyle: React.CSSProperties = {
  padding: "8px 14px",
  borderRadius: "8px",
  border: "none",
  backgroundColor: TOKENS.accent,
  color: TOKENS.onAccent,
  fontSize: "13px",
  fontWeight: 600,
  cursor: "pointer",
};
const secondaryButtonStyle: React.CSSProperties = {
  padding: "8px 14px",
  borderRadius: "8px",
  border: `1px solid ${TOKENS.borderSubtle}`,
  backgroundColor: TOKENS.bgCardSubtle,
  color: TOKENS.textSecondary,
  fontSize: "13px",
  cursor: "pointer",
};

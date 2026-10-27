/**
 * Fontes de uma resposta web (`desktop/src/features/web/WebSources.tsx`, WEB-SEARCH-V1).
 *
 * Máx. 5 fontes por resposta (limite do backend), com a verdade do DTO: estado de leitura
 * (`read`), cortada (`truncated`), de cache (`cached`) e data real — `published_at` quando
 * existe; caso contrário a recolha `retrieved_at` ("acedido"). `date_source` é a ORIGEM da
 * marca, não uma data: nunca rotula publicação. O domínio é derivado do URL canónico
 * para leitura; o `title` mostra o URL público completo (escapado pelo React). Abrir é por
 * gesto e só envia `{session_id, task_id, source_id}` à API opcional `openWebSource`
 * (canal Electron fechado do outro Designer) — nunca um URL. Razões de recusa têm copy fixa
 * honesta; sem a API (preload antigo) o texto fica sem botão de abrir, nunca finge abertura.
 * Funciona em mensagens históricas da mesma sessão (as fontes vêm da projeção canónica).
 * WEB-SEARCH-ORIGIN-V1: linha de evidência tipada e discreta (origem efetiva, evidência e
 * estado do pedido do `chat.message`), que também aparece quando um turno `provider` não
 * publicou fontes — sem lista inventada, sem notas A–F, sem promessa de verdade factual.
 * WEB-SEARCH-AUTO-V1 §9: na recuperação `auto` provada (`fallback_triggered` + `provider_recovered`
 * no mesmo `chat.message`) a mesma linha acrescenta o motivo; sem o par, nada de afirmar
 * recuperação, e as fontes/lista continuam a ser só as da origem efetiva (nunca misturar citações).
 * WEB-SEARCH-REFINEMENT-20261026: cada fonte é um chip compacto com título + domínio; sem
 * excerto nem cartões. Os metadados honestos (lida/encontrada, data real, cortada, cache,
 * motores) vivem no tooltip e no texto acessível do chip; o URL público surge só no tooltip.
 * Abrir continua por gesto com `{session_id, task_id, source_id}` opacos à presença — nunca a
 * URL no IPC; recusas mantêm a copy fixa visível (`role="alert"`), sem API não há botão.
 * WEB-SEARCH-PRECISION-V1 (só copy, DTOs e layout intactos): a secção visível é apenas
 * «Fontes da pesquisa»; a explicação passa para o metadado acessível/tooltip de cada chip —
 * `read` diz «conteúdo da página obtido; não é prova de página inteira nem verificação pela
 * Lunmi» e `!read` diz «apenas excerto de pesquisa». O eixo da evidência fala desta consulta
 * («evidência suficiente/parcial nesta consulta»), nunca de verificação factual universal, e
 * mantém-se separado do eixo do pedido, que só `request_status` pode afirmar; `cache` avisa que
 * não garante atualização. `request_status` em `unknown` continua silencioso: sem novo campo,
 * sem nova alerta, sem alegar atendimento.
 */
import React, { useCallback, useRef, useState } from "react";
import { getPresenceApi } from "../../shared/ipc/presence";
import type { WebEvidenceStatus, WebFallbackReason, WebOrigin, WebRequestStatus, WebSourceDto, WebSourceOpenReason } from "../../shared/types";
import "./web.css";

const REASON_TEXT: Record<WebSourceOpenReason, string> = {
  "": "",
  invalid_payload: "O pedido de abertura foi recusado: fonte inválida.",
  not_found: "Esta fonte já não está disponível nesta sessão.",
  stale: "Esta fonte é de um turno antigo e já não pode ser aberta.",
  blocked_url: "O endereço foi bloqueado por segurança e não foi aberto.",
  unavailable: "A abertura de fontes não está disponível neste momento.",
  open_failed: "O Windows não conseguiu abrir o endereço.",
};

const domainOf = (url: string): string =>
  url.replace(/^https?:\/\//i, "").split("/")[0] || url;

const dateOf = (source: WebSourceDto): { label: string; when: string } => {
  // Só `published_at` é data de publicação. `date_source` diz onde a marca foi encontrada
  // (origem), não quando: sem `published_at` mostra-se sempre a recolha — nunca se inferem
  // publicação nem data da fonte.
  if (source.published_at) return { label: "publicado", when: source.published_at.slice(0, 10) };
  return { label: "acedido", when: source.retrieved_at.slice(0, 10) };
};

// Metadados honestos de uma fonte: vivem no tooltip e no texto acessível do chip, nunca em
// blocos visíveis. O estado de leitura diz o que aconteceu de facto (conteúdo obtido vs apenas
// excerto) sem afirmar página inteira nem verificação da Lunmi; cache avisa que não garante
// atualização. O URL público acompanha no tooltip; jamais entra num payload IPC.
const readLabelOf = (source: WebSourceDto): string =>
  source.read
    ? "conteúdo da página obtido; não é prova de página inteira nem verificação pela Lunmi"
    : "apenas excerto de pesquisa; conteúdo não obtido nem verificado pela Lunmi";

const metadataOf = (source: WebSourceDto): string => {
  const date = dateOf(source);
  return [
    readLabelOf(source),
    `${date.label} ${date.when}`,
    source.truncated ? "conteúdo cortado" : "",
    source.cached ? "de cache; cache não garante atualização" : "",
    source.engines.length > 0 ? `motores: ${source.engines.join(", ")}` : "",
  ].filter(Boolean).join(" · ");
};

const ORIGIN_TEXT: Record<"lunmi" | "provider", string> = {
  lunmi: "Free Search da Lunmi",
  provider: "pesquisa nativa do Brain",
};

const EVIDENCE_TEXT: Record<WebEvidenceStatus, string> = {
  // Eixo da evidência desta consulta — não é verificação factual universal.
  suff: "evidência suficiente nesta consulta",
  partial: "evidência parcial nesta consulta",
  insufficient: "fontes insuficientes para responder",
};

const REQUEST_TEXT: Record<"met" | "partial" | "unmet", string> = {
  met: "pedido atendido",
  partial: "pedido parcialmente atendido",
  unmet: "pedido não atendido",
};

export interface WebSourcesProps {
  sources: WebSourceDto[];
  sessionId: string | undefined;
  taskId: string | undefined;
  /** Variante compacta do mini chat. */
  compact?: boolean;
  /** WEB-SEARCH-ORIGIN-V1: campos do `chat.message`; ausentes fora de turno web. */
  searchSource?: WebOrigin;
  evidenceStatus?: WebEvidenceStatus | null;
  requestStatus?: WebRequestStatus | null;
  /** WEB-SEARCH-AUTO-V1 §9: recuperação `auto` começou de facto (backend publica o par). */
  fallbackTriggered?: boolean;
  webFallback?: WebFallbackReason;
}

export function WebSources({ sources, sessionId, taskId, compact = false, searchSource, evidenceStatus, requestStatus, fallbackTriggered, webFallback }: WebSourcesProps): React.ReactElement | null {
  const presence = getPresenceApi();
  const canOpen = typeof presence?.openWebSource === "function";
  const [reasons, setReasons] = useState<Record<string, WebSourceOpenReason>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const mountedRef = useRef(true);
  React.useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const open = useCallback(async (source: WebSourceDto) => {
    if (!canOpen || !sessionId || !taskId || pending[source.id]) return;
    setPending(prev => ({ ...prev, [source.id]: true }));
    try {
      // Apenas identificadores opacos; a URL canónica vive no backend/main e nunca neste payload.
      const result = await presence!.openWebSource!({ session_id: sessionId, task_id: taskId, source_id: source.id });
      if (mountedRef.current) {
        setReasons(prev => ({ ...prev, [source.id]: result.opened ? "" : (result.reason || "unavailable") }));
      }
    } catch {
      if (mountedRef.current) setReasons(prev => ({ ...prev, [source.id]: "unavailable" }));
    } finally {
      if (mountedRef.current) setPending(prev => ({ ...prev, [source.id]: false }));
    }
  }, [canOpen, pending, presence, sessionId, taskId]);

  const originLabel = searchSource === "lunmi" || searchSource === "provider" ? ORIGIN_TEXT[searchSource] : "";
  const requestLabel = requestStatus === "met" || requestStatus === "partial" || requestStatus === "unmet"
    ? REQUEST_TEXT[requestStatus] : "";
  const evidenceLabel = evidenceStatus && EVIDENCE_TEXT[evidenceStatus] ? EVIDENCE_TEXT[evidenceStatus] : "";
  const hasEvidence = Boolean(originLabel);
  // §9: `provider_recovered` implica `fallback_triggered=true`; sem o par provado no
  // `chat.message`, nada de afirmar recuperação — a linha mostra só a origem efetiva.
  const recovered = fallbackTriggered === true && webFallback === "provider_recovered";
  if (!sources.length && !hasEvidence) return null;

  return (
    <div className={`lunmi-web-sources${compact ? " lunmi-web-sources-compact" : ""}`} data-testid="web-sources" data-compact={compact ? "true" : "false"}>
      {hasEvidence && (
        <p className="lunmi-web-evidence" data-testid="web-evidence" data-search-source={searchSource} data-recovered={recovered ? "true" : "false"}>
          {`Origem da resposta: ${originLabel}` + (evidenceLabel ? ` · ${evidenceLabel}` : "")
            + (requestLabel ? ` · ${requestLabel}` : "")}
          {recovered && " — após falha da pesquisa nativa neste pedido"}
        </p>
      )}
      {sources.length > 0 && (
      <>
      <p className="lunmi-web-sources-title">Fontes da pesquisa</p>
      <ul>
        {sources.slice(0, 5).map(source => {
          const meta = metadataOf(source);
          const chip = (
            <>
              <span className="lunmi-web-source-title">{source.title}</span>
              <span className="lunmi-web-source-domain">{domainOf(source.url)}</span>
              <span className="lunmi-web-source-meta" data-testid={`web-source-read-${source.id}`} data-read={source.read ? "true" : "false"}>{meta}</span>
            </>
          );
          return (
            <li key={source.id} data-testid={`web-source-${source.id}`} className="lunmi-web-source" data-read={source.read ? "true" : "false"}>
              {canOpen ? (
                <button
                  type="button"
                  data-testid={`web-source-open-${source.id}`}
                  className="lunmi-web-source-open"
                  title={`${meta} · ${source.url}`}
                  disabled={!sessionId || !taskId || pending[source.id]}
                  onClick={() => void open(source)}
                >
                  {chip}
                </button>
              ) : (
                <span className="lunmi-web-source-static" title={`${meta} · ${source.url}`}>{chip}</span>
              )}
              {reasons[source.id] && (
                <p role="alert" className="lunmi-web-source-error" data-testid={`web-source-error-${source.id}`}>
                  {REASON_TEXT[reasons[source.id]]}
                </p>
              )}
            </li>
          );
        })}
      </ul>
      </>
      )}
    </div>
  );
}

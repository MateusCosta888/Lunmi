/**
 * Cartão «Pesquisa web» em Configurações (`desktop/src/features/web/WebSearchCard.tsx`).
 *
 * Só o estado canónico do backend comanda a UI: interruptor ON/OFF (`web.set {enabled}`),
 * modo auto/quick/deep (`web.set {mode}`), seletor de origem por Brain (`web.set {origin,
 * brain_id}`) e chip de estado com frases fixas —
 * `starting` "A preparar a pesquisa…", `searching` "A pesquisar na web…",
 * `reading` "A consultar fontes…", `error` com a `message` pública do backend. Runtime ausente
 * é dito com a mensagem do backend ("…runtime não instalado."), sem fallback nem optimismo.
 * WEB-SEARCH-ORIGIN-V1: a opção nativa só aparece com `native_web` AVAILABLE e os quatro
 * sinais verificados (supported, available, can_disable, state); sem prova, nada de entrada
 * "cinzenta com promessa". O seletor reflete a PREFERÊNCIA guardada (mesmo indisponível —
 * "preferência unavailable preservada"), não a origem efetiva. Desligar a web global bloqueia
 * ambas as origens (fieldset desativado). Sem provider label publicado: copy genérica
 * "pesquisa nativa do Brain"; nada de campos inventados, profundidade por provedor ou páginas.
 * WEB-SEARCH-AUTO-V1 §1/§2/§9: o seletor oferece a PREFERÊNCIA — «Auto (recomendado)», Lunmi
 * e nativa. `auto` e `provider` só com prova nativa (sem provedor, auto ≡ lunmi: opção sem
 * alternativa não se oferece); preferência `provider` guardada mantém o seletor para escapatória.
 * O aviso de fallback aparece apenas com `web_fallback` realmente publicado pelo backend:
 * `provider_recovered` diz a origem efetiva Lunmi desta resposta; `cleanup_failed` nunca afirma
 * recuperação; preferência explícita `provider` indisponível não oferece Free Search como
 * escolha — descreve quem está a responder. Aparece apenas com a capability real `web_search`
 * (handlers registados) — ponte antiga não mostra nada. Sem novos tokens de design; apenas os existentes.
 */
import React from "react";
import { useWebState } from "./useWebState";
import type { WebMode, WebPreference } from "../../shared/types";
import "./web.css";

const STATE_TEXT: Record<string, string> = {
  idle: "",
  starting: "A preparar a pesquisa…",
  searching: "A pesquisar na web…",
  reading: "A consultar fontes…",
  error: "A pesquisa falhou.",
};

const MODES: Array<{ value: WebMode; label: string; hint: string }> = [
  { value: "auto", label: "Auto", hint: "A Lunmi escolhe rápida ou profunda conforme o pedido." },
  { value: "quick", label: "Rápida", hint: "Poucas fontes, leitura curta." },
  { value: "deep", label: "Profunda", hint: "Mais fontes e leitura completa, demora mais." },
];

const PREFERENCES: Array<{ value: WebPreference; label: string; hint: string }> = [
  { value: "auto", label: "Auto (recomendado)", hint: "Tenta primeiro a pesquisa nativa do Brain e, só se ela falhar, recupera com a Free Search da Lunmi." },
  { value: "lunmi", label: "Free Search da Lunmi", hint: "A pesquisa integrada gerida pela Lunmi (pesquisa, leitura e fontes)." },
  { value: "provider", label: "Pesquisa nativa do Brain", hint: "Usa a pesquisa integrada do próprio cérebro selecionado, quando validada, sem pesquisa da Lunmi." },
];

// Disponível para o seletor apenas com TODOS os sinais canónicos (contrato §2): nunca
// se infere capacidade de tool calls genéricas nem se oferece estado SUPPORTED/UNKNOWN.
const providerSelectable = (web: { native_web?: { supported: boolean; available: boolean; can_disable: boolean; state: string } }): boolean =>
  web.native_web?.state === "AVAILABLE" && web.native_web.supported === true
  && web.native_web.available === true && web.native_web.can_disable === true;

const cardStyle: React.CSSProperties = {
  padding: "16px", borderRadius: "12px", border: "1px solid var(--lunmi-border-subtle)",
  backgroundColor: "var(--lunmi-bg-card)", maxWidth: "660px",
};

export interface WebSearchCardProps {
  sessionId: string | undefined;
  /** `session.ready.capabilities.web_search === true` — nunca otimista. */
  supportedBySession: boolean;
}

export function WebSearchCard({ sessionId, supportedBySession }: WebSearchCardProps): React.ReactElement | null {
  const { web, supported, busy, error, setWeb } = useWebState(sessionId, supportedBySession);
  if (!supportedBySession || !supported || !web) return null;
  const chip = web.state === "error" ? (web.message || STATE_TEXT.error) : STATE_TEXT[web.state];
  const selectable = providerSelectable(web);
  // WEB-SEARCH-AUTO-V1 §1/§2: `auto` só é escolha real quando há provedor — sem ele o plano
  // seria idêntico a `lunmi`, e uma opção sem alternativa não se oferece.
  const options = PREFERENCES.filter(option =>
    option.value === "lunmi"
    || ((option.value === "provider" || option.value === "auto") && selectable));
  // O seletor existe quando há mais do que uma preferência real, ou quando a preferência
  // guardada é "provider" mas deixou de ser utilizável — o utilizador precisa de poder voltar.
  const showOrigin = options.length > 1 || web.preference === "provider";
  return (
    <div style={cardStyle} data-testid="web-card">
      <h3 style={{ margin: "0 0 12px", fontSize: "15px", fontWeight: 600 }}>Pesquisa web</h3>
      <label style={{ display: "flex", alignItems: "flex-start", gap: "8px", fontSize: "13px" }}>
        <input
          type="checkbox"
          data-testid="web-enabled"
          checked={web.enabled}
          disabled={busy}
          onChange={() => void setWeb({ enabled: !web.enabled })}
        />
        <span>
          Permitir pesquisa na web
          <br />
          <span style={{ color: "var(--lunmi-text-muted)", fontSize: "12px" }}>
            {web.available
              ? "Só quando perguntares algo atual ou deres um endereço; as fontes aparecem com a resposta."
              : (web.message || "Pesquisa web indisponível: runtime não instalado.")}
          </span>
        </span>
      </label>
      {showOrigin && (
        <fieldset style={{ border: 0, margin: "12px 0 0", padding: 0 }} disabled={busy || !web.enabled} data-testid="web-origin-group">
          <legend style={{ fontSize: "12px", color: "var(--lunmi-text-secondary)", marginBottom: "6px" }}>Origem da pesquisa</legend>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "10px" }} role="radiogroup" aria-label="Origem da pesquisa">
            {options.map(option => (
              <label key={option.value} style={{ display: "inline-flex", alignItems: "center", gap: "6px", fontSize: "13px" }} title={option.hint}>
                <input
                  type="radio"
                  name="web-origin"
                  data-testid={`web-origin-${option.value}`}
                  value={option.value}
                  checked={web.preference === option.value}
                  disabled={busy || !web.enabled}
                  onChange={() => void setWeb({ origin: option.value })}
                />
                {option.label}
              </label>
            ))}
          </div>
          <p style={{ margin: "6px 0 0", fontSize: "12px", color: "var(--lunmi-text-muted)" }}>
            A escolha fica guardada por cérebro; desligar a web bloqueia todas as origens.
          </p>
        </fieldset>
      )}
      {/* WEB-SEARCH-AUTO-V1 §9: um aviso por mudança de plano REALMENTE publicada pelo backend. */}
      {web.web_fallback === "provider_unavailable" && (
        <p className="lunmi-web-note" role="status" data-testid="web-fallback-note">
          {!web.available
            ? "A pesquisa nativa não está disponível e a Free Search também está indisponível."
            : web.preference === "provider"
              ? "A pesquisa nativa não está disponível para este cérebro e a Free Search da Lunmi não é oferecida como escolha."
              : "A pesquisa nativa não está disponível para este cérebro; as respostas usam a Free Search da Lunmi."}
        </p>
      )}
      {web.web_fallback === "provider_recovered" && (
        <p className="lunmi-web-note" role="status" data-testid="web-fallback-note">
          A pesquisa nativa do Brain falhou neste pedido; a resposta vem da Free Search da Lunmi.
        </p>
      )}
      {web.web_fallback === "cleanup_failed" && (
        <p className="lunmi-web-note" role="status" data-testid="web-fallback-note">
          A pesquisa nativa não pôde ser finalizada em segurança; não houve nova tentativa e esta resposta não teve pesquisa web.
        </p>
      )}
      {web.web_fallback === "lunmi_unavailable" && (
        <p className="lunmi-web-note" role="status" data-testid="web-fallback-note">
          A Free Search não está disponível (runtime não instalado); a pesquisa web fica inativa até estar.
        </p>
      )}
      <fieldset style={{ border: 0, margin: "12px 0 0", padding: 0 }} disabled={busy || !web.enabled}>
        <legend style={{ fontSize: "12px", color: "var(--lunmi-text-secondary)", marginBottom: "6px" }}>Profundidade</legend>
        <div style={{ display: "flex", flexWrap: "wrap", gap: "10px" }} role="radiogroup" aria-label="Profundidade da pesquisa">
          {MODES.map(option => (
            <label key={option.value} style={{ display: "inline-flex", alignItems: "center", gap: "6px", fontSize: "13px" }} title={option.hint}>
              <input
                type="radio"
                name="web-mode"
                data-testid={`web-mode-${option.value}`}
                value={option.value}
                checked={web.mode === option.value}
                disabled={busy || !web.enabled}
                onChange={() => void setWeb({ mode: option.value })}
              />
              {option.label}
            </label>
          ))}
        </div>
        {web.origin === "provider" && (
          <p style={{ margin: "6px 0 0", fontSize: "12px", color: "var(--lunmi-text-muted)" }}>
            A profundidade aplica-se à Free Search da Lunmi; a pesquisa nativa do Brain não usa estes controlos.
          </p>
        )}
      </fieldset>
      <p className="lunmi-web-privacy" style={{ margin: "10px 0 0", fontSize: "12px", color: "var(--lunmi-text-muted)" }}>
        As perguntas de pesquisa vão para motores de busca públicos e não ficam na memória da Lunmi.
      </p>
      {chip && (
        <p className="lunmi-web-state" role="status" aria-live="polite" data-testid="web-state" data-state={web.state}>
          {chip}
        </p>
      )}
      {error && <p role="alert" className="lunmi-web-error" data-testid="web-error">{error}</p>}
    </div>
  );
}

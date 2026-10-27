/**
 * Estado de pesquisa web (`desktop/src/features/web/useWebState.ts`, WEB-SEARCH-V1).
 *
 * Contrato `.agents/WEB-SEARCH-V1-CONTRACT.md` §3 + `.agents/WEB-SEARCH-ORIGIN-V1-CONTRACT.md` §3.1
 * + `.agents/WEB-SEARCH-AUTO-V1-CONTRACT.md` §1:
 * `web.get {}` (não arranca runtime), `web.set {enabled?/mode?/brain_id?/origin?}`
 * (persistência atómica no backend; sem escrita otimista — o valor só muda no DTO canónico
 * devolvido ou no evento `web.changed`), evento com o mesmo `WebStateDto`. `origin` transporta
 * a **preferência** (`auto|lunmi|provider`; nunca `none`, que é o OFF do interruptor) e viaja
 * acompanhado do `brain_id` do DTO atual (o backend responde `stale_brain` se o Brain mudou).
 * Guardas: `session_id` da sessão atual; `revision` monotónica na sessão (um
 * evento ou GET mais velho é ignorado); epoch de pedido (um evento chegado depois do `get`
 * partir invalida a resposta tardia do `get`); epoch de sessão repõe a contagem. Sem polling,
 * sem novos comandos: apenas os dois do contrato.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { getBridgeApi, toBridgeError } from "../../shared/ipc/client";
import type { WebMode, WebPreference, WebSetPayload, WebStateDto } from "../../shared/types";

const ORIGIN_VALUES: string[] = ["auto", "lunmi", "provider"];

const ERROR_TEXT: Record<string, string> = {
  stale_brain: "O cérebro mudou entretanto; a preferência antiga não foi aplicada.",
  unavailable: "A pesquisa nativa não está disponível para este cérebro.",
  invalid_payload: "O pedido de preferência foi recusado. O valor anterior mantém-se.",
};

export interface UseWebStateApi {
  web: WebStateDto | null;
  supported: boolean;
  busy: boolean;
  error: string | null;
  setWeb: (patch: { enabled?: boolean; mode?: WebMode; origin?: WebPreference }) => Promise<void>;
}

export function useWebState(sessionId: string | undefined, capability = true): UseWebStateApi {
  const api = getBridgeApi();
  const supported = capability && typeof api?.sendCommand === "function" && typeof api?.onEvent === "function";
  const [web, setWeb_] = useState<WebStateDto | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const revisionRef = useRef(-1);
  const requestEpochRef = useRef(0);
  const sessionEpochRef = useRef(0);
  const brainIdRef = useRef("");
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  useEffect(() => {
    revisionRef.current = -1;
    requestEpochRef.current += 1;
    sessionEpochRef.current += 1;
    brainIdRef.current = "";
    setWeb_(null);
    setError(null);
  }, [sessionId]);

  const apply = useCallback((next: WebStateDto | null | undefined) => {
    if (!next || typeof next.revision !== "number") return;
    if (sessionId && next.session_id && next.session_id !== sessionId) return;
    if (next.revision < revisionRef.current) return; // evento/GET velho: ignorado
    revisionRef.current = next.revision;
    requestEpochRef.current += 1;
    // O brain_id usado em `web.set {origin}` é sempre o do último DTO canónico.
    brainIdRef.current = typeof next.brain_id === "string" ? next.brain_id : "";
    setWeb_(next);
    if (next.state !== "error") setError(null);
  }, [sessionId]);

  useEffect(() => {
    if (!supported || !api) return;
    const unsubscribe = api.onEvent((event) => {
      if (event.type !== "web.changed") return;
      apply(event.payload as unknown as WebStateDto);
    });
    let alive = true;
    const epoch = requestEpochRef.current;
    api.sendCommand<WebStateDto>("web.get", {})
      .then(result => {
        if (!alive || !mountedRef.current) return;
        if (requestEpochRef.current !== epoch) return; // já veio um evento: o evento manda
        apply(result);
      })
      .catch(() => { /* ponte antiga/sem handler: cartão fica escondido pelo estado nulo */ });
    return () => { alive = false; unsubscribe(); };
  }, [api, apply, sessionId, supported]);

  const setWeb = useCallback(async (patch: { enabled?: boolean; mode?: WebMode; origin?: WebPreference }) => {
    if (!supported || !api || busy) return;
    const keys = Object.keys(patch);
    if (keys.length === 0) return;
    setBusy(true);
    const sessionEpoch = sessionEpochRef.current;
    try {
      const clean: WebSetPayload = {};
      if (typeof patch.enabled === "boolean") clean.enabled = patch.enabled;
      if (patch.mode === "auto" || patch.mode === "quick" || patch.mode === "deep") clean.mode = patch.mode;
      if (typeof patch.origin === "string" && ORIGIN_VALUES.includes(patch.origin)) {
        // `origin` (preferência WEB-SEARCH-AUTO-V1) exige o brain_id do último DTO canónico;
        // sem estado, nunca se envia preferência. `none` não é offered na UI: OFF é o `enabled`.
        if (!brainIdRef.current) throw toBridgeError("no_canonical_state: sem estado web canónico");
        clean.origin = patch.origin as WebPreference;
        clean.brain_id = brainIdRef.current;
      }
      const result = await api.sendCommand<WebStateDto>("web.set", clean as Record<string, unknown>);
      if (mountedRef.current && sessionEpochRef.current === sessionEpoch) apply(result);
      setError(null);
    } catch (err) {
      if (mountedRef.current && sessionEpochRef.current === sessionEpoch) {
        const code = toBridgeError(err).code;
        setError(ERROR_TEXT[code]
          ?? "Não foi possível guardar a preferência de pesquisa web. O valor anterior mantém-se.");
      }
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  }, [api, apply, busy, supported]);

  return { web, supported, busy, error, setWeb };
}

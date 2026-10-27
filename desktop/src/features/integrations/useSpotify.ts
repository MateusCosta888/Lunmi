/**
 * Estado do cartão Spotify (`desktop/src/features/integrations/useSpotify.ts`).
 *
 * Consome só o contrato congelado em `desktop/src/shared/spotify.ts`: sete comandos e o
 * evento `spotify.changed`. Regras que este hook garante:
 * - `spotify.get`/`plan` não são repetidos por render nem por intervalo; só na montagem,
 *   na mudança de sessão e em cliques explícitos do utilizador.
 * - Evento com `session_id` de outra sessão é descartado; dentro da sessão, `revision` menor
 *   ou igual à última aplicada também (o `epoch` nunca recua).
 * - GET/ACK capturados numa sessão antiga (ou substituídos por um push mais novo) não voltam
 *   a pintar estado antigo.
 * - Nenhum segredo, path ou texto do backend é copiado para a UI: erros usam só o `code`.
 * - O consentimento do plano é de uso único e só o `apply` o recebe; expira sozinho.
 */
import { useEffect, useRef, useState } from "react";
import { getBridgeApi, toBridgeError, type SendCommandFn } from "../../shared/ipc/client";
import type { SessionReadyPayload } from "../../shared/protocol";
import {
  JOB_ID_PATTERN,
  SPOTIFY_ACTIONS,
  SPOTIFY_CONNECTION_STATES,
  SPOTIFY_DEVICES,
  SPOTIFY_EVENT,
  SPOTIFY_SETUP_STATES,
  type SpotifyAction,
  type SpotifyApplyResultDto,
  type SpotifyCancelResultDto,
  type SpotifyChangedDto,
  type SpotifyCommandType,
  type SpotifyConsentDto,
  type SpotifyGetResultDto,
  type SpotifyOpenResultDto,
  type SpotifyOpenTarget,
  type SpotifyPlanDto,
  type SpotifyPlanResultDto,
  type SpotifyStateDto,
} from "../../shared/spotify";

export interface PlanOffer {
  plan: SpotifyPlanDto;
  consent: SpotifyConsentDto;
  action: SpotifyAction;
}

export interface SpotifyController {
  state: SpotifyStateDto | null;
  loading: boolean;
  busy: SpotifyCommandType | "";
  fault: string;
  notice: string;
  offer: PlanOffer | null;
  /** Job aceite por `apply` e ainda não confirmado por evento: permite cancelar já. */
  jobId: string;
  jobAction: SpotifyAction | "";
  prepare(action: SpotifyAction): void;
  apply(): void;
  discard(): void;
  refresh(): void;
  cancelJob(): void;
  connect(): void;
  disconnect(): void;
  open(target: SpotifyOpenTarget): void;
  dismiss(): void;
}

/** Texto da UI a partir do código fechado; nunca ecoa `message`/`details` do backend. */
export function spotifyErrorCopy(err: unknown): string {
  return spotifyCodeCopy(toBridgeError(err).code);
}

export function spotifyCodeCopy(code: string): string {
  switch (code) {
    case "not_implemented":
      return "Esta versão do backend ainda não suporta o Spotify.";
    case "unavailable":
      return "O serviço do Spotify não está disponível nesta ligação. Tenta novamente.";
    case "not_configured":
      return "A ligação ainda não está configurada neste PC.";
    case "spotify_missing":
      return "Não encontrámos o Spotify neste PC. Instala o Spotify oficial.";
    case "store_unsupported":
      return "A versão da Microsoft Store não é suportada. Usa o Spotify oficial para computador.";
    case "arm_unsupported":
      return "Este PC não é compatível com esta versão da ligação.";
    case "policy_blocked":
      return "Uma política deste PC bloqueou a operação.";
    case "write_denied":
      return "O Windows não permitiu escrever os ficheiros necessários.";
    case "disk_space":
      return "Não há espaço suficiente para continuar.";
    case "offline":
      return "Sem ligação à internet.";
    case "cli_unavailable":
      return "Faltam ficheiros da ligação. Tenta reparar a ligação.";
    case "signature_invalid":
    case "hash_mismatch":
      return "A verificação dos ficheiros falhou.";
    case "version_unsupported":
      return "Esta versão do Spotify não é suportada.";
    case "close_refused":
      return "O Spotify não fechou. Fecha-o e tenta novamente.";
    case "not_ready":
      return "A ligação ainda não ficou confirmada. Nada foi dado como pronto.";
    case "port_in_use":
      return "Outra aplicação está a usar a porta da ligação. Fecha-a e tenta novamente.";
    case "profile_unsafe":
      return "Não conseguimos garantir que as tuas definições ficam iguais.";
    case "consent_invalid":
      return "O pedido já não corresponde ao estado atual. Prepara outro plano.";
    case "consent_expired":
      return "O pedido expirou. Prepara outro plano.";
    case "consent_replayed":
      return "Este pedido já foi usado. Prepara outro plano.";
    case "job_busy":
      return "Já há uma configuração em curso.";
    case "interrupted":
      return "A configuração ficou interrompida. Confirma o estado antes de repetir.";
    case "job_cancelled":
      return "A configuração foi cancelada.";
    case "rollback_failed":
      return "A recuperação não ficou concluída. Vê o estado atual antes de repetir.";
    case "invalid_payload":
      return "O pedido não foi aceite.";
    default:
      return "Não foi possível concluir esta ação. Tenta novamente.";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const SETUP_STATES = new Set<string>(SPOTIFY_SETUP_STATES);
const CONNECTION_STATES = new Set<string>(SPOTIFY_CONNECTION_STATES);
const DEVICES = new Set<string>(SPOTIFY_DEVICES);
const ACTIONS = new Set<string>(SPOTIFY_ACTIONS);

/** Valida só o envelope público necessário para pintar; ignora eventos malformados. */
export function isSpotifyState(value: unknown): value is SpotifyStateDto {
  if (!isRecord(value)) return false;
  if (typeof value.setup !== "string" || !SETUP_STATES.has(value.setup)) return false;
  if (typeof value.connection !== "string" || !CONNECTION_STATES.has(value.connection)) return false;
  if (typeof value.device !== "string" || !DEVICES.has(value.device)) return false;
  if (typeof value.revision !== "number" || !Number.isFinite(value.revision)) return false;
  if (typeof value.epoch !== "number" || !Number.isFinite(value.epoch)) return false;
  if (!isRecord(value.detection)) return false;
  if (value.job !== null) {
    if (!isRecord(value.job) || typeof value.job.id !== "string") return false;
    if (typeof value.job.stage !== "string" || typeof value.job.status !== "string") return false;
  }
  if (value.error !== null) {
    if (!isRecord(value.error) || typeof value.error.code !== "string") return false;
  }
  return true;
}

function isPlanOffer(value: unknown): value is { plan: SpotifyPlanDto; consent: SpotifyConsentDto } {
  if (!isRecord(value)) return false;
  const plan = value.plan;
  const consent = value.consent;
  if (!isRecord(plan) || !isRecord(consent)) return false;
  if (typeof plan.action !== "string" || !ACTIONS.has(plan.action)) return false;
  if (typeof plan.plan_hash !== "string") return false;
  if (!Array.isArray(plan.steps) || !plan.steps.every(step => typeof step === "string")) return false;
  if (typeof plan.expires_in !== "number" || !Number.isFinite(plan.expires_in)) return false;
  if (!isRecord(plan.changes) || !isRecord(plan.requires)) return false;
  if (typeof consent.consent_id !== "string" || !consent.consent_id) return false;
  return true;
}

export function useSpotify({
  sendCommand,
  session,
  automaticSetupAvailable = false,
}: {
  sendCommand: SendCommandFn;
  session: SessionReadyPayload | null;
  automaticSetupAvailable?: boolean;
}): SpotifyController {
  const supported = session?.capabilities.spotify === true;
  const sessionId = session?.session_id ?? "";
  const identity = useRef(sessionId);
  identity.current = sessionId;

  const [state, setState] = useState<SpotifyStateDto | null>(null);
  const [loading, setLoading] = useState(supported);
  const [busy, setBusy] = useState<SpotifyCommandType | "">("");
  const [fault, setFault] = useState("");
  const [notice, setNotice] = useState("");
  const [offer, setOffer] = useState<PlanOffer | null>(null);
  const [jobId, setJobId] = useState("");
  const [jobAction, setJobAction] = useState<SpotifyAction | "">("");

  const latest = useRef<SpotifyStateDto | null>(null);
  const alive = useRef(false);
  const operation = useRef(0);
  const busyOwner = useRef(0);
  const busyRef = useRef(false);
  const timer = useRef<number | undefined>(undefined);

  const clearTimer = () => {
    if (timer.current !== undefined) {
      window.clearTimeout(timer.current);
      timer.current = undefined;
    }
  };

  /**
   * Aceita estado público mais novo. `revision` só ordena dentro da mesma sessão; um `epoch`
   * mais antigo nunca substitui o atual (resultado de trabalho assíncrono atrasado).
   * Validade e ordenação são coisas diferentes: um DTO válido mas igual/antigo é ignorado em
   * silêncio, e só um payload realmente malformado vira erro de formato.
   */
  const accept = (next: unknown): "applied" | "ignored" | "invalid" => {
    if (!isSpotifyState(next)) return "invalid";
    const last = latest.current;
    if (last && next.epoch < last.epoch) return "ignored";
    if (last && next.epoch === last.epoch && next.revision <= last.revision) return "ignored";
    latest.current = next;
    setState(next);
    setLoading(false);
    return "applied";
  };

  useEffect(() => {
    alive.current = true;
    latest.current = null;
    operation.current += 1;
    busyRef.current = false;
    busyOwner.current = 0;
    setState(null);
    setOffer(null);
    setJobId("");
    setJobAction("");
    setFault("");
    setNotice("");
    setBusy("");
    setLoading(supported);
    clearTimer();

    if (!supported) {
      return () => {
        alive.current = false;
      };
    }

    let current = true;
    const unsubscribe = getBridgeApi()?.onEvent(event => {
      if (!current || event.type !== SPOTIFY_EVENT) return;
      const payload = event.payload as Partial<SpotifyChangedDto> | undefined;
      if (!payload || payload.session_id !== sessionId) return;
      if (accept(payload.spotify) !== "applied") return;
      setJobId("");
    });
    void sendCommand<SpotifyGetResultDto>("spotify.get", {})
      .then(result => {
        if (!current) return;
        if (accept(result?.spotify) === "invalid") {
          setLoading(false);
          setFault("O backend respondeu com um estado que não reconhecemos. Tenta novamente.");
        }
      })
      .catch(err => {
        if (!current) return;
        setLoading(false);
        setFault(spotifyErrorCopy(err));
      });

    return () => {
      current = false;
      alive.current = false;
      operation.current += 1;
      clearTimer();
      unsubscribe?.();
    };
    // `sendCommand` e a sessão são as entradas reais; nada aqui reage a cada render.
  }, [sendCommand, sessionId, supported]);

  const run = async <TResult,>(
    command: SpotifyCommandType,
    payload: Record<string, unknown>,
    outcome: (result: TResult) => void
  ) => {
    if (!supported || busyRef.current) return;
    const id = ++operation.current;
    const origin = sessionId;
    busyRef.current = true;
    busyOwner.current = id;
    setBusy(command);
    setFault("");
    setNotice("");
    try {
      const result = await sendCommand<TResult>(command, payload);
      if (alive.current && identity.current === origin && operation.current === id) outcome(result);
    } catch (err) {
      if (alive.current && identity.current === origin && operation.current === id) {
        setFault(spotifyErrorCopy(err));
        if (command === "spotify.apply" && toBridgeError(err).code.startsWith("consent_")) {
          clearTimer();
          setOffer(null);
        }
      }
    } finally {
      if (busyOwner.current === id) {
        busyOwner.current = 0;
        busyRef.current = false;
        if (alive.current && identity.current === origin) setBusy("");
      }
    }
  };

  const refresh = async () => {
    if (!supported) return;
    const id = ++operation.current;
    const origin = sessionId;
    setLoading(true);
    setFault("");
    setNotice("");
    try {
      const result = await sendCommand<SpotifyGetResultDto>("spotify.get", {});
      if (!alive.current || identity.current !== origin || operation.current !== id) return;
      if (accept(result?.spotify) === "invalid") {
        setLoading(false);
        setFault("O backend respondeu com um estado que não reconhecemos. Tenta novamente.");
      }
    } catch (err) {
      if (alive.current && identity.current === origin && operation.current === id) {
        setLoading(false);
        setFault(spotifyErrorCopy(err));
      }
    } finally {
      if (alive.current && identity.current === origin && operation.current === id) setLoading(false);
    }
  };

  const prepare = (action: SpotifyAction) => {
    if (!automaticSetupAvailable) return;
    if (!supported || busyRef.current) return;
    clearTimer();
    setOffer(null);
    void run<SpotifyPlanResultDto>("spotify.plan", { action }, result => {
      if (!isPlanOffer(result)) {
        setFault("Não foi possível preparar a configuração.");
        return;
      }
      setOffer({ plan: result.plan, consent: result.consent, action });
      const seconds = Math.max(1, Math.min(result.consent.expires_in || result.plan.expires_in, 3600));
      clearTimer();
      timer.current = window.setTimeout(() => {
        timer.current = undefined;
        setOffer(current => (current && current.consent.consent_id === result.consent.consent_id ? null : current));
        setNotice("O pedido expirou. Prepara outro plano.");
      }, seconds * 1000);
    });
  };

  const apply = () => {
    if (!automaticSetupAvailable) return;
    if (!offer || busyRef.current) return;
    const consentId = offer.consent.consent_id;
    const action = offer.action;
    void run<SpotifyApplyResultDto>("spotify.apply", { consent_id: consentId }, result => {
      clearTimer();
      setOffer(null);
      if (!result || result.accepted !== true) {
        setFault("A configuração não recebeu confirmação de arranque. Confirma o estado atual antes de repetir.");
        return;
      }
      setJobId(JOB_ID_PATTERN.test(result.job_id) ? result.job_id : "");
      setJobAction(action);
      setNotice(action === "remove" ? "Remoção aceite. A acompanhar os passos."
        : action === "repair" ? "Reparação aceite. A acompanhar os passos."
          : "Configuração aceite. A acompanhar os passos.");
    });
  };

  const discard = () => {
    clearTimer();
    setOffer(null);
    setNotice("");
  };

  const cancelJob = () => {
    const id = jobId || state?.job?.id || "";
    if (!id || busyRef.current) return;
    void run<SpotifyCancelResultDto>("spotify.cancel", { job_id: id }, result => {
      if (result?.cancelled) {
        setNotice("Cancelamento pedido. Vamos parar no próximo ponto seguro.");
        return;
      }
      if (result?.reason === "already_finished") {
        setNotice("Já tinha terminado. A atualizar o estado…");
      } else {
        setNotice("Já não encontrámos essa configuração. A atualizar o estado…");
      }
      void refresh();
    });
  };

  const connect = () => {
    void run<SpotifyGetResultDto>("spotify.connect", {}, result => {
      if (accept(result?.spotify) === "invalid") {
        setFault("Não foi possível confirmar a ligação. Tenta novamente.");
      }
    });
  };

  const disconnect = () => {
    void run<SpotifyGetResultDto>("spotify.disconnect", {}, result => {
      if (accept(result?.spotify) === "invalid") {
        setFault("Não foi possível confirmar que a ligação foi desligada.");
      }
    });
  };

  const open = (target: SpotifyOpenTarget) => {
    void run<SpotifyOpenResultDto>("spotify.open", { target }, result => {
      if (!result?.opened) {
        setFault(target === "download"
          ? "Não conseguimos abrir a página oficial. Abre spotify.com no teu browser."
          : "Não conseguimos abrir o Spotify neste PC.");
        return;
      }
      if (target === "download") setNotice("Abrimos a página oficial. Depois de instalares, volta aqui.");
    });
  };

  const dismiss = () => {
    setFault("");
    setNotice("");
  };

  return {
    state,
    loading,
    busy,
    fault,
    notice,
    offer,
    jobId,
    jobAction,
    prepare,
    apply,
    discard,
    refresh: () => void refresh(),
    cancelJob,
    connect,
    disconnect,
    open,
    dismiss,
  };
}

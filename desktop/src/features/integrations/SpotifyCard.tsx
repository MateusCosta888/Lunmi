/**
 * Cartão Spotify do painel Apps (`desktop/src/features/integrations/SpotifyCard.tsx`).
 *
 * Só consome o contrato congelado I0 (`shared/spotify.ts`): `get/plan/apply/cancel/connect/
 * disconnect/open` e o evento `spotify.changed`. Não abre nem toca música, não ativa
 * dispositivo local (não há comando de UI aprovado para isso — a copy remete para o chat) e
 * nunca apresenta percentagem, tempo estimado ou sucesso sem observação.
 *
 * esta revisão é só de apresentação: os handlers, os payloads e as guardas do hook ficam
 * inalterados; o cartão passa a usar o contrato visual `lunmi-settings-card` e os botões
 * `lunmi-action-button` do painel de Configurações.
 */
import React from "react";
import type { SendCommandFn } from "../../shared/ipc/client";
import type { SessionReadyPayload } from "../../shared/protocol";
import type {
  SpotifyAction,
  SpotifyConnectionState,
  SpotifyJobDto,
  SpotifyJobStage,
  SpotifyOpenTarget,
  SpotifySetupState,
  SpotifyStateDto,
} from "../../shared/spotify";
import { Icon } from "../../shared/ui";
import { spotifyCodeCopy, useSpotify, type SpotifyController } from "./useSpotify";
import spotifyIcon from "./assets/spotify-icon.png";
import "./spotify.css";

const STAGE_LABELS: Record<SpotifyJobStage, string> = {
  detect: "Verificar o Spotify deste PC",
  review: "Rever o que vai mudar",
  obtain: "Preparar os ficheiros da ligação",
  backup: "Guardar cópia das tuas definições",
  apply: "Instalar a ligação da Lunmi",
  pair: "Ligar a Lunmi ao Spotify",
  verify: "Confirmar com uma leitura",
  rollback: "Reverter para a cópia guardada",
};

const JOB_VERB: Record<SpotifyAction, string> = {
  install: "A configurar…",
  repair: "A reparar a ligação…",
  remove: "A remover a ligação…",
};

const PLAN_TITLE: Record<SpotifyAction, string> = {
  install: "Instalar a ligação da Lunmi",
  repair: "Reparar a ligação da Lunmi",
  remove: "Remover a ligação da Lunmi",
};

const PLAN_BODY: Record<SpotifyAction, string> = {
  install: "Vamos ligar o teu Spotify à Lunmi. Nada muda antes de confirmares.",
  repair: "Vamos voltar a aplicar a ligação. Guardamos uma cópia do estado atual antes de mexer.",
  remove: "Retiramos só a ligação instalada pela Lunmi. O Spotify e as tuas playlists ficam; definições que mudaste depois da ligação são preservadas.",
};

const APPLY_LABEL: Record<SpotifyAction, string> = {
  install: "Instalar ligação",
  repair: "Reparar ligação",
  remove: "Remover ligação",
};

const TONES: Record<string, "idle" | "ok" | "warn" | "error"> = {
  idle: "idle",
  ok: "ok",
  warn: "warn",
  error: "error",
};

const ACTION_CLASS = {
  primary: "lunmi-action-button lunmi-action-primary",
  secondary: "lunmi-action-button",
  danger: "lunmi-action-button lunmi-action-danger",
} as const;

type Command =
  | { kind: "prepare"; action: SpotifyAction }
  | { kind: "refresh" }
  | { kind: "connect" }
  | { kind: "disconnect" }
  | { kind: "open"; target: SpotifyOpenTarget }
  | { kind: "cancel" };

interface ActionSpec {
  label: string;
  variant?: keyof typeof ACTION_CLASS;
  command: Command;
}

interface Scene {
  label: string;
  body: React.ReactNode;
  tone: "idle" | "ok" | "warn" | "error";
  actions: ActionSpec[];
}

function perform(command: Command, card: SpotifyController) {
  switch (command.kind) {
    case "prepare": card.prepare(command.action); return;
    case "refresh": card.refresh(); return;
    case "connect": card.connect(); return;
    case "disconnect": card.disconnect(); return;
    case "open": card.open(command.target); return;
    case "cancel": card.cancelJob(); return;
  }
}

function Action({ spec, card, busy, automaticSetupAvailable }: {
  spec: ActionSpec; card: SpotifyController; busy: boolean; automaticSetupAvailable: boolean;
}) {
  return (
    <button
      type="button"
      className={ACTION_CLASS[spec.variant ?? "secondary"]}
      disabled={busy || (!automaticSetupAvailable && spec.command.kind === "prepare")}
      onClick={() => perform(spec.command, card)}
    >
      {spec.label}
    </button>
  );
}

/**
 * Ordem documentada do job; `review` e `rollback` só aparecem quando são o estágio atual.
 * Remover não liga nada: a remoção não tem o passo `pair`.
 */
function progressSteps(job: SpotifyJobDto): SpotifyJobStage[] {
  const base: SpotifyJobStage[] = job.action === "remove"
    ? ["detect", "obtain", "backup", "apply", "verify"]
    : ["detect", "obtain", "backup", "apply", "pair", "verify"];
  if (job.stage === "review") return ["detect", "review", ...base.slice(1)];
  if (job.stage === "rollback") return [...base, "rollback"];
  return base;
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  const megabytes = bytes / (1024 * 1024);
  if (megabytes >= 1) {
    return `${new Intl.NumberFormat("pt-PT", { maximumFractionDigits: 1 }).format(megabytes)} MB`;
  }
  return `${new Intl.NumberFormat("pt-PT", { maximumFractionDigits: 0 }).format(bytes / 1024)} KB`;
}

function setupScene(
  setup: SpotifySetupState,
  connection: SpotifyConnectionState | null,
  device: SpotifyStateDto["device"],
  state: SpotifyStateDto
): Scene | null {
  if (setup === "absent") {
    return {
      label: "Spotify não encontrado neste PC",
      body: "A Lunmi controla o Spotify que está a tocar neste computador. Instala o Spotify oficial e volta aqui.",
      tone: "warn",
      actions: [
        { label: "Instalar Spotify", variant: "primary", command: { kind: "open", target: "download" } },
        { label: "Já instalei — continuar", command: { kind: "refresh" } },
      ],
    };
  }
  if (setup === "unsupported") {
    if (state.detection.app === "store") {
      return {
        label: "A versão da Microsoft Store não é suportada",
        body: "A ligação só funciona com o Spotify oficial para computador. Podes instalá-lo ao lado; a Lunmi não desinstala nada.",
        tone: "warn",
        actions: [
          { label: "Abrir página oficial", variant: "primary", command: { kind: "open", target: "download" } },
          { label: "Voltar a verificar", command: { kind: "refresh" } },
        ],
      };
    }
    const architecture = state.detection.architecture;
    return {
      label: "Este PC não é compatível",
      body: architecture === "arm64" || architecture === "x86"
        ? "Esta versão da ligação só funciona em Windows x64."
        : "A Lunmi não altera nada sem compatibilidade confirmada.",
      tone: "error",
      actions: [{ label: "Voltar a verificar", command: { kind: "refresh" } }],
    };
  }
  if (setup === "needs_repair") {
    return {
      label: "A ligação precisa de ser reparada",
      body: "Uma atualização do Spotify pode ter removido a ligação. As tuas definições e extensões ficam iguais.",
      tone: "warn",
      actions: [
        { label: "Reparar ligação", variant: "primary", command: { kind: "prepare", action: "repair" } },
        { label: "Voltar a verificar", command: { kind: "refresh" } },
      ],
    };
  }
  if (setup === "not_configured") {
    return {
      label: "Pronto para configurar",
      body: "Vamos ligar o teu Spotify à Lunmi. Nada muda sem ti.",
      tone: "idle",
      actions: [
        { label: "Configurar Spotify", variant: "primary", command: { kind: "prepare", action: "install" } },
        { label: "Voltar a verificar", command: { kind: "refresh" } },
      ],
    };
  }

  // setup === "configured": o eixo da ligação decide o resto.
  if (connection === "disabled") {
    return {
      label: "Desligado da Lunmi",
      body: "A Lunmi deixou de ter acesso. A ligação continua instalada no Spotify; podes religar quando quiseres.",
      tone: "idle",
      actions: [{ label: "Ligar de novo", variant: "primary", command: { kind: "connect" } }],
    };
  }
  if (connection === "connecting") {
    return {
      label: "A ligar ao Spotify…",
      body: "A confirmar a ligação. Não é preciso fazer nada.",
      tone: "idle",
      actions: [],
    };
  }
  if (connection === "failed") {
    return {
      label: "Não foi possível ligar",
      body: "A ligação não ficou ativa. Nada foi repetido automaticamente.",
      tone: "error",
      actions: [
        { label: "Tentar novamente", variant: "primary", command: { kind: "connect" } },
        { label: "Voltar a verificar", command: { kind: "refresh" } },
      ],
    };
  }
  if (connection === "disconnected") {
    return {
      label: "Ligação perdida",
      body: "O Spotify pode ter fechado ou mudado de sessão. Abre o Spotify neste PC e volta a ligar.",
      tone: "warn",
      actions: [
        { label: "Voltar a ligar", variant: "primary", command: { kind: "connect" } },
        { label: "Abrir Spotify", command: { kind: "open", target: "app" } },
      ],
    };
  }
  if (device === "local") {
    return {
      label: "Pronto · este PC é o destino",
      body: "Podes pedir música no chat: tocar, pausar, retomar, volume, seguinte ou anterior. Só toca quando pedes.",
      tone: "ok",
      actions: [{ label: "Desligar da Lunmi", variant: "danger", command: { kind: "disconnect" } }],
    };
  }
  if (device === "remote") {
    return {
      label: "Ligado — o som está noutro dispositivo",
      body: "A Lunmi só controla quando este PC é o destino. Pede no chat para tocar neste computador quando quiseres ouvir aqui.",
      tone: "warn",
      actions: [{ label: "Desligar da Lunmi", variant: "danger", command: { kind: "disconnect" } }],
    };
  }
  return {
    label: "Ligado — destino por confirmar",
    body: "Ainda não sabemos onde está a tocar. Pede no chat para tocar neste computador quando quiseres ouvir aqui. Isto não confirma que esteja noutro dispositivo.",
    tone: "warn",
    actions: [{ label: "Desligar da Lunmi", variant: "danger", command: { kind: "disconnect" } }],
  };
}

export default function SpotifyCard({ sendCommand, session, automaticSetupAvailable = false }: {
  sendCommand: SendCommandFn;
  session: SessionReadyPayload | null;
  // Production stays blocked until a real install/remove safety gate passes.
  automaticSetupAvailable?: boolean;
}): React.ReactElement {
  const card = useSpotify({ sendCommand, session, automaticSetupAvailable });
  const supported = session?.capabilities.spotify === true;
  const busy = card.busy !== "";
  const state = card.state;
  const job = state?.job ?? null;
  const running = job?.status === "running";
  const waiting = !job && Boolean(card.jobId);
  const activeJob = running ? job : null;
  const failedJob = job && (job.status === "failed" || job.status === "interrupted") ? job : null;
  const cancelledJob = job?.status === "cancelled" ? job : null;
  const showOffer = automaticSetupAvailable && Boolean(card.offer) && !running && !waiting;
  const jobAction = activeJob?.action ?? card.jobAction;

  let scene: Scene | null = null;
  if (state) {
    if (running || waiting) {
      scene = {
        label: jobAction ? JOB_VERB[jobAction] : "A configurar…",
        body: null,
        tone: "idle",
        actions: [{ label: "Cancelar", command: { kind: "cancel" } }],
      };
    } else if (failedJob) {
      scene = {
        label: failedJob.stage === "rollback" ? "A recuperação parou" : "A configuração parou",
        body: `Parou no passo “${STAGE_LABELS[failedJob.stage] ?? "aplicar as alterações"}”. Não sabemos se algo ficou alterado — confirma o estado atual antes de repetir.`,
        tone: "error",
        actions: [
          { label: "Voltar a verificar", variant: "primary", command: { kind: "refresh" } },
        ],
      };
    } else if (cancelledJob) {
      scene = {
        label: "Configuração cancelada",
        body: "Parámos no próximo ponto seguro. Confirma o estado atual antes de tentar de novo.",
        tone: "warn",
        actions: [{ label: "Voltar a verificar", variant: "primary", command: { kind: "refresh" } }],
      };
    } else {
      scene = setupScene(state.setup, state.connection, state.device, state);
      if (scene && !automaticSetupAvailable && state.setup === "not_configured") {
        scene = { ...scene, label: "Configuração manual para testes",
          body: "A ligação deve ser preparada pelo procedimento manual de testes." };
      }
    }
  }

  const steps = activeJob ? progressSteps(activeJob) : [];
  const currentIndex = activeJob ? steps.indexOf(activeJob.stage) : -1;
  // Reverter é uma sequência própria: a ordem anterior não prova que `verify` aconteceu.
  const positionProvesCompletion = Boolean(activeJob) && activeJob?.stage !== "rollback";
  const canOfferOptions = state?.setup === "configured" || state?.setup === "needs_repair";
  const stateError = state?.error && state.setup !== "absent" && state.setup !== "unsupported"
    ? spotifyCodeCopy(state.error.code) : "";
  const offer = card.offer;

  return (
    <article
      className="lunmi-settings-card lunmi-apps-card lunmi-app-card lunmi-spotify-card"
      aria-labelledby="spotify-heading"
      aria-busy={card.loading || busy || Boolean(running)}
      data-testid="spotify-card"
    >
      <header className="lunmi-app-card-head">
        <span className="lunmi-app-logo" data-brand="spotify" data-testid="spotify-logo">
          <img src={spotifyIcon} alt="" width={30} height={30} draggable={false} />
        </span>
        <div className="lunmi-app-card-heading">
          <h3 id="spotify-heading">Spotify</h3>
          <p>Pesquisa e controla música no Spotify deste computador.</p>
        </div>
      </header>

      <div className="lunmi-app-card-body">
        {!supported ? (
          <p className="lunmi-app-copy" role="status">Esta versão do backend ainda não suporta o Spotify.</p>
        ) : card.loading && !state ? (
          <p className="lunmi-app-copy" role="status">A consultar a ligação ao Spotify…</p>
        ) : (
          <>
            {!automaticSetupAvailable && (
              <p className="lunmi-app-copy" role="status">
                A configuração e remoção automáticas estão bloqueadas. Mantém-se apenas a ligação manual para testes.
              </p>
            )}
            {scene && (
              <p
                className="lunmi-app-status lunmi-apps-state lunmi-spotify-state"
                role="status"
                data-tone={TONES[scene.tone]}
                data-testid="spotify-state"
              >
                {scene.tone === "ok" && <Icon name="check" size={15} />}
                {scene.label}
              </p>
            )}
            {scene?.body && <p className="lunmi-app-copy lunmi-spotify-body">{scene.body}</p>}

            {(running || waiting) && (
              <div className="lunmi-app-progress lunmi-spotify-progress">
                {waiting ? (
                  <p className="lunmi-app-copy">Configuração aceite. A começar…</p>
                ) : (
                  <ol
                    className="lunmi-spotify-steps"
                    aria-label="Passos da configuração"
                    data-testid="spotify-steps"
                  >
                    {steps.map((step, index) => {
                      const current = activeJob?.stage === step;
                      const done = positionProvesCompletion && index < currentIndex;
                      return (
                        <li
                          key={step}
                          data-step={step}
                          data-state={current ? "current" : done ? "done" : "todo"}
                          aria-current={current ? "step" : undefined}
                        >
                          {done
                            ? <Icon name="check" size={14} />
                            : <span className="lunmi-spotify-dot" aria-hidden="true" />}
                          <span>{STAGE_LABELS[step]}</span>
                        </li>
                      );
                    })}
                  </ol>
                )}
              </div>
            )}

            {offer && showOffer && automaticSetupAvailable && (
              <div className="lunmi-app-subcard lunmi-spotify-plan" data-testid="spotify-plan">
                <h4>{PLAN_TITLE[offer.action]}</h4>
                <p className="lunmi-app-copy">{PLAN_BODY[offer.action]}</p>
                <ul className="lunmi-spotify-facts">
                  {offer.plan.changes.adds_link && <li><Icon name="check" size={14} /><span>É acrescentada a ligação da Lunmi ao Spotify.</span></li>}
                  {offer.plan.changes.replaces_files > 0 && (
                    <li><Icon name="check" size={14} /><span>{offer.plan.changes.replaces_files} ficheiro(s) do Spotify são substituídos.</span></li>
                  )}
                  {offer.plan.changes.backup_bytes > 0 && (
                    <li><Icon name="check" size={14} /><span>Cópia de segurança das tuas definições: {formatBytes(offer.plan.changes.backup_bytes)}.</span></li>
                  )}
                  {offer.plan.requires.close_spotify && <li><Icon name="check" size={14} /><span>O Spotify vai ter de fechar e voltar a abrir.</span></li>}
                  {offer.plan.requires.network && <li><Icon name="check" size={14} /><span>Precisa de ligação à internet.</span></li>}
                  <li><Icon name="check" size={14} /><span>Não pede permissões de administrador.</span></li>
                </ul>
                <p className="lunmi-spotify-consent">
                  A ligação é uma extensão local da comunidade, instalada no Spotify deste PC. A Lunmi não é afiliada do Spotify.
                  A Lunmi não envia comandos musicais durante a configuração.
                </p>
                <div className="lunmi-app-actions">
                  <button type="button" className={ACTION_CLASS.primary} disabled={busy || !automaticSetupAvailable} onClick={card.apply}>
                    {APPLY_LABEL[offer.action]}
                  </button>
                  <button type="button" className={ACTION_CLASS.secondary} disabled={busy} onClick={card.discard}>Cancelar</button>
                </div>
              </div>
            )}

            {!showOffer && scene && scene.actions.length > 0 && (
              <div className="lunmi-app-actions">
                {scene.actions.map(spec => (
                  <Action key={spec.label} spec={spec} card={card} busy={busy} automaticSetupAvailable={automaticSetupAvailable} />
                ))}
              </div>
            )}

            {!showOffer && canOfferOptions && (
              <details className="lunmi-app-options lunmi-spotify-options">
                <summary><span>Opções</span><Icon name="chevron" size={14} /></summary>
                <div className="lunmi-app-actions">
                  <button type="button" className={ACTION_CLASS.secondary} disabled={busy || !automaticSetupAvailable}
                    onClick={() => card.prepare("repair")}>
                    Reparar ligação
                  </button>
                  <button type="button" className={ACTION_CLASS.danger} disabled={busy || !automaticSetupAvailable}
                    onClick={() => card.prepare("remove")}>
                    Remover ligação
                  </button>
                </div>
                <p className="lunmi-app-card-note">
                  Remover a ligação não desinstala o Spotify. A Lunmi nunca desinstala o Spotify nem apaga as tuas playlists.
                </p>
              </details>
            )}

            {stateError && <p className="lunmi-app-error" role="alert">{stateError}</p>}
            {card.fault && <p className="lunmi-app-error" role="alert" data-testid="spotify-fault">{card.fault}</p>}
            {card.notice && <p className="lunmi-app-copy" role="status" data-testid="spotify-notice">{card.notice}</p>}
          </>
        )}
      </div>

      <p className="lunmi-app-card-note">
        Ler o que está a tocar: sim. Mudar: só quando confirmas. Os títulos lidos podem ser enviados ao provider do Brain.
        Desligar da Lunmi não remove o Spotify nem as tuas playlists.
      </p>
    </article>
  );
}

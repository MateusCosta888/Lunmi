/**
 * Contrato público da integração Spotify (I0) — espelho tipado de
 * `app/integrations/spotify_contract.py`.
 *
 * Só tipos e nomes: nenhum acesso a ficheiros, rede, Electron ou Spotify. O renderer consome
 * estes DTOs; o golden `desktop/src/shared/ipc_golden_v1.json` (§`spotify`) é a fonte única
 * partilhada com Python. Ver `.agents/SPOTIFY-INTEGRATION-V1-CONTRACT.md`.
 */

export const SPOTIFY_COMMANDS = [
  "spotify.get",
  "spotify.plan",
  "spotify.apply",
  "spotify.cancel",
  "spotify.connect",
  "spotify.disconnect",
  "spotify.open",
] as const;

export type SpotifyCommandType = (typeof SPOTIFY_COMMANDS)[number];

export const SPOTIFY_EVENT = "spotify.changed" as const;

/** Payload fechado por comando: qualquer chave extra é `invalid_payload`. */
export const SPOTIFY_COMMAND_PAYLOADS: Record<SpotifyCommandType, readonly string[]> = {
  "spotify.get": [],
  "spotify.plan": ["action"],
  "spotify.apply": ["consent_id"],
  "spotify.cancel": ["job_id"],
  "spotify.connect": [],
  "spotify.disconnect": [],
  "spotify.open": ["target"],
};

/** Resultado imediato de cada comando; o progresso chega por `spotify.changed`. */
export const SPOTIFY_RESULT_KEYS: Record<SpotifyCommandType, readonly string[]> = {
  "spotify.get": ["spotify"],
  "spotify.plan": ["plan", "consent"],
  "spotify.apply": ["accepted", "job_id", "epoch"],
  "spotify.cancel": ["cancelled", "reason"],
  "spotify.connect": ["spotify"],
  "spotify.disconnect": ["spotify"],
  "spotify.open": ["opened"],
};

export const SPOTIFY_SETUP_STATES = [
  "absent",
  "unsupported",
  "not_configured",
  "configured",
  "needs_repair",
] as const;
export const SPOTIFY_CONNECTION_STATES = [
  "disabled",
  "connecting",
  "connected",
  "disconnected",
  "failed",
] as const;
export const SPOTIFY_DEVICES = ["unknown", "local", "remote"] as const;
export const SPOTIFY_ACTIONS = ["install", "repair", "remove"] as const;
export const SPOTIFY_JOB_STAGES = [
  "detect",
  "review",
  "obtain",
  "backup",
  "apply",
  "pair",
  "verify",
  "rollback",
] as const;
export const SPOTIFY_JOB_STATUSES = ["running", "done", "failed", "cancelled", "interrupted"] as const;
export const SPOTIFY_APP_CHANNELS = ["absent", "win32", "store", "other"] as const;
export const SPOTIFY_VERSION_FIT = ["supported", "newer", "older", "unknown"] as const;
export const SPOTIFY_ARCHITECTURES = ["x64", "arm64", "x86", "unknown"] as const;
export const SPOTIFY_CLI_STATES = ["missing", "pinned", "other"] as const;
export const SPOTIFY_LINK_STATES = ["absent", "present", "unknown"] as const;
export const SPOTIFY_CANCEL_REASONS = ["cancelled", "no_such_job", "already_finished"] as const;
/** `app` = Spotify já detectado/validado; `download` = página oficial fixa. */
export const SPOTIFY_OPEN_TARGETS = ["app", "download"] as const;

export const SPOTIFY_ERROR_CODES = [
  "invalid_payload",
  "not_implemented",
  "consent_invalid",
  "consent_expired",
  "consent_replayed",
  "job_busy",
  "unavailable",
  "not_configured",
  "spotify_missing",
  "store_unsupported",
  "arm_unsupported",
  "policy_blocked",
  "write_denied",
  "disk_space",
  "offline",
  "cli_unavailable",
  "signature_invalid",
  "hash_mismatch",
  "version_unsupported",
  "close_refused",
  "not_ready",
  "port_in_use",
  "profile_unsafe",
  "interrupted",
  "job_cancelled",
  "rollback_failed",
] as const;

export const SPOTIFY_STATE_KEYS = [
  "setup",
  "connection",
  "device",
  "revision",
  "epoch",
  "job",
  "detection",
  "error",
] as const;
export const SPOTIFY_DETECTION_KEYS = [
  "app",
  "version",
  "architecture",
  "cli",
  "link",
  "app_running",
] as const;
export const SPOTIFY_JOB_KEYS = ["id", "action", "stage", "status", "error_code"] as const;
export const SPOTIFY_ERROR_KEYS = ["code", "retryable"] as const;
export const SPOTIFY_PLAN_KEYS = [
  "action",
  "plan_hash",
  "steps",
  "changes",
  "requires",
  "expires_in",
  "version_pin",
  "cli_version",
] as const;
export const SPOTIFY_PLAN_CHANGES_KEYS = ["adds_link", "replaces_files", "backup_bytes"] as const;
export const SPOTIFY_PLAN_REQUIRES_KEYS = ["close_spotify", "network", "elevation"] as const;
export const SPOTIFY_CONSENT_KEYS = ["consent_id", "expires_in"] as const;
/** Evento no fio: `session_id` é o header da sessão, fora do `SpotifyStateDto`. */
export const SPOTIFY_CHANGED_KEYS = ["session_id", "spotify"] as const;

export const SESSION_ID_PATTERN = /^sess-[0-9a-f]{16}$/;
export const CONSENT_ID_PATTERN = /^cns-[0-9a-f]{32}$/;
export const JOB_ID_PATTERN = /^spj-[0-9a-f]{16}$/;
export const PLAN_HASH_PATTERN = /^[0-9a-f]{64}$/;

/** Registo validado nesta V1; outra versão entra no manifesto apenas após gate próprio. */
export const SPOTIFY_PINNED_SPOTIFY = "1.3.5.3058";
export const SPOTIFY_PINNED_CLI = "2.46.2";
export const SPOTIFY_CONSENT_TTL_SECONDS = 600;
export const SPOTIFY_ACTIVATION_TTL_SECONDS = 15;
export const SPOTIFY_LOOPBACK_PORT = 45871;

export type SpotifySetupState = (typeof SPOTIFY_SETUP_STATES)[number];
export type SpotifyConnectionState = (typeof SPOTIFY_CONNECTION_STATES)[number];
export type SpotifyDevice = (typeof SPOTIFY_DEVICES)[number];
export type SpotifyAction = (typeof SPOTIFY_ACTIONS)[number];
export type SpotifyJobStage = (typeof SPOTIFY_JOB_STAGES)[number];
export type SpotifyJobStatus = (typeof SPOTIFY_JOB_STATUSES)[number];
export type SpotifyAppChannel = (typeof SPOTIFY_APP_CHANNELS)[number];
export type SpotifyVersionFit = (typeof SPOTIFY_VERSION_FIT)[number];
export type SpotifyArchitecture = (typeof SPOTIFY_ARCHITECTURES)[number];
export type SpotifyCliState = (typeof SPOTIFY_CLI_STATES)[number];
export type SpotifyLinkState = (typeof SPOTIFY_LINK_STATES)[number];
export type SpotifyCancelReason = (typeof SPOTIFY_CANCEL_REASONS)[number];
export type SpotifyOpenTarget = (typeof SPOTIFY_OPEN_TARGETS)[number];
export type SpotifyErrorCode = (typeof SPOTIFY_ERROR_CODES)[number];

/** `unknown` não é remoto; pausa não é um eixo (é `connection:"connected"` + leitura). */
export interface SpotifyDetectionDto {
  app: SpotifyAppChannel;
  version: SpotifyVersionFit;
  architecture: SpotifyArchitecture;
  cli: SpotifyCliState;
  link: SpotifyLinkState;
  app_running: boolean;
}

export interface SpotifyJobDto {
  id: string;
  action: SpotifyAction;
  stage: SpotifyJobStage;
  status: SpotifyJobStatus;
  error_code: SpotifyErrorCode | null;
}

export interface SpotifyErrorDto {
  code: SpotifyErrorCode;
  retryable: boolean;
}

export interface SpotifyStateDto {
  setup: SpotifySetupState;
  connection: SpotifyConnectionState;
  device: SpotifyDevice;
  revision: number;
  epoch: number;
  job: SpotifyJobDto | null;
  detection: SpotifyDetectionDto;
  error: SpotifyErrorDto | null;
}

export interface SpotifyPlanChangesDto {
  adds_link: boolean;
  replaces_files: number;
  backup_bytes: number;
}

/** `elevation` é sempre `false` na V1: a Lunmi não eleva para aplicar. */
export interface SpotifyPlanRequiresDto {
  close_spotify: boolean;
  network: boolean;
  elevation: false;
}

export interface SpotifyPlanDto {
  action: SpotifyAction;
  plan_hash: string;
  steps: SpotifyJobStage[];
  changes: SpotifyPlanChangesDto;
  requires: SpotifyPlanRequiresDto;
  expires_in: number;
  version_pin: string;
  cli_version: string;
}

/** Token opaco single-use; nunca reutilizável, nunca no renderer além do `apply` imediato. */
export interface SpotifyConsentDto {
  consent_id: string;
  expires_in: number;
}

export interface SpotifyChangedDto {
  /** Header da sessão da bridge: um hook descarta eventos de sessão antiga. */
  session_id: string;
  spotify: SpotifyStateDto;
}

export interface SpotifyCommandPayloads {
  "spotify.get": Record<string, never>;
  "spotify.plan": { action: SpotifyAction };
  "spotify.apply": { consent_id: string };
  "spotify.cancel": { job_id: string };
  "spotify.connect": Record<string, never>;
  "spotify.disconnect": Record<string, never>;
  "spotify.open": { target: SpotifyOpenTarget };
}

export interface SpotifyGetResultDto {
  spotify: SpotifyStateDto;
}

export interface SpotifyPlanResultDto {
  plan: SpotifyPlanDto;
  consent: SpotifyConsentDto;
}

export interface SpotifyApplyResultDto {
  accepted: true;
  job_id: string;
  epoch: number;
}

export interface SpotifyCancelResultDto {
  cancelled: boolean;
  reason: SpotifyCancelReason;
}

export interface SpotifyOpenResultDto {
  opened: boolean;
}

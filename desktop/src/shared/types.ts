/**
 * DTOs públicos da ponte IPC v1 usados pelo renderer React da Lunmi (`desktop/src/shared/types.ts`).
 *
 * Espelham exatamente o que `app/bridge/service.py` publica. Nenhum campo aqui carrega segredo:
 * `api_key` nunca é devolvida pelo backend e o renderer nunca a envia de volta em claro.
 */
import type { SessionReadyPayload } from "./protocol";

export type ChatMode = "converse" | "execute" | "auto";

export interface ChatMessageDto {
  role: "user" | "assistant" | "system";
  text: string;
  mode?: ChatMode;
  task_id?: string;
  scope?: string;
  message_id?: string;
  response_id?: string;
  state?: "streaming" | "completed" | "interrupted" | "cancelled";
}

/** Um fluxo público por resposta; offsets contam pontos Unicode, não unidades UTF-16. */
export interface PublicResponseDto {
  session_id: string;
  task_id: string;
  response_id: string;
  message_id: string;
  seq: number;
  text?: string;
  offset?: number;
  start?: number;
  end?: number;
  streaming?: boolean;
  status?: string;
  operation_id?: string;
  state?: string;
  source?: "answer" | "verified_result";
  verified?: boolean;
  tool?: string;
  category?: "read_only" | "mutation";
  failure_reason?: string;
  code?: string;
}

export interface TaskResultDto {
  task_id: string;
  status: string;
  message: string;
  provider?: string;
  model?: string;
  app_provider?: string;
  app_model?: string;
  scope?: string;
  plan_source?: string;
  fast_path_label?: string;
  failure_kind?: string | null;
  false_success?: boolean;
  cold_start?: boolean;
  duration_ms?: number;
  llm_time_ms?: number;
  first_provider_event_ms?: number | null;
  first_provider_text_ms?: number | null;
  first_public_text_ms?: number | null;
  operation_states?: Array<{
    operation_id: string;
    state: string;
    verified?: boolean;
    tool?: string;
    category?: "read_only" | "mutation";
    failure_reason?: string;
  }>;
  planning_time_ms?: number;
  observation_time_ms?: number;
  execution_time_ms?: number;
  verification_time_ms?: number;
  startup_time_ms?: number;
  waits_ms?: number;
  recall_ms?: number;
  llm_calls?: number;
  actions?: number;
  actions_executed?: number;
  steps?: number;
  input_tokens?: number | null;
  output_tokens?: number | null;
  total_tokens?: number | null;
  token_usage?: TokenUsageDto;
  provider_calls?: ProviderCallMetricDto[];
  provider_startup_ms?: number | null;
  provider_wait_ms?: number | null;
  operation_counts?: {
    total: number;
    read_only: number;
    mutation: number;
    verified_mutations: number;
    uncertain: number;
  };
  evidence_count?: number;
  evidence_verified_count?: number;
  approval_mode?: string;
  approvals_total?: number;
  approvals_asked?: number;
  app_index_hit?: boolean;
  app_source?: string;
  app_launch_kind?: string;
  app_resolve_ms?: number;
  app_search_ms?: number;
  app_candidates?: number;
  app_index_cold?: boolean;
  memory_context_items?: number;
  memory_error?: boolean;
  [key: string]: unknown;
}

export interface ConfirmRequestDto {
  task_id: string;
  request_id: string;
  message: string;
}

export interface InputCandidateDto {
  name: string;
  publisher: string;
  install_dir: string;
  app_id: string;
  source: string;
}

export interface InputRequestDto {
  task_id: string;
  request_id: string;
  kind: "choice" | "location";
  label?: string;
  candidates?: InputCandidateDto[];
  query?: string;
  timeout_seconds?: number;
}

export interface BrainEnvOverridesDto {
  provider: boolean;
  model: boolean;
  app_provider: boolean;
  app_model: boolean;
}

export interface BrainSetErrorDto {
  /** Onde o commit falhou: `reconfigure` (nada aplicado) ou `persist` (aplicado, não gravado). */
  stage: string;
  code: string;
  message: string;
}

export interface BrainStateDto {
  provider: string;
  provider_label: string;
  model: string;
  base_url: string;
  has_api_key: boolean;
  app_provider: string;
  app_model: string;
  effort: string;
  /** Effective applied selection, calculated by the backend; optional for IPC v1 peers. */
  effective?: {
    host: EffectiveBrainSelectionDto;
    app: EffectiveBrainSelectionDto;
    app_inheritance: "full" | "provider" | "model" | "none";
  };
  env_overrides: BrainEnvOverridesDto;
  /**
   * Resultado do commit de `brain.set`, no `command.result` e no evento `brain.updated`.
   * Ausentes num estado apenas lido (`brain.get`), em que assumem "aplicado e gravado".
   */
  ok?: boolean;
  /** `false` quando o cérebro foi aplicado mas a seleção não pôde ser gravada. */
  persisted?: boolean;
  error?: BrainSetErrorDto | null;
}

export interface EffectiveBrainSelectionDto {
  provider: string;
  model: string;
  provider_inherited: boolean;
  model_inherited: boolean;
}

export interface ProviderCallMetricDto {
  brain: "host" | "app";
  provider: string;
  transport: "http" | "app_server" | "cli";
  ok: boolean;
  total_ms: number | null;
  first_event_ms?: number | null;
  first_text_ms?: number | null;
  spawn_ms?: number | null;
  /** HTTP attempts known locally; CLI/app-server internal requests remain unknown. */
  requests: number | null;
  retries: number | null;
}

export interface TokenUsageDto {
  status: "complete" | "partial" | "unavailable";
  calls_total: number;
  calls_with_input: number;
  calls_with_output: number;
  input_reported: number | null;
  output_reported: number | null;
}

export interface BrainProviderInfoDto {
  id: string;
  label: string;
  kind: string;
  location: string;
  requires_api_key: boolean;
  supports_model_discovery: boolean;
  supports_interactive_login: boolean;
}

export interface BrainGetDto {
  current: BrainStateDto;
  providers: BrainProviderInfoDto[];
}

export interface ModelInfoDto {
  id: string;
  label: string;
  default: boolean;
}

export interface ModelListingDto {
  provider: string;
  label: string;
  status: string;
  ok: boolean;
  detail: string;
  selection_problem: string | null;
  cached: boolean;
  models: ModelInfoDto[];
}

export interface BrainVerifyDto {
  provider: string;
  model: string;
  status: string;
  detail: string;
  ok: boolean;
  listing: ModelListingDto;
}

export interface BrainProbeDto {
  provider: string;
  model: string;
  ok: boolean;
  latency_ms: number;
  summary: string;
  error_kind: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
}

export interface ApprovalDto {
  auto_approve: boolean;
  mode: string;
  label: string;
}

export interface MemoryItemDto {
  id: string;
  kind: string;
  text: string;
  status: string;
  source: string;
  updated_at: string;
  created_at: string;
}

export interface MemoryListDto {
  enabled: boolean;
  items: MemoryItemDto[];
}

export interface MemoryProposalsDto {
  proposals: MemoryItemDto[];
}

/**
 * Perfil pessoal v2: documento de texto livre. `Enter` é texto, não registro.
 * `state` descreve o documento sem inventar conteúdo; `legacy_long` significa texto acima do
 * teto, que abre integralmente e só pode encolher.
 */
export type MemoryProfileState = "empty" | "ready" | "legacy_long" | "disabled";

export interface MemoryProfileDto {
  enabled: boolean;
  auto_learn: boolean;
  text: string;
  revision: number;
  can_undo: boolean;
  char_count: number;
  max_chars: number;
  state: MemoryProfileState;
}

export interface MemoryProfileSavePayload {
  text: string;
  base_revision: number;
}

export interface MemoryProfileUndoPayload {
  base_revision: number;
}

/** Evento `memory.changed`: nunca carrega texto pessoal. */
export interface MemoryChangedDto {
  reason: string;
  revision: number;
  id?: string;
}

export type MemoryOptionsDto = Record<string, boolean>;

export interface QuickActionDto {
  label: string;
  draft: string;
}

/** Perfil local do usuário — nome de tratamento e foto. Não depende da memória nem do Brain. */
export interface ProfileDto {
  name: string;
  /** Data URL PNG/JPEG redimensionada (campo canônico `avatar_data_url`); nunca URL remota nem caminho. */
  avatar_data_url: string;
}

/** Anexo escolhido no compositor. Conteúdo é dado não confiável, nunca instrução de sistema. */
export interface AttachmentDto {
  id: string;
  name: string;
  size: number;
  status: "ready" | "error";
  error?: string;
}

export interface UiPreferencesDto {
  theme?: string;
  window: number[] | null;
  scale_pct: number;
  actions: QuickActionDto[];
  profile?: { name: string; avatar_data_url: string };
}

/** Estado público de falha de sessão emitido pelo supervisor do processo principal. */
export interface SessionFailureDto {
  code: string;
  message: string;
  detail: string | null;
  session_id: string;
  exit_code: number | null;
  signal: string | null;
  executable?: string;
}

export type TaskPhase =
  | "idle"
  | "running"
  | "awaiting_confirm"
  | "awaiting_input"
  | "finished"
  | "failed"
  | "cancelled";

export interface TaskStateDto {
  task_id: string;
  mode: ChatMode;
  instruction: string;
  phase: TaskPhase;
  /** Pedido que mantém esta fase de espera no renderer; não é enviado como autoridade. */
  pending_request_id?: string;
  progress: string;
  result: TaskResultDto | null;
  error: string | null;
}

export type { SessionReadyPayload };

/** Composio Connect identity is public; tokens never cross IPC. */
export type GmailConnectionState = 'disconnected' | 'connecting' | 'composio_connected' | 'gmail_connecting' | 'select_account' | 'connected' | 'reauth_required' | 'unavailable' | 'error';
export interface GmailAccountDto { id: string; email: string }
export interface IntegrationsDto { gmail: { state: GmailConnectionState; account: GmailAccountDto | null; accounts: GmailAccountDto[]; flow_id: string | null; epoch: number; error: string | null } }

/**
 * DTOs públicos da ponte IPC v1 usados pelo renderer React da Lunmi (`desktop/src/shared/types.ts`).
 *
 * Espelham exatamente o que `app/bridge/service.py` publica. Nenhum campo aqui carrega segredo:
 * `api_key` nunca é devolvida pelo backend e o renderer nunca a envia de volta em claro.
 */
import type { SessionReadyPayload } from "./protocol";

export type ChatMode = "converse" | "execute";

export interface ChatMessageDto {
  role: "user" | "assistant" | "system";
  text: string;
  mode?: ChatMode;
  task_id?: string;
  scope?: string;
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

export interface BrainStateDto {
  provider: string;
  provider_label: string;
  model: string;
  base_url: string;
  has_api_key: boolean;
  app_provider: string;
  app_model: string;
  effort: string;
  env_overrides: BrainEnvOverridesDto;
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

export type MemoryOptionsDto = Record<string, boolean>;

export interface QuickActionDto {
  label: string;
  draft: string;
}

export interface UiPreferencesDto {
  theme?: string;
  window: number[] | null;
  scale_pct: number;
  actions: QuickActionDto[];
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

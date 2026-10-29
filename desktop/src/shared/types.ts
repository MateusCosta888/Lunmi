/**
 * DTOs públicos da ponte IPC v1 usados pelo renderer React da Lunmi (`desktop/src/shared/types.ts`).
 *
 * Espelham exatamente o que `app/bridge/service.py` publica. Nenhum campo aqui carrega segredo:
 * `api_key` nunca é devolvida pelo backend e o renderer nunca a envia de volta em claro.
 */
import type { SessionReadyPayload } from "./protocol";
import type { ChatHistorySnapshotDto } from './chatHistory';
export type { ChatHistorySnapshotDto, ConversationDto, HistoryOptionsDto, HistoryListDto } from './chatHistory';

export type ChatMode = "converse" | "execute" | "auto";

export interface ChatMessageDto {
  conversation_id?: string;
  created_at?: string;
  provider?: string;
  model?: string;
  status?: string;
  attachments?: { name: string; size_bytes: number }[];
  role: "user" | "assistant" | "system";
  text: string;
  mode?: ChatMode;
  task_id?: string;
  scope?: string;
  message_id?: string;
  response_id?: string;
  state?: "streaming" | "completed" | "interrupted" | "cancelled";
  /** WEB-SEARCH-V1: fontes da resposta web canónica; ausente fora de turno web. */
  sources?: WebSourceDto[];
  /** WEB-SEARCH-ORIGIN-V1: origem efetiva e evidência simples do turno web. */
  search_source?: WebOrigin;
  /** WEB-SEARCH-AUTO-V1: preferência guardada e motivo da mudança de plano. */
  web_preference?: WebPreference;
  web_fallback?: WebFallbackReason;
  fallback_triggered?: boolean;
  evidence_status?: WebEvidenceStatus | null;
  request_status?: WebRequestStatus | null;
}

/**
 * WEB-SEARCH-V1 — estado e fontes públicas. Contrato: `.agents/WEB-SEARCH-V1-CONTRACT.md`.
 * Nenhum DTO carrega query, conteúdo externo cru, path ou segredo.
 */
export type WebMode = "auto" | "quick" | "deep";
export type WebStateName = "idle" | "starting" | "searching" | "reading" | "error";

/**
 * WEB-SEARCH-AUTO-V1 — **preferência** escolhida (persistida por Brain). `auto` não é
 * uma origem: é o plano «provedor primeiro, Free Search só em recuperação». `none` é OFF.
 * Contrato: `.agents/WEB-SEARCH-AUTO-V1-CONTRACT.md`.
 */
export type WebPreference = "auto" | "lunmi" | "provider" | "none";
/** WEB-SEARCH-ORIGIN-V1 — **origem efetiva** de uma tentativa, sem notas A–F. */
export type WebOrigin = "none" | "lunmi" | "provider";
export type WebNativeState = "SUPPORTED" | "AVAILABLE" | "UNAVAILABLE" | "UNKNOWN";
export type WebEvidenceStatus = "suff" | "partial" | "insufficient";
export type WebRequestStatus = "met" | "partial" | "unmet" | "unknown";
export type WebFallbackReason =
  | ""
  | "forbidden"
  | "provider_unavailable"
  | "provider_recovered"
  | "cleanup_failed"
  | "lunmi_unavailable";
/** Classe fechada da tentativa nativa falhada; `security` nunca é recuperável. */
export type WebProviderFailure =
  | ""
  | "cancelled"
  | "security"
  | "permission"
  | "unavailable"
  | "timeout"
  | "malformed"
  | "failed"
  | "exhausted"
  | "unknown";

export interface NativeWebCapabilityDto {
  supported: boolean;
  available: boolean;
  can_disable: boolean;
  state: WebNativeState;
}

export interface WebStateDto {
  session_id: string;
  /** Monotónica apenas nesta sessão; um evento com revisão velha é ignorado. */
  revision: number;
  enabled: boolean;
  mode: WebMode;
  available: boolean;
  state: WebStateName;
  message: string;
  /** Fingerprint estável do Brain atual; nunca uma credencial. */
  brain_id: string;
  /** Preferência guardada para este Brain (nunca `auto` como origem). */
  preference: WebPreference;
  /** Origem efetiva agora: primeira tentativa do plano, depois de enabled/capacidade. */
  origin: WebOrigin;
  /** Capacidade nativa pequena; `state` UNKNOWN/SUPPORTED não é oferecido. */
  native_web: NativeWebCapabilityDto;
  /** Motivo do plano não ter a origem pedida; vazio quando não houve mudança. */
  web_fallback: WebFallbackReason;
}

/**
 * Payload fechado de `web.set`; `origin` exige `brain_id` (guarda stale_brain). A chave
 * `origin` é histórica e carrega a **preferência** (`WebPreference`), não uma origem.
 */
export interface WebSetPayload {
  enabled?: boolean;
  mode?: WebMode;
  brain_id?: string;
  origin?: WebPreference;
}

/** Evento `web.changed`: exatamente o DTO acima, sem campos extra. */
export type WebStateEventDto = WebStateDto;

export interface WebSourceDto {
  /** Opaco do backend, único por tarefa; o renderer nunca o deriva de uma URL. */
  id: string;
  title: string;
  url: string;
  snippet: string;
  engines: string[];
  published_at: string | null;
  date_source: string | null;
  retrieved_at: string;
  read: boolean;
  truncated: boolean;
  cached: boolean;
}

export type WebOutcome =
  | "SUCCESS"
  | "NO_RESULTS"
  | "TIMEOUT"
  | "BLOCKED"
  | "FAILED"
  | "NOT_VERIFIED";

export type WebSourceOpenReason =
  | ""
  | "invalid_payload"
  | "not_found"
  | "stale"
  | "blocked_url"
  | "unavailable"
  | "open_failed";

export interface WebSourceOpenResultDto {
  opened: boolean;
  reason: WebSourceOpenReason;
}

/** Um fluxo público por resposta; offsets contam pontos Unicode, não unidades UTF-16. */
export interface PublicResponseDto {
  conversation_id?: string;
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
  conversation_id?: string;
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
  /** WEB-SEARCH-V1: sempre presente (vazio fora de turno web). */
  web_sources?: WebSourceDto[];
  /** WEB-SEARCH-V1: `null` fora de turno web. */
  web_outcome?: WebOutcome | null;
  /** WEB-SEARCH-ORIGIN-V1: origem efetiva, proveniência e evidência simples. */
  search_source?: WebOrigin;
  provider_id?: string | null;
  native_tool_names?: string[];
  source_count?: number;
  search_outcome?: WebOutcome | null;
  web_preference?: WebPreference;
  web_fallback?: WebFallbackReason;
  /** Desfechos separados: a tentativa anterior não se perde na recuperação `auto`. */
  fallback_triggered?: boolean;
  provider_outcome?: WebOutcome | null;
  lunmi_outcome?: WebOutcome | null;
  web_provider_failure?: WebProviderFailure;
  evidence_status?: WebEvidenceStatus | null;
  request_status?: WebRequestStatus | null;
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
  /** A supported adapter is distinct from a locally installed command. */
  available?: boolean;
  installed?: boolean;
  unavailable_reason?: string;
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
  reduce_motion?: boolean;
  onboarding_done?: boolean;
  show_task_results?: boolean;
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
  conversation_id?: string;
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

/**
 * VOICE V1 — saída de voz. Contrato: `.agents/VOICE-V1-OUTPUT-CONTRACT.md`.
 * Nenhum destes DTOs carrega texto de conversa, caminho, modelo ou segredo; `capture_supported`
 * é sempre `false` nesta entrega (zero captura de microfone).
 */
export type VoiceStateName =
  | "disabled"
  | "loading"
  | "ready"
  | "synthesizing"
  | "playing"
  | "error";

export interface VoiceStateDto {
  enabled: boolean;
  auto_speak: boolean;
  available: boolean;
  state: VoiceStateName;
  generation: number;
  can_speak_last: boolean;
  /**
   * Legado congelado do contrato de saída: permanece sempre `false` e **não** é sinal da entrada
   * nova. Clientes novos usam `voice.input.*` / `voice.input.state`.
   */
  capture_supported: false;
  message: string;
}

/** Evento `voice.state`: o DTO acima + `session_id`, sem mais nenhum campo. */
export interface VoiceStateEventDto extends VoiceStateDto {
  session_id: string;
}

/** Resultado imediato de `voice.test`, `voice.speak_last` e `voice.stop`. */
export interface VoiceCommandAckDto {
  accepted: boolean;
  generation: number;
  state: VoiceStateName;
  /** `input_active` é aditivo (VOICE INPUT V1): microfone aberto recusa fala nova. */
  reason: "" | "disabled" | "unavailable" | "no_last_response" | "input_active";
}

export interface VoiceSetPayload {
  enabled?: boolean;
  auto_speak?: boolean;
}

/**
 * VOICE REFINEMENTS V2 — preparação da introdução. Nenhuma perna produz áudio ou abre microfone;
 * `ready=true` é o único sinal de que o loading pode abrir a aplicação sem ficar bloqueado.
 */
export type VoiceWarmupLegState =
  | "idle"
  | "preparing"
  | "ready"
  | "unavailable"
  | "error";

export interface VoiceWarmupDto {
  state: "idle" | "preparing" | "ready";
  ready: boolean;
  tts: VoiceWarmupLegState;
  stt: VoiceWarmupLegState;
  message: string;
}

/** Evento `voice.readiness`: DTO + `session_id`, sem mais nenhum campo. */
export interface VoiceWarmupEventDto extends VoiceWarmupDto {
  session_id: string;
}

/**
 * VOICE INPUT V1 — entrada de voz (ditado revisto). Contrato:
 * `.agents/VOICE-INPUT-V1-CONTRACT.md`. Ortogonal ao TTS: nenhum campo destes DTOs carrega texto
 * de conversa; a transcrição só sai em `voice.input.result`, uma vez.
 */
export type VoiceInputStateName =
  | "unavailable"
  | "idle"
  | "starting"
  | "listening"
  | "transcribing"
  | "ready"
  | "error";

export interface VoiceInputStateDto {
  available: boolean;
  state: VoiceInputStateName;
  generation: number;
  message: string;
}

/** Evento `voice.input.state`: o DTO acima + `session_id`, sem mais nenhum campo nem texto. */
export interface VoiceInputStateEventDto extends VoiceInputStateDto {
  session_id: string;
}

/** Resultado imediato de `voice.input.start` e `voice.input.cancel`. */
export interface VoiceInputAckDto {
  accepted: boolean;
  generation: number;
  state: VoiceInputStateName;
  reason: "" | "unavailable" | "busy" | "not_active";
}

export type VoiceInputMode = "manual" | "hold";

export interface VoiceInputStartPayload {
  mode?: VoiceInputMode;
}

export interface VoiceInputFinishPayload {
  generation: number;
}

/** Texto privado e consumível uma única vez; `status:"none"` nunca traz texto. */
export interface VoiceInputResultDto {
  status: "ready" | "none";
  generation: number;
  text: string;
  truncated: boolean;
  /**
   * Só presente em turnos hold: `true` apenas quando o release foi aceite e a captura terminou
   * por release. O renderer nunca envia sem este campo verdadeiro.
   */
  auto_send?: boolean;
}

/** Fase sanitizada publicada pelo main; o renderer é o único que chama start/finish/chat.send. */
export type VoiceInputHoldPhase = "pressed" | "released" | "cancelled";

export interface VoiceInputHoldPhaseDto {
  session_id: string;
  /** Monotónico por sessão; identifica o gesto físico e não substitui a geração do backend. */
  press_id: number;
  phase: VoiceInputHoldPhase;
  reason:
    | ""
    | "extra_key"
    | "injected"
    | "pause"
    | "reconfigured"
    | "hook_lost"
    | "blur"
    | "shutdown"
    | "lifecycle";
}

export const DEFAULT_VOICE_INPUT_SHORTCUT = "Ctrl+Alt";

/**
 * DTO único do atalho de ditado (`window.lunmi.voiceInputShortcut`, API opcional). O preload do
 * Electron importa este tipo de `shared/types.ts`; `shared/ipc/client.ts` só reexporta a forma.
 */
export interface VoiceInputShortcutResultDto {
  /** Ordem visual normalizada escolhida; `null` = sem binding. */
  accelerator: string | null;
  error:
    | ""
    | "invalid_shortcut"
    | "shortcut_conflict"
    | "io_error"
    | "hold_unavailable";
}

export interface VoiceInputShortcutControlDto {
  ok: boolean;
  error: "" | "hold_unavailable";
}

/** Ordem visual da captura local; `accelerator` é o visual normalizado aceite por `set`. */
export interface VoiceShortcutCaptureDto {
  keys: string[];
  display: string;
  accelerator: string;
  valid: boolean;
  error: "" | "duplicate" | "ambiguous" | "incomplete";
}

export type { SessionReadyPayload };

/** Composio Connect identity is public; tokens never cross IPC. */
export type GmailConnectionState = 'disconnected' | 'connecting' | 'composio_connected' | 'gmail_connecting' | 'select_account' | 'connected' | 'reauth_required' | 'unavailable' | 'error';
export interface GmailAccountDto { id: string; email: string }
export interface IntegrationsDto { gmail: { state: GmailConnectionState; account: GmailAccountDto | null; accounts: GmailAccountDto[]; flow_id: string | null; epoch: number; error: string | null } }

/**
 * BACKGROUND V1 — presença residente, orb e mini chat. Contrato: `.agents/BACKGROUND-V1-CONTRACT.md`.
 *
 * Nada aqui carrega segredo, caminho, URL ou código: só o estado público da mesma sessão que a
 * janela principal já mostra. O mini não tem autoridade extra sobre a tarefa.
 */
export type PresenceMode = "hidden" | "orb" | "mini";

/** Ações fechadas do companheiro; nenhuma recebe janela-alvo, path, URL ou método arbitrário. */
export type PresenceAction =
  | "show-main"
  | "show-settings"
  | "toggle-mini"
  | "hide-mini"
  | "stop-voice"
  | "cancel-dictation"
  | "quit"
  // LUNMI NODE V1 (N1): ações do Node, aceites apenas a partir da janela companheira.
  | "node-reveal"
  | "node-open"
  | "node-close"
  | "node-dock";

/**
 * LUNMI NODE V1 — superfície única do companheiro (`.agents/LUNMI-NODE-V1-CONTRACT.md`).
 * `hidden` não tem rect; as restantes vivem dentro do envelope fixo de 420×520 DIP.
 */
export type NodeSurface = "hidden" | "docked" | "revealed" | "pill" | "chat" | "attention";

export type NodeSide = "left" | "right";

/** Estados de negócio da tarefa (N2). Em N1 o snapshot publica `status: null`. */
export type NodeStatus =
  | "idle"
  | "working"
  | "needs_confirm"
  | "needs_input"
  | "done"
  | "not_verified"
  | "cancelled"
  | "failed"
  | "offline";

/** Tom visual derivado do status pelo main; a UI nunca o inventa. */
export type NodeTone = "neutral" | "working" | "needs" | "success" | "error" | "offline";

/** Preferências do Node (N1). Distintas de `resident`: desligar o Node não mata a sessão. */
export interface NodePreferencesDto {
  /** Node visível. `false` esconde para a bandeja; o backend e a principal continuam vivos. */
  enabled: boolean;
  /** E = `left`, D = `right`. */
  side: NodeSide;
  /** Recolhe sozinho depois da histerese de 600 ms fora do círculo revelado. */
  auto_dock: boolean;
  /** Autoriza o Node a mostrar atividade (N2). Não inventa progresso. */
  show_activity: boolean;
  /** Legacy snapshots omit this field; the mini chat then follows the theme. */
  chat_appearance?: "theme" | "black";
  /** Acelerador Electron (`"Control+Shift+L"`); `null` = sem atalho. */
  shortcut: string | null;
}

export interface NodeTransitionDto {
  id: number;
  from: NodeSurface;
  to: NodeSurface;
  duration_ms: number;
}

/** Estado público do Node. `v` versiona o DTO; ids de transição são monotónicos. */
export interface NodeStateDto {
  v: 1;
  enabled: boolean;
  side: NodeSide;
  /** Destino atual: durante um morph já é a superfície de chegada. */
  surface: NodeSurface;
  /** O conteúdo novo só entra em `settled`; em `transitioning` mantém-se o anterior. */
  phase: "settled" | "transitioning";
  transition_id: number;
  transition: NodeTransitionDto | null;
  status: NodeStatus | null;
  tone: NodeTone;
  session_id: string | null;
  /** Apresentacao de um turno de voz background; nunca contem transcricao. */
  voice_phase?: "starting" | "listening" | "thinking" | "error" | null;
}

export interface NodePreferencesResultDto {
  ok: boolean;
  preferences: NodePreferencesDto;
  error?: "invalid_payload" | "io_error" | "shortcut_conflict" | "shortcut_unavailable";
}

/** Preferência própria do recurso residente (`.local/desktop-presence.json`), não o schema fechado de `lunmi-ui.json`. */
export interface PresencePreferencesDto {
  resident: boolean;
  /** Âncora do canto superior esquerdo do orb legado em DIP; `null` = canto inferior direito. */
  companion: [number, number] | null;
  /** N1: preferências do Node, migradas de um ficheiro antigo sem apagar `companion`. */
  node?: NodePreferencesDto;
}

/** Projeção pública da conversa: uma conversa, duas vistas. `cursor` é monotónico por sessão. */
export interface ChatProjectionDto {
  conversation_id?: string;
  history?: ChatHistorySnapshotDto;
  session_id: string;
  cursor: number;
  messages: ChatMessageDto[];
  recent_tasks: TaskStateDto[];
  task: TaskStateDto | null;
  confirm: ConfirmRequestDto | null;
  input: InputRequestDto | null;
}

/**
 * Mudança incremental da projeção. Só os campos presentes mudam; mensagens são upsert por
 * `message_id` (nunca um segundo histórico). `reset` descarta a projeção anterior (sessão nova).
 */
export interface ChatProjectionDeltaDto {
  conversation_id?: string;
  history?: ChatHistorySnapshotDto;
  session_id: string;
  cursor: number;
  reset?: boolean;
  messages?: ChatMessageDto[];
  recent_tasks?: TaskStateDto[];
  task?: TaskStateDto | null;
  confirm?: ConfirmRequestDto | null;
  input?: InputRequestDto | null;
}

export interface PresenceSnapshotDto {
  /** Legado derivado de `node.surface` enquanto existir renderer antigo. */
  mode: PresenceMode;
  resident: boolean;
  session: SessionReadyPayload | null;
  chat: ChatProjectionDto;
  draft: string;
  /** Tema público aplicado pela principal (`celestial`, `light`, `redvelvet`…). */
  theme: string;
  /** Último `voice.state` público; `null` até haver um. Abrir o mini durante a fala não perde o estado. */
  voice: VoiceStateEventDto | null;
  /** N1: estado único do Node. Ausente num peer antigo (preload/main anteriores). */
  node?: NodeStateDto;
  /** N1: preferências completas do Node para a UI de definições. */
  node_preferences?: NodePreferencesDto;
}

export interface PresenceActionResultDto {
  ok: boolean;
  mode: PresenceMode;
  resident: boolean;
  error?: string;
}

/** Canal `lunmi:presence-event` (main -> renderer), separado dos envelopes da ponte. */
export type PresenceEventDto =
  | { type: "mode"; mode: PresenceMode }
  | { type: "chat"; delta: ChatProjectionDeltaDto }
  | { type: "draft"; text: string }
  | { type: "session"; session: SessionReadyPayload | null }
  | { type: "resident"; resident: boolean }
  | { type: "voice"; state: VoiceStateEventDto }
  | { type: "theme"; theme: string }
  | { type: "node"; state: NodeStateDto }
  | { type: "node-prefs"; preferences: NodePreferencesDto };

/**
 * API do preload exposta como `window.lunmiPresence` nas duas janelas. O processo principal valida
 * o remetente e a allowlist de cada janela: os métodos opcionais existem só na janela indicada.
 */
export interface LunmiPresenceApi {
  /** Local system-media controls, only exposed to the companion. */
  media?: import("./media").NodeMediaApi;
  snapshot(): Promise<PresenceSnapshotDto>;
  action(action: PresenceAction): Promise<PresenceActionResultDto>;
  setDraft(text: string, conversationId?: string): Promise<{ ok: boolean }>;
  sendCommand<TResult = Record<string, unknown>>(
    type: string,
    payload?: Record<string, unknown>
  ): Promise<TResult>;
  onEvent(callback: (event: PresenceEventDto) => void): () => void;
  /**
   * WEB-SEARCH-V1: abrir uma fonte por gesto, nas duas janelas.
   * Só viajam IDs; a URL é resolvida na projeção canónica do main e validada lá.
   */
  openWebSource?(input: {
    session_id: string;
    task_id: string;
    source_id: string;
  }): Promise<WebSourceOpenResultDto>;
  /** Só na janela principal: a opção residente pertence às definições. */
  setResident?(resident: boolean): Promise<PresencePreferencesDto>;
  /** Só na janela principal: preferências do Node (N1), com validação e rollback no main. */
  setNodePreferences?(patch: Partial<NodePreferencesDto>): Promise<NodePreferencesResultDto>;
  /** @deprecated N1 não tem arrasto livre; mantido para o renderer antigo não falhar. */
  dragBy?(dx: number, dy: number): Promise<{ ok: boolean }>;
  /** @deprecated idem. */
  endDrag?(): Promise<{ ok: boolean }>;
  /** @deprecated a shape nativa é a única autoridade de input. */
  hitTest?(inside: boolean): Promise<{ ok: boolean }>;
}

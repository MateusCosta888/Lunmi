/**
 * Contrato IPC JSONL v1 da Lunmi — espelho tipado de `app/bridge/protocol.py`.
 */

export const PROTOCOL_VERSION = 1 as const;

export const MAX_IMPORT_BYTES = 4 * 1024 * 1024;
export const MAX_LINE_BYTES = 5 * 1024 * 1024;
export const MAX_CHAT_TEXT_CHARS = 8000;
export const MAX_MEMORY_TEXT_CHARS = 800;
/** Teto do documento de perfil livre (`memory.profile.*`), contado sobre o texto real. */
export const MAX_PROFILE_CHARS = 8000;
export const MAX_ID_CHARS = 128;
export const MAX_TYPE_CHARS = 64;
export const MAX_HOST_PIDS = 64;

export const MAX_PROFILE_NAME_CHARS = 40;
export const MAX_PROFILE_AVATAR_CHARS = 40 * 1024;
export const MAX_PROFILE_AVATAR_BYTES = 30 * 1024;
export const MAX_CHAT_ATTACHMENTS = 4;
export const MAX_ATTACHMENT_NAME_CHARS = 120;
export const MAX_ATTACHMENT_BYTES = 32 * 1024;
export const MAX_TOTAL_ATTACHMENTS_BYTES = 64 * 1024;

export const UNSUPPORTED_ATTACHMENT_EXTENSIONS = [
  ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".tiff", ".tif", ".avif", ".heic",
  ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
  ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
  ".exe", ".dll", ".msi", ".bat", ".cmd", ".com", ".scr", ".bin", ".iso",
  ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac",
  ".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv",
  ".ttf", ".otf", ".woff", ".woff2", ".sqlite", ".sqlite3", ".db",
] as const;

export const ID_PATTERN = /^[A-Za-z0-9_.:-]{1,128}$/;
export const TYPE_PATTERN = /^[a-z][a-z0-9_.]*$/;

export const COMMAND_TYPES = [
  "session.hello",
  "integrations.get",
  "integrations.connect",
  "integrations.cancel",
  "integrations.select",
  "integrations.disconnect",

  "chat.send",
  "chat.history.get",
  "chat.history.list",
  "chat.history.read",
  "chat.history.create",
  "chat.history.select",
  "chat.history.rename",
  "chat.history.delete",
  "chat.history.clear",
  "chat.history.options.get",
  "chat.history.options.set",
  "task.cancel",
  "task.confirm",
  "task.answer",
  /** Capacidade aditiva do DoD global8 (T5): opcional, anunciada por `file_read`. */
  "fs.read",
  "brain.list",
  "brain.get",
  "brain.set",
  "brain.verify",
  "brain.probe",
  "approval.get",
  "approval.set",
  "memory.list",
  "memory.search",
  "memory.upsert",
  "memory.delete",
  "memory.proposals",
  "memory.propose",
  "memory.accept",
  "memory.reject",
  "memory.export",
  "memory.import",
  "memory.wipe",
  "memory.options.get",
  "memory.options.set",
  "memory.profile.get",
  "memory.profile.save",
  "memory.profile.undo",
  "ui.preferences.get",
  "ui.preferences.set",
  /** VOICE V1 (aditivo): saída de voz opcional; ver `.agents/VOICE-V1-OUTPUT-CONTRACT.md`. */
  "voice.get",
  "voice.set",
  "voice.test",
  "voice.speak_last",
  "voice.stop",
  /** VOICE REFINEMENTS V2: warmup de introdução e release hold; ver `.agents/VOICE-REFINEMENTS-V2-CONTRACT.md`. */
  "voice.warmup",
  "voice.warmup.get",
  /** VOICE INPUT V1 (aditivo): ditado revisto; ver `.agents/VOICE-INPUT-V1-CONTRACT.md`. */
  "voice.input.get",
  "voice.input.start",
  "voice.input.finish",
  "voice.input.cancel",
  "voice.input.result",
  /** WEB-SEARCH-V1 (aditivo): estado/definições; ver `.agents/WEB-SEARCH-V1-CONTRACT.md`. */
  "web.get",
  "web.set",
  /** SPOTIFY INTEGRAÇÃO V1 (I0): nomes congelados; handlers só em I1. */
  "spotify.get",
  "spotify.plan",
  "spotify.apply",
  "spotify.cancel",
  "spotify.connect",
  "spotify.disconnect",
  "spotify.open",
  "app.shutdown",
] as const;

export type CommandType = (typeof COMMAND_TYPES)[number];

export const RESPONSE_TYPES = ["command.result", "command.error"] as const;
export type ResponseType = (typeof RESPONSE_TYPES)[number];

export const EVENT_TYPES = [
  "integrations.changed",
  "response.started",
  "response.text.delta",
  "response.text.stable",
  "operation.started",
  "operation.finished",
  "response.completed",
  "response.error",
  "response.cancelled",
  "session.ready",
  "chat.message",
  "chat.history.changed",
  "task.started",
  "task.progress",
  "task.finished",
  "task.need_confirm",
  "task.need_input",
  "task.cancelled",
  "task.error",
  "brain.updated",
  "memory.changed",
  /** VOICE V1: estado público de voz; nunca carrega texto de conversa. */
  "voice.state",
  /** VOICE REFINEMENTS V2: readiness de TTS+STT; sem texto, áudio ou teclas. */
  "voice.readiness",
  /** VOICE INPUT V1: estado público do ditado; nunca carrega transcrição/áudio. */
  "voice.input.state",
  /** SPOTIFY INTEGRAÇÃO V1 (aditivo): payload fechado `{spotify}`; ver `shared/spotify.ts`. */
  "spotify.changed",
  /** WEB-SEARCH-V1: payload `WebStateDto` exato, com `session_id` e `revision`. */
  "web.changed",
  "log.append",
] as const;

export type EventType = (typeof EVENT_TYPES)[number];

export interface Capabilities {
  integrations?: boolean;
  /** Chat único: só anunciado quando o backend valida resposta e ferramentas no mesmo fluxo. */
  auto?: boolean;
  converse: boolean;
  execute: boolean;
  memory: boolean;
  brain: boolean;
  /**
   * T5/DoD global8: leitura de arquivo próprio (`fs.read`). Opcional de propósito — um backend
   * v1 anterior a esta entrega não a envia, e um cliente v1 que não a conhece segue lendo as
   * seis chaves de sempre. O índice de string mantém o contrato extensível.
   */
  file_read?: boolean;
  /**
   * SPOTIFY INTEGRAÇÃO V1: `true` só quando os seis comandos `spotify.*` estão realmente
   * registados na sessão. Aditiva e opcional: um cliente antigo ignora-a.
   */
  spotify?: boolean;
  /**
   * VOICE INPUT V1/V2: `true` só quando os cinco comandos `voice.input.*` (incluindo
   * `finish`) estão registados. Aditiva e opcional; não reutiliza `capture_supported`.
   */
  voice_input?: boolean;
  web_search: boolean;
  browser_use: boolean;
  [key: string]: boolean | undefined;
}

export interface ProtocolLimits {
  max_line_bytes: number;
  max_import_bytes: number;
  max_chat_text_chars: number;
  max_memory_text_chars?: number;
  max_profile_chars?: number;
  max_id_chars?: number;
  max_type_chars?: number;
  max_host_pids?: number;
  max_profile_name_chars?: number;
  max_profile_avatar_chars?: number;
  max_profile_avatar_bytes?: number;
  max_chat_attachments?: number;
  max_attachment_name_chars?: number;
  max_attachment_bytes?: number;
  max_total_attachments_bytes?: number;
}

export interface SessionReadyPayload {
  chat_history?: import('./types').ChatHistorySnapshotDto;
  v: number;
  session_id: string;
  pid: number;
  capabilities: Capabilities;
  limits: ProtocolLimits;
}

export interface CommandErrorPayload {
  code: string;
  message: string;
  details?: Record<string, unknown>;
}

export interface Envelope<TPayload = Record<string, unknown>> {
  v: 1;
  id: string;
  type: string;
  payload: TPayload;
}

export interface FrameOutcome {
  line?: string;
  error?: ProtocolValidationError;
}

export class ProtocolValidationError extends Error {
  public readonly code: string;
  public readonly requestId: string;

  constructor(code: string, message: string, requestId = "unknown") {
    super(message);
    this.name = "ProtocolValidationError";
    this.code = code;
    this.requestId = requestId;
  }
}

const strictUtf8Decoder = new TextDecoder("utf-8", { fatal: true });
const utf8Encoder = new TextEncoder();

export function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function containsLoneSurrogate(str: string): boolean {
  for (let i = 0; i < str.length; i++) {
    const code = str.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      if (i + 1 >= str.length) {
        return true;
      }
      const next = str.charCodeAt(i + 1);
      if (next < 0xdc00 || next > 0xdfff) {
        return true;
      }
      i += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      return true;
    }
  }
  return false;
}

function validateStrictJsonValue(value: unknown): void {
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new ProtocolValidationError("invalid_json", "Número não finito não permitido.");
    }
    return;
  }
  if (typeof value === "string") {
    if (containsLoneSurrogate(value)) {
      throw new ProtocolValidationError("invalid_encoding", "String contém surrogate inválido.");
    }
    return;
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      validateStrictJsonValue(item);
    }
    return;
  }
  if (isPlainObject(value)) {
    for (const [k, v] of Object.entries(value)) {
      if (containsLoneSurrogate(k)) {
        throw new ProtocolValidationError("invalid_encoding", "Chave contém surrogate inválido.");
      }
      validateStrictJsonValue(v);
    }
  }
}

export function decodeUtf8Strict(bytes: Uint8Array): string {
  try {
    return strictUtf8Decoder.decode(bytes);
  } catch {
    throw new ProtocolValidationError("invalid_encoding", "Mensagem não está em UTF-8 válido.");
  }
}

export function validateHostPids(rawPids: unknown): number[] {
  if (rawPids === undefined || rawPids === null) {
    return [];
  }
  if (!Array.isArray(rawPids)) {
    throw new ProtocolValidationError("invalid_payload", "Campo 'host_pids' deve ser uma lista.");
  }
  if (rawPids.length > MAX_HOST_PIDS) {
    throw new ProtocolValidationError(
      "invalid_payload",
      `Campo 'host_pids' excede o limite de ${MAX_HOST_PIDS} itens.`
    );
  }
  const result: number[] = [];
  for (const item of rawPids) {
    if (typeof item !== "number" || !Number.isInteger(item) || item <= 0 || item > 2147483647) {
      throw new ProtocolValidationError(
        "invalid_payload",
        "Cada item de 'host_pids' deve ser um inteiro positivo válido."
      );
    }
    result.push(item);
  }
  return result;
}

export function parseEnvelope(raw: string | Uint8Array): Envelope {
  let text: string;
  if (typeof raw === "string") {
    if (containsLoneSurrogate(raw)) {
      throw new ProtocolValidationError("invalid_encoding", "Mensagem contém surrogate inválido.");
    }
    const byteLength = utf8Encoder.encode(raw).byteLength;
    if (byteLength > MAX_LINE_BYTES) {
      throw new ProtocolValidationError("message_too_large", "Mensagem excede MAX_LINE_BYTES.");
    }
    text = raw;
  } else if (raw instanceof Uint8Array) {
    if (raw.byteLength > MAX_LINE_BYTES) {
      throw new ProtocolValidationError("message_too_large", "Mensagem excede MAX_LINE_BYTES.");
    }
    text = decodeUtf8Strict(raw);
  } else {
    throw new ProtocolValidationError("invalid_envelope", "Formato de frame inválido.");
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new ProtocolValidationError("invalid_json", "Frame não é JSON válido.");
  }
  if (!isPlainObject(parsed)) {
    throw new ProtocolValidationError("invalid_envelope", "Envelope deve ser um objeto JSON.");
  }

  validateStrictJsonValue(parsed);

  const reqId =
    typeof parsed.id === "string" && ID_PATTERN.test(parsed.id) ? parsed.id : "unknown";

  if (typeof parsed.v !== "number" || !Number.isInteger(parsed.v) || parsed.v !== PROTOCOL_VERSION) {
    throw new ProtocolValidationError(
      "unsupported_version",
      `Versão incompatível: esperado v=${PROTOCOL_VERSION}.`,
      reqId
    );
  }
  if (
    typeof parsed.id !== "string" ||
    parsed.id.length > MAX_ID_CHARS ||
    !ID_PATTERN.test(parsed.id)
  ) {
    throw new ProtocolValidationError(
      "invalid_envelope",
      "Campo 'id' deve ser uma string ASCII válida (1..128 chars, [A-Za-z0-9_.:-]).",
      "unknown"
    );
  }
  if (
    typeof parsed.type !== "string" ||
    parsed.type.length > MAX_TYPE_CHARS ||
    !TYPE_PATTERN.test(parsed.type)
  ) {
    throw new ProtocolValidationError(
      "invalid_envelope",
      "Campo 'type' deve ser uma string ASCII válida (1..64 chars, [a-z][a-z0-9_.]*).",
      parsed.id
    );
  }
  if (!isPlainObject(parsed.payload)) {
    throw new ProtocolValidationError(
      "invalid_payload",
      "Campo 'payload' deve ser um objeto.",
      parsed.id
    );
  }
  return {
    v: 1,
    id: parsed.id,
    type: parsed.type,
    payload: parsed.payload,
  };
}

export function encodeEnvelope(envelope: Envelope): string {
  validateStrictJsonValue(envelope);
  const line = JSON.stringify(envelope);
  if (utf8Encoder.encode(line).byteLength > MAX_LINE_BYTES) {
    throw new ProtocolValidationError(
      "message_too_large",
      "Envelope excede MAX_LINE_BYTES.",
      envelope.id
    );
  }
  return `${line}\n`;
}

export class JsonlLineFramer {
  private buffer: Uint8Array = new Uint8Array(0);
  private discardingOversized = false;
  private oversizedNotified = false;
  private pendingValidLines: string[] = [];
  private readonly maxLineBytes: number;

  constructor(maxLineBytes: number = MAX_LINE_BYTES) {
    this.maxLineBytes = maxLineBytes;
  }

  private concatBytes(a: Uint8Array, b: Uint8Array): Uint8Array {
    if (a.byteLength === 0) {
      return b;
    }
    if (b.byteLength === 0) {
      return a;
    }
    const merged = new Uint8Array(a.byteLength + b.byteLength);
    merged.set(a, 0);
    merged.set(b, a.byteLength);
    return merged;
  }

  public feedOutcomes(chunk: Uint8Array | string): FrameOutcome[] {
    let data: Uint8Array;
    if (typeof chunk === "string") {
      if (containsLoneSurrogate(chunk)) {
        return [
          {
            error: new ProtocolValidationError(
              "invalid_encoding",
              "Chunk string contém surrogate inválido."
            ),
          },
        ];
      }
      data = utf8Encoder.encode(chunk);
    } else {
      data = chunk;
    }
    if (data.byteLength === 0) {
      return [];
    }

    const outcomes: FrameOutcome[] = [];
    let offset = 0;

    while (offset < data.byteLength) {
      let nlIndex = -1;
      for (let i = offset; i < data.byteLength; i++) {
        if (data[i] === 0x0a) {
          nlIndex = i;
          break;
        }
      }

      if (this.discardingOversized) {
        if (nlIndex === -1) {
          return outcomes;
        }
        this.discardingOversized = false;
        this.oversizedNotified = false;
        offset = nlIndex + 1;
        continue;
      }

      if (nlIndex === -1) {
        const rest = data.subarray(offset);
        if (this.buffer.byteLength + rest.byteLength > this.maxLineBytes) {
          this.buffer = new Uint8Array(0);
          this.discardingOversized = true;
          if (!this.oversizedNotified) {
            this.oversizedNotified = true;
            outcomes.push({
              error: new ProtocolValidationError(
                "message_too_large",
                "Linha JSONL excede MAX_LINE_BYTES."
              ),
            });
          }
        } else {
          this.buffer = this.concatBytes(this.buffer, rest);
        }
        break;
      }

      const segment = data.subarray(offset, nlIndex);
      offset = nlIndex + 1;

      if (this.buffer.byteLength + segment.byteLength > this.maxLineBytes) {
        this.buffer = new Uint8Array(0);
        outcomes.push({
          error: new ProtocolValidationError(
            "message_too_large",
            "Linha JSONL excede MAX_LINE_BYTES."
          ),
        });
        continue;
      }

      const full =
        this.buffer.byteLength > 0 ? this.concatBytes(this.buffer, segment) : segment;
      this.buffer = new Uint8Array(0);

      let line: string;
      try {
        line = decodeUtf8Strict(full);
      } catch (err) {
        outcomes.push({
          error:
            err instanceof ProtocolValidationError
              ? err
              : new ProtocolValidationError("invalid_encoding", "UTF-8 inválido."),
        });
        continue;
      }

      if (line.endsWith("\r")) {
        line = line.slice(0, -1);
      }
      if (line.trim().length > 0) {
        outcomes.push({ line });
      }
    }

    return outcomes;
  }

  public drainPendingLines(): string[] {
    const lines = [...this.pendingValidLines];
    this.pendingValidLines = [];
    return lines;
  }

  public feed(chunk: Uint8Array | string): string[] {
    const outcomes = this.feedOutcomes(chunk);
    const lines: string[] = this.drainPendingLines();
    let firstError: ProtocolValidationError | null = null;

    for (const item of outcomes) {
      if (item.error && !firstError) {
        firstError = item.error;
      } else if (item.line !== undefined) {
        lines.push(item.line);
      }
    }

    if (firstError) {
      this.pendingValidLines.push(...lines);
      throw firstError;
    }
    return lines;
  }
}

/**
 * Acesso tipado à ponte preload (`window.lunmi`) — `desktop/src/shared/ipc/client.ts`.
 *
 * O renderer não tem `require`, `process` nem fs: tudo passa pelas três rotas expostas pelo
 * preload (`lunmi:hello`, `lunmi:command`, `lunmi:restart-backend`, ...). Este módulo centraliza
 * o acesso, normaliza erros de comando (`code: message`) e mantém uma assinatura de eventos.
 */
import type { Envelope, SessionReadyPayload } from "../protocol";
import type { UpdatesApi } from "../updates";
import type {
  VoiceInputHoldPhaseDto,
  VoiceInputShortcutControlDto,
  VoiceInputShortcutResultDto,
} from "../types";

/**
 * VOICE INPUT V1 — atalho de ditado (API opcional exposta pelo preload). O DTO vive em
 * `shared/types.ts`; este módulo só o referencia para tipar o renderer.
 */
export interface VoiceInputShortcutApi {
  get(): Promise<VoiceInputShortcutResultDto>;
  set(accelerator: string | null): Promise<VoiceInputShortcutResultDto>;
  /** Fases sanitizadas do helper; devolve a função de cancelamento da subscrição. */
  onHold(listener: (phase: VoiceInputHoldPhaseDto) => void): () => void;
  /** Só a janela principal confiável; falha fechada com `hold_unavailable`. */
  pause(): Promise<VoiceInputShortcutControlDto>;
  resume(): Promise<VoiceInputShortcutControlDto>;
}

export interface PickedAttachmentDto {
  id: string;
  name: string;
  size_bytes: number;
  sha256: string;
  text: string;
}

export interface PickAttachmentsResultDto {
  cancelled: boolean;
  attachments: PickedAttachmentDto[];
  errors: string[];
}

export interface WindowStateDto {
  minimized: boolean;
  maximized: boolean;
  closed?: boolean;
}

export type WindowControlAction = "minimize" | "toggle-maximize" | "close" | "state";

export interface LunmiBridgeApi {
  updates?: UpdatesApi;
  hello(): Promise<SessionReadyPayload>;
  notifyRendererReady?(): Promise<{
    ok: boolean;
    rendererPaintedReadyMs: number | null;
    windowShownMs: number | null;
    windowReadyToShowMs: number | null;
  }>;
  sendCommand<TResult = Record<string, unknown>>(
    type: string,
    payload?: Record<string, unknown>
  ): Promise<TResult>;
  pickLocation?(kind: "folder" | "file", query: string): Promise<{ path: string }>;
  pickAttachments?(): Promise<PickAttachmentsResultDto>;
  windowControl?(action: WindowControlAction): Promise<WindowStateDto>;
  restartBackend?(): Promise<SessionReadyPayload>;
  /** Aditiva/opcional: builds sem atalho de ditado simplesmente não a expõem. */
  voiceInputShortcut?: VoiceInputShortcutApi;
  shutdown(): Promise<{ status: string; session_id: string }>;
  onEvent(callback: (event: Envelope) => void): () => void;
}

declare global {
  interface Window {
    lunmi?: LunmiBridgeApi;
  }
}

export function getBridgeApi(): LunmiBridgeApi | undefined {
  return typeof window === "undefined" ? undefined : window.lunmi;
}

export function requireBridgeApi(): LunmiBridgeApi {
  const api = getBridgeApi();
  if (!api) {
    throw new Error("Ponte preload indisponível: window.lunmi não foi exposta.");
  }
  return api;
}

/** Comando genérico tipado. Lança BridgeError quando o backend responde `command.error`. */
export type SendCommandFn = <TResult = Record<string, unknown>>(
  type: string,
  payload?: Record<string, unknown>
) => Promise<TResult>;

export class BridgeError extends Error {
  public readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "BridgeError";
    this.code = code;
  }
}

export function toBridgeError(err: unknown): BridgeError {
  if (err instanceof BridgeError) {
    return err;
  }
  const raw = err instanceof Error ? err.message : String(err);
  const separator = raw.indexOf(": ");
  if (separator > 0 && separator < 40) {
    const code = raw.slice(0, separator).trim();
    if (/^[a-z][a-z0-9_]*$/.test(code)) {
      return new BridgeError(code, raw.slice(separator + 2).trim());
    }
  }
  return new BridgeError("error", raw);
}

export function describeError(err: unknown): string {
  const bridge = toBridgeError(err);
  return bridge.message || bridge.code;
}

export function makeSendCommand(api: LunmiBridgeApi): SendCommandFn {
  return async function sendCommand<TResult = Record<string, unknown>>(
    type: string,
    payload: Record<string, unknown> = {}
  ): Promise<TResult> {
    try {
      return (await api.sendCommand<TResult>(type, payload)) as TResult;
    } catch (err) {
      throw toBridgeError(err);
    }
  };
}

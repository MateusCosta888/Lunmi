/**
 * Preload mínimo e seguro da Lunmi.
 * - `contextIsolation: true`
 * - `nodeIntegration: false`
 * - `sandbox: true`
 * Não expõe `ipcRenderer`, `process`, `require` ou filesystem ao renderer.
 *
 * BACKGROUND V1: a janela auxiliar carrega `index.html?window=companion` e recebe apenas
 * `window.lunmiPresence` — sem `window.lunmi`, sem comandos de credenciais/caminhos. A janela
 * principal recebe `window.lunmi` (contrato v1) e `window.lunmiPresence` (projeção/estado).
 * O processo principal revalida remetente e allowlist; a separação aqui não é a única guarda.
 */
import { contextBridge, ipcRenderer, IpcRendererEvent } from "electron";
import type { Envelope, SessionReadyPayload } from "../src/shared/protocol";
import type { UpdatesApi, UpdateState } from "../src/shared/updates";
import type { MediaCommandInput, MediaCommandResultDto, MediaStateDto } from "../src/shared/media";
import type {
  LunmiPresenceApi,
  NodePreferencesDto,
  NodePreferencesResultDto,
  PresenceAction,
  PresenceActionResultDto,
  PresenceEventDto,
  PresencePreferencesDto,
  PresenceSnapshotDto,
  VoiceInputHoldPhaseDto,
  VoiceInputShortcutControlDto,
  VoiceInputShortcutResultDto,
  WebSourceOpenResultDto,
} from "../src/shared/types";

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

export interface WindowControlDto {
  minimized: boolean;
  maximized: boolean;
  closed?: boolean;
}

export type WindowControlActionDto = "minimize" | "toggle-maximize" | "close" | "state";

export interface LunmiBridgeApi {
  updates: UpdatesApi;
  hello(): Promise<SessionReadyPayload>;
  notifyRendererReady(): Promise<{
    ok: boolean;
    rendererPaintedReadyMs: number | null;
    windowShownMs: number | null;
    windowReadyToShowMs: number | null;
  }>;
  sendCommand<TResult = Record<string, unknown>>(
    type: string,
    payload?: Record<string, unknown>
  ): Promise<TResult>;
  pickLocation(kind: "folder" | "file", query: string): Promise<{ path: string }>;
  pickAttachments(): Promise<PickAttachmentsResultDto>;
  windowControl(action: WindowControlActionDto): Promise<WindowControlDto>;
  /** VOICE INPUT V1/V2: só a janela principal recebe `lunmi`; o companheiro não a vê. */
  voiceInputShortcut: {
    get(): Promise<VoiceInputShortcutResultDto>;
    set(accelerator: string | null): Promise<VoiceInputShortcutResultDto>;
    /** Fases sanitizadas do helper; devolve a função de cancelamento da subscrição. */
    onHold(listener: (phase: VoiceInputHoldPhaseDto) => void): () => void;
    /** Só a janela principal confiável; suspende o observer durante o key-recorder. */
    pause(): Promise<VoiceInputShortcutControlDto>;
    resume(): Promise<VoiceInputShortcutControlDto>;
  };
  restartBackend(): Promise<SessionReadyPayload>;
  onboarding: {
    open(target: "groq-keys" | "kiro-docs"): Promise<{ opened: boolean }>;
    groqKey(): Promise<{ key: string | null }>;
  };
  shutdown(): Promise<{ status: string; session_id: string }>;
  onEvent(callback: (event: Envelope) => void): () => void;
}

const companionWindow = (() => {
  try {
    return new URLSearchParams(window.location.search).get("window") === "companion";
  } catch {
    return false;
  }
})();

const lunmiApi: LunmiBridgeApi = Object.freeze({
  onboarding: Object.freeze({
    open: (target: "groq-keys" | "kiro-docs") => ipcRenderer.invoke("lunmi:onboarding-open", { target }),
    groqKey: (): Promise<{ key: string | null }> => ipcRenderer.invoke("lunmi:onboarding-groq-key"),
  }),
  updates: Object.freeze({
    get: (): Promise<UpdateState> => ipcRenderer.invoke("lunmi:updates-get"),
    check: (): Promise<UpdateState> => ipcRenderer.invoke("lunmi:updates-check"),
    install: (): Promise<UpdateState> => ipcRenderer.invoke("lunmi:updates-install"),
    onState(listener: (state: UpdateState) => void): () => void {
      const handler = (_event: IpcRendererEvent, state: UpdateState) => listener(state);
      ipcRenderer.on("lunmi:updates-state", handler);
      return () => ipcRenderer.removeListener("lunmi:updates-state", handler);
    },
  }),
  hello(): Promise<SessionReadyPayload> {
    return ipcRenderer.invoke("lunmi:hello");
  },
  notifyRendererReady(): Promise<{
    ok: boolean;
    rendererPaintedReadyMs: number | null;
    windowShownMs: number | null;
    windowReadyToShowMs: number | null;
  }> {
    return ipcRenderer.invoke("lunmi:renderer-ready");
  },
  sendCommand<TResult = Record<string, unknown>>(
    type: string,
    payload: Record<string, unknown> = {}
  ): Promise<TResult> {
    if (typeof type !== "string" || type.trim().length === 0) {
      return Promise.reject(new Error("Tipo de comando inválido."));
    }
    if (type === "session.hello" || type === "app.shutdown") {
      return Promise.reject(
        new Error(`forbidden_command: O comando ${type} é exclusivo do processo principal.`)
      );
    }
    return ipcRenderer.invoke("lunmi:command", { type, payload });
  },
  pickLocation(kind: "folder" | "file", query: string): Promise<{ path: string }> {
    return ipcRenderer.invoke("lunmi:pick-location", { kind, query });
  },
  pickAttachments(): Promise<PickAttachmentsResultDto> {
    return ipcRenderer.invoke("lunmi:pick-attachments");
  },
  windowControl(action: WindowControlActionDto): Promise<WindowControlDto> {
    return ipcRenderer.invoke("lunmi:window-control", { action });
  },
  voiceInputShortcut: Object.freeze({
    get(): Promise<VoiceInputShortcutResultDto> {
      return ipcRenderer.invoke("lunmi:voice-input-shortcut-get");
    },
    set(accelerator: string | null): Promise<VoiceInputShortcutResultDto> {
      // O main revalida remetente, frame, documento e o accelerator; aqui só se transporta.
      return ipcRenderer.invoke("lunmi:voice-input-shortcut-set", { accelerator });
    },
    onHold(listener: (phase: VoiceInputHoldPhaseDto) => void): () => void {
      const handler = (_evt: IpcRendererEvent, phase: VoiceInputHoldPhaseDto) => {
        listener(phase);
      };
      ipcRenderer.on("lunmi:voice-input-hold", handler);
      return () => {
        ipcRenderer.removeListener("lunmi:voice-input-hold", handler);
      };
    },
    pause(): Promise<VoiceInputShortcutControlDto> {
      return ipcRenderer.invoke("lunmi:voice-input-shortcut-pause");
    },
    resume(): Promise<VoiceInputShortcutControlDto> {
      return ipcRenderer.invoke("lunmi:voice-input-shortcut-resume");
    },
  }),
  restartBackend(): Promise<SessionReadyPayload> {
    return ipcRenderer.invoke("lunmi:restart-backend");
  },
  shutdown(): Promise<{ status: string; session_id: string }> {
    return ipcRenderer.invoke("lunmi:shutdown");
  },
  onEvent(callback: (event: Envelope) => void): () => void {
    const handler = (_evt: IpcRendererEvent, envelope: Envelope) => {
      callback(envelope);
    };
    ipcRenderer.on("lunmi:event", handler);
    return () => {
      ipcRenderer.removeListener("lunmi:event", handler);
    };
  },
});

const presenceApi: LunmiPresenceApi = Object.freeze({
  snapshot(): Promise<PresenceSnapshotDto> {
    return ipcRenderer.invoke("lunmi:presence-snapshot");
  },
  action(action: PresenceAction): Promise<PresenceActionResultDto> {
    // O preload sandbox só pode importar `electron` e tipos: a validação de runtime fica no
    // main (`shared/node.ts` é a fonte canónica do enum). Aqui só se transporta a ação.
    return ipcRenderer.invoke("lunmi:presence-action", { action });
  },
  setDraft(text: string, conversationId?: string): Promise<{ ok: boolean }> {
    return ipcRenderer.invoke("lunmi:presence-draft", { text,
      ...(conversationId ? { conversation_id: conversationId } : {}) });
  },
  openWebSource(input: {
    session_id: string;
    task_id: string;
    source_id: string;
  }): Promise<WebSourceOpenResultDto> {
    // O main revalida o sender, resolve a URL na projeção canónica e valida-a antes de abrir.
    return ipcRenderer.invoke("lunmi:web-open-source", input);
  },
  sendCommand<TResult = Record<string, unknown>>(
    type: string,
    payload: Record<string, unknown> = {}
  ): Promise<TResult> {
    return ipcRenderer.invoke("lunmi:presence-command", { type, payload });
  },
  onEvent(callback: (event: PresenceEventDto) => void): () => void {
    const handler = (_evt: IpcRendererEvent, event: PresenceEventDto) => {
      callback(event);
    };
    ipcRenderer.on("lunmi:presence-event", handler);
    return () => {
      ipcRenderer.removeListener("lunmi:presence-event", handler);
    };
  },
  ...(companionWindow
    ? {
        media: Object.freeze({
          get: (): Promise<MediaStateDto> => ipcRenderer.invoke("lunmi:node-media-get"),
          command: (input: MediaCommandInput): Promise<MediaCommandResultDto> => ipcRenderer.invoke("lunmi:node-media-command", input),
        }),
        dragBy(dx: number, dy: number): Promise<{ ok: boolean }> {
          return ipcRenderer.invoke("lunmi:presence-drag", { dx, dy });
        },
        endDrag(): Promise<{ ok: boolean }> {
          return ipcRenderer.invoke("lunmi:presence-drag-end");
        },
        hitTest(inside: boolean): Promise<{ ok: boolean }> {
          return ipcRenderer.invoke("lunmi:presence-hit-test", { inside });
        },
      }
    : {
        setResident(resident: boolean): Promise<PresencePreferencesDto> {
          return ipcRenderer.invoke("lunmi:presence-resident", { resident });
        },
        setNodePreferences(patch: Partial<NodePreferencesDto>): Promise<NodePreferencesResultDto> {
          // O main revalida chaves/tipos; aqui só se impede um payload que nem é objecto.
          const safe = patch && typeof patch === "object" && !Array.isArray(patch) ? patch : {};
          return ipcRenderer.invoke("lunmi:node-preferences", { patch: safe });
        },
      }),
});

contextBridge.exposeInMainWorld("lunmiPresence", presenceApi);
if (!companionWindow) {
  contextBridge.exposeInMainWorld("lunmi", lunmiApi);
}

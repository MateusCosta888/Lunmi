/**
 * Preload mínimo e seguro da Lunmi.
 * - `contextIsolation: true`
 * - `nodeIntegration: false`
 * - `sandbox: true`
 * Não expõe `ipcRenderer`, `process`, `require` ou filesystem ao renderer.
 */
import { contextBridge, ipcRenderer, IpcRendererEvent } from "electron";
import type { Envelope, SessionReadyPayload } from "../src/shared/protocol";

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

export interface LunmiBridgeApi {
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
  restartBackend(): Promise<SessionReadyPayload>;
  shutdown(): Promise<{ status: string; session_id: string }>;
  onEvent(callback: (event: Envelope) => void): () => void;
}

const lunmiApi: LunmiBridgeApi = Object.freeze({
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

contextBridge.exposeInMainWorld("lunmi", lunmiApi);

/**
 * Modelos de voz transferidos depois da instalação (01/11): a voz da Lunmi (~400 MB) e o
 * ditado (~1,6 GB), em separado. Nada é transferido sem o utilizador carregar em «Transferir»;
 * o backend verifica cada ficheiro (tamanho e SHA-256) antes de ativar e retoma onde parou.
 * Usado na introdução e no cartão de voz das Configurações.
 */
import React, { useCallback, useEffect, useState } from "react";
import { describeError, getBridgeApi } from "../../shared/ipc/client";
import "./voice-models.css";

export type VoiceModelGroup = "speech" | "dictation";
export type VoiceModelState = "missing" | "paused" | "downloading" | "verifying" | "installed" | "error";

export interface VoiceModelStatus {
  state: VoiceModelState;
  bytes_total: number;
  bytes_done: number;
  message: string;
}

export interface VoiceModelsDto {
  groups: Partial<Record<VoiceModelGroup, VoiceModelStatus>>;
  message: string;
}

const LABELS: Record<VoiceModelGroup, { title: string; detail: string }> = {
  speech: { title: "Voz da Lunmi", detail: "Para a Lunmi ler as respostas em voz alta." },
  dictation: { title: "Ditado", detail: "Para falares com a Lunmi em vez de escrever." },
};

const isModelsDto = (value: unknown): value is VoiceModelsDto =>
  Boolean(value) && typeof (value as VoiceModelsDto).groups === "object";

export function formatSize(bytes: number): string {
  return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1).replace(".", ",")} GB` : `${Math.round(bytes / 1e6)} MB`;
}

export function useVoiceModels(sessionId?: string) {
  const api = getBridgeApi();
  const supported = typeof api?.sendCommand === "function" && typeof api?.onEvent === "function";
  const [models, setModels] = useState<VoiceModelsDto | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!supported || !api) return;
    let alive = true;
    const unsubscribe = api.onEvent((event) => {
      if (!alive || event.type !== "voice.models") return;
      const payload = event.payload as unknown as VoiceModelsDto & { session_id?: string };
      if (sessionId && payload.session_id && payload.session_id !== sessionId) return;
      if (isModelsDto(payload)) setModels(payload);
    });
    api.sendCommand("voice.models.get", {})
      .then(result => { if (alive && isModelsDto(result)) setModels(result); })
      .catch(() => { if (alive) setModels(null); });  // backend antigo: o painel não aparece
    return () => { alive = false; unsubscribe(); };
  }, [api, sessionId, supported]);

  const act = useCallback((type: "voice.models.download" | "voice.models.cancel", payload: Record<string, unknown>) => {
    if (!api) return;
    setError("");
    api.sendCommand(type, payload)
      .then(result => { if (isModelsDto(result)) setModels(result); })
      .catch(err => setError(describeError(err)));
  }, [api]);

  return {
    supported: supported && models !== null,
    models,
    error,
    download: (group: VoiceModelGroup, repair = false) => act("voice.models.download", repair ? { group, repair } : { group }),
    cancel: (group: VoiceModelGroup) => act("voice.models.cancel", { group }),
  };
}

export function VoiceModelsPanel({ sessionId, compact = false }: { sessionId?: string; compact?: boolean }):
    React.ReactElement | null {
  const { supported, models, error, download, cancel } = useVoiceModels(sessionId);
  if (!supported || !models) return null;
  return (
    <div className="lunmi-voice-models" data-testid="voice-models">
      {(Object.keys(LABELS) as VoiceModelGroup[]).map(group => {
        const status = models.groups[group];
        if (!status) return null;
        const busy = status.state === "downloading" || status.state === "verifying";
        const percent = status.bytes_total ? Math.floor((status.bytes_done / status.bytes_total) * 100) : 0;
        return (
          <div key={group} className="lunmi-voice-model" data-testid={`voice-model-${group}`}>
            <div className="lunmi-voice-model-text">
              <strong>{LABELS[group].title}</strong>
              {!compact && <span>{LABELS[group].detail}</span>}
              <span role="status">
                {status.state === "installed" && "Instalada e verificada."}
                {status.state === "missing" && `Por transferir · ${formatSize(status.bytes_total)}`}
                {status.state === "paused" && `Em pausa · ${percent}% de ${formatSize(status.bytes_total)}`}
                {status.state === "downloading" && `A transferir · ${percent}% de ${formatSize(status.bytes_total)}`}
                {status.state === "verifying" && "A verificar os ficheiros…"}
                {status.state === "error" && (status.message || "A transferência parou.")}
              </span>
              {busy && <progress max={100} value={percent} aria-label={`${LABELS[group].title}: ${percent}%`} />}
            </div>
            <div className="lunmi-voice-model-actions">
              {busy && <button type="button" onClick={() => cancel(group)}>Pausar</button>}
              {!busy && status.state !== "installed" &&
                <button type="button" className="lunmi-voice-model-primary" onClick={() => download(group)}>
                  {status.state === "missing" ? "Transferir" : "Retomar"}
                </button>}
              {!busy && status.state === "installed" && !compact &&
                <button type="button" onClick={() => download(group, true)}>Verificar e reparar</button>}
            </div>
          </div>
        );
      })}
      {error && <p role="alert" className="lunmi-voice-models-error">{error}</p>}
    </div>
  );
}

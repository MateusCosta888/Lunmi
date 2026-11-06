/**
 * Raciocínio e modo rápido do Codex, no menu do modelo e nas Configurações. Vale logo na mensagem
 * seguinte; o backend lê a escolha a cada turno e ignora um nível que o modelo não tenha.
 */
import React, { useCallback, useEffect, useId, useState } from "react";
import { describeError } from "../../shared/ipc/client";
import type { SendCommandFn } from "../../shared/ipc/client";
import type { ModelInfoDto } from "../../shared/types";
import "./codex-speed.css";

export interface CodexOptions { effort: string; fast: boolean }

export const CODEX_LEVELS: Readonly<Record<string, string>> = {
  low: "Baixo", medium: "Médio", high: "Alto", xhigh: "Muito alto", max: "Máximo", ultra: "Ultra",
};

// O seletor do chat e as Configurações vivem ao mesmo tempo: uma mudança chega aos dois.
const listeners = new Set<(options: CodexOptions) => void>();

export function useCodexOptions(sendCommand: SendCommandFn, enabled = true) {
  const [options, setOptions] = useState<CodexOptions | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!enabled) { setOptions(null); return; }
    let alive = true;
    listeners.add(setOptions);
    sendCommand<CodexOptions>("brain.codex.options", {})
      .then(result => { if (alive) setOptions(result); })
      .catch(() => { if (alive) setOptions(null); });  // backend antigo: a opção não aparece
    return () => { alive = false; listeners.delete(setOptions); };
  }, [sendCommand, enabled]);
  const save = useCallback((next: CodexOptions) => {
    setError("");
    sendCommand<CodexOptions>("brain.codex.options", { ...next })
      .then(result => listeners.forEach(listener => listener(result)))
      .catch(err => setError(describeError(err)));
  }, [sendCommand]);
  return { options, save, error };
}

export function CodexSpeedOptions({ sendCommand, model, compact = false }: {
  sendCommand: SendCommandFn; model: ModelInfoDto | undefined; compact?: boolean;
}): React.ReactElement | null {
  const { options, save, error } = useCodexOptions(sendCommand);
  const titleId = useId();
  if (!options || !model?.efforts?.length) return null;
  const effort = model.efforts.includes(options.effort) ? options.effort : "";
  const level = (value: string, label: string) => (
    <button key={value} type="button" role="radio" aria-checked={effort === value} data-testid={`codex-effort-${value || "default"}`}
      onClick={() => save({ ...options, effort: value })}>{label}</button>
  );
  return (
    <div className="lunmi-codex-speed" data-testid="codex-speed">
      <span className="lunmi-codex-speed-title" id={titleId}>Raciocínio</span>
      <div className="lunmi-codex-levels" role="radiogroup" aria-labelledby={titleId}>
        {level("", "Padrão")}
        {Object.entries(CODEX_LEVELS).filter(([value]) => model.efforts!.includes(value)).map(([value, label]) => level(value, label))}
      </div>
      {!compact && <span className="lunmi-codex-note">Menos raciocínio responde mais depressa, mas pode falhar em pedidos difíceis.</span>}
      {model.fast && (
        <label className="lunmi-codex-fast">
          <input type="checkbox" data-testid="codex-fast" checked={options.fast}
            onChange={event => save({ ...options, fast: event.target.checked })} />
          <span>Modo rápido (/fast)
            <span className="lunmi-codex-note">Mais depressa, mas pode gastar mais da cota do Codex.</span>
          </span>
        </label>
      )}
      {error && <p role="alert" className="lunmi-codex-error">{error}</p>}
    </div>
  );
}

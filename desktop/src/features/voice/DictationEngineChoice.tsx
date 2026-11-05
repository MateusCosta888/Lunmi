/**
 * Motor de ditado: local por defeito. O Groq só liga depois de o utilizador aceitar que o áudio
 * sai do PC; nada aqui lê ou mostra a chave, só se ela existe.
 */
import React, { useEffect, useState } from "react";
import { describeError, getBridgeApi } from "../../shared/ipc/client";
import { TOKENS } from "../../shared/theme";

type Engine = "local" | "groq";
interface EngineDto { engine: Engine; key_ready: boolean }

const PRIVACY = "O áudio de cada ditado vai para o Groq, nos EUA. Por defeito não o guardam, mas podem "
  + "registá-lo até 30 dias para resolver falhas ou investigar abusos. Queres ligar?";

export function DictationEngineChoice(): React.ReactElement | null {
  const api = getBridgeApi();
  const [state, setState] = useState<EngineDto | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    api?.sendCommand?.<EngineDto>("voice.input.engine", {})
      .then(result => { if (alive) setState(result); })
      .catch(() => { if (alive) setState(null); });  // ponte antiga: a escolha não aparece
    return () => { alive = false; };
  }, [api]);

  if (!api || !state) return null;
  const choose = (engine: Engine) => {
    if (engine === state.engine || (engine === "groq" && !window.confirm(PRIVACY))) return;
    setError("");
    api.sendCommand<EngineDto>("voice.input.engine", { engine })
      .then(setState)
      .catch(err => setError(describeError(err)));
  };
  const option = (engine: Engine, title: string, detail: string, disabled = false) => (
    <label style={{ display: "flex", alignItems: "flex-start", gap: "8px", fontSize: "13px", marginTop: "6px" }}>
      <input type="radio" name="dictation-engine" data-testid={`dictation-engine-${engine}`} disabled={disabled}
        checked={state.engine === engine} onChange={() => choose(engine)} />
      <span>{title}<br /><span style={{ color: TOKENS.textMuted, fontSize: "12px" }}>{detail}</span></span>
    </label>
  );
  return (
    <fieldset style={{ border: 0, padding: 0, margin: "0 0 14px" }} data-testid="dictation-engine">
      <legend style={{ fontSize: "13px", fontWeight: 600, padding: 0 }}>Motor de ditado</legend>
      {option("local", "Local", "O áudio fica no PC. Precisa de um modelo transferido e é mais lento em PCs fracos.")}
      {option("groq", "Groq (na nuvem)", state.key_ready
        ? "Muito mais rápido, com a tua chave do Groq. O áudio sai do PC."
        : "Guarda primeiro a chave do Groq em Cérebro.", !state.key_ready)}
      {error && <p role="alert" style={{ margin: "6px 0 0", color: TOKENS.error, fontSize: "12px" }}>{error}</p>}
    </fieldset>
  );
}

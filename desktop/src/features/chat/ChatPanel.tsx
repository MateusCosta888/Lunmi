/**
 * Painel de chat (`desktop/src/features/chat/ChatPanel.tsx`, F2a).
 *
 * - Dois modos explícitos: **Conversar** (nenhuma ação no Windows) e **Executar**.
 * - O usuário e a assistente aparecem somente a partir dos eventos reais da ponte.
 * - `Parar` pede cancelamento da tarefa em voo; confirmações e perguntas (escolha de
 *   aplicativo / localização) são respondidas com `request_id` — nunca por evento solto.
 * - A resposta da assistente é Markdown sanitizado; HTML vindo do modelo nunca é executado.
 */
import React, { useEffect, useRef, useState } from "react";
import { TOKENS } from "../../shared/theme";
import { SafeMarkdown } from "../../shared/markdown";
import { getBridgeApi } from "../../shared/ipc/client";
import type { SessionReadyPayload } from "../../shared/protocol";
import type { ChatMode } from "../../shared/types";
import type { ChatTaskState } from "./useChatTask";
import { MAX_COMPOSER_CHARS } from "./useChatTask";
import { TaskPanel } from "../task/TaskPanel";
import { BrandMark, Icon } from "../../shared/ui";
import { ThoughtLine } from "./ThoughtLine";
import { useVisualActivity } from "../../shared/useVisualActivity";

export interface ChatPanelProps {
  chat: ChatTaskState;
  session: SessionReadyPayload | null;
  draft?: string;
  setDraft?: React.Dispatch<React.SetStateAction<string>>;
  modelPicker?: React.ReactNode;
  modelChanging?: boolean;
}

function bubbleStyle(role: string): React.CSSProperties {
  const base: React.CSSProperties = {
    padding: "10px 14px",
    borderRadius: "12px",
    maxWidth: "min(640px, 82%)",
    fontSize: "14px",
    lineHeight: 1.55,
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
  };
  if (role === "user") {
    return {
      ...base,
      alignSelf: "flex-end",
      backgroundColor: TOKENS.bgUser,
      color: TOKENS.textPrimary,
      border: `1px solid ${TOKENS.borderOnUser}`,
    };
  }
  if (role === "system") {
    return {
      ...base,
      alignSelf: "center",
      backgroundColor: "transparent",
      color: TOKENS.textMuted,
      fontStyle: "italic",
      fontSize: "13px",
    };
  }
  return {
    ...base,
    alignSelf: "flex-start",
    backgroundColor: TOKENS.bgCard,
    color: TOKENS.textPrimary,
    border: `1px solid ${TOKENS.borderSubtle}`,
  };
}

export function ChatPanel({ chat, session, draft, setDraft, modelPicker, modelChanging = false }: ChatPanelProps): React.ReactElement {
  const [localText, setLocalText] = useState("");
  const text = draft ?? localText;
  const setText = setDraft ?? setLocalText;
  const [mode, setMode] = useState<ChatMode>("execute");
  const [locationBusy, setLocationBusy] = useState(false);
  const listEndRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const spotlightFrame = useRef(0);
  const { reduced, visible } = useVisualActivity();
  useEffect(() => () => cancelAnimationFrame(spotlightFrame.current), []);
  useEffect(() => {
    if (reduced || !visible) { cancelAnimationFrame(spotlightFrame.current); spotlightFrame.current = 0; }
  }, [reduced, visible]);

  useEffect(() => {
    const input = inputRef.current;
    if (!input) return;
    input.style.height = "auto";
    input.style.height = `${Math.min(176, Math.max(64, input.scrollHeight))}px`;
  }, [text]);

  useEffect(() => {
    // `scrollIntoView` não existe em todo ambiente (jsdom, por exemplo): rolar é conveniência e
    // não pode derrubar a conversa.
    const node = listEndRef.current;
    if (node && typeof node.scrollIntoView === "function") {
      node.scrollIntoView({ block: "end" });
    }
  }, [chat.messages.length]);

  const converseEnabled = session ? session.capabilities.converse !== false : false;
  const executeEnabled = session ? session.capabilities.execute !== false : false;
  const canSend =
    Boolean(session) && !chat.busy && !chat.submitting && !modelChanging && text.trim().length > 0;
  const composerStatus = modelChanging ? "Aplicando modelo" : chat.confirmRequest ? "Confirmação pendente" : chat.inputRequest ? "Resposta pendente"
    : chat.busy ? "Tarefa em andamento" : session ? "Conectado" : "Sem conexão";

  const onSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canSend) {
      return;
    }
    const outgoing = text;
    // O rascunho NÃO é apagado antes da resposta do backend: se o envio for recusado
    // (`task_busy`, `reconfiguring`, capacidade ausente), a instrução digitada continua na tela.
    const outcome = await chat.send(mode, outgoing);
    if (!outcome.accepted) {
      return;
    }
    // Aceito: limpa apenas se o campo ainda contiver exatamente o texto enviado. Um rascunho novo
    // digitado durante a espera pertence ao usuário e não pode ser sobrescrito.
    setText((current) => (current === outgoing ? "" : current));
  };

  const pickLocation = async (kind: "folder" | "file", requestId: string) => {
    const api = getBridgeApi();
    if (!api || typeof api.pickLocation !== "function") {
      return;
    }
    setLocationBusy(true);
    try {
      const res = await api.pickLocation(kind, chat.inputRequest?.query ?? "aplicativo");
      if (res.path) {
        await chat.answerLocation(requestId, res.path);
      }
    } finally {
      setLocationBusy(false);
    }
  };

  return (
    <section
      className="lunmi-conversation"
      aria-label="Conversa"
      style={{
        display: "flex",
        flexDirection: "column",
        flex: "1 1 auto",
        minHeight: 0,
      }}
    >
      <div
        data-testid="chat-messages"
        style={{
          flex: "1 1 auto",
          minHeight: 0,
          overflowY: "auto",
          display: "flex",
          flexDirection: "column",
          gap: "18px",
          padding: "18px 20px",
        }}
      >
        {chat.messages.length === 0 && (
          <div className="lunmi-welcome"><BrandMark size={52} /><h1>Como posso ajudar?</h1>
            <p>Converse, organize uma ideia ou descreva uma tarefa.<br />Você controla o que a Lunmi faz no Windows.</p>
          </div>
        )}
        {chat.messages.map((message, index) => (
          <div
            key={`${message.task_id ?? "msg"}-${index}`}
            data-testid={`chat-message-${message.role}`}
            style={bubbleStyle(message.role)}
          >
            {message.role === "assistant" ? <SafeMarkdown text={message.text} /> : message.text}
          </div>
        ))}
        {chat.task && <ThoughtLine key={chat.task.task_id} task={chat.task} />}
        {chat.task && !chat.busy && <TaskPanel chat={chat} />}
        <div ref={listEndRef} />
      </div>

      {chat.confirmRequest && (
        <div
          role="alertdialog"
          aria-label="Confirmação necessária"
          data-testid="confirm-request"
          style={{
            margin: "0 20px 12px",
            padding: "12px 14px",
            borderRadius: "10px",
            border: `1px solid ${TOKENS.warning}`,
            backgroundColor: TOKENS.bgCard,
          }}
        >
          <p style={{ margin: 0, color: TOKENS.textPrimary, fontSize: "14px" }}>
            {chat.confirmRequest.message}
          </p>
          <div style={{ display: "flex", gap: "8px", marginTop: "10px" }}>
            <button
              type="button"
              data-testid="confirm-accept"
              onClick={() => void chat.approve(chat.confirmRequest!.request_id, true)}
              style={primaryButtonStyle}
            >
              Aprovar
            </button>
            <button
              type="button"
              data-testid="confirm-deny"
              onClick={() => void chat.approve(chat.confirmRequest!.request_id, false)}
              style={secondaryButtonStyle}
            >
              Negar
            </button>
          </div>
        </div>
      )}

      {chat.inputRequest && (
        <div
          aria-label="Pergunta da tarefa"
          data-testid="input-request"
          style={{
            margin: "0 20px 12px",
            padding: "12px 14px",
            borderRadius: "10px",
            border: `1px solid ${TOKENS.accent}`,
            backgroundColor: TOKENS.bgCard,
          }}
        >
          <p style={{ margin: 0, color: TOKENS.textPrimary, fontSize: "14px" }}>
            {chat.inputRequest.kind === "choice"
              ? chat.inputRequest.label || "Qual destes?"
              : `Localize: ${chat.inputRequest.query ?? ""}`}
          </p>

          {chat.inputRequest.kind === "choice" ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "6px", marginTop: "10px" }}>
              {(chat.inputRequest.candidates ?? []).map((candidate, index) => (
                <button
                  key={`${candidate.app_id || candidate.name}-${index}`}
                  type="button"
                  data-testid={`input-candidate-${index}`}
                  onClick={() => void chat.answerChoice(chat.inputRequest!.request_id, index)}
                  style={secondaryButtonStyle}
                >
                  {candidate.name || candidate.install_dir || `Opção ${index + 1}`}
                  {candidate.publisher ? ` — ${candidate.publisher}` : ""}
                </button>
              ))}
              <button
                type="button"
                data-testid="input-candidate-none"
                onClick={() => void chat.answerChoice(chat.inputRequest!.request_id, null)}
                style={secondaryButtonStyle}
              >
                Nenhum destes
              </button>
            </div>
          ) : (
            <div style={{ display: "flex", gap: "8px", marginTop: "10px" }}>
              <button
                type="button"
                data-testid="pick-folder"
                disabled={locationBusy}
                onClick={() => void pickLocation("folder", chat.inputRequest!.request_id)}
                style={primaryButtonStyle}
              >
                Escolher pasta
              </button>
              <button
                type="button"
                data-testid="pick-file"
                disabled={locationBusy}
                onClick={() => void pickLocation("file", chat.inputRequest!.request_id)}
                style={secondaryButtonStyle}
              >
                Escolher executável
              </button>
            </div>
          )}
        </div>
      )}

      {chat.lastError && (
        <div
          role="alert"
          data-testid="chat-error"
          style={{
            margin: "0 20px 12px",
            padding: "10px 12px",
            borderRadius: "8px",
            border: `1px solid ${TOKENS.error}`,
            color: TOKENS.error,
            fontSize: "13px",
            display: "flex",
            justifyContent: "space-between",
            gap: "10px",
          }}
        >
          <span>{chat.lastError}</span>
          <button type="button" onClick={chat.dismissError} style={linkButtonStyle}>
            fechar
          </button>
        </div>
      )}

      <form onSubmit={(event) => void onSubmit(event)} className="lunmi-composer">
        <div className="lunmi-composer-surface" ref={surfaceRef} data-testid="composer-surface"
          onPointerMove={event => {
            if (reduced || !visible || event.pointerType === "touch" || spotlightFrame.current) return;
            const { clientX, clientY } = event;
            spotlightFrame.current = requestAnimationFrame(() => {
              spotlightFrame.current = 0;
              const surface = surfaceRef.current; if (!surface) return;
              const rect = surface.getBoundingClientRect();
              surface.style.setProperty("--spot-x", `${clientX - rect.left}px`);
              surface.style.setProperty("--spot-y", `${clientY - rect.top}px`);
            });
          }}
          onPointerLeave={() => {
            cancelAnimationFrame(spotlightFrame.current); spotlightFrame.current = 0;
            surfaceRef.current?.style.removeProperty("--spot-x"); surfaceRef.current?.style.removeProperty("--spot-y");
          }}>
          <div className="lunmi-composer-heading">
            <span className="lunmi-composer-label"><Icon name={mode === "converse" ? "conversation" : "monitor"} size={15} />
              {mode === "converse" ? "Converse com a Lunmi" : "Sua próxima tarefa"}</span>
            <span className="lunmi-composer-presence" data-ready={Boolean(session)}><i aria-hidden="true" />{composerStatus}</span>
          </div>
          <div className="lunmi-input-well">
          <textarea ref={inputRef} data-testid="composer" aria-label="Instrução" className="lunmi-composer-input"
            value={text} rows={2} maxLength={MAX_COMPOSER_CHARS} placeholder="No que posso ajudar?"
            onChange={event => setText(event.target.value)}
            onKeyDown={event => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault(); void onSubmit(event);
              }
            }} />
            <span className="lunmi-input-hint"><Icon name="enter" size={13} />Shift + Enter para nova linha</span>
          </div>
          <div className="lunmi-composer-toolbar">
            {modelPicker}
            <div className="lunmi-composer-actions">
          <div role="radiogroup" aria-label="Modo" className="lunmi-mode">
            {(["execute", "converse"] as ChatMode[]).map(option => {
              const enabled = option === "execute" ? executeEnabled : converseEnabled;
              return <label key={option} className="lunmi-mode-option" data-checked={mode === option}>
                <input type="radio" name="chat-mode" value={option} data-testid={`mode-${option}`}
                  checked={mode === option} disabled={!enabled} onChange={() => setMode(option)} />
                <Icon name={option === "execute" ? "monitor" : "conversation"} size={14} />{option === "execute" ? "Executar" : "Conversar"}
              </label>;
            })}
          </div>
          <span className="lunmi-composer-counter" data-testid="composer-counter" data-near-limit={text.length >= MAX_COMPOSER_CHARS * .9} aria-label={`${text.length} de ${MAX_COMPOSER_CHARS} caracteres`}>{text.length}/{MAX_COMPOSER_CHARS}</span>
          {chat.busy ? <button type="button" data-testid="stop-task" className="lunmi-send lunmi-stop" aria-label="Parar tarefa" title="Parar tarefa" onClick={() => void chat.stop()}><Icon name="stop" size={16} /><span>Parar</span></button>
            : <button type="submit" data-testid="send-task" className="lunmi-send" aria-label="Enviar mensagem" title="Enviar · Enter" disabled={!canSend}><span>{mode === "converse" ? "Enviar" : "Executar"}</span><Icon name="enter" size={16} /></button>}
            </div>
          </div>
        </div>
        <div className="lunmi-composer-caption"><span className="lunmi-mode-hint">{modelChanging ? "Aplicando modelo…" : mode === "converse" ? "Sem ações no Windows" : "Ações respeitam sua aprovação"}</span>
          <span className="lunmi-composer-note">Enter para enviar</span></div>
      </form>
    </section>
  );
}

const primaryButtonStyle: React.CSSProperties = {
  padding: "8px 16px",
  borderRadius: "8px",
  border: "none",
  backgroundColor: TOKENS.accent,
  color: TOKENS.onAccent,
  fontSize: "13px",
  fontWeight: 600,
  cursor: "pointer",
};

const secondaryButtonStyle: React.CSSProperties = {
  padding: "8px 14px",
  borderRadius: "8px",
  border: `1px solid ${TOKENS.borderSubtle}`,
  backgroundColor: TOKENS.bgCardSubtle,
  color: TOKENS.textSecondary,
  fontSize: "13px",
  cursor: "pointer",
  textAlign: "left",
};

const linkButtonStyle: React.CSSProperties = {
  border: "none",
  background: "transparent",
  color: TOKENS.accent,
  cursor: "pointer",
  fontSize: "13px",
  padding: 0,
};

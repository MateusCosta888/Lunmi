/**
 * Mini chat do companheiro (`desktop/src/features/companion/MiniChat.tsx`).
 *
 * Consome a projeção canónica do main via `useCompanionChat` (mesmo `useChatTask`, mesmo parser,
 * mesmos `message_id`) — nunca monta o App nem abre sessão própria, e só pode enviar os comandos
 * da allowlist (`chat.send`, `task.cancel`, `voice.stop`, `voice.get`). Rascunho partilhado com a
 * principal (`usePresenceDraft`). Confirmação pendente mostra «Abrir tarefa» → `show-main`; o mini
 * não aprova nem responde a pedidos. Voz é apenas estado/parar: o mini nunca sintetiza nem abre
 * microfone (`capture_supported=false` preservado pelo backend).
 *
 * N3 (dentro do Node):
 * - `focusKey` foca o composer quando o chat entra (por gesto ou por atalho — o main é que
 *   publica a superfície); `interactive` desliga tudo o que não é a camada ativa.
 * - O scroll segue o fundo só quando o leitor lá está: afastado do fundo, as mensagens novas não
 *   o puxam. A pendência fica FIXADA fora da lista, para continuar acessível sem scroll.
 * - O erro local é limpo em nova tentativa e em nova sessão (a árvore fica sempre montada).
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useCompanionChat } from "../chat/useCompanionChat";
import { presenceAction, usePresenceDraft, usePresenceSession, usePresenceVoiceState } from "../../shared/ipc/presence";
import { MAX_DRAFT_CHARS } from "../../shared/presence";
import { Icon } from "../../shared/ui";
import type { ChatMode } from "../../shared/types";

const PHASE_TEXT: Record<string, string> = {
  running: "Executando…",
  awaiting_confirm: "Aguardando confirmação na janela principal",
  awaiting_input: "Aguardando tua resposta na janela principal",
};

/** Distância ao fundo (px) a partir da qual se considera que o leitor saiu do fundo. */
const STICK_THRESHOLD_PX = 24;

export interface MiniChatProps {
  /**
   * `onClose` deixa o dono da superfície fechar o chat com a ação certa: dentro do NODE é
   * `node-close` (chat → círculo revelado); fora dele mantém-se a ação legada.
   */
  onClose?: () => void;
  /** Muda quando o chat entra: o composer recebe o foco (gesto ou atalho). */
  focusKey?: number;
  /** `false` durante o morph ou fora da camada ativa: nada recebe input. */
  interactive?: boolean;
}

export function MiniChat({ onClose, focusKey, interactive = true }: MiniChatProps = {}): React.ReactElement {
  const chat = useCompanionChat();
  const { session } = usePresenceSession();
  const [draft, setDraft] = usePresenceDraft();
  const voice = usePresenceVoiceState();
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const stickToBottom = useRef(true);
  const lastFocusKey = useRef<number | undefined>(undefined);

  /**
   * Foco do composer ao ENTRAR no chat. O main é a autoridade da superfície (gesto ou atalho
   * chegam pelo mesmo estado), por isso a entrada é um contador, não um evento de UI.
   */
  useEffect(() => {
    if (focusKey === undefined || focusKey === lastFocusKey.current) return;
    if (!interactive) return;
    lastFocusKey.current = focusKey;
    inputRef.current?.focus();
  }, [focusKey, interactive]);

  /**
   * Scroll: só se segue o fundo quando o leitor lá está. Se afastou, as mensagens novas não o
   * puxam — a leitura continua onde ele a deixou.
   */
  const onListScroll = useCallback(() => {
    const node = listRef.current;
    if (!node) return;
    const distance = node.scrollHeight - node.scrollTop - node.clientHeight;
    stickToBottom.current = distance <= STICK_THRESHOLD_PX;
  }, []);

  useEffect(() => {
    const node = listRef.current;
    if (!node) return;
    if (!stickToBottom.current) return;
    node.scrollTop = node.scrollHeight;
  }, [chat.messages, chat.confirmRequest, chat.inputRequest, chat.task?.phase]);

  // Erro local: uma tentativa nova (ou uma sessão nova) limpa o erro anterior — a árvore do Node
  // fica sempre montada e um erro velho não pode continuar no ecrã.
  useEffect(() => {
    setError(null);
  }, [session?.session_id]);

  const mode: ChatMode = session?.capabilities && (session.capabilities as Record<string, unknown>).auto === true ? "auto" : "converse";
  const speaking = voice?.state === "synthesizing" || voice?.state === "playing";
  const pending = Boolean(chat.confirmRequest || chat.inputRequest);

  const submit = async () => {
    const text = draft.trim();
    if (!text || chat.submitting) return;
    setError(null);
    const outcome = await chat.send(mode, text);
    // Aceite limpa apenas se o campo ainda contém exatamente o texto enviado: um rascunho novo
    // digitado durante a espera pertence ao utilizador (mesma regra do ChatPanel).
    if (outcome.accepted) setDraft(current => (current === text || current === draft ? "" : current));
    else setError(outcome.error ?? "O pedido não foi aceite.");
  };

  return (
    <section className="lunmi-mini" aria-label="Mini chat da Lunmi" data-testid="mini-chat">
      <header className="lunmi-mini-head">
        <strong>Lunmi</strong>
        <div className="lunmi-mini-head-actions">
          <button type="button" data-testid="mini-settings" className="lunmi-mini-icon-button"
            aria-label="Abrir definições" title="Abrir definições"
            disabled={!interactive} tabIndex={interactive ? 0 : -1}
            onClick={() => void presenceAction("show-settings").catch(() => undefined)}>
            <Icon name="settings" size={15} />
          </button>
          <button type="button" data-testid="mini-close" className="lunmi-mini-icon-button"
            aria-label="Fechar o chat" title="Fechar (recolhe o Node)"
            disabled={!interactive} tabIndex={interactive ? 0 : -1}
            onClick={() => {
              if (onClose) {
                onClose();
                return;
              }
              void presenceAction("hide-mini").catch(() => undefined);
            }}>
            <Icon name="win-close" size={14} />
          </button>
        </div>
      </header>

      {/*
        Pendência FIXADA fora da lista: pode chegar depois das mensagens (worker frio) e tem de
        continuar acessível sem scroll — e sem puxar o leitor que se afastou do fundo.
      */}
      {pending && (
        <div className="lunmi-mini-pending-dock" data-testid="mini-pending-dock">
          <div className="lunmi-mini-pending" data-testid="mini-pending" role="status">
            <span>Ação pendente de confirmação — decide na janela principal.</span>
            <button type="button" data-testid="mini-open-task" className="lunmi-mini-button"
              disabled={!interactive} tabIndex={interactive ? 0 : -1}
              onClick={() => void presenceAction("show-main").catch(() => undefined)}>
              Abrir tarefa
            </button>
          </div>
        </div>
      )}

      <div className="lunmi-mini-messages" data-testid="mini-messages" ref={listRef} role="log" aria-live="polite"
        onScroll={onListScroll}>
        {chat.messages.length === 0 && <p className="lunmi-mini-empty">Ainda não há conversa nesta sessão.</p>}
        {chat.messages.map(message => (
          <p key={`${message.task_id ?? "s"}:${message.message_id ?? message.text}`}
            className="lunmi-mini-bubble" data-role={message.role} data-testid={`mini-message-${message.message_id ?? ""}`}>
            {message.text}
          </p>
        ))}
        {chat.task && (chat.task.phase === "running" || chat.task.phase === "awaiting_confirm" || chat.task.phase === "awaiting_input") && (
          <p className="lunmi-mini-phase" data-testid="mini-phase">{PHASE_TEXT[chat.task.phase] ?? chat.task.phase}</p>
        )}
      </div>

      {(error || chat.lastError) && (
        <p className="lunmi-mini-error" role="alert" data-testid="mini-error">{error ?? chat.lastError}</p>
      )}

      <footer className="lunmi-mini-composer">
        <label className="lunmi-sr-only" htmlFor="mini-input">Mensagem</label>
        <textarea id="mini-input" data-testid="mini-input" rows={2} value={draft} ref={inputRef}
          maxLength={MAX_DRAFT_CHARS} placeholder="Escreve para a Lunmi…"
          disabled={!interactive} tabIndex={interactive ? 0 : -1}
          onChange={event => setDraft(event.target.value)}
          onKeyDown={event => {
            if (event.key !== "Enter" || event.shiftKey) return;
            // IME em composição: Enter confirma caracteres, nunca envia. Busy/submitting: sem 2.º envio.
            if (event.nativeEvent.isComposing || event.keyCode === 229) return;
            if (chat.busy || chat.submitting) { event.preventDefault(); return; }
            event.preventDefault();
            void submit();
          }} />
        <div className="lunmi-mini-actions">
          {chat.busy ? (
            <button type="button" data-testid="mini-stop" className="lunmi-mini-button"
              disabled={!interactive} tabIndex={interactive ? 0 : -1}
              onClick={() => void chat.stop().catch(() => undefined)}>Parar</button>
          ) : (
            <button type="button" data-testid="mini-send" className="lunmi-mini-button lunmi-mini-send"
              disabled={!draft.trim() || chat.submitting || !interactive} tabIndex={interactive ? 0 : -1}
              onClick={() => void submit()}>Enviar</button>
          )}
          <button type="button" data-testid="mini-stop-voice" className="lunmi-mini-button lunmi-mini-quiet"
            disabled={!speaking || !interactive} tabIndex={interactive ? 0 : -1}
            onClick={() => void presenceAction("stop-voice").catch(() => undefined)}>
            {speaking ? "Parar voz" : "Voz"}
          </button>
        </div>
      </footer>
    </section>
  );
}

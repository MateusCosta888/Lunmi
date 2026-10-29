/**
 * Superfície do LUNMI NODE V1 (`desktop/src/features/companion/NodeSurface.tsx`).
 *
 * O objeto é UM só: o DOM nasce uma vez e nunca é remontado quando a superfície muda
 * (círculo → cápsula → chat). O main é a autoridade da forma — a `setShape` nativa recorta a
 * janela no rect da superfície e é ela que define também a região de input. Por isso aqui não há
 * clip-path nem raio CSS a imitar a silhueta: o corpo cobre o envelope inteiro
 * e o recorte nativo faz o resto (D4).
 *
 * O conteúdo é posicionado com a ajuda partilhada (`nodeContentRect` de
 * `desktop/src/shared/node.ts`), a mesma que o main usa para a shape: um número para o desenho e
 * para o input. Nenhum rect nem IPC por frame — a geometria só é recalculada quando muda a
 * superfície, o lado ou o tamanho da janela.
 *
 * Um gesto abre o chat: o clique no círculo pede `node-open` uma vez e o main percorre a
 * sequência aprovada (círculo → cápsula → chat). A cápsula é uma etapa visual do morph, nunca
 * um segundo clique.
 *
 * O Node nunca mostra texto de conversa fora do chat: o único conteúdo livre é a etiqueta curta
 * do estado finito publicado pelo main (`status`/`tone`).
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { nodeContentRect, NODE, type NodeBounds } from "../../shared/node";
import { presenceAction, useNodePreferences, useNodeState, usePresenceVoiceState } from "../../shared/ipc/presence";
import { GalaxyOrb } from "../chat/orb/GalaxyOrb";
import { deriveOrbMode } from "../chat/orb/orbState";
import type {
  NodeSide,
  NodeStateDto,
  NodeStatus,
  NodeSurface as NodeSurfaceKind,
  PresenceAction,
} from "../../shared/types";
import { MiniChat } from "./MiniChat";
import "./node.css";

/** Camadas de conteúdo. Todas permanecem montadas: o objeto não é remontado. */
type NodeLayer = "circle" | "pill" | "chat";

const SURFACE_LAYER: Record<NodeSurfaceKind, NodeLayer | null> = {
  hidden: null,
  docked: "circle",
  revealed: "circle",
  pill: "pill",
  attention: "pill",
  chat: "chat",
};

/**
 * Etiquetas honestas por estado finito. São texto fixo do enum publicado pelo main — nunca
 * progresso, instrução ou mensagem da conversa. Curtas para caberem nos 280 px da cápsula e
 * dizem o que o clique faz: confirmar/responder acontece na janela principal; os estados
 * terminais abrem o chat onde o resultado já está.
 */
const STATUS_LABEL: Record<NodeStatus, string> = {
  idle: "Pronta",
  working: "A trabalhar",
  needs_confirm: "Confirmar na principal",
  needs_input: "Responder na principal",
  done: "Concluída",
  not_verified: "Por verificar",
  cancelled: "Cancelada",
  failed: "Falhou",
  offline: "Sem ligação",
};

/** Para onde a cápsula leva: decisões ficam na principal; resultados abrem no chat. */
const SHOW_MAIN_STATUSES: ReadonlySet<NodeStatus> = new Set<NodeStatus>(["needs_confirm", "needs_input"]);

const IDLE_LABEL = "Lunmi";

/**
 * VOICE/NODE V3: etiquetas fixas e honestas do campo canónico `NodeStateDto.voice_phase`
 * (publicado pelo main; `null`/ausente = comportamento V1). Nenhum texto livre, nenhuma
 * transcrição, nenhum progresso — a UI só espelha o enum fechado. A atividade explícita do
 * microfone não é ocultada por `show_activity=false`: enquanto houver fase de voz, é ela que a
 * cápsula mostra (limpeza/precedência de turno são autoridade do main).
 */
const VOICE_LABEL: Record<NonNullable<NodeStateDto["voice_phase"]>, string> = {
  starting: "A preparar o microfone",
  listening: "A escutar",
  thinking: "A pensar",
  // delta do contrato: falha de ENTRADA de voz — etiqueta fixa honesta, nunca "tarefa falhou".
  error: "O ditado falhou",
};

/** Envelope medido: a janela do Node É o envelope (420×520 DIP, ou o que couber no ecrã). */
function measureEnvelope(): NodeBounds {
  const width = Math.max(1, Math.round(window.innerWidth || NODE.envelope_width));
  const height = Math.max(1, Math.round(window.innerHeight || NODE.envelope_height));
  return { x: 0, y: 0, width, height };
}

function rectStyle(x: number, y: number, width: number, height: number): React.CSSProperties {
  return {
    left: `${Math.round(x)}px`,
    top: `${Math.round(y)}px`,
    width: `${Math.round(width)}px`,
    height: `${Math.round(height)}px`,
  };
}

export function NodeSurface(): React.ReactElement | null {
  const { state, available } = useNodeState();
  const { preferences } = useNodePreferences();
  const presenceVoice = usePresenceVoiceState();
  const [envelope, setEnvelope] = useState<NodeBounds>(measureEnvelope);
  /**
   * A superfície que já assentou. Durante um morph o main publica o DESTINO; o renderer mantém
   * o conteúdo anterior e só troca quando recebe `phase === "settled"`.
   */
  const [settled, setSettled] = useState<NodeSurfaceKind>("docked");
  const [seen, setSeen] = useState<NodeStateDto | null>(null);
  /**
   * Contador de entradas no chat: quando o chat assenta, o composer recebe o foco. Vale para
   * gesto e para atalho — nos dois casos é o main que publica a superfície.
   */
  const [chatFocusKey, setChatFocusKey] = useState(0);

  useEffect(() => {
    const onResize = () => setEnvelope(measureEnvelope());
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  /**
   * Ajuste de estado durante o render (padrão do React para props que mudam): a superfície
   * assente troca NO MESMO commit em que o main publica `settled`, sem um segundo tempo.
   */
  if (state !== seen) {
    setSeen(state);
    if (state && state.phase === "settled" && state.surface !== settled) {
      setSettled(state.surface);
      if (state.surface === "chat") setChatFocusKey(key => key + 1);
    }
  }

  const request = useCallback((action: PresenceAction) => {
    // A resposta do main é a autoridade; um gesto recusado não muda a UI (o estado não muda).
    void presenceAction(action).catch(() => undefined);
  }, []);

  const side: NodeSide = state?.side ?? "right";
  const rects = useMemo(
    () => ({
      circle: nodeContentRect("revealed", side, envelope),
      docked: nodeContentRect("docked", side, envelope),
      pill: nodeContentRect("pill", side, envelope),
      chat: nodeContentRect("chat", side, envelope),
    }),
    [side, envelope]
  );

  const open = useCallback(() => request("node-open"), [request]);
  const close = useCallback(() => request("node-close"), [request]);

  useEffect(() => {
    const onBlur = () => {
      if (state?.surface !== "chat" || state.phase !== "settled" || state.voice_phase
        || state.status === "working" || state.status === "needs_confirm" || state.status === "needs_input") return;
      close();
    };
    window.addEventListener("blur", onBlur);
    return () => window.removeEventListener("blur", onBlur);
  }, [state, close]);

  /** Hover sobre o arco recolhido: UM pedido, só quando a mudança acontece. */
  const onPointerEnter = useCallback(() => {
    if (settled !== "docked") return;
    request("node-reveal");
  }, [request, settled]);

  /** Esc fecha o que está aberto; nunca cancela tarefa nem pedido de voz. */
  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key !== "Escape") return;
      if (event.nativeEvent.isComposing) return;
      if (settled !== "chat" && settled !== "pill" && settled !== "attention") return;
      event.preventDefault();
      close();
    },
    [close, settled]
  );

  /**
   * Só a camada ativa de uma superfície assente recebe input. Durante o morph nada recebe
   * input: a forma está a mudar e o conteúdo visível não pode ser operado por engano.
   */
  const settledNow = state?.phase === "settled";
  const layer = SURFACE_LAYER[settled];
  const circleActive = settledNow && layer === "circle";
  const pillActive = settledNow && layer === "pill";
  const chatActive = settledNow && layer === "chat";

  if (!available || !state) return null;

  const phase = state.phase;
  const tone = state.tone;
  const status = state.status;
  const voicePhase = state.voice_phase ?? null;
  // Saída de voz (TTS) é um facto do main; a etiqueta «A falar» só aparece quando ele é real.
  const label = voicePhase ? VOICE_LABEL[voicePhase]
    : presenceVoice?.state === "playing" ? "A falar"
    : presenceVoice?.state === "synthesizing" ? "A preparar a voz"
    : status ? STATUS_LABEL[status] : IDLE_LABEL;
  // A cápsula é acionável: confirmação/resposta levam à principal (onde se decide) e os estados
  // terminais abrem o chat, onde o resultado já está. Nunca aprova nem responde no Node.
  const pillNeedsMain = status !== null && SHOW_MAIN_STATUSES.has(status);
  const pillAction = pillNeedsMain
    ? () => request("show-main")
    : open;
  const pillLabel = pillNeedsMain ? `Ir para a janela principal — ${label}` : `Abrir o chat — ${label}`;
  // ORB REACTIVE V1: estados reais espelhados na face circular — voice_phase do main manda;
  // TTS playing → speaking; nada de áudio medido nem estados inventados.
  const orbMode = deriveOrbMode({
    voicePhase,
    ttsState: presenceVoice?.state ?? null,
    taskWorking: status === "working",
  });
  const circleTop = settled === "docked" ? rects.docked.y : rects.circle.y;

  return (
    <div
      className="lunmi-node"
      data-testid="lunmi-node"
      data-surface={settled}
      data-chat-appearance={preferences?.chat_appearance ?? "theme"}
      data-target={state.surface}
      data-phase={phase}
      data-side={side}
      data-tone={tone}
      data-status={status ?? "none"}
      data-voice={voicePhase ?? "none"}
      data-visible={phase === "settled" ? "true" : "false"}
      onPointerEnter={onPointerEnter}
      onKeyDown={onKeyDown}
    >
      {/* O corpo acompanha a aparência do chat; a shape nativa continua a única silhueta. */}
      <div className="lunmi-node-body" data-testid="node-body" aria-hidden="true" />

      {/*
        `inert` é um atributo do commit, não um efeito: sai na MESMA mutação que assenta a
        superfície, por isso o foco do composer (efeito do filho, que corre antes do efeito do
        pai) já encontra o atributo removido. Um `removeAttribute` em `useEffect` chegava tarde
        e o focus era bloqueado pelo ancestral inert.
      */}
      <div className="lunmi-node-content" data-testid="node-content" inert={!settledNow}>
        <button
          type="button"
          className="lunmi-node-layer lunmi-node-circle"
          data-testid="node-circle"
          hidden={layer !== "circle"}
          style={rectStyle(rects.circle.x, circleTop, rects.circle.width, rects.circle.height)}
          aria-label="Abrir o chat da Lunmi"
          title="Abrir o chat"
          disabled={!circleActive}
          tabIndex={circleActive ? 0 : -1}
          onClick={open}
        >
          <GalaxyOrb mode={orbMode} compact active={layer === "circle"} />
          <span className="lunmi-node-dot" data-testid="node-dot" aria-hidden="true" />
        </button>

        <button
          type="button"
          className="lunmi-node-layer lunmi-node-pill"
          data-testid="node-pill"
          data-action={pillNeedsMain ? "show-main" : "node-open"}
          hidden={layer !== "pill"}
          style={rectStyle(rects.pill.x, rects.pill.y, rects.pill.width, rects.pill.height)}
          aria-label={pillLabel}
          disabled={!pillActive}
          tabIndex={pillActive ? 0 : -1}
          onClick={pillAction}
        >
          <span className="lunmi-node-dot" aria-hidden="true" />
          <span className="lunmi-node-pill-text" data-testid="node-pill-text">{label}</span>
        </button>

        <div
          className="lunmi-node-layer lunmi-node-chat"
          data-testid="node-chat"
          hidden={layer !== "chat"}
          aria-hidden={!chatActive}
          style={rectStyle(rects.chat.x, rects.chat.y, rects.chat.width, rects.chat.height)}
        >
          <MiniChat onClose={close} focusKey={chatFocusKey} interactive={chatActive} />
        </div>
      </div>
    </div>
  );
}

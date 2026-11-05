/**
 * Superfície do LUNMI NODE V1 (`desktop/src/features/companion/NodeSurface.tsx`).
 *
 * A orb fica sempre visível e nunca é remontada. O resto sai dela como um balão de fala: uma
 * frase curta ao lado, o chat por baixo. A janela é transparente e o desenho é CSS, com bordas
 * suaves; o main define só a região de cliques (`setShape`), com os mesmos números de
 * `desktop/src/shared/node.ts`. A animação é CSS, sem rect nem IPC por frame.
 *
 * Clicar na orb abre o chat; com o chat aberto, fecha-o.
 *
 * O Node nunca mostra texto de conversa fora do chat: o único conteúdo livre é a etiqueta curta
 * do estado finito publicado pelo main (`status`/`tone`).
 */
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { nodeContentRect, nodeLabelFor, nodePillWidth, NODE, type NodeBounds } from "../../shared/node";
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

/** Para onde a cápsula leva: decisões ficam na principal; resultados abrem no chat. */
const SHOW_MAIN_STATUSES: ReadonlySet<NodeStatus> = new Set<NodeStatus>(["needs_confirm", "needs_input"]);

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
  const voicePhase = state?.voice_phase ?? null;
  // Saída de voz (TTS) é um facto do main; a etiqueta «A falar» só aparece quando ele é real.
  const label = nodeLabelFor(state?.status ?? null, voicePhase, presenceVoice?.state, state?.activity);
  const pillWidth = nodePillWidth(label);
  const rects = useMemo(
    () => ({
      circle: nodeContentRect("revealed", side, envelope),
      docked: nodeContentRect("docked", side, envelope),
      pill: nodeContentRect("pill", side, envelope, pillWidth),
      chat: nodeContentRect("chat", side, envelope),
    }),
    [side, envelope, pillWidth]
  );

  const open = useCallback(() => request("node-open"), [request]);
  const close = useCallback(() => request("node-close"), [request]);

  useEffect(() => {
    const onBlur = () => {
      // A tarefa continua: ao fechar, o balão curto mostra o que ela está a fazer.
      if (state?.surface !== "chat" || state.phase !== "settled" || state.voice_phase) return;
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
  // O destino abre o balão já; o que está a sair continua montado até assentar, para animar.
  const target = state ? SURFACE_LAYER[state.surface] : layer;
  const circleActive = settledNow && layer !== null;
  const pillActive = settledNow && layer === "pill";
  const chatActive = settledNow && layer === "chat";

  if (!available || !state) return null;

  const phase = state.phase;
  const tone = state.tone;
  const status = state.status;
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
  const circleTop = state.surface === "docked" ? rects.docked.y : rects.circle.y;
  const chatOpen = state.surface === "chat";

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
      onPointerEnter={onPointerEnter}
      onKeyDown={onKeyDown}
    >
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
          style={rectStyle(rects.circle.x, circleTop, rects.circle.width, rects.circle.height)}
          aria-label={chatOpen ? "Fechar o chat da Lunmi" : "Abrir o chat da Lunmi"}
          title={chatOpen ? "Fechar o chat" : "Abrir o chat"}
          disabled={!circleActive}
          tabIndex={circleActive ? 0 : -1}
          onClick={chatOpen ? close : open}
        >
          {/* Recolhida, só se vê um arco sem os olhos: não há nada para animar. */}
          <GalaxyOrb mode={orbMode} compact active={state.surface !== "docked"} />
        </button>

        <button
          type="button"
          className="lunmi-node-layer lunmi-node-pill"
          data-testid="node-pill"
          data-action={pillNeedsMain ? "show-main" : "node-open"}
          data-open={target === "pill"}
          hidden={layer !== "pill" && target !== "pill"}
          style={rectStyle(rects.pill.x, rects.pill.y, rects.pill.width, rects.pill.height)}
          aria-label={pillLabel}
          disabled={!pillActive}
          tabIndex={pillActive ? 0 : -1}
          onClick={pillAction}
        >
          <span className="lunmi-node-pill-text" data-testid="node-pill-text">{label}</span>
        </button>

        <div
          className="lunmi-node-layer lunmi-node-chat"
          data-testid="node-chat"
          data-open={target === "chat"}
          hidden={layer !== "chat" && target !== "chat"}
          aria-hidden={!chatActive}
          style={rectStyle(rects.chat.x, rects.chat.y, rects.chat.width, rects.chat.height)}
        >
          <MiniChat onClose={close} focusKey={chatFocusKey} interactive={chatActive} />
        </div>
      </div>
    </div>
  );
}

/**
 * Geometria e estado do LUNMI NODE N1 (`desktop/src/shared/node.ts`).
 *
 * Migração explícita de `companion-shape.test.ts` (região do orb de 112 px): N1 substitui o orb
 * por um envelope fixo de 420×520 e uma shape por superfície. Fixa a equivalência com os números
 * aprovados em N0 (envelope, arco de 20 px, cápsula 280×60, chat 380×480, raio 26, faixa 2) e as
 * decisões puras que o main usa: sequência de morph, interrupção a partir do rect desenhado e
 * custódia de hover sem `pointerleave`.
 */
import { describe, expect, it } from "vitest";
import {
  nodeActivityFor,
  nodeSurfaceAfterChat,
  nodeLabelFor,
  nodePassiveFromTaskPhase,
  nodePillWidth,
  COMPANION_PRESENCE_ACTIONS,
  isCompanionPresenceAction,
  isPresenceAction,
  NODE_ACTIONS,
  PRESENCE_ACTIONS,
  isNodeVisibleSurface,
  NODE,
  NODE_DURATIONS,
  nextNodeSurface,
  nodeContentRect,
  nodeEnvelopeBounds,
  nodeRetractionDecision,
  nodeShapeForSurface,
  nodeShapeRects,
  nodeSurfaceRect,
  nodeToneFor,
  nodeTransitionDuration,
  nodeVisibleDockedRect,
  pointInNodeCustody,
} from "../src/shared/node";

const AREA = { x: 0, y: 0, width: 1920, height: 1040 };
const SMALL = { x: 10, y: 20, width: 320, height: 300 };

function expectInsideWindow(
  rects: { x: number; y: number; width: number; height: number }[],
  width: number,
  height: number
) {
  expect(rects.length).toBeGreaterThan(0);
  for (const rect of rects) {
    expect(rect.x).toBeGreaterThanOrEqual(0);
    expect(rect.y).toBeGreaterThanOrEqual(0);
    expect(rect.width).toBeGreaterThan(0);
    expect(rect.height).toBeGreaterThan(0);
    expect(rect.x + rect.width).toBeLessThanOrEqual(width);
    expect(rect.y + rect.height).toBeLessThanOrEqual(height);
  }
}

describe("envelope N0", () => {
  it("420×570 encostado ao topo, margem 16 do lado pedido", () => {
    const right = nodeEnvelopeBounds("right", AREA);
    expect(right).toEqual({ x: 1920 - 16 - 420, y: 0, width: 420, height: 570 });
    const left = nodeEnvelopeBounds("left", AREA);
    expect(left).toEqual({ x: 16, y: 0, width: 420, height: 570 });
  });

  it("área útil pequena limita o envelope e nunca produz medidas negativas", () => {
    const bounds = nodeEnvelopeBounds("left", SMALL);
    expect(bounds.width).toBe(320);
    expect(bounds.height).toBe(300);
    expect(bounds.x).toBe(SMALL.x);
    expect(bounds.y).toBe(SMALL.y);
  });
});

describe("rects de conteúdo", () => {
  const envelope = nodeEnvelopeBounds("right", AREA);

  it("docked mostra só o arco de 20 px no topo do lado", () => {
    const rect = nodeContentRect("docked", "right", envelope);
    expect(rect).toEqual({ x: 420 - 52, y: -(52 - 20), width: 52, height: 52 });
    expect(nodeVisibleDockedRect("right", envelope)).toEqual({ x: 368, y: 0, width: 52, height: 20 });
  });

  it("a orb fica no canto; a frase sai ao lado dela e o chat por baixo", () => {
    expect(nodeContentRect("revealed", "right", envelope)).toEqual({ x: 368, y: 10, width: 52, height: 52 });
    expect(nodeContentRect("pill", "right", envelope, 120)).toEqual({ x: 236, y: 14, width: 120, height: 44 });
    expect(nodeContentRect("attention", "right", envelope, 120)).toEqual({ x: 236, y: 14, width: 120, height: 44 });
    expect(nodeContentRect("chat", "right", envelope)).toEqual({ x: 40, y: 74, width: 380, height: 480 });
    // Lado esquerdo espelha o rect, nunca o conteúdo.
    expect(nodeContentRect("revealed", "left", envelope).x).toBe(0);
    expect(nodeContentRect("pill", "left", envelope).x).toBe(64);
    expect(nodeContentRect("chat", "left", envelope).x).toBe(0);
  });

  it("um ecrã baixo encolhe o chat em vez de o tirar da janela", () => {
    const low = { x: 0, y: 0, width: 420, height: 400 };
    const chat = nodeContentRect("chat", "right", low);
    expect(chat.y + chat.height).toBe(400);
  });

  it("hidden não tem rect", () => {
    expect(nodeSurfaceRect("hidden", "right", envelope)).toBeNull();
    expect(isNodeVisibleSurface("hidden")).toBe(false);
  });
});

describe("shape nativa", () => {
  const envelope = nodeEnvelopeBounds("right", AREA);

  it("docked cobre o arco visível com a folga do desenho", () => {
    const rects = nodeShapeForSurface("docked", "right", envelope);
    expectInsideWindow(rects, 420, 570);
    expect(rects.reduce((sum, rect) => sum + rect.height, 0)).toBe(20 + NODE.region_outset);
    expect(rects.every((rect) => rect.y < 20 + NODE.region_outset)).toBe(true);
  });

  it("revealed cobre o círculo com folga, para o antialiasing não ser cortado", () => {
    const rects = nodeShapeForSurface("revealed", "right", envelope);
    expectInsideWindow(rects, 420, 570);
    expect(rects.reduce((sum, rect) => sum + rect.height, 0)).toBe(52 + 2 * NODE.region_outset);
    expect(rects[0].width).toBeLessThan(52);
    expect(rects[0].y).toBe(10 - NODE.region_outset);
  });

  it("com o balão aberto, a orb, o bico e o balão recebem cliques", () => {
    const inside = (rects: { x: number; y: number; width: number; height: number }[], x: number, y: number) =>
      rects.some((rect) => x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height);
    const pill = nodeShapeForSurface("pill", "right", envelope, 120);
    const chat = nodeShapeForSurface("chat", "right", envelope);
    expectInsideWindow(pill, 420, 570);
    expectInsideWindow(chat, 420, 570);
    expect(inside(pill, 394, 36)).toBe(true); // orb
    expect(inside(pill, 362, 36)).toBe(true); // bico
    expect(inside(pill, 300, 36)).toBe(true); // balão
    expect(inside(pill, 150, 36)).toBe(false); // ao lado da frase curta, o clique passa
    expect(inside(chat, 394, 36)).toBe(true);
    expect(inside(chat, 394, 68)).toBe(true);
    expect(inside(chat, 200, 300)).toBe(true);
  });

  it("nunca devolve região vazia (setShape([]) seria o rectângulo inteiro)", () => {
    // Envelope minúsculo força rects degenerados; a shape ainda tem de existir.
    const tiny = { x: 0, y: 0, width: 20, height: 10 };
    const rects = nodeShapeForSurface("chat", "right", tiny);
    expectInsideWindow(rects, 20, 10);
    expect(nodeShapeRects({ x: 0, y: 0, width: 0, height: 0 }, 420, 520).length).toBeGreaterThan(0);
  });

});

describe("transições", () => {
  it("um gesto humano abre o chat a partir de qualquer superfície visível", () => {
    expect(nextNodeSurface("docked", "node-open")).toBe("chat");
    expect(nextNodeSurface("revealed", "node-open")).toBe("chat");
    expect(nextNodeSurface("pill", "node-open")).toBe("chat");
    expect(nextNodeSurface("chat", "node-open")).toBe("chat");
    expect(nextNodeSurface("hidden", "node-open")).toBeNull();
    // Hover e recolha continuam separados do clique.
    expect(nextNodeSurface("docked", "node-reveal")).toBe("revealed");
    expect(nextNodeSurface("revealed", "node-close")).toBe("docked");
    expect(nextNodeSurface("chat", "node-close")).toBe("revealed");
    expect(nextNodeSurface("pill", "node-close")).toBe("revealed");
    expect(nextNodeSurface("revealed", "node-dock")).toBe("docked");
  });

  it("durações aprovadas e reduced-motion a zero", () => {
    expect(NODE_DURATIONS).toEqual({ docked: 180, revealed: 190, pill: 260, attention: 260, chat: 320 });
    expect(nodeTransitionDuration("chat")).toBe(320);
    expect(nodeTransitionDuration("chat", { reduced: true })).toBe(0);
    expect(nodeTransitionDuration("pill", { scale: 2 })).toBe(520);
  });

});

describe("balão à medida da frase", () => {
  it("frases curtas ficam estreitas e nenhuma passa do limite", () => {
    expect(nodePillWidth("A escutar…")).toBeLessThan(130);
    expect(nodePillWidth("A escutar…")).toBeGreaterThanOrEqual(NODE.pill_min_width);
    expect(nodePillWidth("Confirmar na principal")).toBeLessThan(NODE.pill_width);
    expect(nodePillWidth("x".repeat(200))).toBe(NODE.pill_width);
  });

  it("a voz vem antes do estado da tarefa", () => {
    expect(nodeLabelFor("working", "listening", null)).toBe("A escutar…");
    expect(nodeLabelFor("working", null, "playing")).toBe("A falar…");
    expect(nodeLabelFor("failed", null, null)).toBe("Falhou");
    expect(nodeLabelFor(null, null, null)).toBe("Lunmi");
  });
});

describe("o que ela está a fazer, sem chat aberto", () => {
  it("a frase diz o passo real, e a pesquisa é mais precisa do que pensar", () => {
    expect(nodeLabelFor("working", null, null, nodeActivityFor("Thinking", "idle"))).toBe("A pensar…");
    expect(nodeLabelFor("working", null, null, nodeActivityFor("Thinking", "searching"))).toBe("A pesquisar…");
    expect(nodeLabelFor("working", null, null, nodeActivityFor("Thinking", "reading"))).toBe("A ler fontes…");
    expect(nodeLabelFor("working", null, null, nodeActivityFor("Executing", "idle"))).toBe("A executar…");
    expect(nodeLabelFor("working", null, null, nodeActivityFor("Verifying", null))).toBe("A verificar…");
    expect(nodeLabelFor("working", null, null, null)).toBe("A trabalhar…");
    expect(nodeLabelFor("done", null, null, "thinking")).toBe("Concluída");
  });

  it("fechar o chat a meio mostra o trabalho; pendências e falhas continuam à vista", () => {
    expect(nodeSurfaceAfterChat("working", true, false)).toBe("pill");
    expect(nodeSurfaceAfterChat("working", false, false)).toBe("revealed");
    expect(nodeSurfaceAfterChat("needs_confirm", true, false)).toBe("attention");
    expect(nodeSurfaceAfterChat("failed", true, true)).toBe("revealed");
    expect(nodeSurfaceAfterChat("idle", true, false)).toBe("revealed");
  });
});

describe("bloqueio durante tarefas", () => {
  it("só a automação bloqueia o Node; conversa e pesquisa não", () => {
    expect(nodePassiveFromTaskPhase("running", false)).toBe(false);
    expect(nodePassiveFromTaskPhase("running", true)).toBe(true);
    expect(nodePassiveFromTaskPhase("finished", true)).toBe(false);
  });
});

describe("custódia de hover", () => {
  const envelope = nodeEnvelopeBounds("left", AREA);

  it("o arco original e o círculo revelado seguram a revelação", () => {
    expect(pointInNodeCustody({ x: 26, y: 8 }, "left", envelope)).toBe(true);
    expect(pointInNodeCustody({ x: 26, y: 30 }, "left", envelope)).toBe(true);
    expect(pointInNodeCustody({ x: 300, y: 300 }, "left", envelope)).toBe(false);
  });

  it("recolhe só em revealed, depois da histerese e sem pointerleave", () => {
    expect(nodeRetractionDecision({ surface: "chat", inside: false, elapsed: 5000 })).toBe("stop");
    expect(nodeRetractionDecision({ surface: "docked", inside: false, elapsed: 5000 })).toBe("stop");
    expect(nodeRetractionDecision({ surface: "revealed", passive: true, inside: false, elapsed: 5000 })).toBe("stop");
    expect(nodeRetractionDecision({ surface: "revealed", inside: true, elapsed: 5000 })).toBe("hold");
    expect(nodeRetractionDecision({ surface: "revealed", inside: false, elapsed: 599 })).toBe("wait");
    expect(nodeRetractionDecision({ surface: "revealed", inside: false, elapsed: 600 })).toBe("dock");
  });
});

describe("tom derivado do status", () => {
  it("mapeia os estados finitos sem inventar progresso", () => {
    expect(nodeToneFor(null)).toBe("neutral");
    expect(nodeToneFor("idle")).toBe("neutral");
    expect(nodeToneFor("working")).toBe("working");
    expect(nodeToneFor("needs_confirm")).toBe("needs");
    expect(nodeToneFor("needs_input")).toBe("needs");
    expect(nodeToneFor("done")).toBe("success");
    expect(nodeToneFor("not_verified")).toBe("error");
    expect(nodeToneFor("cancelled")).toBe("error");
    expect(nodeToneFor("failed")).toBe("error");
    expect(nodeToneFor("offline")).toBe("offline");
  });
});

describe("enum canónico de ações (main + preload + testes)", () => {
  it("inclui as ações do Node e mantém as antigas", () => {
    for (const action of ["show-main", "show-settings", "toggle-mini", "hide-mini", "stop-voice", "cancel-dictation", "quit"]) {
      expect(isPresenceAction(action)).toBe(true);
    }
    for (const action of NODE_ACTIONS) {
      expect(isPresenceAction(action)).toBe(true);
    }
    expect(PRESENCE_ACTIONS).toHaveLength(7 + NODE_ACTIONS.length);
  });

  it("recusa ações desconhecidas e comandos de tarefa", () => {
    for (const invalid of ["node-explode", "task.confirm", "app.shutdown", "", 42, null]) {
      expect(isPresenceAction(invalid)).toBe(false);
    }
  });

  it("companheiro pode tudo menos sair, e inclui as ações do Node", () => {
    expect(COMPANION_PRESENCE_ACTIONS).not.toContain("quit");
    expect(isCompanionPresenceAction("quit")).toBe(false);
    for (const action of NODE_ACTIONS) {
      expect(COMPANION_PRESENCE_ACTIONS).toContain(action);
    }
  });
});

it("constantes N0 permanecem as aprovadas", () => {
  expect(NODE.envelope_width).toBe(420);
  expect(NODE.envelope_height).toBe(570);
  expect(NODE.circle).toBe(52);
  expect(NODE.dock_visible).toBe(20);
  expect(NODE.margin).toBe(16);
  expect(NODE.reveal_top).toBe(10);
  expect(NODE.radius).toBe(26);
  expect(NODE.band_height).toBe(2);
  expect(NODE.hover_hysteresis_ms).toBe(600);
});

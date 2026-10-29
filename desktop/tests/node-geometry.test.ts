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
  beginNodeTransition,
  COMPANION_PRESENCE_ACTIONS,
  easeInOutCubic,
  isCompanionPresenceAction,
  isPresenceAction,
  NODE_ACTIONS,
  PRESENCE_ACTIONS,
  interpolateNodeRect,
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
  planNodeSequence,
  pointInNodeCustody,
  sampleNodeTransition,
  type NodeVisibleSurface,
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
  it("420×520 encostado ao topo, margem 16 do lado pedido", () => {
    const right = nodeEnvelopeBounds("right", AREA);
    expect(right).toEqual({ x: 1920 - 16 - 420, y: 0, width: 420, height: 520 });
    const left = nodeEnvelopeBounds("left", AREA);
    expect(left).toEqual({ x: 16, y: 0, width: 420, height: 520 });
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

  it("revealed fica a 10 px do topo; pill e chat encostam ao lado", () => {
    expect(nodeContentRect("revealed", "right", envelope)).toEqual({ x: 368, y: 10, width: 52, height: 52 });
    expect(nodeContentRect("pill", "right", envelope)).toEqual({ x: 420 - 280, y: 10, width: 280, height: 60 });
    expect(nodeContentRect("attention", "right", envelope)).toEqual({ x: 420 - 280, y: 10, width: 280, height: 60 });
    expect(nodeContentRect("chat", "right", envelope)).toEqual({ x: 420 - 380, y: 0, width: 380, height: 480 });
    // Lado esquerdo espelha o rect, nunca o conteúdo.
    expect(nodeContentRect("revealed", "left", envelope).x).toBe(0);
    expect(nodeContentRect("pill", "left", envelope).x).toBe(0);
    expect(nodeContentRect("chat", "left", envelope).x).toBe(0);
  });

  it("hidden não tem rect", () => {
    expect(nodeSurfaceRect("hidden", "right", envelope)).toBeNull();
    expect(isNodeVisibleSurface("hidden")).toBe(false);
  });
});

describe("shape nativa", () => {
  const envelope = nodeEnvelopeBounds("right", AREA);

  it("docked é um arco de 20 px, mais estreito nas pontas", () => {
    const rects = nodeShapeForSurface("docked", "right", envelope);
    expectInsideWindow(rects, 420, 520);
    expect(rects.reduce((sum, rect) => sum + rect.height, 0)).toBe(20);
    expect(rects[0].width).toBeLessThan(52);
    expect(rects.at(-1)!.width).toBeLessThan(52);
    expect(rects.every((rect) => rect.y < 20)).toBe(true);
  });

  it("revealed cobre o círculo de 52 px, largo no meio e estreito nos topos", () => {
    const rects = nodeShapeForSurface("revealed", "right", envelope);
    expectInsideWindow(rects, 420, 520);
    expect(rects.reduce((sum, rect) => sum + rect.height, 0)).toBe(52);
    const middle = rects[Math.floor(rects.length / 2)];
    expect(middle.width).toBe(52);
    expect(rects[0].width).toBeLessThan(52);
    expect(rects.at(-1)!.width).toBeLessThan(52);
    expect(rects[0].y).toBe(10);
  });

  it("pill e chat têm cantos redondos (topo estreito, meio inteiro)", () => {
    for (const surface of ["pill", "chat"] as NodeVisibleSurface[]) {
      const rects = nodeShapeForSurface(surface, "right", envelope);
      expectInsideWindow(rects, 420, 520);
      const top = rects[0];
      const middle = rects[Math.floor(rects.length / 2)];
      expect(top.width).toBeLessThan(middle.width);
      expect(rects.at(-1)!.width).toBeLessThan(middle.width);
    }
  });

  it("nunca devolve região vazia (setShape([]) seria o rectângulo inteiro)", () => {
    // Envelope minúsculo força rects degenerados; a shape ainda tem de existir.
    const tiny = { x: 0, y: 0, width: 20, height: 10 };
    const rects = nodeShapeForSurface("chat", "right", tiny);
    expectInsideWindow(rects, 20, 10);
    expect(nodeShapeRects({ x: 0, y: 0, width: 0, height: 0 }, 420, 520).length).toBeGreaterThan(0);
  });

  it("uma única cor/rect por faixa: sem buracos verticais", () => {
    const rects = nodeShapeForSurface("chat", "right", envelope);
    for (let i = 1; i < rects.length; i += 1) {
      expect(rects[i].y).toBe(rects[i - 1].y + rects[i - 1].height);
    }
  });
});

describe("morph N0", () => {
  it("chat é sempre horizontal (pill) e depois vertical, nunca um salto", () => {
    expect(planNodeSequence("docked", "chat")).toEqual(["pill", "chat"]);
    expect(planNodeSequence("revealed", "chat")).toEqual(["pill", "chat"]);
    expect(planNodeSequence("pill", "chat")).toEqual(["chat"]);
    expect(planNodeSequence("revealed", "pill")).toEqual(["pill"]);
    expect(planNodeSequence("chat", "revealed")).toEqual(["revealed"]);
    expect(planNodeSequence("chat", "chat")).toEqual([]);
  });

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

  it("interpolação começa e termina nos rects exactos", () => {
    const from = { x: 368, y: -32, width: 52, height: 52 };
    const to = { x: 140, y: 10, width: 280, height: 60 };
    expect(interpolateNodeRect(from, to, 0)).toEqual(from);
    expect(interpolateNodeRect(from, to, 1)).toEqual(to);
    const mid = interpolateNodeRect(from, to, 0.5);
    expect(mid.x).toBe(from.x + (to.x - from.x) * 0.5);
    expect(mid.width).toBe(from.width + (to.width - from.width) * 0.5);
    expect(easeInOutCubic(0)).toBe(0);
    expect(easeInOutCubic(0.5)).toBe(0.5);
    expect(easeInOutCubic(1)).toBe(1);
  });

  it("amostrar no fim assenta no destino e marca done", () => {
    const envelope = nodeEnvelopeBounds("right", AREA);
    const transition = beginNodeTransition({
      from: "revealed",
      to: "pill",
      fromRect: nodeContentRect("revealed", "right", envelope),
      toRect: nodeContentRect("pill", "right", envelope),
      durationMs: 260,
      startedAt: 1000,
    });
    expect(sampleNodeTransition(transition, 1000).progress).toBe(0);
    const done = sampleNodeTransition(transition, 1260);
    expect(done.done).toBe(true);
    expect(done.rect).toEqual(nodeContentRect("pill", "right", envelope));
    const mid = sampleNodeTransition(transition, 1130).rect;
    expect(mid.x).toBeLessThan(nodeContentRect("revealed", "right", envelope).x);
    expect(mid.x).toBeGreaterThan(done.rect.x);
  });

  it("um morph interrompido pode partir de um rect desenhado (não do repouso)", () => {
    const envelope = nodeEnvelopeBounds("right", AREA);
    const drawn = { x: 300, y: 4, width: 100, height: 54 };
    const transition = beginNodeTransition({
      from: "chat",
      to: "docked",
      fromRect: drawn,
      toRect: nodeContentRect("docked", "right", envelope),
      durationMs: 180,
      startedAt: 0,
    });
    expect(sampleNodeTransition(transition, 0).rect).toEqual(drawn);
    expect(sampleNodeTransition(transition, 180).rect).toEqual(nodeContentRect("docked", "right", envelope));
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
  expect(NODE.envelope_height).toBe(520);
  expect(NODE.circle).toBe(52);
  expect(NODE.dock_visible).toBe(20);
  expect(NODE.margin).toBe(16);
  expect(NODE.reveal_top).toBe(10);
  expect(NODE.radius).toBe(26);
  expect(NODE.band_height).toBe(2);
  expect(NODE.hover_hysteresis_ms).toBe(600);
  expect(NODE.morph_tick_ms).toBe(8);
});

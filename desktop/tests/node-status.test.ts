/**
 * Redução N2 do estado do Node (`desktop/src/shared/node.ts`).
 *
 * Usa a projeção REAL (`applyChatEvent` + `projectChat`), não DTOs inventados: prova que o
 * desfecho vem de `result.status` + `false_success` e não apenas da fase. Nenhum texto de
 * conversa, instrução ou progresso entra na derivação.
 */
import { describe, expect, it } from "vitest";
import {
  applyChatEvent,
  createChatProjection,
  projectChat,
  type ChatEventLike,
} from "../src/shared/chatProjection";
import {
  applyNodeDoneExpiry,
  deriveNodeStatus,
  effectiveNodeStatus,
  NODE_SUCCESS_VISIBLE_MS,
  nodeDoneExpiryMatches,
  nodeStatusInputFromProjection,
  nodeSurfaceForStatus,
  nodeToneFor,
} from "../src/shared/node";
import type { ChatProjectionDto, TaskResultDto } from "../src/shared/types";

const SESSION = "s1";

function result(overrides: Partial<TaskResultDto> = {}): TaskResultDto {
  return {
    task_id: "t1",
    status: "SUCCESS",
    message: "feito",
    false_success: false,
    ...overrides,
  };
}

function projection(events: ChatEventLike[]): ChatProjectionDto {
  let state = createChatProjection(SESSION);
  for (const event of events) {
    const outcome = applyChatEvent(state, event);
    if (outcome) state = outcome.state;
  }
  return projectChat(state);
}

function statusOf(events: ChatEventLike[], sessionPresent = true) {
  return deriveNodeStatus(nodeStatusInputFromProjection(projection(events), sessionPresent));
}

const started: ChatEventLike = {
  type: "task.started",
  payload: { session_id: SESSION, task_id: "t1", mode: "auto", instruction: "texto privado" },
};

describe("derivação sobre a projeção real", () => {
  it("sem sessão é offline, mesmo com tarefa a correr", () => {
    expect(statusOf([started], false)).toBe("offline");
  });

  it("sem tarefa é idle; running é working", () => {
    expect(statusOf([])).toBe("idle");
    expect(statusOf([started])).toBe("working");
  });

  it("pedidos pendentes vencem running e têm prioridade própria", () => {
    const confirm = projection([
      started,
      { type: "task.need_confirm", payload: { session_id: SESSION, task_id: "t1", request_id: "r1", message: "Confirma?" } },
    ]);
    expect(deriveNodeStatus(nodeStatusInputFromProjection(confirm, true))).toBe("needs_confirm");

    const input = projection([
      started,
      { type: "task.need_input", payload: { session_id: SESSION, task_id: "t1", request_id: "r2", kind: "choice", label: "Qual?" } },
    ]);
    expect(deriveNodeStatus(nodeStatusInputFromProjection(input, true))).toBe("needs_input");
  });

  it("SUCCESS com evidência é done", () => {
    expect(statusOf([started, { type: "task.finished", payload: { ...result(), session_id: SESSION } }])).toBe("done");
  });

  it("SUCCESS sem evidência é not_verified — fase finished não chega", () => {
    const chat = projection([
      started,
      { type: "task.finished", payload: { ...result({ false_success: true }), session_id: SESSION } },
    ]);
    expect(chat.task?.phase).toBe("finished");
    expect(deriveNodeStatus(nodeStatusInputFromProjection(chat, true))).toBe("not_verified");
  });

  it("NOT_VERIFIED é not_verified; FAILED é failed; CANCELLED é cancelled", () => {
    expect(statusOf([started, { type: "task.finished", payload: { ...result({ status: "NOT_VERIFIED" }), session_id: SESSION } }])).toBe("not_verified");
    expect(statusOf([started, { type: "task.finished", payload: { ...result({ status: "FAILED" }), session_id: SESSION } }])).toBe("failed");
    expect(statusOf([started, { type: "task.cancelled", payload: { session_id: SESSION, task_id: "t1", result: null } }])).toBe("cancelled");
  });

  it("erro da tarefa é failed mesmo sem result.status", () => {
    expect(statusOf([started, { type: "task.error", payload: { session_id: SESSION, task_id: "t1", code: "task_failed", message: "quebrou" } }])).toBe("failed");
  });

  it("status desconhecido numa fase terminal nunca vira done", () => {
    expect(statusOf([started, { type: "task.finished", payload: { ...result({ status: "ALGO_NOVO" }), session_id: SESSION } }])).toBe("not_verified");
  });
});

describe("casos puros e política de atividade", () => {
  const base = {
    session_present: true,
    phase: null,
    has_confirm: false,
    has_input: false,
    result_status: null,
    false_success: false,
    has_error: false,
  } as const;

  it("pendência vence fase terminal antiga", () => {
    expect(deriveNodeStatus({ ...base, phase: "finished", result_status: "SUCCESS", has_confirm: true })).toBe("needs_confirm");
    expect(deriveNodeStatus({ ...base, phase: "cancelled", has_input: true })).toBe("needs_input");
  });

  it("fase finished sem resultado é not_verified, nunca done", () => {
    expect(deriveNodeStatus({ ...base, phase: "finished" })).toBe("not_verified");
  });

  it("show_activity=false esconde só working/done; atenção continua visível", () => {
    expect(effectiveNodeStatus("working", false)).toBeNull();
    expect(effectiveNodeStatus("done", false)).toBeNull();
    for (const status of ["needs_confirm", "needs_input", "not_verified", "failed", "cancelled", "offline", "idle"] as const) {
      expect(effectiveNodeStatus(status, false)).toBe(status);
    }
    expect(effectiveNodeStatus("working", true)).toBe("working");
  });

  it("política working/done abre cápsula e preserva o chat humano", () => {
    for (const status of ["working", "done"] as const) {
      expect(nodeSurfaceForStatus({ status, show_activity: true, current: "docked", auto_pill: false })).toBe("pill");
      expect(nodeSurfaceForStatus({ status, show_activity: true, current: "revealed", auto_pill: false })).toBe("pill");
      expect(nodeSurfaceForStatus({ status, show_activity: true, current: "pill", auto_pill: true })).toBe("none");
      expect(nodeSurfaceForStatus({ status, show_activity: true, current: "chat", auto_pill: false })).toBe("none");
      // show_activity desligado não mostra atividade transitória.
      expect(nodeSurfaceForStatus({ status, show_activity: false, current: "docked", auto_pill: false })).toBe("none");
    }
  });

  it("pendências e erros vão para attention, venham de onde vierem", () => {
    for (const status of ["needs_confirm", "needs_input", "not_verified", "failed", "cancelled", "offline"] as const) {
      for (const current of ["docked", "revealed", "pill"] as const) {
        expect(nodeSurfaceForStatus({ status, show_activity: true, current, auto_pill: true })).toBe("attention");
      }
      expect(nodeSurfaceForStatus({ status, show_activity: true, current: "attention", auto_pill: false })).toBe("none");
      expect(nodeSurfaceForStatus({ status, show_activity: true, current: "chat", auto_pill: false })).toBe("none");
      // show_activity não esconde atenção.
      expect(nodeSurfaceForStatus({ status, show_activity: false, current: "docked", auto_pill: false })).toBe("attention");
    }
  });

  it("idle não força nada; cápsula automática recolhe e gesto explícito vence", () => {
    expect(nodeSurfaceForStatus({ status: "idle", show_activity: true, current: "docked", auto_pill: false })).toBe("none");
    expect(nodeSurfaceForStatus({ status: "idle", show_activity: true, current: "revealed", auto_pill: false })).toBe("none");
    expect(nodeSurfaceForStatus({ status: "idle", show_activity: true, current: "pill", auto_pill: true })).toBe("docked");
    expect(nodeSurfaceForStatus({ status: "idle", show_activity: true, current: "pill", auto_pill: false })).toBe("none");
    expect(nodeSurfaceForStatus({ status: "working", show_activity: true, current: "docked", auto_pill: false, suppressed: true })).toBe("none");
    // Chat nunca é fechado por política, nem com atenção pendente.
    expect(nodeSurfaceForStatus({ status: "needs_confirm", show_activity: true, current: "chat", auto_pill: false, suppressed: false })).toBe("none");
  });

  it("done expirado não ressuscita no resync da mesma sessão; tarefa/sessão nova limpa", () => {
    // Resync real: recarregar o companheiro emite session.ready com o MESMO session_id.
    const chat = projection([
      started,
      { type: "task.finished", payload: { ...result(), session_id: SESSION } },
    ]);
    const done = deriveNodeStatus(nodeStatusInputFromProjection(chat, true));
    expect(done).toBe("done");

    const expiry = { session_id: SESSION, task_id: "t1" };
    expect(applyNodeDoneExpiry(done, expiry, SESSION, "t1")).toBe("idle");
    expect(nodeDoneExpiryMatches(expiry, SESSION, "t1")).toBe(true);

    // Tarefa nova ou sessão nova: a conclusão volta a ser válida.
    expect(applyNodeDoneExpiry(done, expiry, SESSION, "t2")).toBe("done");
    expect(applyNodeDoneExpiry(done, expiry, "s2", "t1")).toBe("done");
    expect(nodeDoneExpiryMatches(expiry, SESSION, "t2")).toBe(false);
    expect(nodeDoneExpiryMatches(expiry, "s2", "t1")).toBe(false);
    expect(applyNodeDoneExpiry(done, null, SESSION, "t1")).toBe("done");
    // Outros estados nunca são tocados pela marca de expiração.
    expect(applyNodeDoneExpiry("working", expiry, SESSION, "t1")).toBe("working");
    expect(applyNodeDoneExpiry("not_verified", expiry, SESSION, "t1")).toBe("not_verified");
    expect(applyNodeDoneExpiry("idle", expiry, SESSION, "t1")).toBe("idle");
  });

  it("sucesso breve é 4 s e tons seguem o status", () => {
    expect(NODE_SUCCESS_VISIBLE_MS).toBe(4000);
    expect(nodeToneFor("done")).toBe("success");
    expect(nodeToneFor("not_verified")).toBe("error");
    expect(nodeToneFor(effectiveNodeStatus("working", false))).toBe("neutral");
  });
});

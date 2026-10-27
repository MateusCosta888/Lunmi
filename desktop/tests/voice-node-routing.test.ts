import { expect, it, vi } from "vitest";
import { BackgroundVoiceTurn, nodeVoicePhase } from "../electron/voice-node";
import { VoiceHoldCoordinator } from "../electron/voice-hotkey";

const phase = (kind: "pressed" | "released" | "cancelled", press_id = 1) =>
  ({ session_id: "s1", press_id, phase: kind, reason: "" as const });
function listening() {
  const turn = new BackgroundVoiceTurn();
  turn.hold(phase("pressed"), true);
  const start = turn.begin("voice.input.start", { mode: "hold" }, true);
  turn.input({ session_id: "s1", generation: 1, state: "starting" });
  turn.end("voice.input.start", { accepted: true, generation: 1 }, start);
  turn.input({ session_id: "s1", generation: 1, state: "listening" });
  return turn;
}
function ready(turn = listening()) {
  turn.hold(phase("released"), true);
  turn.input({ session_id: "s1", generation: 1, state: "ready" });
  const result = turn.begin("voice.input.result", { generation: 1 }, true);
  turn.input({ session_id: "s1", generation: 1, state: "idle" });
  turn.end("voice.input.result", { status: "ready", generation: 1, text: "teste local", auto_send: true }, result);
  return turn;
}
it("foreground preserves the old routing; background release never asks to open main", () => {
  for (const background of [false, true]) {
    const turn = new BackgroundVoiceTurn(), show = vi.fn();
    const coordinator = new VoiceHoldCoordinator({ getSessionId: () => "s1", isQuitting: () => false,
      publish: e => turn.hold(e, background), cancelCapture: vi.fn(),
      showMain: e => { if (!turn.backgroundRelease(e)) show(); } });
    coordinator.onPhase("pressed", ""); coordinator.onPhase("released", "");
    expect(show).toHaveBeenCalledTimes(background ? 0 : 1);
  }
});
it("presentation follows real listening and cannot regress after release", () => {
  const turn = listening(); expect(turn.phase).toBe("listening");
  turn.hold(phase("released"), true); expect(turn.phase).toBe("thinking");
  turn.input({ session_id: "s1", generation: 1, state: "listening" });
  expect(turn.phase).toBe("thinking");
});
it("only the matching accepted task completes, once, after result consumption", () => {
  const turn = ready(); expect(turn.phase).toBe("thinking");
  expect(turn.begin("chat.send", { text: "outro pedido" }, true)).toBeNull();
  const send = turn.begin("chat.send", { text: "teste local" }, true); expect(send).not.toBeNull();
  turn.taskStarted("task1"); turn.end("chat.send", { task_id: "task1" }, send);
  turn.finished("old-task", "SUCCESS"); expect(turn.takeCompleted()).toBe(false);
  turn.finished("task1", "SUCCESS"); expect(turn.takeCompleted()).toBe(true);
  expect(turn.takeCompleted()).toBe(false); expect(turn.phase).toBeNull();
});
it("a terminal before send ACK waits for actual acceptance", () => {
  const turn = ready(), send = turn.begin("chat.send", { text: "teste local" }, true);
  turn.taskStarted("task1"); turn.finished("task1", "SUCCESS"); expect(turn.takeCompleted()).toBe(false);
  turn.end("chat.send", { task_id: "task1" }, send); expect(turn.takeCompleted()).toBe(true);
});
it("a failed send cannot open the task that raced ahead of its refusal", () => {
  const turn = ready(), send = turn.begin("chat.send", { text: "teste local" }, true);
  turn.taskStarted("other-task"); turn.finished("other-task", "SUCCESS"); turn.failed(send);
  expect(turn.takeCompleted()).toBe(false); expect(turn.phase).toBe("error");
});
it("silence is an input failure, not indefinite thinking or permission to open main", () => {
  const turn = listening(); turn.input({ session_id: "s1", generation: 1, state: "idle" });
  expect(turn.phase).toBe("error"); turn.hold(phase("released"), true);
  expect(turn.backgroundRelease(phase("released"))).toBe(true); expect(turn.takeCompleted()).toBe(false);
});
it("cancellation and session reset invalidate every late ticket", () => {
  const turn = listening(), ticket = turn.begin("voice.input.result", { generation: 1 }, true);
  turn.hold(phase("cancelled"), true);
  turn.end("voice.input.result", { status: "ready", generation: 1, text: "old", auto_send: true }, ticket);
  expect(turn.phase).toBeNull(); expect(turn.takeCompleted()).toBe(false);
  turn.reset(); expect(turn.backgroundRelease(phase("released"))).toBe(false);
});
it("a new physical gesture rejects ACKs belonging to the previous gesture", () => {
  const turn = listening(), old = turn.begin("voice.input.start", { mode: "hold" }, true);
  turn.hold(phase("pressed", 2), true);
  turn.end("voice.input.start", { accepted: false, generation: 1 }, old);
  expect(turn.phase).toBe("starting");
});
it("a new generation invalidates the old turn; old session events do not", () => {
  const turn = listening(); turn.input({ session_id: "other-session", generation: 9, state: "error" });
  expect(turn.phase).toBe("listening");
  turn.input({ session_id: "s1", generation: 2, state: "idle" });
  expect(turn.phase).toBe("error");
});
it("manual dismissal wins over the later correlated response", () => {
  const turn = ready(), send = turn.begin("chat.send", { text: "teste local" }, true);
  turn.taskStarted("task1"); turn.end("chat.send", { task_id: "task1" }, send);
  turn.dismiss(); expect(turn.phase).toBeNull(); turn.finished("task1", "SUCCESS");
  expect(turn.takeCompleted()).toBe(false);
});
it("the companion cannot claim a voice result through its manual sender", () => {
  const turn = ready(); expect(turn.begin("chat.send", { text: "teste local" }, false)).toBeNull();
  expect(turn.begin("chat.send", { text: "teste local" }, true)).not.toBeNull();
});
it.each([{}, { auto_send: false }, { auto_send: true, truncated: true }])("an unapproved result cannot authorize automatic presentation %j", extra => {
  const turn = listening(); turn.hold(phase("released"), true);
  const result = turn.begin("voice.input.result", { generation: 1 }, true);
  turn.end("voice.input.result", { status: "ready", generation: 1, text: "teste", ...extra }, result);
  expect(turn.phase).toBe("error"); expect(turn.begin("chat.send", { text: "teste" }, true)).toBeNull();
});
it("busy start becomes a truthful input error with no background release fallback", () => {
  const turn = new BackgroundVoiceTurn(); turn.hold(phase("pressed"), true);
  const ticket = turn.begin("voice.input.start", { mode: "hold" }, true);
  turn.end("voice.input.start", { accepted: false, generation: 0 }, ticket);
  expect(turn.phase).toBe("error"); expect(turn.backgroundRelease(phase("released"))).toBe(true);
});
it("only SUCCESS/FAILED/NOT_VERIFIED terminals are presentable; cancellation clears", () => {
  for (const status of ["SUCCESS", "FAILED", "NOT_VERIFIED"]) {
    const turn = ready(), send = turn.begin("chat.send", { text: "teste local" }, true);
    turn.taskStarted("task1"); turn.end("chat.send", { task_id: "task1" }, send);
    turn.finished("task1", status);
    expect(turn.takeCompleted(), status).toBe(true);
    expect(turn.takeCompleted(), status).toBe(false);
  }
  for (const status of ["CANCELLED", "PARTIAL", ""]) {
    const turn = ready(), send = turn.begin("chat.send", { text: "teste local" }, true);
    turn.taskStarted("task1"); turn.end("chat.send", { task_id: "task1" }, send);
    turn.finished("task1", status);
    expect(turn.phase).toBeNull();
    expect(turn.takeCompleted()).toBe(false);
  }
});
it("task.cancelled ends the claimed turn before a late task.finished can present it", () => {
  const turn = ready(), send = turn.begin("chat.send", { text: "teste local" }, true);
  turn.taskStarted("task1"); turn.end("chat.send", { task_id: "task1" }, send);
  turn.cancelledTask("task1");
  expect(turn.phase).toBeNull();
  turn.finished("task1", "CANCELLED");
  expect(turn.takeCompleted()).toBe(false);
});
it("confirmation/input attention hides the voice label; show_activity absence does not", () => {
  expect(nodeVoicePhase("listening", null)).toBe("listening");
  expect(nodeVoicePhase("thinking", "working")).toBe("thinking");
  expect(nodeVoicePhase("thinking", "needs_confirm")).toBeNull();
  expect(nodeVoicePhase("error", "needs_input")).toBeNull();
  expect(nodeVoicePhase(null, "needs_confirm")).toBeNull();
});

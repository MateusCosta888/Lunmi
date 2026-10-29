import { afterEach, describe, expect, it, vi } from "vitest";
import { NodeTerminalNotice, type NodeTerminalNoticeSnapshot } from "../electron/node-terminal-notice";
import type { NodeStatus } from "../src/shared/types";

const failed: NodeTerminalNoticeSnapshot = {
  session_id: "s1", task_id: "t1", status: "failed", gesture: 3,
};

afterEach(() => { vi.useRealTimers(); });

describe("NodeTerminalNotice", () => {
  it("expires a failed task notice after six seconds without changing its status", () => {
    vi.useFakeTimers();
    const onExpire = vi.fn();
    const notice = new NodeTerminalNotice(onExpire);
    expect(notice.observe(failed)).toBe(true);
    vi.advanceTimersByTime(5999);
    expect(onExpire).not.toHaveBeenCalled();
    expect(notice.expiredFor(failed)).toBe(false);
    vi.advanceTimersByTime(1);
    expect(onExpire).toHaveBeenCalledExactlyOnceWith(failed);
    expect(notice.expiredFor(failed)).toBe(true);
    expect(failed.status).toBe("failed");
    notice.dispose();
  });

  it.each(["not_verified", "cancelled"] as const)("expires %s without changing the outcome", status => {
    vi.useFakeTimers();
    const onExpire = vi.fn();
    const notice = new NodeTerminalNotice(onExpire);
    const snapshot = { ...failed, status };
    notice.observe(snapshot);
    vi.advanceTimersByTime(6000);
    expect(onExpire).toHaveBeenCalledExactlyOnceWith(snapshot);
    expect(notice.expiredFor(snapshot)).toBe(true);
    expect(snapshot.status).toBe(status);
    notice.dispose();
  });

  it("does not rearm on resync or a new gesture, preserving the first gesture for main guards", () => {
    vi.useFakeTimers();
    const onExpire = vi.fn();
    const notice = new NodeTerminalNotice(onExpire);
    notice.observe(failed);
    vi.advanceTimersByTime(2000);
    expect(notice.observe({ ...failed, gesture: 99 })).toBe(false);
    vi.advanceTimersByTime(4000);
    expect(onExpire).toHaveBeenCalledExactlyOnceWith(failed);
    expect(notice.observe({ ...failed, gesture: 100 })).toBe(false);
    vi.advanceTimersByTime(10000);
    expect(onExpire).toHaveBeenCalledTimes(1);
    expect(notice.expiredFor(failed)).toBe(true);
    notice.dispose();
  });

  it.each([
    { ...failed, session_id: "s2" },
    { ...failed, task_id: "t2" },
    { ...failed, status: "not_verified" as const },
  ])("cancels the old deadline when identity changes to $session_id/$task_id/$status", next => {
    vi.useFakeTimers();
    const onExpire = vi.fn();
    const notice = new NodeTerminalNotice(onExpire);
    notice.observe(failed);
    vi.advanceTimersByTime(5000);
    expect(notice.observe(next)).toBe(true);
    vi.advanceTimersByTime(1000);
    expect(onExpire).not.toHaveBeenCalled();
    expect(notice.expiredFor(failed)).toBe(false);
    vi.advanceTimersByTime(5000);
    expect(onExpire).toHaveBeenCalledExactlyOnceWith(next);
    expect(notice.expiredFor(failed)).toBe(false);
    expect(notice.expiredFor(next)).toBe(true);
    notice.dispose();
  });

  it("clears expired identity for a new task and expires the new task independently", () => {
    vi.useFakeTimers();
    const onExpire = vi.fn();
    const notice = new NodeTerminalNotice(onExpire);
    notice.observe(failed);
    vi.advanceTimersByTime(6000);
    expect(notice.expiredFor(failed)).toBe(true);
    const next = { ...failed, task_id: "t2" };
    notice.observe(next);
    expect(notice.expiredFor(failed)).toBe(false);
    expect(notice.expiredFor(next)).toBe(false);
    vi.advanceTimersByTime(6000);
    expect(onExpire).toHaveBeenCalledTimes(2);
    expect(onExpire).toHaveBeenLastCalledWith(next);
    notice.dispose();
  });

  it.each<NodeStatus>(["needs_confirm", "needs_input", "offline", "working", "done", "idle"])(
    "cancels a terminal deadline and never expires %s", status => {
      vi.useFakeTimers();
      const onExpire = vi.fn();
      const notice = new NodeTerminalNotice(onExpire);
      notice.observe(failed);
      vi.advanceTimersByTime(3000);
      const pending = { ...failed, status };
      expect(notice.observe(pending)).toBe(true);
      vi.advanceTimersByTime(20000);
      expect(onExpire).not.toHaveBeenCalled();
      expect(notice.expiredFor(pending)).toBe(false);
      notice.dispose();
    });

  it.each([
    { ...failed, session_id: null }, { ...failed, task_id: null },
    { ...failed, session_id: "" }, { ...failed, task_id: "" },
    { ...failed, session_id: "  " }, { ...failed, task_id: "\t" },
  ])("never expires a notice without valid session/task IDs: $session_id/$task_id", snapshot => {
    vi.useFakeTimers();
    const onExpire = vi.fn();
    const notice = new NodeTerminalNotice(onExpire);
    notice.observe(snapshot);
    vi.advanceTimersByTime(20000);
    expect(onExpire).not.toHaveBeenCalled();
    expect(notice.expiredFor(snapshot)).toBe(false);
    notice.dispose();
  });

  it("captures an immutable copy before the caller mutates its object", () => {
    vi.useFakeTimers();
    const onExpire = vi.fn();
    const notice = new NodeTerminalNotice(onExpire);
    const mutable = { ...failed };
    notice.observe(mutable);
    mutable.session_id = "other-session";
    mutable.task_id = "other-task";
    mutable.status = "working";
    mutable.gesture = 99;
    vi.advanceTimersByTime(6000);
    expect(onExpire).toHaveBeenCalledExactlyOnceWith(failed);
    const received = onExpire.mock.calls[0][0];
    expect(received).not.toBe(mutable);
    expect(Object.isFrozen(received)).toBe(true);
    expect(notice.expiredFor(failed)).toBe(true);
    expect(notice.expiredFor(mutable)).toBe(false);
    notice.dispose();
  });

  it("dispose cancels pending callbacks, clears expired state, and permits a fresh observation", () => {
    vi.useFakeTimers();
    const onExpire = vi.fn();
    const notice = new NodeTerminalNotice(onExpire);
    notice.observe(failed);
    vi.advanceTimersByTime(3000);
    notice.dispose();
    vi.advanceTimersByTime(10000);
    expect(onExpire).not.toHaveBeenCalled();
    expect(notice.expiredFor(failed)).toBe(false);
    expect(notice.observe(failed)).toBe(true);
    vi.advanceTimersByTime(6000);
    expect(onExpire).toHaveBeenCalledTimes(1);
    notice.dispose();
    expect(notice.expiredFor(failed)).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });
});

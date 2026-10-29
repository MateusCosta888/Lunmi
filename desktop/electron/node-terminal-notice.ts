import type { NodeStatus } from "../src/shared/types";

export const NODE_TERMINAL_NOTICE_MS = 6000;

export type NodeTerminalNoticeSnapshot = Readonly<{
  session_id: string | null;
  task_id: string | null;
  status: NodeStatus;
  gesture: number;
}>;

type Identity = Pick<NodeTerminalNoticeSnapshot, "session_id" | "task_id" | "status">;

function sameIdentity(a: Identity | null, b: Identity): boolean {
  return a !== null && a.session_id === b.session_id && a.task_id === b.task_id && a.status === b.status;
}

/** Expires presentation only; the main process retains authority over surfaces and task state. */
export class NodeTerminalNotice {
  private current: NodeTerminalNoticeSnapshot | null = null;
  private expired: Identity | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly onExpire: (notice: NodeTerminalNoticeSnapshot) => void) {}

  observe(notice: NodeTerminalNoticeSnapshot): boolean {
    if (sameIdentity(this.current, notice)) return false;
    this.dispose();
    const captured = Object.freeze({ ...notice });
    this.current = captured;
    if (captured.session_id?.trim() && captured.task_id?.trim() &&
        (captured.status === "failed" || captured.status === "not_verified" || captured.status === "cancelled")) {
      this.timer = setTimeout(() => {
        this.timer = null;
        this.expired = captured;
        this.onExpire(captured);
      }, NODE_TERMINAL_NOTICE_MS);
    }
    return true;
  }

  expiredFor(notice: Identity): boolean {
    return sameIdentity(this.expired, notice);
  }

  dispose(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.current = null;
    this.expired = null;
  }
}

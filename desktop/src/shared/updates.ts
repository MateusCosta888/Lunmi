export type UpdatePhase = "disabled" | "idle" | "checking" | "available" | "downloading" | "ready" | "installing" | "error";

export interface UpdateState {
  phase: UpdatePhase;
  currentVersion: string;
  version?: string;
  percent?: number;
  /** Plain-text release notes ("what's new"), only while an update is offered. */
  notes?: string;
  error?: "unavailable" | "verification" | "busy" | "cancelled";
}

export interface UpdatesApi {
  get(): Promise<UpdateState>;
  check(): Promise<UpdateState>;
  install(): Promise<UpdateState>;
  onState(listener: (state: UpdateState) => void): () => void;
}

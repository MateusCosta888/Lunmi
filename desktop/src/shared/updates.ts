export type UpdatePhase = "disabled" | "idle" | "checking" | "downloading" | "ready" | "installing" | "error";

export interface UpdateState {
  phase: UpdatePhase;
  currentVersion: string;
  version?: string;
  percent?: number;
  error?: "unavailable" | "verification" | "busy" | "cancelled";
}

export interface UpdatesApi {
  get(): Promise<UpdateState>;
  check(): Promise<UpdateState>;
  install(): Promise<UpdateState>;
  onState(listener: (state: UpdateState) => void): () => void;
}

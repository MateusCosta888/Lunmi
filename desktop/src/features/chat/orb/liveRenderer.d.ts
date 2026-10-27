/** Motor Live Orb (JavaScript) — o React é dono do ciclo de vida. */
export interface LiveOrbHandle {
  setMode(mode: "idle" | "listening" | "thinking" | "speaking"): void;
  setActive(active: boolean): void;
  destroy(): void;
}
export function mountLiveOrb(
  canvas: HTMLCanvasElement,
  options?: { compact?: boolean; onUnavailable?: () => void }
): LiveOrbHandle;

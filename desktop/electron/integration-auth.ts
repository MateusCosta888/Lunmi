/** Browser authority belongs to an explicit connection attempt, never chat content. */
const AUTH_HOSTS = new Set(["connect.composio.dev", "login.composio.dev", "backend.composio.dev"]);
const AUTH_STATES = new Set(["connecting", "composio_connected", "gmail_connecting"]);

export function isComposioAuthUrl(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 8192 || /[\x00-\x20\x7f]/.test(value)) return false;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && AUTH_HOSTS.has(url.hostname)
      && !url.username && !url.password && (!url.port || url.port === "443");
  } catch {
    return false;
  }
}

export class IntegrationAuthGate {
  private session: string | null = null;
  private flow: string | null = null;
  private active = false;
  private readonly opened = new Set<string>();

  arm(sessionId: string): void {
    this.clear();
    this.session = sessionId;
    this.active = true;
  }

  clear(): void {
    this.session = null;
    this.flow = null;
    this.active = false;
    this.opened.clear();
  }

  observe(payload: Record<string, unknown>): void {
    const gmail = payload.gmail;
    if (!gmail || typeof gmail !== "object") return;
    const state = gmail as Record<string, unknown>;
    if (!AUTH_STATES.has(String(state.state))) {
      this.clear();
    } else if (this.active && typeof state.flow_id === "string" && state.flow_id.length <= 128) {
      // Bind once: a different flow cannot inherit a previous button click.
      if (this.flow === null) this.flow = state.flow_id;
      else if (this.flow !== state.flow_id) this.clear();
    }
  }

  accept(payload: Record<string, unknown>): payload is Record<string, unknown> & { url: string; flow_id: string } {
    if (!this.isCurrent(payload) || !isComposioAuthUrl(payload.url)) return false;
    if (this.opened.has(payload.url) || this.opened.size >= 2) return false;
    this.opened.add(payload.url);
    return true;
  }

  isCurrent(payload: Record<string, unknown>): boolean {
    return Boolean(this.active && this.flow && payload.session_id === this.session && payload.flow_id === this.flow);
  }
}

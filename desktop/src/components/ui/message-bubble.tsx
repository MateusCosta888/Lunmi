// Adapted from Framecn Message Bubble (21st.dev), MIT; see THIRD-PARTY-MOTION.md.
import type { ReactNode } from "react";

export function MessageBubble({ variant = "incoming", children, testId }: {
  variant?: "incoming" | "outgoing";
  children: ReactNode;
  testId?: string;
}) {
  const role = variant === "outgoing" ? "user" : "assistant";
  return <div className="lunmi-mini-message" data-role={role}>
    <p className="lunmi-mini-bubble" data-role={role} data-testid={testId}>{children}</p>
  </div>;
}

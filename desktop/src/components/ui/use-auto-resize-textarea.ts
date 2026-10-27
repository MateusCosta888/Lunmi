// Adapted from Kokonut UI (21st.dev), MIT; see THIRD-PARTY-MOTION.md.
import { useCallback, useLayoutEffect, useRef } from "react";

export function useAutoResizeTextarea(value: string, active: boolean) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const adjustHeight = useCallback(() => {
    const textarea = textareaRef.current;
    if (!textarea || !active) return;
    textarea.style.height = "44px";
    textarea.style.height = `${Math.max(44, Math.min(textarea.scrollHeight, 88))}px`;
  }, [active]);

  useLayoutEffect(() => { adjustHeight(); }, [value, adjustHeight]);
  useLayoutEffect(() => {
    window.addEventListener("resize", adjustHeight);
    return () => window.removeEventListener("resize", adjustHeight);
  }, [adjustHeight]);

  return textareaRef;
}

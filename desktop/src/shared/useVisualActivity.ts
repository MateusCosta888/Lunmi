import { useEffect, useState } from "react";

/** Decorative motion stops in hidden windows and follows the live system preference. */
export function useVisualActivity() {
  const [reduced, setReduced] = useState(() => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false);
  const [visible, setVisible] = useState(() => !document.hidden);
  useEffect(() => {
    const media = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    const preference = () => setReduced(media?.matches ?? false);
    const visibility = () => setVisible(!document.hidden);
    media?.addEventListener("change", preference);
    document.addEventListener("visibilitychange", visibility);
    // A change between the first render and this effect has no listener yet: resync once.
    preference();
    visibility();
    return () => {
      media?.removeEventListener("change", preference);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, []);
  return { reduced, visible };
}

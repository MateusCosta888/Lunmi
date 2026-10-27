// Vertical, non-draggable adaptation of React Bits RubberSegment's two-edge dilation/landing.
import React, { useLayoutEffect, useRef } from "react";
import { Icon, type IconName } from "./ui";
import { useVisualActivity } from "./useVisualActivity";

type Item = { id: string; label: string; icon: IconName };
export function RubberNavigation({ items, value, disabled, onChange, branch }: {
  items: readonly Item[]; value: string; disabled: boolean; onChange: (value: string) => void;
  branch?: { id: string; content: React.ReactNode; expanded: boolean; controls: string };
}): React.ReactElement {
  const track = useRef<HTMLElement>(null), thumb = useRef<HTMLSpanElement>(null);
  const animation = useRef<Animation | null>(null);
  const current = useRef(value); current.current = value;
  const { reduced, visible } = useVisualActivity();
  useLayoutEffect(() => {
    const nav = track.current, indicator = thumb.current;
    if (!nav || !indicator) return;
    let previous: { top: number; bottom: number } | null = null;
    const place = (move: boolean) => {
      const button = nav.querySelector<HTMLButtonElement>('.lunmi-nav-item[aria-current="page"]');
      if (!button) return;
      const navBox = nav.getBoundingClientRect(), box = button.getBoundingClientRect();
      const next = { top: box.top - navBox.top, bottom: navBox.bottom - box.bottom };
      const clip = (top: number, bottom: number) => `inset(${top}px 0px ${bottom}px 0px round 8px)`;
      // Sample the live shape before cancelling so rapid navigation has no visual jump.
      const from = getComputedStyle(indicator).clipPath;
      animation.current?.cancel(); indicator.style.clipPath = clip(next.top, next.bottom);
      if (move && previous && !reduced && visible && indicator.animate) {
        animation.current = indicator.animate([
          { clipPath: from, offset: 0 },
          { clipPath: clip(Math.min(previous.top, next.top), Math.min(previous.bottom, next.bottom)), offset: 0.33, easing: "cubic-bezier(.23,1,.32,1)" },
          { clipPath: clip(next.top, next.bottom), offset: 1 },
        ], { duration: 460, easing: "cubic-bezier(.23,1,.32,1)" });
      }
      previous = next;
    };
    place(false);
    const mutation = new MutationObserver(() => place(true));
    mutation.observe(nav, { subtree: true, attributes: true, attributeFilter: ["aria-current"] });
    const resize = new ResizeObserver(() => place(false)); resize.observe(nav);
    let alive = true; document.fonts?.ready.then(() => { if (alive) place(false); });
    return () => { alive = false; mutation.disconnect(); resize.disconnect(); animation.current?.cancel(); };
  }, [reduced, visible]);
  return <nav ref={track} className="lunmi-nav-group lunmi-rubber-nav" aria-label="Seções"
    onKeyDown={event => {
      if (disabled || !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      if (event.target !== event.currentTarget && !(event.target as HTMLElement).closest('.lunmi-nav-item')) return;
      event.preventDefault();
      const focused = (event.target as HTMLElement).closest<HTMLButtonElement>("button");
      const selected = items.findIndex(item => item.id === current.current);
      const index = focused ? Array.from(track.current?.querySelectorAll(".lunmi-nav-item") ?? []).indexOf(focused) : selected;
      const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      onChange(items[next].id); track.current?.querySelectorAll<HTMLButtonElement>(".lunmi-nav-item")[next]?.focus();
    }}>
    <span className="lunmi-rubber-thumb" ref={thumb} aria-hidden="true" />
    {items.map(item => <React.Fragment key={item.id}><button type="button" className="lunmi-nav-item"
      data-testid={`nav-${item.id}`} aria-label={item.label} title={item.label} aria-current={value === item.id ? "page" : undefined}
      aria-expanded={branch?.id === item.id ? branch.expanded : undefined}
      aria-controls={branch?.id === item.id ? branch.controls : undefined}
      disabled={disabled} onClick={() => onChange(item.id)}>
      <span className="lunmi-nav-icon"><Icon name={item.icon} /></span><span className="lunmi-nav-text">{item.label}</span>
    </button>{branch?.id === item.id && branch.content}</React.Fragment>)}
  </nav>;
}

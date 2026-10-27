// Adapted from beUI Agent Activity; see THIRD-PARTY-MOTION.md for attribution.
import { AnimatePresence, motion } from "motion/react";
import { Check, ChevronDown } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useVisualActivity } from "../../shared/useVisualActivity";

export interface AgentActivityItem {
  id: string;
  type: "step";
  label: string;
  status?: "pending" | "active" | "complete" | "error";
  meta?: string;
}

export interface AgentActivityProps {
  items: AgentActivityItem[];
  status?: "working" | "complete";
  contentType?: "step";
  animated?: boolean;
  pulsing?: boolean;
  activeLabel?: string;
  summary?: string;
  icon?: ReactNode;
  duration?: number;
  durationDisplay?: ReactNode;
  maxHeight?: number;
}

/** Displays only public activity received by its caller, with one bounded activity list. */
export function AgentActivity({ items, status = "working", contentType = "step", animated = true, pulsing = true, activeLabel = "Pensando…", summary = "Atividade encerrada", icon, duration = 0, durationDisplay, maxHeight = 208 }: AgentActivityProps) {
  const working = status === "working";
  const { reduced, visible } = useVisualActivity();
  const [open, setOpen] = useState(false);
  const [inView, setInView] = useState(true);
  const [capped, setCapped] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLUListElement>(null);
  const id = useId();
  const expanded = items.length > 0 && (working || open);
  const motionEnabled = animated && inView && visible && !reduced;

  useEffect(() => {
    const node = container.current;
    if (!node || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(([entry]) => setInView(entry.isIntersecting));
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => { if (!working) setOpen(false); }, [working]);

  useLayoutEffect(() => {
    const node = viewport.current;
    const list = content.current;
    if (!node || !list) return;
    const update = () => {
      setCapped(list.scrollHeight > maxHeight);
      if (working) node.scrollTop = node.scrollHeight;
    };
    update();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(update);
    observer.observe(list);
    return () => observer.disconnect();
  }, [items, maxHeight, working]);

  const heading = <>
    {icon && <span className="lunmi-activity-icon" aria-hidden="true">{icon}</span>}
    <span className="lunmi-activity-label">{working ? activeLabel : summary}</span>
    {durationDisplay ?? <span className="lunmi-activity-timer">{Math.max(0, Math.round(duration))} s</span>}
  </>;

  return <div ref={container} className="lunmi-activity" data-testid="task-progress" data-content={contentType}
    data-working={working && pulsing && motionEnabled} data-open={expanded} aria-busy={working}>
    {working || items.length === 0
      ? <div className="lunmi-activity-head">{heading}</div>
      : <button type="button" className="lunmi-activity-head lunmi-activity-trigger"
          aria-expanded={expanded} aria-controls={id} onClick={() => {
            setOpen(value => !value);
            if (!expanded && viewport.current) viewport.current.scrollTop = 0;
          }}>{heading}<ChevronDown className="lunmi-activity-chevron" size={14} aria-hidden="true" /></button>}
    <span className="lunmi-sr-only" role="status" aria-live="polite">{working ? activeLabel : summary}</span>
    <motion.div id={id} role="region" aria-label="Estados recebidos da tarefa"
      aria-hidden={!expanded} inert={!expanded ? true : undefined} initial={false}
      animate={{ height: expanded ? "auto" : 0, opacity: expanded ? 1 : 0 }}
      transition={motionEnabled ? { duration: 0.22, ease: [0.16, 1, 0.3, 1] } : { duration: 0 }}
      className="lunmi-activity-disclosure">
      <div ref={viewport} className="lunmi-activity-viewport" data-capped={capped} data-live={working}
        style={{ maxHeight }}>
        <motion.ul ref={content} className="lunmi-activity-list" role="list" initial={false}>
          <AnimatePresence initial={false}>
            {items.map((item, index) => <motion.li key={item.id} layout={motionEnabled ? "position" : false}
              className="lunmi-activity-row" data-current={working && index === items.length - 1}
              initial={motionEnabled ? { opacity: 0, y: 6 } : false}
              animate={{ opacity: 1, y: 0 }} exit={motionEnabled ? { opacity: 0, y: -3 } : undefined}
              transition={motionEnabled ? { duration: 0.2, ease: [0.16, 1, 0.3, 1] } : { duration: 0 }}>
              {item.status === "complete" ? <Check size={12} aria-hidden="true" /> : <span className="lunmi-activity-dot" aria-hidden="true" />}
              <span>{item.label}</span>
              {item.meta && <span className="lunmi-activity-meta">{item.meta}</span>}
            </motion.li>)}
          </AnimatePresence>
        </motion.ul>
      </div>
    </motion.div>
  </div>;
}

export default AgentActivity;

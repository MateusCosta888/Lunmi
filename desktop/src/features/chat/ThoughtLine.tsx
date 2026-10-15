// Adapted from React Bits ThoughtLine. Trace contains public task states, never model reasoning.
import React, { useEffect, useId, useRef, useState } from "react";
import type { TaskStateDto } from "../../shared/types";
import { BrandMark, Icon } from "../../shared/ui";
import { useVisualActivity } from "../../shared/useVisualActivity";

const LABELS: Record<string, string> = { Thinking: "Pensando", Executing: "Executando", Verifying: "Verificando", Planning: "Planejando", Running: "Trabalhando", Completed: "Encerrada", Failed: "Falhou", Cancelled: "Cancelada" };
export function ThoughtLine({ task }: { task: TaskStateDto }): React.ReactElement {
  const { reduced, visible } = useVisualActivity();
  const running = task.phase === "running";
  const busy = running || task.phase === "awaiting_confirm" || task.phase === "awaiting_input";
  const label = task.phase === "awaiting_confirm" ? "Aguardando sua confirmação" : task.phase === "awaiting_input" ? "Aguardando sua resposta"
    : running ? LABELS[task.progress] || task.progress || (task.mode === "converse" ? "Preparando resposta" : "Preparando tarefa") : task.phase === "cancelled" ? "Atividade cancelada" : task.phase === "failed" ? "Atividade encerrada com falha" : "Atividade encerrada";
  const [steps, setSteps] = useState<string[]>([]);
  const [open, setOpen] = useState(true);
  const timer = useRef<HTMLSpanElement>(null), started = useRef(performance.now());
  const ended = useRef<number | null>(null);
  const traceId = useId();
  const hasTrace = steps.length > 0;
  const Header = hasTrace ? "button" : "div";
  useEffect(() => {
    if (task.progress) setSteps(previous => previous.at(-1) === task.progress ? previous : [...previous, task.progress].slice(-8));
  }, [task.progress]);
  useEffect(() => { if (!busy) setOpen(false); }, [busy]);
  useEffect(() => {
    if (!busy && ended.current === null) ended.current = performance.now();
    const paint = () => {
      const seconds = !busy && typeof task.result?.duration_ms === "number" ? task.result.duration_ms / 1000 : ((ended.current ?? performance.now()) - started.current) / 1000;
      if (timer.current) timer.current.textContent = `${Math.max(0, seconds).toFixed(1).replace(".", ",")} s`;
    };
    paint();
    if (!busy || !visible) return;
    const id = window.setInterval(paint, 100);
    return () => window.clearInterval(id);
  }, [busy, visible, task.result]);
  return <div className="lunmi-thought-line" data-testid="task-progress" data-working={running && !reduced && visible} data-open={open}>
    <Header type={hasTrace ? "button" : undefined} className="lunmi-thought-head" aria-expanded={hasTrace ? open : undefined} aria-controls={hasTrace ? traceId : undefined}
      onClick={hasTrace ? () => setOpen(value => !value) : undefined}>
      <span className="lunmi-thought-glyph" aria-hidden="true"><BrandMark size={20} /></span>
      <span className="lunmi-thought-label">{label}</span>
      <span ref={timer} className="lunmi-thought-timer" aria-hidden="true">0,0 s</span>
      {hasTrace && <Icon name="chevron" size={13} />}
    </Header>
    <span className="lunmi-sr-only" role="status">{label}</span>
    {hasTrace && <div id={traceId} className="lunmi-thought-trace" hidden={!open}>
      <p>Estados recebidos da tarefa</p>
      <ul>{steps.map((step, index) => <li key={`${index}-${step}`}>{LABELS[step] || step}</li>)}</ul>
    </div>}
  </div>;
}

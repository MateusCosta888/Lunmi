import React, { useEffect, useRef, useState } from "react";
import { AgentActivity, type AgentActivityItem } from "../../components/ui/agent-activity";
import type { TaskStateDto } from "../../shared/types";
import { useVisualActivity } from "../../shared/useVisualActivity";

const LABELS: Record<string, string> = { Thinking: "Pensando", Executing: "Executando", Verifying: "Verificando", Planning: "Planejando", Running: "Trabalhando", Completed: "Encerrada", Failed: "Falhou", Cancelled: "Cancelada" };

/** Adapts the public task states; it never displays private model reasoning. */
export function ThoughtLine({ task }: { task: TaskStateDto }): React.ReactElement {
  const { reduced, visible } = useVisualActivity();
  const running = task.phase === "running";
  const busy = running || task.phase === "awaiting_confirm" || task.phase === "awaiting_input";
  const label = task.phase === "awaiting_confirm" ? "Aguardando sua confirmação" : task.phase === "awaiting_input" ? "Aguardando sua resposta"
    : running ? LABELS[task.progress] || task.progress || (task.mode === "converse" ? "Preparando resposta" : "Preparando tarefa") : task.phase === "cancelled" ? "Atividade cancelada" : task.phase === "failed" ? "Atividade encerrada com falha" : "Atividade encerrada";
  const [steps, setSteps] = useState<AgentActivityItem[]>(() =>
    !task.progress && running ? [{ id: "initial", type: "step", label }] : []);
  const sequence = useRef(0);
  const timer = useRef<HTMLSpanElement>(null), started = useRef(performance.now());
  const ended = useRef<number | null>(null);

  useEffect(() => {
    if (!task.progress) return;
    const progress = task.progress;
    const item: AgentActivityItem = { id: String(sequence.current++), type: "step", label: LABELS[progress] || progress };
    setSteps(previous => previous.at(-1)?.label === item.label ? previous : [...previous, item].slice(-8));
  }, [task.progress]);

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

  return <AgentActivity items={steps} status={busy ? "working" : "complete"} pulsing={running} animated={!reduced && visible}
    activeLabel={running ? "Pensando…" : label} summary={label}
    durationDisplay={<span ref={timer} hidden={busy} className="lunmi-activity-timer" aria-hidden="true">0,0 s</span>} />;
}

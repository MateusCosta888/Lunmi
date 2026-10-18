import { expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { RubberNavigation } from "../src/shared/RubberNavigation";
import { ThoughtLine } from "../src/features/chat/ThoughtLine";
import { Starfield } from "../src/shared/Starfield";
import type { TaskStateDto } from "../src/shared/types";

const task: TaskStateDto = { task_id: "real-state", mode: "execute", phase: "running", instruction: "teste", progress: "Thinking", result: null, error: null };

it("mostra só estados recebidos, não inventa etapas ou sucesso, e recolhe depois da falha", () => {
  const { rerender } = render(<ThoughtLine task={task} />);
  expect(screen.getByRole("status").textContent).toBe("Pensando");
  expect(screen.queryByText("Verificando")).toBeNull();
  rerender(<ThoughtLine task={{ ...task, progress: "Executing" }} />);
  expect(screen.getByRole("status").textContent).toBe("Executando");
  const trace = screen.getByRole("list"); expect(trace.textContent).toBe("PensandoExecutando");
  rerender(<ThoughtLine task={{ ...task, phase: "failed", progress: "Failed" }} />);
  expect(screen.getByRole("status").textContent).toBe("Atividade encerrada com falha");
  expect(screen.getByRole("button").getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(screen.getByRole("button"));
  expect(screen.getByRole("list").textContent).toContain("Falhou");
  expect(screen.queryByText("Sucesso verificado")).toBeNull();
});

it("espera por consentimento sem simular pensamento; cronômetro encerra e limpa timers", () => {
  vi.useFakeTimers();
  const { rerender, unmount } = render(<ThoughtLine task={{ ...task, phase: "awaiting_confirm" }} />);
  expect(screen.getByTestId("task-progress").dataset.working).toBe("false");
  expect(screen.getByRole("status").textContent).toBe("Aguardando sua confirmação");
  act(() => vi.advanceTimersByTime(1200));
  rerender(<ThoughtLine task={{ ...task, phase: "finished", result: { duration_ms: 2500, status: "SUCCESS" } as TaskStateDto["result"] }} />);
  expect(screen.getByText("2,5 s")).toBeTruthy();
  expect(vi.getTimerCount()).toBe(0);
  unmount(); expect(vi.getTimerCount()).toBe(0); vi.useRealTimers();
});

it("navegação mantém botões reais, clique assistivo, Enter nativo e setas sem executar tarefa", () => {
  const change = vi.fn();
  const { rerender } = render(<RubberNavigation items={[{ id: "chat", label: "Conversa", icon: "chat" }, { id: "memory", label: "Memória", icon: "memory" }]} value="chat" disabled={false} onChange={change} />);
  fireEvent.click(screen.getByRole("button", { name: "Memória" }), { detail: 0 });
  expect(change).toHaveBeenLastCalledWith("memory");
  fireEvent.keyDown(screen.getByRole("navigation"), { key: "End" });
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Memória" }));
  rerender(<RubberNavigation items={[{ id: "chat", label: "Conversa", icon: "chat" }]} value="chat" disabled onChange={change} />);
  change.mockClear(); fireEvent.click(screen.getByRole("button")); fireEvent.keyDown(screen.getByRole("navigation"), { key: "Home" });
  expect(change).not.toHaveBeenCalled();
});

it("movimento reduzido deixa fundo estático e sem loop RAF", () => {
  const raf = vi.spyOn(window, "requestAnimationFrame");
  const { rerender, unmount } = render(<Starfield paused={false} />);
  expect(screen.getByTestId("starfield").dataset.paused).toBe("true");
  expect(screen.getByTestId("starfield").getAttribute("aria-hidden")).toBe("true");
  expect(raf).not.toHaveBeenCalled();
  rerender(<Starfield paused />); unmount(); expect(raf).not.toHaveBeenCalled();
});

it("erro sem duração congela o relógio mesmo depois de ocultar/restaurar a janela", () => {
  let now = 1000; vi.spyOn(performance, "now").mockImplementation(() => now);
  let hidden = false; vi.spyOn(document, "hidden", "get").mockImplementation(() => hidden);
  const { rerender } = render(<ThoughtLine task={task} />);
  now = 3500; rerender(<ThoughtLine task={{ ...task, phase: "failed" }} />);
  expect(screen.getByText("2,5 s")).toBeTruthy();
  now = 20000; hidden = true; fireEvent(document, new Event("visibilitychange"));
  hidden = false; fireEvent(document, new Event("visibilitychange"));
  expect(screen.getByText("2,5 s")).toBeTruthy();
});

it("setas partem do botão focado por Tab, não da rota selecionada", () => {
  const change = vi.fn();
  render(<RubberNavigation items={[{ id: "chat", label: "Conversa", icon: "chat" }, { id: "memory", label: "Memória", icon: "memory" }, { id: "brain", label: "Brain", icon: "brain" }]} value="chat" disabled={false} onChange={change} />);
  const memory = screen.getByRole("button", { name: "Memória" }); memory.focus();
  fireEvent.keyDown(memory, { key: "ArrowDown" });
  expect(change).toHaveBeenLastCalledWith("brain");
});

it("conversa sem eventos de progresso não inventa etapas nem oferece expansão vazia", () => {
  render(<ThoughtLine task={{ ...task, mode: "converse", progress: "" }} />);
  expect(screen.getByRole("status").textContent).toBe("Preparando resposta");
  expect(screen.queryByRole("button")).toBeNull();
  expect(screen.queryByRole("list")).toBeNull();
});

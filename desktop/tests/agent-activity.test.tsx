import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import AgentActivity, { type AgentActivityItem } from "../src/components/ui/agent-activity";

it("shows supplied steps, respects reduced motion, and collapses on completion", () => {
  const items: AgentActivityItem[] = [
    { id: "first", type: "step", label: "Reading the brief", status: "complete" },
    { id: "next", type: "step", label: "Verifying", status: "active", meta: "2 steps" },
  ];
  const { rerender } = render(<AgentActivity items={items} status="working" contentType="step" duration={4.2} />);
  expect(screen.getByRole("status").textContent).toBe("Pensando…");
  expect(screen.getByRole("list").textContent).toContain("Verifying");
  expect(screen.getByText("2 steps")).toBeTruthy();
  expect(screen.getByTestId("task-progress").dataset.working).toBe("false");
  rerender(<AgentActivity items={items} status="complete" duration={4.2} />);
  const button = screen.getByRole("button");
  expect(button.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("list")).toBeNull();
  fireEvent.click(button);
  expect(button.getAttribute("aria-expanded")).toBe("true");
  expect(screen.getByRole("list").textContent).toContain("Reading the brief");
});

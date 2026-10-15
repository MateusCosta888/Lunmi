import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { SafeMarkdown } from "../src/shared/markdown";

it("renderiza títulos e listas numeradas mantendo HTML como texto", () => {
  render(<SafeMarkdown text={"## Organização\n4. Primeiro\n5. Segundo\n<script>alert(1)</script>"} />);
  expect(screen.getByRole("heading", { name: "Organização" })).toBeTruthy();
  expect(document.querySelector("ol")?.getAttribute("start")).toBe("4");
  expect(screen.getAllByRole("listitem")).toHaveLength(2);
  expect(document.querySelector("script")).toBeNull();
});

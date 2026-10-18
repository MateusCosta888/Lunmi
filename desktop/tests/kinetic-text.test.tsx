/**
 * Kinetic Text (`desktop/tests/kinetic-text.test.tsx`, marca central 2026-10-16).
 *
 * O jsdom não calcula `:hover` nem executa a transição de peso, então o valor destes testes é o
 * contrato do componente: um span por caractere com espaço não separável, o rótulo completo
 * visível só para leitores de tela, a cor herdada por `currentColor` e o elemento configurável.
 * A animação de fato (peso 300 → 900 e as letras vizinhas) é medida na inspeção Electron.
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { KineticText } from "../src/shared/KineticText";

describe("KineticText — marca central", () => {
  it("desenha um span por caractere e mantém o rótulo completo para leitores de tela", () => {
    render(<KineticText text="Lunmi" data-testid="kinetic" />);
    const node = screen.getByTestId("kinetic");
    expect(node.getAttribute("data-kinetic-engine")).toBe("magicui-kinetic-text");
    const letters = node.querySelectorAll<HTMLElement>(".lunmi-kinetic-letter");
    expect(letters).toHaveLength(5);
    expect(Array.from(letters, letter => letter.textContent).join("")).toBe("Lunmi");
    // As letras são decorativas; o texto acessível vem do rótulo oculto, uma única vez.
    expect(Array.from(letters).every(letter => letter.getAttribute("aria-hidden") === "true")).toBe(true);
    expect(node.querySelector(".lunmi-kinetic-sr")?.textContent).toBe("Lunmi");
  });

  it("troca o espaço por espaço não separável para o efeito não quebrar entre palavras", () => {
    const { container } = render(<KineticText text="Lu mi" data-testid="kinetic" />);
    const letters = container.querySelectorAll<HTMLElement>(".lunmi-kinetic-letter");
    expect(Array.from(letters, letter => letter.textContent).join("")).toBe("Lu\u00A0mi");
    expect(container.textContent).toBe("Lu\u00A0miLu mi");
  });

  it("herda a cor do tema por currentColor e aceita outro elemento e estilo", () => {
    render(
      <span style={{ color: "currentColor" }}>
        <KineticText text="Lunmi" as="h1" data-testid="kinetic" style={{ fontSize: "32px" }} />
      </span>
    );
    const node = screen.getByTestId("kinetic");
    expect(node.tagName).toBe("H1");
    expect(node.style.fontSize).toBe("32px");
    // As fórmulas do upstream continuam disponíveis como custom properties.
    expect(node.style.getPropertyValue("--hover-padding")).toBe("calc(1em / 12)");
    expect(node.style.getPropertyValue("--text-stroke-width")).toBe("calc(1em * 125 / 6000)");
  });
});

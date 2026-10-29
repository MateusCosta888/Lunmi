import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fake = vi.hoisted(() => ({ render: vi.fn(), app: vi.fn(), companion: vi.fn() }));
vi.mock("react-dom/client", () => ({ createRoot: () => ({ render: fake.render }) }));
vi.mock("../src/shared/themeDom", () => ({ applyTheme: vi.fn() }));

beforeEach(() => {
  vi.resetModules();
  fake.render.mockClear(); fake.app.mockClear(); fake.companion.mockClear();
  document.body.innerHTML = '<div id="root"></div>';
  window.history.replaceState({}, "", "/");
  delete document.documentElement.dataset.companion;
  vi.doMock("../src/App", () => { fake.app(); return { App: () => <p>Principal</p> }; });
  vi.doMock("../src/features/companion/CompanionApp", () => {
    fake.companion(); return { CompanionApp: () => <p>Node</p> };
  });
});

afterEach(() => {
  vi.doUnmock("../src/App"); vi.doUnmock("../src/features/companion/CompanionApp");
  window.history.replaceState({}, "", "/");
  delete document.documentElement.dataset.companion;
});

describe("renderer por janela", () => {
  it("Node não carrega a shell principal nem os painéis de configuração", async () => {
    window.history.replaceState({}, "", "/?window=companion");
    await import("../src/main");
    await vi.dynamicImportSettled();
    expect(fake.companion).toHaveBeenCalledTimes(1);
    expect(fake.app).not.toHaveBeenCalled();
    expect(fake.render).toHaveBeenCalledTimes(1);
    expect(document.documentElement.dataset.companion).toBe("companion");
  });

  it("a principal não carrega o Node", async () => {
    await import("../src/main");
    await vi.dynamicImportSettled();
    expect(fake.app).toHaveBeenCalledTimes(1);
    expect(fake.companion).not.toHaveBeenCalled();
    expect(fake.render).toHaveBeenCalledTimes(1);
  });

  it("falha de carregamento mostra recuperação sem expor erro interno", async () => {
    vi.doMock("../src/App", () => { throw new Error("Private loader path"); });
    await import("../src/main");
    await vi.dynamicImportSettled();
    const root = fake.render.mock.calls.at(-1)?.[0] as React.ReactElement<{ role?: string; children?: string }>;
    expect(root.props.role).toBe("alert");
    expect(root.props.children).toContain("Reinicia a Lunmi");
    expect(root.props.children).not.toContain("Private loader path");
  });
});

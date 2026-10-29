/**
 * Testes das preferências da camada confiável (`desktop/tests/preferences.test.ts`, reparo F2-3).
 *
 * A revisão F2 apontou que a tela oferecia escala mas nada aplicava o valor: era persistência de
 * JSON vendida como escala. Estes testes cobrem a parte pura do reparo — fator de zoom, leitura
 * tolerante e retângulo utilizável — e a leitura real de arquivo (salvar/reabrir).
 *
 * O efeito observável na janela (zoom aplicado de fato) é verificado fora daqui, contra a janela
 * Electron real; um teste de jsdom não provaria isso.
 */
import { describe, expect, it } from "vitest";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import {
  DEFAULT_PREFERENCES,
  MAX_PREFS_BYTES,
  MIN_VISIBLE,
  parsePreferences,
  readPreferencesFile,
  usableBounds,
  zoomFactorFor,
} from "../electron/preferences";

const FULL_HD = { x: 0, y: 0, width: 1920, height: 1040 };

describe("zoomFactorFor", () => {
  it("converte as quatro escalas oferecidas", () => {
    expect(zoomFactorFor(100)).toBe(1);
    expect(zoomFactorFor(125)).toBe(1.25);
    expect(zoomFactorFor(150)).toBe(1.5);
    expect(zoomFactorFor(200)).toBe(2);
  });

  it("recusa qualquer outro valor, caindo em 1 em vez de inventar zoom", () => {
    expect(zoomFactorFor(0)).toBe(1);
    expect(zoomFactorFor(133)).toBe(1);
    expect(zoomFactorFor(-50)).toBe(1);
    expect(zoomFactorFor("125")).toBe(1);
    expect(zoomFactorFor(null)).toBe(1);
    expect(zoomFactorFor(undefined)).toBe(1);
    expect(zoomFactorFor(Number.NaN)).toBe(1);
  });
});

describe("parsePreferences", () => {
  it("aceita um arquivo válido", () => {
    expect(parsePreferences({ scale_pct: 150, window: [10, 20, 1280, 800] })).toEqual({
      theme: "celestial",
      reduceMotion: false,
      scalePct: 150,
      window: { x: 10, y: 20, width: 1280, height: 800 },
    });
  });

  it("só reduz movimento quando a Lunmi o pede; o Windows não decide", () => {
    expect(parsePreferences({}).reduceMotion).toBe(false);
    expect(parsePreferences({ reduce_motion: true }).reduceMotion).toBe(true);
    expect(parsePreferences({ reduce_motion: "true" }).reduceMotion).toBe(false);
  });

  it("devolve o padrão para escala inválida e para janela fora dos limites", () => {
    expect(parsePreferences({ scale_pct: 133 }).scalePct).toBe(100);
    expect(parsePreferences({ scale_pct: "125" }).scalePct).toBe(100);
    // Menor que a borda mínima, maior que o teto, ou com tipos errados.
    expect(parsePreferences({ window: [0, 0, 200, 800] }).window).toBeNull();
    expect(parsePreferences({ window: [0, 0, 99999, 800] }).window).toBeNull();
    expect(parsePreferences({ window: [0, 0, 1280] }).window).toBeNull();
    expect(parsePreferences({ window: ["0", 0, 1280, 800] }).window).toBeNull();
    expect(parsePreferences({ window: [0.5, 0, 1280, 800] }).window).toBeNull();
  });

  it("nunca levanta com entrada absurda", () => {
    expect(parsePreferences(null)).toEqual(DEFAULT_PREFERENCES);
    expect(parsePreferences([])).toEqual(DEFAULT_PREFERENCES);
    expect(parsePreferences("x")).toEqual(DEFAULT_PREFERENCES);
    expect(parsePreferences({})).toEqual(DEFAULT_PREFERENCES);
  });
});

describe("readPreferencesFile (salvar/reabrir)", () => {
  it("relê o que foi gravado, em diretório próprio", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "lunmi-prefs-"));
    try {
      fs.writeFileSync(
        path.join(dir, "lunmi-ui.json"),
        JSON.stringify({ window: [100, 100, 1280, 800], scale_pct: 125, reduce_motion: true, actions: [] }),
        "utf8"
      );
      expect(readPreferencesFile(dir)).toEqual({
        theme: "celestial",
        reduceMotion: true,
        scalePct: 125,
        window: { x: 100, y: 100, width: 1280, height: 800 },
      });
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("arquivo ausente, inválido ou gigante devolve o padrão", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "lunmi-prefs-"));
    try {
      expect(readPreferencesFile(path.join(dir, "nao-existe"))).toEqual(DEFAULT_PREFERENCES);

      fs.writeFileSync(path.join(dir, "lunmi-ui.json"), "{nao-json", "utf8");
      expect(readPreferencesFile(dir)).toEqual(DEFAULT_PREFERENCES);

      fs.writeFileSync(
        path.join(dir, "lunmi-ui.json"),
        JSON.stringify({ scale_pct: 200, filler: "x".repeat(MAX_PREFS_BYTES + 10) }),
        "utf8"
      );
      expect(readPreferencesFile(dir)).toEqual(DEFAULT_PREFERENCES);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("usableBounds", () => {
  it("preserva um retângulo que cabe na tela", () => {
    expect(usableBounds({ x: 100, y: 50, width: 1280, height: 800 }, FULL_HD)).toEqual({
      x: 100,
      y: 50,
      width: 1280,
      height: 800,
    });
  });

  it("traz para dentro a janela que só transborda a borda", () => {
    const rect = usableBounds({ x: 1800, y: 900, width: 1280, height: 800 }, FULL_HD);
    expect(rect).not.toBeNull();
    expect(rect!.x).toBe(1920 - 1280);
    expect(rect!.y).toBe(1040 - 800);
    expect(rect!.width).toBe(1280);
  });

  it("devolve null para monitor que não existe mais (sem sobreposição útil)", () => {
    // Janela salva em x=2600 numa tela de 1920: sobreposição nenhuma.
    expect(usableBounds({ x: 2600, y: 100, width: 1280, height: 800 }, FULL_HD)).toBeNull();
    // Sobrepõe menos que o mínimo exigido.
    const barely = usableBounds({ x: 1920 - (MIN_VISIBLE - 5), y: 100, width: 1280, height: 800 }, FULL_HD);
    expect(barely).toBeNull();
  });

  it("devolve null quando não há janela salva ou a tela é pequena demais", () => {
    expect(usableBounds(null, FULL_HD)).toBeNull();
    expect(usableBounds({ x: 0, y: 0, width: 1280, height: 800 }, { x: 0, y: 0, width: 320, height: 240 })).toBeNull();
  });

  it("reduz à tela o retângulo maior que a tela", () => {
    const rect = usableBounds({ x: 0, y: 0, width: 16000, height: 9000 }, FULL_HD);
    expect(rect).toEqual({ x: 0, y: 0, width: 1920, height: 1040 });
  });

  it("respeita a origem da área útil (segundo monitor)", () => {
    const second = { x: 1920, y: 0, width: 1920, height: 1040 };
    expect(usableBounds({ x: 2000, y: 100, width: 1280, height: 800 }, second)).toEqual({
      x: 2000,
      y: 100,
      width: 1280,
      height: 800,
    });
  });
});

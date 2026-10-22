/**
 * Preferências residentes v2 (`desktop/electron/presence.ts`).
 *
 * N1 acrescenta `version: 2` e o bloco `node`. Estes testes fixam: migração de um ficheiro v1
 * sem apagar `companion`, leitura tolerante (nunca levanta), downgrade que só perde a preferência
 * do Node, gravação atómica e validação do patch (chaves/tipos/atalho).
 */
import { afterEach, describe, expect, it } from "vitest";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import {
  DEFAULT_PRESENCE,
  PRESENCE_FILE_NAME,
  PRESENCE_FILE_VERSION,
  parsePresencePreferences,
  readPresenceFile,
  serializePresencePreferences,
  toPresencePreferencesDto,
  writePresenceFile,
} from "../electron/presence";
import {
  applyNodePreferencesPatch,
  DEFAULT_NODE_PREFERENCES,
  parseNodePreferences,
} from "../src/shared/node";
import { parsePresenceAnchor } from "../src/shared/presence";

const tempDirs: string[] = [];

function tempDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "lunmi-presence-"));
  tempDirs.push(dir);
  return dir;
}

afterEach(() => {
  while (tempDirs.length) {
    fs.rmSync(tempDirs.pop()!, { recursive: true, force: true });
  }
});

describe("preferências residentes v2", () => {
  it("default resident=true, Node ligado e ausência nunca levanta", () => {
    expect(DEFAULT_PRESENCE.resident).toBe(true);
    expect(DEFAULT_PRESENCE.node).toEqual(DEFAULT_NODE_PREFERENCES);
    expect(readPresenceFile(path.join(tempDir(), "ausente"))).toEqual(DEFAULT_PRESENCE);
  });

  it("grava versão 2 e relê o mesmo estado, sem temporário pendente", () => {
    const dir = tempDir();
    const prefs = {
      resident: false,
      companion: { x: 120, y: 340 },
      node: { ...DEFAULT_NODE_PREFERENCES, side: "left" as const, auto_dock: false, shortcut: "Control+Shift+L" },
    };
    expect(writePresenceFile(dir, prefs)).toBe(true);
    expect(readPresenceFile(dir)).toEqual(prefs);

    const raw = JSON.parse(fs.readFileSync(path.join(dir, PRESENCE_FILE_NAME), "utf8"));
    expect(raw.version).toBe(PRESENCE_FILE_VERSION);
    expect(PRESENCE_FILE_VERSION).toBe(2);
    expect(Object.keys(raw).sort()).toEqual(["companion", "node", "resident", "version"]);
    expect(raw.companion).toEqual([120, 340]);
    expect(fs.readdirSync(dir).filter((name) => name.endsWith(".tmp"))).toEqual([]);
  });

  it("ficheiro v1 migra para os defaults do Node sem apagar companion", () => {
    const dir = tempDir();
    fs.writeFileSync(
      path.join(dir, PRESENCE_FILE_NAME),
      JSON.stringify({ resident: false, companion: [10, 20] }),
      "utf8"
    );
    const migrated = readPresenceFile(dir);
    expect(migrated).toEqual({
      resident: false,
      companion: { x: 10, y: 20 },
      node: DEFAULT_NODE_PREFERENCES,
    });
    // Regravar já escreve v2 e conserva a âncora legada.
    expect(writePresenceFile(dir, migrated)).toBe(true);
    const raw = JSON.parse(fs.readFileSync(path.join(dir, PRESENCE_FILE_NAME), "utf8"));
    expect(raw.version).toBe(2);
    expect(raw.companion).toEqual([10, 20]);
    expect(raw.node).toEqual(DEFAULT_NODE_PREFERENCES);
  });

  it("downgrade perde só a preferência do Node: resident/companion continuam legíveis", () => {
    const dir = tempDir();
    const prefs = {
      resident: true,
      companion: { x: 5, y: 6 },
      node: { ...DEFAULT_NODE_PREFERENCES, enabled: false, side: "left" as const },
    };
    expect(writePresenceFile(dir, prefs)).toBe(true);
    const raw = JSON.parse(fs.readFileSync(path.join(dir, PRESENCE_FILE_NAME), "utf8"));
    // Leitor v1 real: só conhece resident + companion e ignora o resto sem falhar.
    const oldReader = { resident: raw.resident, companion: raw.companion };
    expect(oldReader).toEqual({ resident: true, companion: [5, 6] });
  });

  it("JSON inválido, tipos errados e âncora impossível caem no padrão", () => {
    const dir = tempDir();
    fs.writeFileSync(path.join(dir, PRESENCE_FILE_NAME), "{ nao-json", "utf8");
    expect(readPresenceFile(dir)).toEqual(DEFAULT_PRESENCE);

    const parsed = parsePresencePreferences({ resident: "yes", companion: ["1", 2] });
    expect(parsed.resident).toBe(true);
    expect(parsed.companion).toBeNull();
    expect(parsed.node).toEqual(DEFAULT_NODE_PREFERENCES);

    expect(parsePresenceAnchor([1.5, 2])).toBeNull();
    expect(parsePresenceAnchor([99999, 0])).toBeNull();
    expect(parseNodePreferences({ side: "up", enabled: "yes", auto_dock: 1 }).side).toBe("right");
    expect(parseNodePreferences({ shortcut: "Ctrl+" }).shortcut).toBeNull();
  });

  it("DTO expõe par de inteiros + preferências do Node, nunca o objeto interno", () => {
    const dto = toPresencePreferencesDto({
      resident: true,
      companion: { x: 1, y: 2 },
      node: { ...DEFAULT_NODE_PREFERENCES },
    });
    expect(dto).toEqual({
      resident: true,
      companion: [1, 2],
      node: DEFAULT_NODE_PREFERENCES,
    });
    const serialized = serializePresencePreferences({
      resident: true,
      companion: null,
      node: DEFAULT_NODE_PREFERENCES,
    });
    expect(JSON.parse(serialized)).toEqual({
      version: 2,
      resident: true,
      companion: null,
      node: DEFAULT_NODE_PREFERENCES,
    });
  });
});

describe("patch de preferências do Node", () => {
  it("aplica só os campos presentes e preserva o resto", () => {
    const result = applyNodePreferencesPatch(DEFAULT_NODE_PREFERENCES, {
      side: "left",
      auto_dock: false,
    });
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.next).toEqual({
      ...DEFAULT_NODE_PREFERENCES,
      side: "left",
      auto_dock: false,
    });
  });

  it("atalho válido aplica, null desregista e inválido recusa o patch inteiro", () => {
    const valid = applyNodePreferencesPatch(DEFAULT_NODE_PREFERENCES, { shortcut: "Control+Alt+L" });
    expect(valid.ok && valid.next.shortcut).toBe("Control+Alt+L");

    const cleared = applyNodePreferencesPatch(
      { ...DEFAULT_NODE_PREFERENCES, shortcut: "Control+Alt+L" },
      { shortcut: null }
    );
    expect(cleared.ok && cleared.next.shortcut).toBeNull();

    for (const shortcut of ["Ctrl+", "Control+Shift+ç", "L", 42]) {
      const invalid = applyNodePreferencesPatch(DEFAULT_NODE_PREFERENCES, { shortcut });
      expect(invalid).toEqual({ ok: false, error: "invalid_payload" });
    }
  });

  it("chave desconhecida ou tipo errado recusa tudo, sem aplicar metade", () => {
    expect(applyNodePreferencesPatch(DEFAULT_NODE_PREFERENCES, { resident: false })).toEqual({
      ok: false,
      error: "invalid_payload",
    });
    expect(applyNodePreferencesPatch(DEFAULT_NODE_PREFERENCES, { side: "up" })).toEqual({
      ok: false,
      error: "invalid_payload",
    });
    expect(applyNodePreferencesPatch(DEFAULT_NODE_PREFERENCES, { enabled: "yes" })).toEqual({
      ok: false,
      error: "invalid_payload",
    });
    expect(applyNodePreferencesPatch(DEFAULT_NODE_PREFERENCES, null)).toEqual({
      ok: false,
      error: "invalid_payload",
    });
    // Patch válido parcial não pode contaminar o objeto recebido.
    const base = { ...DEFAULT_NODE_PREFERENCES };
    applyNodePreferencesPatch(base, { enabled: false });
    expect(base.enabled).toBe(true);
  });
});

/**
 * Preferências próprias do modo residente (`desktop/electron/presence.ts`).
 *
 * Arquivo separado e atómico (`.local/desktop-presence.json`): o schema fechado de
 * `lunmi-ui.json` fica intocado. Nada aqui guarda conversa, rascunho, transcrição ou caminho —
 * só se o recurso residente está ativo, onde ficou o orb legado e as preferências do Node.
 * Leitura tolerante: ausente, inválido, gigante ou com tipo errado devolve o padrão e nunca
 * levanta. Um ficheiro antigo (`{resident, companion}`) migra para o bloco `node` padrão sem
 * apagar `companion`.
 */
import * as fs from "fs";
import * as path from "path";
import { parsePresenceAnchor, type PresenceAnchor } from "../src/shared/presence";
import {
  DEFAULT_NODE_PREFERENCES,
  parseNodePreferences,
} from "../src/shared/node";
import type { NodePreferencesDto, PresencePreferencesDto } from "../src/shared/types";

export const PRESENCE_FILE_NAME = "desktop-presence.json";
export const MAX_PRESENCE_BYTES = 16 * 1024;
/**
 * Versão do ficheiro. v1 = `{resident, companion}`; v2 acrescenta `node`. Um build antigo que
 * leia este ficheiro ignora `node` e conserva residente/companion: o downgrade perde a
 * preferência do Node, nunca a conversa nem a tarefa (essas vivem noutro sítio).
 */
export const PRESENCE_FILE_VERSION = 2;

export interface PresencePreferences {
  /** Default `true`: o recurso foi pedido explicitamente; desativar devolve X = sair. */
  resident: boolean;
  /** Âncora do orb legado; preservada na migração para N1. */
  companion: PresenceAnchor | null;
  /** N1: preferências do Node (visibilidade/lado/recolha/atividade/atalho). */
  node: NodePreferencesDto;
}

export const DEFAULT_PRESENCE: PresencePreferences = {
  resident: true,
  companion: null,
  node: { ...DEFAULT_NODE_PREFERENCES },
};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parsePresencePreferences(raw: unknown): PresencePreferences {
  if (!isPlainObject(raw)) {
    return { ...DEFAULT_PRESENCE };
  }
  // v1 sem `version`/`node` migra em leitura. Versão desconhecida (futura) também não levanta:
  // lê-se o que é reconhecido e o resto cai nos defaults — nunca se apaga `companion`.
  return {
    resident: typeof raw.resident === "boolean" ? raw.resident : DEFAULT_PRESENCE.resident,
    companion: parsePresenceAnchor(raw.companion),
    node: parseNodePreferences(raw.node),
  };
}

export function readPresenceFile(localDir: string): PresencePreferences {
  try {
    const target = path.join(localDir, PRESENCE_FILE_NAME);
    const stat = fs.statSync(target);
    if (stat.size > MAX_PRESENCE_BYTES) {
      return { ...DEFAULT_PRESENCE };
    }
    return parsePresencePreferences(JSON.parse(fs.readFileSync(target, "utf8")));
  } catch {
    return { ...DEFAULT_PRESENCE };
  }
}

/** Serializa só o que o formato aceita; o chamador valida antes de gravar. */
export function serializePresencePreferences(preferences: PresencePreferences): string {
  const companion = preferences.companion
    ? [preferences.companion.x, preferences.companion.y]
    : null;
  return JSON.stringify({
    version: PRESENCE_FILE_VERSION,
    resident: preferences.resident,
    companion,
    node: preferences.node,
  });
}

/**
 * Gravação atómica: escreve um temporário no mesmo diretório e renomeia. Uma falha de disco
 * devolve `false` e mantém o estado em RAM — nunca derruba a aplicação por uma preferência.
 */
export function writePresenceFile(localDir: string, preferences: PresencePreferences): boolean {
  const target = path.join(localDir, PRESENCE_FILE_NAME);
  const temporary = `${target}.${process.pid}.${Date.now()}.tmp`;
  try {
    fs.mkdirSync(localDir, { recursive: true });
    fs.writeFileSync(temporary, serializePresencePreferences(preferences), "utf8");
    fs.renameSync(temporary, target);
    return true;
  } catch {
    try {
      fs.rmSync(temporary, { force: true });
    } catch {
      // o temporário é descartável; a preferência em RAM continua válida
    }
    return false;
  }
}

export function toPresencePreferencesDto(preferences: PresencePreferences): PresencePreferencesDto {
  return {
    resident: preferences.resident,
    companion: preferences.companion ? [preferences.companion.x, preferences.companion.y] : null,
    node: { ...preferences.node },
  };
}

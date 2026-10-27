/**
 * WEB-SEARCH-V1 — abrir uma fonte web por gesto (`desktop/electron/web-source-open.ts`).
 *
 * O renderer envia apenas IDs. Este módulo resolve a URL na projeção canónica que o main já
 * mantém em RAM, valida-a e só então entrega ao `shell.openExternal`. Nunca aceita uma URL vinda
 * do renderer e nunca deixa uma falha de rede/SO chegar como exceção ao processo principal.
 *
 * Contrato: `.agents/WEB-SEARCH-V1-CONTRACT.md` §5.
 */
import * as dns from "node:dns";
import { isIP } from "node:net";
import { isPlainObject } from "../src/shared/protocol";
import type {
  ChatProjectionDto,
  WebSourceDto,
  WebSourceOpenResultDto,
} from "../src/shared/types";

export interface WebSourceOpenInput {
  session_id: string;
  task_id: string;
  source_id: string;
}

export interface WebSourceLookupAddress {
  address: string;
  family?: number;
}

export type WebSourceLookup = (
  hostname: string
) => Promise<readonly WebSourceLookupAddress[]>;

export interface WebSourceOpenDeps {
  /** `shell.openExternal` no main; injetável nos testes. */
  openExternal: (url: string) => Promise<void> | void;
  /** `dns.promises.lookup(..., { all: true })` por omissão. */
  lookup?: WebSourceLookup;
  /** Prazo do lookup DNS. O contrato fixa 2 s; os testes usam um valor curto. */
  lookupTimeoutMs?: number;
}

export interface WebSourceWindowLike {
  isDestroyed(): boolean;
  isVisible(): boolean;
  isFocused(): boolean;
}

export interface WebSourceOpenHandlerDeps {
  /** Devolve a janela confiável (main ou companion) ou `null` para um sender hostil. */
  trustedWindow: (event: unknown) => WebSourceWindowLike | null;
  snapshot: () => ChatProjectionDto | null;
  opener: WebSourceOpener;
}

const WEB_SOURCE_LOOKUP_TIMEOUT_MS = 2000;
const MAX_OPEN_ID_CHARS = 512;

const defaultLookup: WebSourceLookup = (hostname) =>
  dns.promises.lookup(hostname, { all: true, verbatim: true });

function isOpaqueId(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= MAX_OPEN_ID_CHARS &&
    value.trim().length > 0
  );
}

const WEB_SOURCE_INPUT_KEYS = ["session_id", "task_id", "source_id"] as const;

/** Payload fechado: exatamente os três IDs; qualquer campo extra rejeita o pedido inteiro. */
export function parseWebSourceOpenInput(raw: unknown): WebSourceOpenInput | null {
  if (!isPlainObject(raw)) {
    return null;
  }
  const record = raw as Record<string, unknown>;
  const keys = Object.keys(record);
  if (
    keys.length !== WEB_SOURCE_INPUT_KEYS.length ||
    keys.some((key) => !(WEB_SOURCE_INPUT_KEYS as readonly string[]).includes(key))
  ) {
    return null;
  }
  if (
    !isOpaqueId(record.session_id) ||
    !isOpaqueId(record.task_id) ||
    !isOpaqueId(record.source_id)
  ) {
    return null;
  }
  return {
    session_id: record.session_id,
    task_id: record.task_id,
    source_id: record.source_id,
  };
}

function findSource(list: unknown, sourceId: string): WebSourceDto | null {
  if (!Array.isArray(list)) {
    return null;
  }
  for (const item of list) {
    if (item && typeof item === "object" && (item as WebSourceDto).id === sourceId) {
      return item as WebSourceDto;
    }
  }
  return null;
}

/**
 * Resolução canónica: mensagens primeiro (mesma sessão, `task_id` do próprio turno), depois o
 * resultado da tarefa atual. `recent_tasks` nunca é atalho; turnos históricos ainda presentes nas
 * mensagens contam.
 */
export type ResolvedWebSource =
  | { ok: true; source: WebSourceDto }
  | { ok: false; reason: "stale" | "not_found" };

export function resolveWebSource(
  snapshot: ChatProjectionDto | null,
  input: WebSourceOpenInput
): ResolvedWebSource {
  if (!snapshot || snapshot.session_id !== input.session_id) {
    return { ok: false, reason: "stale" };
  }

  for (const message of snapshot.messages ?? []) {
    if (!message || message.task_id !== input.task_id) {
      continue;
    }
    const source = findSource(message.sources, input.source_id);
    if (source) {
      return { ok: true, source };
    }
  }

  const task = snapshot.task;
  if (task && task.task_id === input.task_id) {
    const source = findSource(task.result?.web_sources, input.source_id);
    if (source) {
      return { ok: true, source };
    }
  }

  return { ok: false, reason: "not_found" };
}

// ---------------------------------------------------------------------------
// URL pública
// ---------------------------------------------------------------------------

/** IPv4 privado, loopback, link-local, CGNAT, documentação, multicast e reservado. */
const BLOCKED_IPV4: ReadonlyArray<readonly [string, number]> = [
  ["0.0.0.0", 8],
  ["10.0.0.0", 8],
  ["100.64.0.0", 10],
  ["127.0.0.0", 8],
  ["169.254.0.0", 16],
  ["172.16.0.0", 12],
  ["192.0.0.0", 24],
  ["192.0.2.0", 24],
  ["192.88.99.0", 24],
  ["192.168.0.0", 16],
  ["198.18.0.0", 15],
  ["198.51.100.0", 24],
  ["203.0.113.0", 24],
  ["224.0.0.0", 4],
  ["240.0.0.0", 4],
];

function parseIPv4(value: string): number[] | null {
  const parts = value.split(".");
  if (parts.length !== 4) {
    return null;
  }
  const bytes: number[] = [];
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) {
      return null;
    }
    const byte = Number(part);
    if (byte > 255) {
      return null;
    }
    bytes.push(byte);
  }
  return bytes;
}

function ipv4ToInt(bytes: readonly number[]): number {
  return (
    ((bytes[0] << 24) >>> 0) +
    (bytes[1] << 16) +
    (bytes[2] << 8) +
    bytes[3]
  ) >>> 0;
}

const BLOCKED_IPV4_NETS = BLOCKED_IPV4.map(([base, bits]) => {
  const parsed = parseIPv4(base);
  const baseInt = parsed ? ipv4ToInt(parsed) : 0;
  const mask = bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0;
  return { base: (baseInt & mask) >>> 0, mask };
});

function isPublicIPv4(bytes: readonly number[]): boolean {
  const value = ipv4ToInt(bytes);
  return !BLOCKED_IPV4_NETS.some((net) => ((value & net.mask) >>> 0) === net.base);
}

function appendIPv4Words(words: number[], value: string): boolean {
  const bytes = parseIPv4(value);
  if (!bytes) {
    return false;
  }
  words.push((bytes[0] << 8) | bytes[1], (bytes[2] << 8) | bytes[3]);
  return true;
}

function parseIPv6(value: string): number[] | null {
  let address = value.toLowerCase();
  if (address.startsWith("[") && address.endsWith("]")) {
    address = address.slice(1, -1);
  }
  if (!address || address.includes("%")) {
    return null;
  }

  const sides = address.split("::");
  if (sides.length > 2) {
    return null;
  }

  const parseSide = (side: string): number[] | null => {
    if (side === "") {
      return [];
    }
    const groups = side.split(":");
    const words: number[] = [];
    for (let index = 0; index < groups.length; index += 1) {
      const group = groups[index];
      if (group === "") {
        return null;
      }
      if (group.includes(".")) {
        if (index !== groups.length - 1 || !appendIPv4Words(words, group)) {
          return null;
        }
        continue;
      }
      if (!/^[0-9a-f]{1,4}$/.test(group)) {
        return null;
      }
      words.push(parseInt(group, 16));
    }
    return words;
  };

  const left = parseSide(sides[0]);
  const right = sides.length === 2 ? parseSide(sides[1]) : [];
  if (!left || !right) {
    return null;
  }

  let words: number[];
  if (sides.length === 2) {
    const zeros = 8 - left.length - right.length;
    if (zeros < 0) {
      return null;
    }
    words = [...left, ...Array<number>(zeros).fill(0), ...right];
  } else {
    if (left.length !== 8) {
      return null;
    }
    words = left;
  }

  if (words.length !== 8) {
    return null;
  }
  const bytes: number[] = [];
  for (const word of words) {
    bytes.push((word >> 8) & 0xff, word & 0xff);
  }
  return bytes;
}

function isPublicIPv6(bytes: readonly number[]): boolean {
  const all = (start: number, end: number, byte: number): boolean => {
    for (let index = start; index < end; index += 1) {
      if (bytes[index] !== byte) {
        return false;
      }
    }
    return true;
  };

  // :: (não especificado) e ::1 (loopback).
  if (all(0, 16, 0)) {
    return false;
  }
  if (all(0, 15, 0) && bytes[15] === 1) {
    return false;
  }

  // ::ffff:0:0/96 — IPv4 mapeado: classificar o IPv4 embutido.
  if (all(0, 10, 0) && bytes[10] === 0xff && bytes[11] === 0xff) {
    return isPublicIPv4(bytes.slice(12, 16));
  }

  // ::/96 — IPv4 compatível (obsoleto), excepto :: e ::1 já tratados.
  if (all(0, 12, 0)) {
    return isPublicIPv4(bytes.slice(12, 16));
  }

  // 64:ff9b::/96 — NAT64 bem conhecido.
  if (
    bytes[0] === 0x00 &&
    bytes[1] === 0x64 &&
    bytes[2] === 0xff &&
    bytes[3] === 0x9b &&
    all(4, 12, 0)
  ) {
    return isPublicIPv4(bytes.slice(12, 16));
  }

  // 2002::/16 — 6to4: o IPv4 embutido decide.
  if (bytes[0] === 0x20 && bytes[1] === 0x02) {
    return isPublicIPv4(bytes.slice(2, 6));
  }

  // fc00::/7 — ULA.
  if ((bytes[0] & 0xfe) === 0xfc) {
    return false;
  }
  // fe80::/10 — link-local.
  if (bytes[0] === 0xfe && (bytes[1] & 0xc0) === 0x80) {
    return false;
  }
  // ff00::/8 — multicast.
  if (bytes[0] === 0xff) {
    return false;
  }
  // 2001:db8::/32 — documentação.
  if (bytes[0] === 0x20 && bytes[1] === 0x01 && bytes[2] === 0x0d && bytes[3] === 0xb8) {
    return false;
  }
  // 3fff::/20 — documentação.
  if (bytes[0] === 0x3f && bytes[1] === 0xff && (bytes[2] & 0xf0) === 0) {
    return false;
  }
  // 2001::/32 — Teredo (servidor/cliente embutidos; não é um destino público direto).
  if (bytes[0] === 0x20 && bytes[1] === 0x01 && bytes[2] === 0x00 && bytes[3] === 0x00) {
    return false;
  }
  // 2001:2::/48 — benchmarking.
  if (
    bytes[0] === 0x20 &&
    bytes[1] === 0x01 &&
    bytes[2] === 0x00 &&
    bytes[3] === 0x02
  ) {
    return false;
  }
  // 2001:10::/28 e 2001:20::/28 — ORCHID/ORCHIDv2.
  if (
    bytes[0] === 0x20 &&
    bytes[1] === 0x01 &&
    bytes[2] === 0x00 &&
    bytes[3] >= 0x10 &&
    bytes[3] <= 0x2f
  ) {
    return false;
  }
  // 64:ff9b:1::/48 — NAT64 de uso local.
  if (
    bytes[0] === 0x00 &&
    bytes[1] === 0x64 &&
    bytes[2] === 0xff &&
    bytes[3] === 0x9b &&
    bytes[4] === 0x00 &&
    bytes[5] === 0x01
  ) {
    return false;
  }

  return true;
}

/** Um endereço literal é público? IPv4 mapeado/compatível é classificado pelo IPv4 embutido. */
export function isPublicAddress(address: string): boolean {
  const family = isIP(address);
  if (family === 4) {
    const bytes = parseIPv4(address);
    return bytes !== null && isPublicIPv4(bytes);
  }
  if (family === 6) {
    const bytes = parseIPv6(address);
    return bytes !== null && isPublicIPv6(bytes);
  }
  return false;
}

function hostnameOf(url: URL): string {
  const raw = url.hostname;
  if (raw.startsWith("[") && raw.endsWith("]")) {
    return raw.slice(1, -1).toLowerCase();
  }
  return raw.toLowerCase().replace(/\.$/, "");
}

function isInternalHostname(hostname: string): boolean {
  return (
    hostname === "localhost" ||
    hostname.endsWith(".localhost") ||
    hostname.endsWith(".local") ||
    hostname.endsWith(".internal") ||
    hostname.endsWith(".home.arpa")
  );
}

/**
 * Validação síncrona da URL canónica. Hostnames DNS públicos ainda precisam do lookup seguinte;
 * IPs literais são decididos aqui.
 */
export function isPublicHttpUrl(raw: unknown): boolean {
  if (typeof raw !== "string" || raw.length === 0) {
    return false;
  }
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return false;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return false;
  }
  if (url.username !== "" || url.password !== "") {
    return false;
  }
  if (url.port !== "" && url.port !== "80" && url.port !== "443") {
    return false;
  }

  const bracketed = url.hostname.startsWith("[") && url.hostname.endsWith("]");
  const host = hostnameOf(url);
  if (!host || host.length > 253 || host.includes("%")) {
    return false;
  }
  if (bracketed) {
    return isIP(host) === 6 && isPublicAddress(host);
  }
  if (isInternalHostname(host)) {
    return false;
  }
  const family = isIP(host);
  if (family !== 0) {
    return isPublicAddress(host);
  }
  // Um hostname público tem de ter um domínio; nomes de uma só etiqueta são internos.
  return host.includes(".");
}

function withDeadline<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(
      () => reject(new Error("web_source_lookup_timeout")),
      Math.max(0, timeoutMs)
    );
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
  });
}

/**
 * URL pública e resolúvel: exige que TODOS os endereços DNS sejam públicos. Falha de DNS,
 * timeout ou mistura de público/privado ⇒ `false` (`blocked_url`).
 */
export async function isOpenableWebUrl(
  raw: unknown,
  options: Pick<WebSourceOpenDeps, "lookup" | "lookupTimeoutMs"> = {}
): Promise<boolean> {
  if (!isPublicHttpUrl(raw)) {
    return false;
  }
  const url = new URL(raw as string);
  const host = hostnameOf(url);
  if (isIP(host) !== 0) {
    return true;
  }

  const lookup = options.lookup ?? defaultLookup;
  try {
    const addresses = await withDeadline(
      Promise.resolve().then(() => lookup(host)),
      options.lookupTimeoutMs ?? WEB_SOURCE_LOOKUP_TIMEOUT_MS
    );
    if (!addresses || addresses.length === 0) {
      return false;
    }
    return addresses.every(
      (entry) => Boolean(entry) && typeof entry.address === "string" && isPublicAddress(entry.address)
    );
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Operação
// ---------------------------------------------------------------------------

/**
 * A projeção pode vir já lida (testes puros) ou como getter (handler IPC), para que a
 * revalidação antes do efeito leia o estado atual e não um retrato antigo ao início do DNS.
 */
export type WebSourceSnapshotSource =
  | ChatProjectionDto
  | null
  | (() => ChatProjectionDto | null);

function readSnapshot(source: WebSourceSnapshotSource): ChatProjectionDto | null {
  if (typeof source === "function") {
    try {
      return source();
    } catch {
      return null;
    }
  }
  return source;
}

function guardAllows(guard: (() => boolean) | undefined): boolean {
  if (!guard) {
    return true;
  }
  try {
    return guard() === true;
  } catch {
    return false;
  }
}

/**
 * Executa a abertura com dedupe in-flight por `(session_id, task_id, source_id)`. Chamadas
 * concorrentes do mesmo gesto partilham a mesma promessa; só uma chama `openExternal`.
 *
 * Antes do efeito, a identidade é revalidada na projeção atual: sessão, tarefa, fonte e URL
 * canónica têm de continuar exatamente as mesmas que foram validadas. Uma mudança durante o
 * lookup DNS torna o gesto `stale` e não abre nada. `canStillOpen` repete a confiança da janela
 * no mesmo ponto; a URL nunca vem do renderer.
 */
export class WebSourceOpener {
  private readonly inFlight = new Map<string, Promise<WebSourceOpenResultDto>>();

  constructor(private readonly deps: WebSourceOpenDeps) {}

  public open(
    rawInput: unknown,
    snapshot: WebSourceSnapshotSource,
    canStillOpen?: () => boolean
  ): Promise<WebSourceOpenResultDto> {
    const input = parseWebSourceOpenInput(rawInput);
    if (!input) {
      return Promise.resolve({ opened: false, reason: "invalid_payload" });
    }

    const key = JSON.stringify([input.session_id, input.task_id, input.source_id]);
    const pending = this.inFlight.get(key);
    if (pending) {
      return pending;
    }

    const run = this.run(input, snapshot, canStillOpen);
    this.inFlight.set(key, run);
    void run.finally(() => {
      if (this.inFlight.get(key) === run) {
        this.inFlight.delete(key);
      }
    });
    return run;
  }

  private async run(
    input: WebSourceOpenInput,
    snapshot: WebSourceSnapshotSource,
    canStillOpen?: () => boolean
  ): Promise<WebSourceOpenResultDto> {
    try {
      const resolved = resolveWebSource(readSnapshot(snapshot), input);
      if (!resolved.ok) {
        return { opened: false, reason: resolved.reason };
      }
      const url = resolved.source.url;
      if (!(await isOpenableWebUrl(url, this.deps))) {
        return { opened: false, reason: "blocked_url" };
      }

      // Snapshot getter + janela ainda confiável: o efeito só acontece se nada mudou no await.
      const current = resolveWebSource(readSnapshot(snapshot), input);
      if (!current.ok || current.source.url !== url || !guardAllows(canStillOpen)) {
        return { opened: false, reason: "stale" };
      }

      try {
        await this.deps.openExternal(url);
        return { opened: true, reason: "" };
      } catch {
        return { opened: false, reason: "open_failed" };
      }
    } catch {
      // Nenhuma falha interna pode chegar ao renderer como exceção.
      return { opened: false, reason: "open_failed" };
    }
  }
}

function gestureWindowReady(win: WebSourceWindowLike | null): boolean {
  if (!win) {
    return false;
  }
  try {
    return !win.isDestroyed() && win.isVisible() && win.isFocused();
  } catch {
    return false;
  }
}

/**
 * Handler IPC: valida a janela confiável/visível/focada e delega no opener. O snapshot é
 * passado como getter e a confiança da janela é reavaliada imediatamente antes de abrir.
 */
export function createWebSourceOpenHandler(
  deps: WebSourceOpenHandlerDeps
): (event: unknown, rawInput: unknown) => Promise<WebSourceOpenResultDto> {
  return async (event, rawInput) => {
    let win: WebSourceWindowLike | null = null;
    try {
      win = deps.trustedWindow(event);
    } catch {
      win = null;
    }
    if (!gestureWindowReady(win)) {
      // Sender hostil, janela oculta ou sem foco: não é um gesto confiável.
      throw new Error("forbidden");
    }
    return deps.opener.open(rawInput, deps.snapshot, () => {
      let current: WebSourceWindowLike | null = null;
      try {
        current = deps.trustedWindow(event);
      } catch {
        current = null;
      }
      return gestureWindowReady(current);
    });
  };
}

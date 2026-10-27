/**
 * WEB-SEARCH-V1 — abertura de fontes no Electron (helper puro + handler).
 *
 * Prova as decisões do contrato §5: só IDs do renderer, resolução na projeção canónica,
 * URL pública/HTTP(S), DNS com todos os endereços públicos, dedupe in-flight e nenhuma exceção
 * de `openExternal` para o renderer.
 */
import { describe, expect, it, vi } from "vitest";
import {
  createWebSourceOpenHandler,
  isPublicAddress,
  isPublicHttpUrl,
  parseWebSourceOpenInput,
  resolveWebSource,
  WebSourceOpener,
  type WebSourceLookup,
} from "../electron/web-source-open";
import type {
  ChatMessageDto,
  ChatProjectionDto,
  TaskResultDto,
  TaskStateDto,
  WebSourceDto,
} from "../src/shared/types";

const SESSION = "sess-1";
const TASK = "task-1";
const SOURCE = "src-0123456789abcdef";

const PUBLIC_LOOKUP: WebSourceLookup = async () => [
  { address: "93.184.216.34", family: 4 },
];

function source(overrides: Partial<WebSourceDto> = {}): WebSourceDto {
  return {
    id: SOURCE,
    title: "Example",
    url: "https://example.com/article",
    snippet: "",
    engines: ["ddg"],
    published_at: null,
    date_source: null,
    retrieved_at: "2026-10-24T00:00:00Z",
    read: true,
    truncated: false,
    cached: false,
    ...overrides,
  };
}

function taskResult(overrides: Partial<TaskResultDto> = {}): TaskResultDto {
  return {
    task_id: TASK,
    status: "SUCCESS",
    message: "ok",
    ...overrides,
  };
}

function taskState(overrides: Partial<TaskStateDto> = {}): TaskStateDto {
  return {
    task_id: TASK,
    mode: "auto",
    instruction: "",
    phase: "finished",
    progress: "",
    result: null,
    error: null,
    ...overrides,
  };
}

function message(overrides: Partial<ChatMessageDto> = {}): ChatMessageDto {
  return {
    role: "assistant",
    text: "",
    message_id: "m-1",
    task_id: TASK,
    ...overrides,
  };
}

function projection(overrides: Partial<ChatProjectionDto> = {}): ChatProjectionDto {
  return {
    session_id: SESSION,
    cursor: 1,
    messages: [],
    recent_tasks: [],
    task: null,
    confirm: null,
    input: null,
    ...overrides,
  };
}

const INPUT = { session_id: SESSION, task_id: TASK, source_id: SOURCE };

describe("parseWebSourceOpenInput", () => {
  it("aceita exatamente os três IDs", () => {
    expect(parseWebSourceOpenInput(INPUT)).toEqual(INPUT);
  });

  it("rejeita o payload inteiro se houver qualquer campo fora dos três IDs", () => {
    expect(
      parseWebSourceOpenInput({ ...INPUT, url: "https://evil.example" })
    ).toBeNull();
    expect(parseWebSourceOpenInput({ ...INPUT, extra: 1 })).toBeNull();
  });

  it("rejeita payloads que não sejam três strings não vazias", () => {
    expect(parseWebSourceOpenInput(null)).toBeNull();
    expect(parseWebSourceOpenInput([])).toBeNull();
    expect(parseWebSourceOpenInput({ ...INPUT, session_id: 1 })).toBeNull();
    expect(parseWebSourceOpenInput({ ...INPUT, task_id: "" })).toBeNull();
    expect(parseWebSourceOpenInput({ ...INPUT, source_id: "   " })).toBeNull();
    expect(parseWebSourceOpenInput({ session_id: SESSION, task_id: TASK })).toBeNull();
  });
});

describe("resolveWebSource", () => {
  it("recusa sessão diferente antes de olhar para as fontes", () => {
    const snap = projection({ messages: [message({ sources: [source()] })] });
    expect(resolveWebSource(snap, { ...INPUT, session_id: "outra" })).toEqual({
      ok: false,
      reason: "stale",
    });
  });

  it("prefere messages[].sources com o task_id do próprio turno ao fallback", () => {
    const snap = projection({
      messages: [
        message({ sources: [source({ url: "https://message.example/a" })] }),
      ],
      task: taskState({
        result: taskResult({ web_sources: [source({ url: "https://fallback.example/a" })] }),
      }),
    });
    const resolved = resolveWebSource(snap, INPUT);
    expect(resolved.ok).toBe(true);
    if (resolved.ok) {
      expect(resolved.source.url).toBe("https://message.example/a");
    }
  });

  it("aceita fontes de turnos históricos da mesma sessão que ainda estão nas mensagens", () => {
    const snap = projection({
      messages: [message({ sources: [source()] })],
      task: taskState({ task_id: "task-atual" }),
    });
    expect(resolveWebSource(snap, INPUT).ok).toBe(true);
  });

  it("usa task.result.web_sources apenas quando o task_id coincide", () => {
    const snap = projection({
      task: taskState({
        task_id: TASK,
        result: taskResult({ web_sources: [source()] }),
      }),
    });
    expect(resolveWebSource(snap, INPUT).ok).toBe(true);

    const wrongTask = projection({
      task: taskState({
        task_id: "task-2",
        result: taskResult({ task_id: "task-2", web_sources: [source()] }),
      }),
    });
    expect(resolveWebSource(wrongTask, INPUT)).toEqual({
      ok: false,
      reason: "not_found",
    });
  });

  it("não usa recent_tasks como atalho", () => {
    const snap = projection({
      recent_tasks: [
        taskState({ result: taskResult({ web_sources: [source()] }) }),
      ],
    });
    expect(resolveWebSource(snap, INPUT)).toEqual({
      ok: false,
      reason: "not_found",
    });
  });

  it("não encontra uma fonte pertencente a outro task_id", () => {
    const snap = projection({
      messages: [
        message({ task_id: "outro-task", sources: [source()] }),
      ],
    });
    expect(resolveWebSource(snap, INPUT)).toEqual({
      ok: false,
      reason: "not_found",
    });
  });
});

describe("isPublicAddress", () => {
  it("aceita endereços públicos IPv4/IPv6", () => {
    expect(isPublicAddress("8.8.8.8")).toBe(true);
    expect(isPublicAddress("93.184.216.34")).toBe(true);
    expect(isPublicAddress("2606:4700:4700::1111")).toBe(true);
    expect(isPublicAddress("::ffff:8.8.8.8")).toBe(true);
    expect(isPublicAddress("2002:0808:0808::")).toBe(true);
  });

  it("recusa privados, loopback, link-local, ULA, multicast e documentação", () => {
    for (const address of [
      "0.0.0.0",
      "10.0.0.1",
      "100.64.0.1",
      "127.0.0.1",
      "169.254.169.254",
      "172.16.0.1",
      "192.0.2.1",
      "192.168.1.1",
      "198.18.0.1",
      "198.51.100.1",
      "203.0.113.1",
      "224.0.0.1",
      "255.255.255.255",
      "::",
      "::1",
      "fe80::1",
      "fc00::1",
      "ff02::1",
      "2001:db8::1",
      "2001:2::1",
      "::ffff:127.0.0.1",
      "2002:7f00:0001::",
    ]) {
      expect(isPublicAddress(address), address).toBe(false);
    }
  });
});

describe("isPublicHttpUrl", () => {
  it("aceita HTTP(S) público, incluindo IDN e IPv6 literal", () => {
    expect(isPublicHttpUrl("https://example.com/article")).toBe(true);
    expect(isPublicHttpUrl("http://example.com:80/article")).toBe(true);
    expect(isPublicHttpUrl("https://example.com:443/article")).toBe(true);
    expect(isPublicHttpUrl("http://münchen.de/")).toBe(true);
    expect(isPublicHttpUrl("https://[2606:4700:4700::1111]/")).toBe(true);
  });

  it("recusa scheme, credenciais, portas, hosts internos e IPs privados", () => {
    for (const url of [
      "ftp://example.com/article",
      "file:///C:/Windows/win.ini",
      "http://user:pass@example.com/article",
      "http://example.com:8080/article",
      "http://localhost/article",
      "http://foo.localhost/article",
      "http://foo.local/article",
      "http://foo.internal/article",
      "http://foo.home.arpa/article",
      "http://intranet/article",
      "http://127.0.0.1/article",
      "http://10.0.0.1/article",
      "http://169.254.169.254/latest/meta-data",
      "http://[::1]/article",
      "http://[fe80::1]/article",
      "http://[fc00::1]/article",
      "http://[::ffff:127.0.0.1]/article",
    ]) {
      expect(isPublicHttpUrl(url), url).toBe(false);
    }
  });
});

describe("WebSourceOpener", () => {
  function opener(
    openExternal: (url: string) => Promise<void> | void = async () => undefined,
    lookup: WebSourceLookup = PUBLIC_LOOKUP,
    lookupTimeoutMs = 100
  ) {
    const opened = vi.fn(openExternal);
    const lookupSpy = vi.fn(lookup);
    return {
      openExternal: opened,
      lookup: lookupSpy,
      instance: new WebSourceOpener({
        openExternal: opened,
        lookup: lookupSpy,
        lookupTimeoutMs,
      }),
    };
  }

  function snapWith(url: string): ChatProjectionDto {
    return projection({
      messages: [message({ sources: [source({ url })] })],
    });
  }

  it("abre a URL canónica", async () => {
    const fake = opener();
    const result = await fake.instance.open(
      INPUT,
      snapWith("https://example.com/canonical")
    );
    expect(result).toEqual({ opened: true, reason: "" });
    expect(fake.openExternal).toHaveBeenCalledTimes(1);
    expect(fake.openExternal).toHaveBeenCalledWith("https://example.com/canonical");
  });

  it("rejeita uma URL enviada pelo renderer antes de resolver ou abrir", async () => {
    const fake = opener();
    const result = await fake.instance.open(
      { ...INPUT, url: "https://evil.example" },
      snapWith("https://example.com/canonical")
    );
    expect(result).toEqual({ opened: false, reason: "invalid_payload" });
    expect(fake.lookup).not.toHaveBeenCalled();
    expect(fake.openExternal).not.toHaveBeenCalled();
  });

  it("recusa abrir se a sessão mudar durante o lookup DNS", async () => {
    let release!: (addresses: Array<{ address: string; family: number }>) => void;
    let entered!: () => void;
    const waiting = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const lookup: WebSourceLookup = () =>
      new Promise((resolve) => {
        release = resolve;
        entered();
      });
    const openExternal = vi.fn(async () => undefined);
    const instance = new WebSourceOpener({ openExternal, lookup, lookupTimeoutMs: 1000 });
    let current = snapWith("https://example.com/a");

    const pending = instance.open(INPUT, () => current);
    await waiting;
    current = projection({ session_id: "sess-2", messages: [] });
    release([{ address: "93.184.216.34", family: 4 }]);

    await expect(pending).resolves.toEqual({ opened: false, reason: "stale" });
    expect(openExternal).not.toHaveBeenCalled();
  });

  it("recusa abrir se a URL canónica mudar durante o lookup DNS", async () => {
    let release!: (addresses: Array<{ address: string; family: number }>) => void;
    let entered!: () => void;
    const waiting = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const lookup: WebSourceLookup = () =>
      new Promise((resolve) => {
        release = resolve;
        entered();
      });
    const openExternal = vi.fn(async () => undefined);
    const instance = new WebSourceOpener({ openExternal, lookup, lookupTimeoutMs: 1000 });
    let current = snapWith("https://example.com/a");

    const pending = instance.open(INPUT, () => current);
    await waiting;
    current = snapWith("https://example.com/b");
    release([{ address: "93.184.216.34", family: 4 }]);

    await expect(pending).resolves.toEqual({ opened: false, reason: "stale" });
    expect(openExternal).not.toHaveBeenCalled();
  });

  it("devolve invalid_payload sem tocar na projeção", async () => {
    const fake = opener();
    const result = await fake.instance.open(null, snapWith("https://example.com/a"));
    expect(result).toEqual({ opened: false, reason: "invalid_payload" });
    expect(fake.openExternal).not.toHaveBeenCalled();
    expect(fake.lookup).not.toHaveBeenCalled();
  });

  it("devolve stale quando a sessão já não é a atual", async () => {
    const fake = opener();
    const result = await fake.instance.open(
      { ...INPUT, session_id: "sess-antiga" },
      snapWith("https://example.com/a")
    );
    expect(result).toEqual({ opened: false, reason: "stale" });
    expect(fake.openExternal).not.toHaveBeenCalled();
  });

  it("devolve not_found quando a fonte não existe na projeção", async () => {
    const fake = opener();
    const result = await fake.instance.open(
      { ...INPUT, source_id: "src-ffffffffffffffff" },
      snapWith("https://example.com/a")
    );
    expect(result).toEqual({ opened: false, reason: "not_found" });
    expect(fake.openExternal).not.toHaveBeenCalled();
  });

  it("bloqueia URL privada sem consultar DNS nem abrir", async () => {
    const fake = opener();
    const result = await fake.instance.open(INPUT, snapWith("http://127.0.0.1/private"));
    expect(result).toEqual({ opened: false, reason: "blocked_url" });
    expect(fake.lookup).not.toHaveBeenCalled();
    expect(fake.openExternal).not.toHaveBeenCalled();
  });

  it("recusa se qualquer endereço DNS não for público", async () => {
    const fake = opener(
      vi.fn(async () => undefined),
      async () => [
        { address: "93.184.216.34", family: 4 },
        { address: "10.0.0.1", family: 4 },
      ]
    );
    const result = await fake.instance.open(INPUT, snapWith("https://example.com/a"));
    expect(result).toEqual({ opened: false, reason: "blocked_url" });
    expect(fake.openExternal).not.toHaveBeenCalled();
  });

  it("mapeia falha de DNS e timeout para blocked_url", async () => {
    const failing = opener(
      vi.fn(async () => undefined),
      async () => {
        throw new Error("dns_down");
      }
    );
    expect(await failing.instance.open(INPUT, snapWith("https://example.com/a"))).toEqual({
      opened: false,
      reason: "blocked_url",
    });

    const never: WebSourceLookup = () => new Promise(() => undefined);
    const hanging = opener(vi.fn(async () => undefined), never, 20);
    expect(await hanging.instance.open(INPUT, snapWith("https://example.com/a"))).toEqual({
      opened: false,
      reason: "blocked_url",
    });
  });

  it("usa o hostname IDN já convertido no lookup", async () => {
    const lookup = vi.fn(PUBLIC_LOOKUP);
    const fake = opener(vi.fn(async () => undefined), lookup);
    const result = await fake.instance.open(INPUT, snapWith("http://münchen.de/"));
    expect(result).toEqual({ opened: true, reason: "" });
    expect(lookup).toHaveBeenCalledWith("xn--mnchen-3ya.de");
  });

  it("não faz lookup para um IPv6 público literal", async () => {
    const lookup = vi.fn(PUBLIC_LOOKUP);
    const fake = opener(vi.fn(async () => undefined), lookup);
    const result = await fake.instance.open(
      INPUT,
      snapWith("https://[2606:4700:4700::1111]/")
    );
    expect(result).toEqual({ opened: true, reason: "" });
    expect(lookup).not.toHaveBeenCalled();
  });

  it("mapeia erro do shell para open_failed", async () => {
    const fake = opener(
      vi.fn(async () => {
        throw new Error("shell_error");
      })
    );
    const result = await fake.instance.open(INPUT, snapWith("https://example.com/a"));
    expect(result).toEqual({ opened: false, reason: "open_failed" });
  });

  it("deduplica chamadas concorrentes do mesmo gesto", async () => {
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const openExternal = vi.fn(() => gate);
    const fake = opener(openExternal);

    const first = fake.instance.open(INPUT, snapWith("https://example.com/a"));
    const second = fake.instance.open(INPUT, snapWith("https://example.com/a"));
    expect(first).toBe(second);

    await vi.waitFor(() => expect(fake.openExternal).toHaveBeenCalledTimes(1));
    release();
    await expect(first).resolves.toEqual({ opened: true, reason: "" });
    await expect(second).resolves.toEqual({ opened: true, reason: "" });
    expect(fake.openExternal).toHaveBeenCalledTimes(1);
  });
});

describe("createWebSourceOpenHandler", () => {
  function handlerWith(
    win: { isDestroyed(): boolean; isVisible(): boolean; isFocused(): boolean } | null
  ) {
    const fake = {
      openExternal: vi.fn(async () => undefined),
      lookup: PUBLIC_LOOKUP,
    };
    const instance = new WebSourceOpener(fake);
    const snap = projection({
      messages: [message({ sources: [source()] })],
    });
    return {
      fake,
      handler: createWebSourceOpenHandler({
        trustedWindow: () => win,
        snapshot: () => snap,
        opener: instance,
      }),
    };
  }

  const visible = {
    isDestroyed: () => false,
    isVisible: () => true,
    isFocused: () => true,
  };

  it("delega quando a janela é confiável, visível e focada", async () => {
    const { handler, fake } = handlerWith(visible);
    await expect(handler({}, INPUT)).resolves.toEqual({ opened: true, reason: "" });
    expect(fake.openExternal).toHaveBeenCalledWith("https://example.com/article");
  });

  it("não abre se a janela perder o foco durante o lookup DNS", async () => {
    let focused = true;
    let release!: (addresses: Array<{ address: string; family: number }>) => void;
    let entered!: () => void;
    const waiting = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const lookup: WebSourceLookup = () =>
      new Promise((resolve) => {
        release = resolve;
        entered();
      });
    const openExternal = vi.fn(async () => undefined);
    const handler = createWebSourceOpenHandler({
      trustedWindow: () => ({
        isDestroyed: () => false,
        isVisible: () => true,
        isFocused: () => focused,
      }),
      snapshot: () => projection({ messages: [message({ sources: [source()] })] }),
      opener: new WebSourceOpener({ openExternal, lookup, lookupTimeoutMs: 1000 }),
    });

    const pending = handler({}, INPUT);
    await waiting;
    focused = false;
    release([{ address: "93.184.216.34", family: 4 }]);

    await expect(pending).resolves.toEqual({ opened: false, reason: "stale" });
    expect(openExternal).not.toHaveBeenCalled();
  });

  it("lança forbidden para sender não confiável", async () => {
    const { handler, fake } = handlerWith(null);
    await expect(handler({}, INPUT)).rejects.toThrow("forbidden");
    expect(fake.openExternal).not.toHaveBeenCalled();
  });

  it("lança forbidden para janela oculta, sem foco ou destruída", async () => {
    for (const win of [
      { ...visible, isVisible: () => false },
      { ...visible, isFocused: () => false },
      { ...visible, isDestroyed: () => true },
    ]) {
      const { handler, fake } = handlerWith(win);
      await expect(handler({}, INPUT)).rejects.toThrow("forbidden");
      expect(fake.openExternal).not.toHaveBeenCalled();
    }
  });
});

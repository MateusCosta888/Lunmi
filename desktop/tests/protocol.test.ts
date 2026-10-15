import { describe, expect, it } from "vitest";
import { spawn } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import golden from "../src/shared/ipc_golden_v1.json";
import {
  COMMAND_TYPES,
  decodeUtf8Strict,
  encodeEnvelope,
  EVENT_TYPES,
  ID_PATTERN,
  JsonlLineFramer,
  MAX_CHAT_TEXT_CHARS,
  MAX_HOST_PIDS,
  MAX_ID_CHARS,
  MAX_IMPORT_BYTES,
  MAX_LINE_BYTES,
  MAX_MEMORY_TEXT_CHARS,
  MAX_TYPE_CHARS,
  parseEnvelope,
  PROTOCOL_VERSION,
  ProtocolValidationError,
  RESPONSE_TYPES,
  TYPE_PATTERN,
  validateHostPids,
} from "../src/shared/protocol";
import { PythonBackendSupervisor } from "../electron/backend";
import { isTrustedSenderFrame, normalizeDocumentUrl } from "../electron/main";

describe("Contrato IPC v1 (TypeScript <-> Golden <-> Python)", () => {
  it("concorda com o golden v1 compartilhado", () => {
    expect(PROTOCOL_VERSION).toBe(golden.version);
    expect(MAX_LINE_BYTES).toBe(golden.limits.max_line_bytes);
    expect(MAX_IMPORT_BYTES).toBe(golden.limits.max_import_bytes);
    expect(MAX_CHAT_TEXT_CHARS).toBe(golden.limits.max_chat_text_chars);
    expect(MAX_MEMORY_TEXT_CHARS).toBe(golden.limits.max_memory_text_chars);
    expect(MAX_ID_CHARS).toBe(golden.limits.max_id_chars);
    expect(MAX_TYPE_CHARS).toBe(golden.limits.max_type_chars);
    expect(MAX_HOST_PIDS).toBe(golden.limits.max_host_pids);
    expect(ID_PATTERN.source).toBe(golden.patterns.id);
    expect(TYPE_PATTERN.source).toBe(golden.patterns.type);
    expect([...COMMAND_TYPES].sort()).toEqual([...golden.commands].sort());
    expect([...RESPONSE_TYPES].sort()).toEqual([...golden.responses].sort());
    expect([...EVENT_TYPES].sort()).toEqual([...golden.events].sort());
  });

  it("rejeita versão incompatível, JSON inválido, NaN/Infinity, surrogates e payload não-objeto", () => {
    expect(() => parseEnvelope("nao-json")).toThrowError(ProtocolValidationError);
    expect(() =>
      parseEnvelope('{"v":1,"id":"nan-1","type":"session.hello","payload":{"x":NaN}}')
    ).toThrowError(ProtocolValidationError);
    expect(() =>
      parseEnvelope('{"v":1,"id":"inf-1","type":"session.hello","payload":{"x":Infinity}}')
    ).toThrowError(ProtocolValidationError);
    expect(() =>
      parseEnvelope(JSON.stringify({ v: 2, id: "req-1", type: "session.hello", payload: {} }))
    ).toThrowError(/Versão incompatível/);
    expect(() =>
      parseEnvelope(JSON.stringify({ v: 1, id: "req-1", type: "session.hello", payload: [] }))
    ).toThrowError(/payload/);
    expect(() =>
      parseEnvelope(JSON.stringify({ v: 1, id: "id com espaco", type: "session.hello", payload: {} }))
    ).toThrowError(/id/);
    expect(() =>
      parseEnvelope(JSON.stringify({ v: 1, id: "req-1", type: "Upper.Case", payload: {} }))
    ).toThrowError(/type/);
  });

  it("rejeita estritamente bytes UTF-8 inválidos (sem aceitar U+FFFD)", () => {
    const invalidBytes = Buffer.from(
      '{"v":1,"id":"u1","type":"session.hello","payload":{"bad":"',
      "utf8"
    );
    const tail = Buffer.from('"}}\n', "utf8");
    const corrupted = Buffer.concat([invalidBytes, Buffer.from([0xff, 0xfe]), tail]);

    expect(() => decodeUtf8Strict(corrupted)).toThrowError(ProtocolValidationError);
    expect(() => parseEnvelope(corrupted.subarray(0, corrupted.length - 1))).toThrowError(
      ProtocolValidationError
    );

    const framer = new JsonlLineFramer(1024);
    const validLine = Buffer.from(
      encodeEnvelope({ v: 1, id: "ok-after-bad-utf8", type: "session.hello", payload: {} }),
      "utf8"
    );
    const outcomes = framer.feedOutcomes(Buffer.concat([corrupted, validLine]));
    expect(outcomes).toHaveLength(2);
    expect(outcomes[0].error?.code).toBe("invalid_encoding");
    expect(outcomes[1].line).toBeDefined();
    expect(parseEnvelope(outcomes[1].line!).id).toBe("ok-after-bad-utf8");
  });

  it("preserva frame válido quando precedido por linha oversized no mesmo chunk", () => {
    const framer = new JsonlLineFramer(128);
    const oversized = Buffer.alloc(200, 0x78); // 200 'x'
    const valid = Buffer.from(
      encodeEnvelope({ v: 1, id: "preserved-1", type: "session.hello", payload: {} }),
      "utf8"
    );
    const combined = Buffer.concat([oversized, Buffer.from("\n"), valid]);

    const outcomes = framer.feedOutcomes(combined);
    expect(outcomes).toHaveLength(2);
    expect(outcomes[0].error?.code).toBe("message_too_large");
    expect(outcomes[1].line).toBeDefined();
    expect(parseEnvelope(outcomes[1].line!).id).toBe("preserved-1");
  });

  it("valida MAX_HOST_PIDS=64 e inteiros positivos", () => {
    expect(validateHostPids([1, 2, 3])).toEqual([1, 2, 3]);
    expect(() => validateHostPids(Array.from({ length: 65 }, (_, i) => i + 1))).toThrowError(
      ProtocolValidationError
    );
    expect(() => validateHostPids([0])).toThrowError(ProtocolValidationError);
    expect(() => validateHostPids([-5])).toThrowError(ProtocolValidationError);
  });

  it("remonta frames divididos no meio de caracteres UTF-8 multibyte", () => {
    const framer = new JsonlLineFramer(1024);
    const line1 = encodeEnvelope({
      v: 1,
      id: "utf-1",
      type: "session.hello",
      payload: { text: "Ação rápida — Lunmi 🚀 漢字" },
    });
    const line2 = encodeEnvelope({
      v: 1,
      id: "utf-2",
      type: "app.shutdown",
      payload: {},
    });
    const fullBuffer = Buffer.from(line1 + line2, "utf8");
    const output: string[] = [];
    for (let i = 0; i < fullBuffer.length; i += 5) {
      output.push(...framer.feed(fullBuffer.subarray(i, i + 5)));
    }
    expect(output).toHaveLength(2);
    const parsed1 = parseEnvelope(output[0]);
    const parsed2 = parseEnvelope(output[1]);
    expect(parsed1.payload.text).toBe("Ação rápida — Lunmi 🚀 漢字");
    expect(parsed2.type).toBe("app.shutdown");
  });

  it("valida origem/documento exato do sender frame e bloqueia session.hello pelo renderer", async () => {
    const expectedUrl = "file:///C:/Projects/Lunmi/desktop/dist/index.html";
    const mainFrame = { url: `${expectedUrl}#hash` };
    const wc = { mainFrame, getURL: () => expectedUrl };
    const win = { isDestroyed: () => false, webContents: wc } as unknown as Parameters<
      typeof isTrustedSenderFrame
    >[1];

    expect(
      isTrustedSenderFrame(
        { sender: wc, senderFrame: mainFrame } as unknown as Parameters<
          typeof isTrustedSenderFrame
        >[0],
        win,
        expectedUrl
      )
    ).toBe(true);

    // Outro arquivo local file:// não é aceito
    const evilFrame = { url: "file:///C:/Outro/index.html" };
    expect(
      isTrustedSenderFrame(
        { sender: wc, senderFrame: evilFrame } as unknown as Parameters<
          typeof isTrustedSenderFrame
        >[0],
        win,
        expectedUrl
      )
    ).toBe(false);
    expect(normalizeDocumentUrl(`${expectedUrl}?a=1#b`)).toBe(expectedUrl);

    const sup = new PythonBackendSupervisor({
      repoRoot: path.resolve(__dirname, "..", ".."),
      clientVersion: "0.1.0",
      getHostPids: () => [process.pid],
      onEvent: () => undefined,
    });
    await expect(
      sup.sendRendererCommand("session.hello", { host_pids: [999999] })
    ).rejects.toThrow(/forbidden_command/);
    await expect(sup.sendRendererCommand("app.shutdown", {})).rejects.toThrow(
      /forbidden_command/
    );
  });

  it("completa handshake session.hello -> session.ready -> app.shutdown com o servidor Python real em sandbox", async () => {
    const repoRoot = path.resolve(__dirname, "..", "..");
    const winPython = path.join(repoRoot, ".venv", "Scripts", "python.exe");
    const pythonExe = fs.existsSync(winPython) ? winPython : "python";
    const sandboxDir = fs.mkdtempSync(path.join(os.tmpdir(), "lunmi-f0-vitest-"));

    try {
      const child = spawn(
        pythonExe,
        ["-u", "-m", "app.bridge.server", "--sandbox-dir", sandboxDir],
        {
          cwd: repoRoot,
          stdio: ["pipe", "pipe", "pipe"],
          shell: false,
          windowsHide: true,
        }
      );

      const framer = new JsonlLineFramer();
      const received: ReturnType<typeof parseEnvelope>[] = [];

      const donePromise = new Promise<number | null>((resolve, reject) => {
        child.stdout.on("data", (chunk: Buffer) => {
          for (const raw of framer.feed(chunk)) {
            received.push(parseEnvelope(raw));
          }
        });
        child.on("error", reject);
        child.on("exit", (code) => resolve(code));
      });

      child.stdin.write(
        encodeEnvelope({
          v: 1,
          id: "hello-ts-1",
          type: "session.hello",
          payload: { client: "vitest", host_pids: [process.pid] },
        })
      );
      child.stdin.write(
        encodeEnvelope({
          v: 1,
          id: "shut-ts-1",
          type: "app.shutdown",
          payload: {},
        })
      );

      const exitCode = await donePromise;
      expect(exitCode).toBe(0);

      const readyEvent = received.find((m) => m.type === "session.ready");
      expect(readyEvent).toBeDefined();
      expect((readyEvent!.payload.capabilities as Record<string, boolean>).converse).toBe(true);
      expect((readyEvent!.payload.capabilities as Record<string, boolean>).web_search).toBe(false);

      const helloResult = received.find((m) => m.type === "command.result" && m.id === "hello-ts-1");
      expect(helloResult).toBeDefined();

      const shutResult = received.find((m) => m.type === "command.result" && m.id === "shut-ts-1");
      expect(shutResult).toBeDefined();
      expect(shutResult!.payload.status).toBe("shutting_down");
    } finally {
      fs.rmSync(sandboxDir, { recursive: true, force: true });
    }
  });

  it("sessão de backend mais novo com capability desconhecida continua válida (cliente v1 antigo)", () => {
    // O cliente v1 conhece seis capabilities. Um backend posterior ao T5 acrescenta `file_read`,
    // e um backend futuro pode acrescentar outra: nenhuma delas pode mudar o que o cliente antigo
    // decide, nem fazer a sessão ser recusada.
    const newerBackendReady = {
      v: 1,
      id: "evt-ready-2",
      type: "session.ready",
      payload: {
        ...golden.examples.ready_event.payload,
        capabilities: {
          ...golden.examples.ready_event.payload.capabilities,
          converse: true,
          execute: true,
          memory: true,
          brain: true,
          file_read: true,
          capability_do_futuro: true,
        },
      },
    };

    const envelope = parseEnvelope(JSON.stringify(newerBackendReady));
    expect(envelope.type).toBe("session.ready");
    const capabilities = envelope.payload.capabilities as Record<string, boolean>;
    expect(capabilities.converse).toBe(true);
    expect(capabilities.execute).toBe(true);
    expect(capabilities.memory).toBe(true);
    expect(capabilities.brain).toBe(true);
    expect(capabilities.web_search).toBe(false);
    expect(capabilities.browser_use).toBe(false);
    expect(capabilities.file_read).toBe(true);
    expect(capabilities.capability_do_futuro).toBe(true);
    // O comando novo é opcional no contrato: conhecê-lo ou não não quebra o envelope antigo.
    expect([...COMMAND_TYPES]).toContain("fs.read");
    expect([...golden.commands]).toContain("fs.read");
  });
});

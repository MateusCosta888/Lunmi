/**
 * Aceite GUI do PACOTE instalado, acionando a interface REAL do renderer via CDP.
 *
 * O que é e o que não é: o driver abre o executável instalado, conecta ao DevTools Protocol do
 * renderer e dispara cliques/entradas no DOM verdadeiro — mesmas rotas IPC, mesmos handlers
 * React, mesmo backend empacotado. Não é mouse físico e não substitui uma sessão humana; é a
 * prova automatizável de que os fluxos atravessam a UI e não só a ponte.
 *
 * Fluxos: configurar Brain (provider `custom` apontando para o provedor FAKE de loopback),
 * conversar, memória propor -> aceitar -> buscar -> reiniciar backend -> recuperar -> apagar,
 * fechar a janela. Sem provedor real, sem inferência paga, sem tocar apps pessoais.
 *
 * Uso: node desktop/scripts/ui_acceptance.mjs --app=<Lunmi.exe> --user-data=<dir> \
 *        --port=9223 --fake=http://127.0.0.1:8791/v1 --out=<json>
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const flags = Object.fromEntries(
  process.argv.slice(2).map((raw) => {
    const [key, ...rest] = raw.replace(/^--/, "").split("=");
    return [key, rest.join("=")];
  })
);
const APP = flags.app;
const USER_DATA = flags.userData ?? flags["user-data"];
const DEBUG_PORT = Number(flags.port ?? 9223);
const FAKE = flags.fake ?? "http://127.0.0.1:8791/v1";
const OUT = flags.out ?? "ui-acceptance.json";
const MODEL = "fake-alpha";
const MEMORY_TEXT = "O testador prefere janelas maximizadas (fixture sintética de aceite).";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const HELPERS = `
window.__acc = {
  click: (sel) => { const el = document.querySelector(sel); if (!el) return false; el.click(); return true; },
  set: (sel, value) => {
    const el = document.querySelector(sel); if (!el) return false;
    const proto = el.tagName === "SELECT" ? HTMLSelectElement.prototype
      : el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  },
  testids: (prefix) => Array.from(document.querySelectorAll('[data-testid]'))
    .map((el) => el.getAttribute("data-testid"))
    .filter((id) => !prefix || id.startsWith(prefix)),
  body: () => document.body.innerText,
  status: (sel) => { const el = document.querySelector(sel); return el ? el.textContent : null; },
};
true;
`;

async function waitForTarget(timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${DEBUG_PORT}/json/list`);
      const list = await res.json();
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch {
      /* ainda subindo */
    }
    await sleep(300);
  }
  throw new Error("sem alvo de renderer no CDP");
}

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 0;
    this.pending = new Map();
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      const entry = message.id && this.pending.get(message.id);
      if (!entry) return;
      this.pending.delete(message.id);
      if (message.error) entry.reject(new Error(JSON.stringify(message.error)));
      else entry.resolve(message.result);
    });
  }
  send(method, params = {}) {
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description ?? "erro no renderer");
    }
    return result.result?.value;
  }
}

async function until(cdp, expression, label, timeoutMs = 45000) {
  const deadline = Date.now() + timeoutMs;
  let last;
  while (Date.now() < deadline) {
    last = await cdp.evaluate(expression);
    if (last) return last;
    await sleep(250);
  }
  throw new Error(`tempo esgotado em: ${label}`);
}

async function main() {
  const report = { steps: [], app: APP, userData: USER_DATA, fake: FAKE, model: MODEL };
  const child = spawn(APP, [`--remote-debugging-port=${DEBUG_PORT}`], {
    env: { ...process.env, LUNMI_USER_DATA: USER_DATA },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk.toString("utf8"); });
  child.stdout.on("data", () => {});

  const page = await waitForTarget();
  report.target = { url: page.url, title: page.title };
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve);
    socket.addEventListener("error", reject);
  });
  const cdp = new Cdp(socket);
  await cdp.send("Runtime.enable");
  await cdp.evaluate(HELPERS);

  const step = async (name, fn) => {
    try {
      const detail = await fn();
      report.steps.push({ name, ok: true, detail });
    } catch (error) {
      report.steps.push({ name, ok: false, detail: String(error.message ?? error) });
      throw error;
    }
  };

  try {
    await step("boot: interface montada e sessão pronta", async () => {
      await until(cdp, `Boolean(window.__acc.click && document.querySelector('[data-testid="composer"]'))`, "composer");
      return "composer visível";
    });

    await step("brain: provedor fake de loopback configurado pela UI", async () => {
      await cdp.evaluate(`window.__acc.click('[data-testid="nav-brain"]')`);
      await until(cdp, `Boolean(document.querySelector('[data-testid="brain-panel"]'))`, "brain-panel");
      await cdp.evaluate(`window.__acc.set('[data-testid="provider-select"]', 'custom')`);
      await until(cdp, `Boolean(document.querySelector('[data-testid="base-url"]'))`, "campo de endpoint");
      await cdp.evaluate(`window.__acc.set('[data-testid="base-url"]', ${JSON.stringify(FAKE)})`);
      await cdp.evaluate(`window.__acc.click('[data-testid="list-models"]')`);
      await until(cdp, `Array.from(document.querySelectorAll('[data-testid="model-select"] option')).some(o => o.value === ${JSON.stringify(MODEL)})`, "modelo fake na lista");
      await cdp.evaluate(`window.__acc.set('[data-testid="model-select"]', ${JSON.stringify(MODEL)})`);
      await cdp.evaluate(`window.__acc.click('[data-testid="apply-brain"]')`);
      const status = await until(cdp, `(() => { const s = window.__acc.status('[data-testid="brain-status"]'); return s && s.includes('Cérebro aplicado') ? s : null; })()`, "brain aplicado");
      const current = await cdp.evaluate(`window.__acc.status('[data-testid="current-brain"]')`);
      return { status, current };
    });

    await step("conversa: mensagem chega ao provedor fake e volta pela UI", async () => {
      await cdp.evaluate(`window.__acc.click('[data-testid="nav-chat"]')`);
      await until(cdp, `Boolean(document.querySelector('[data-testid="composer"]'))`, "composer");
      await cdp.evaluate(`window.__acc.set('[data-testid="composer"]', 'Diga olá em uma frase curta.')`);
      await cdp.evaluate(`window.__acc.click('[data-testid="mode-converse"]')`);
      await cdp.evaluate(`window.__acc.click('[data-testid="send-task"]')`);
      const seen = await until(cdp, `window.__acc.body().includes('FAKE local') ? window.__acc.body() : null`, "resposta do provedor fake", 120000);
      return seen.split("\n").filter((line) => line.includes("FAKE local")).slice(0, 1);
    });

    await step("memória: propor pelo painel", async () => {
      await cdp.evaluate(`window.__acc.click('[data-testid="nav-memory"]')`);
      await until(cdp, `Boolean(document.querySelector('[data-testid="memory-panel"]'))`, "memory-panel");
      await until(cdp, `Boolean(document.querySelector('[data-testid="propose-memory"]'))`, "formulário de proposta");
      await cdp.evaluate(`window.__acc.set('[data-testid="propose-kind"]', 'fact')`);
      await cdp.evaluate(`window.__acc.set('[data-testid="propose-text"]', ${JSON.stringify(MEMORY_TEXT)})`);
      await cdp.evaluate(`window.__acc.click('[data-testid="propose-memory"]')`);
      const ids = await until(cdp, `(() => { const ids = window.__acc.testids('proposal-').filter(i => i.endsWith('-accept')); return ids.length ? ids : null; })()`, "proposta pendente");
      return ids;
    });

    const proposalId = await cdp.evaluate(`window.__acc.testids('proposal-').filter(i => i.endsWith('-accept'))[0]`);
    await step("memória: aceitar a proposta", async () => {
      await cdp.evaluate(`window.__acc.click(${JSON.stringify(`[data-testid="${proposalId}"]`)})`);
      const seen = await until(cdp, `Array.from(document.querySelectorAll('[data-testid^="memory-item-"]')).some(el => el.textContent.includes('janelas maximizadas')) ? window.__acc.testids('memory-item-') : null`, "item aceito na lista");
      return seen;
    });

    await step("memória: buscar o item aceito", async () => {
      await cdp.evaluate(`window.__acc.set('[data-testid="memory-search"]', 'maximizadas')`);
      await cdp.evaluate(`window.__acc.click('[data-testid="memory-search-button"]')`);
      const seen = await until(cdp, `Array.from(document.querySelectorAll('[data-testid^="memory-item-"]')).some(el => el.textContent.includes('janelas maximizadas')) ? 'ok' : null`, "busca encontrou o item");
      return seen;
    });

    await step("reinício do backend preserva a memória do testador", async () => {
      await cdp.evaluate(`window.lunmi.restartBackend()`);
      await until(cdp, `Boolean(document.querySelector('[data-testid="composer"]'))`, "sessão nova");
      await sleep(1500);
      await cdp.evaluate(`window.__acc.click('[data-testid="nav-memory"]')`);
      await until(cdp, `Boolean(document.querySelector('[data-testid="memory-panel"]'))`, "memory-panel novo");
      const seen = await until(cdp, `Array.from(document.querySelectorAll('[data-testid^="memory-item-"]')).some(el => el.textContent.includes('janelas maximizadas')) ? 'ok' : null`, "memória recuperada após reinício");
      return seen;
    });

    await step("memória: apagar o item pela UI", async () => {
      const deleteId = await cdp.evaluate(`window.__acc.testids('memory-delete-')[0]`);
      await cdp.evaluate(`window.__acc.click(${JSON.stringify(`[data-testid="${deleteId}"]`)})`);
      await sleep(500);
      const confirmId = await cdp.evaluate(`window.__acc.testids('confirm-delete-')[0]`);
      await cdp.evaluate(`window.__acc.click(${JSON.stringify(`[data-testid="${confirmId}"]`)})`);
      await until(cdp, `!Array.from(document.querySelectorAll('[data-testid^="memory-item-"]')).some(el => el.textContent.includes('janelas maximizadas')) ? 'ok' : null`, "item apagado");
      return { deleteId, confirmId };
    });

    await step("fechar a janela pela própria UI", async () => {
      await cdp.evaluate(`window.lunmi.windowControl('close')`);
      return "windowControl('close')";
    });
  } catch (error) {
    report.failure = String(error.message ?? error);
  } finally {
    try { socket.close(); } catch { /* já fechado */ }
    const exited = await Promise.race([
      new Promise((resolve) => child.once("exit", (code) => resolve(code))),
      sleep(15000).then(() => "timeout"),
    ]);
    report.exitCode = exited;
    report.stderrTail = stderr.slice(-1500);
    fs.mkdirSync(path.dirname(OUT), { recursive: true });
    fs.writeFileSync(OUT, JSON.stringify(report, null, 2), "utf8");
  }

  const failed = report.steps.filter((s) => !s.ok);
  console.log(JSON.stringify({
    steps: report.steps.map((s) => ({ name: s.name, ok: s.ok })),
    exitCode: report.exitCode,
    failure: report.failure ?? null,
  }, null, 2));
  return failed.length === 0 && report.failure === undefined ? 0 : 1;
}

main().then((code) => process.exit(code));

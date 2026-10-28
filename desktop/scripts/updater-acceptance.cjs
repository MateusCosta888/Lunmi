/* Real NSIS updater transport, synthetic payload change; never executes an installer. */
const { app } = require('electron');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const fsp = require('node:fs/promises');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');
const { NsisUpdater } = require('electron-updater');
const { ElectronHttpExecutor } = require('electron-updater/out/electronHttpExecutor');
const { buildBlockMap } = require('app-builder-lib/out/targets/blockmap/blockmap');
const { LunmiUpdates } = require('../dist-electron/electron/updates');

const [source, output] = process.argv.slice(2);
if (!source || !output) throw Error('Usage: Electron updater-acceptance.cjs <existing setup.exe> <new output directory>');
if (fs.existsSync(output)) throw Error('Acceptance output must be a fresh directory');
fs.mkdirSync(output, { recursive: true });
app.setPath('userData', path.resolve(output, 'chromium'));
const report = { syntheticChange: true, nativeInstall: 'NOT_RUN', scenarios: [] };
let server;

app.whenReady().then(async () => {
  const version = '0.1.0-alpha.5', previous = '0.1.0-alpha.4';
  const file = `Lunmi-${version}-Setup.exe`, oldFile = `Lunmi-${previous}-Setup.exe`;
  const payload = path.resolve(output, file);
  await fsp.copyFile(source, payload);
  await fsp.appendFile(payload, Buffer.from('LUNMI-SYNTHETIC-UPDATER-QA-DO-NOT-INSTALL'));
  const blockmap = `${payload}.blockmap`, oldMap = path.resolve(output, `${oldFile}.blockmap`);
  await buildBlockMap(payload, 'gzip', blockmap);
  await buildBlockMap(source, 'gzip', oldMap);
  const hash = crypto.createHash('sha512');
  for await (const chunk of fs.createReadStream(payload)) hash.update(chunk);
  const descriptor = { version, platform: 'win32', arch: 'x64', file, size: (await fsp.stat(payload)).size, sha512: hash.digest('base64') };
  const keys = crypto.generateKeyPairSync('ed25519');
  const raw = Buffer.from(JSON.stringify(descriptor)), signature = crypto.sign(null, raw, keys.privateKey);
  let bytes = 0, ranges = 0, full = 0;
  server = http.createServer((request, response) => {
    const name = path.basename(new URL(request.url, 'http://localhost').pathname);
    if (name === 'alpha.yml') {
      response.end(JSON.stringify({ version, files: [{ url: file, size: descriptor.size, sha512: descriptor.sha512 }] })); return;
    }
    const target = name === file ? payload : name === `${file}.blockmap` ? blockmap : name === `${oldFile}.blockmap` ? oldMap : null;
    if (!target) { response.writeHead(404); response.end(); return; }
    const size = fs.statSync(target).size;
    const range = request.headers.range?.match(/^bytes=(\d+)-(\d+)$/);
    const start = range ? Number(range[1]) : 0, end = range ? Number(range[2]) : size - 1;
    response.writeHead(range ? 206 : 200, { 'Content-Length': end - start + 1, 'Accept-Ranges': 'bytes', ...(range ? { 'Content-Range': `bytes ${start}-${end}/${size}` } : {}) });
    if (name === file) { if (range) ranges++; else full++; }
    const stream = fs.createReadStream(target, { start, end });
    stream.on('data', chunk => { bytes += chunk.length; });
    stream.pipe(response); response.on('close', () => stream.destroy());
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}/`;
  for (const cached of [true, false]) {
    bytes = ranges = full = 0;
    const base = path.resolve(output, cached ? 'differential' : 'fallback');
    const cache = path.join(base, 'cache'); await fsp.mkdir(cache, { recursive: true });
    const config = path.join(base, 'app-update.yml');
    await fsp.writeFile(config, JSON.stringify({ updaterCacheDirName: 'cache' }));
    if (cached) await fsp.copyFile(source, path.join(cache, 'installer.exe'));
    const adapter = { version: previous, name: 'LunmiUpdaterQA', isPackaged: true, appUpdateConfigPath: config, userDataPath: base, baseCachePath: base,
      whenReady: () => Promise.resolve(), onQuit: () => { throw Error('Automatic installation must remain disabled'); }, quit: () => { throw Error('Installer execution is forbidden in transport QA'); } };
    const updater = new NsisUpdater(null, adapter);
    updater.httpExecutor = new ElectronHttpExecutor();
    updater.setFeedURL({ provider: 'generic', url, useMultipleRangeRequest: false, channel: 'alpha' });
    updater.disableWebInstaller = true;
    const logs = []; updater.logger = { info: s => logs.push(s), warn: s => logs.push(s), error: s => logs.push(s), debug: () => {} };
    const controller = new LunmiUpdates({ version: previous, updater, publicKey: keys.publicKey.export({ format: 'pem', type: 'spki' }),
      publish: () => {}, blocked: () => false, confirmRestart: async () => false, finish: async () => { throw Error('Unexpected install'); },
      fetch: async value => value.endsWith('.sig') ? signature : raw });
    const state = await controller.check();
    await fsp.writeFile(path.join(base, 'transport.log'), logs.join('\n'));
    assert.equal(state.phase, 'ready', JSON.stringify(state));
    assert.equal(updater.autoInstallOnAppQuit, false);
    if (cached) { assert(ranges > 0); assert.equal(full, 0); assert(bytes < descriptor.size / 2); }
    else { assert.equal(full, 1); assert(bytes >= descriptor.size); }
    report.scenarios.push({ cached, state: state.phase, bytes, ranges, fullDownloads: full, installerBytes: descriptor.size });
    controller.dispose();
  }
  report.ok = true;
}).catch(error => { report.ok = false; report.error = String(error.stack); process.exitCode = 1; })
  .finally(() => {
    server?.close(); fs.writeFileSync(path.resolve(output, 'RESULT.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report)); app.exit(report.ok ? 0 : 1);
  });

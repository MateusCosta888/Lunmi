// Junta a janela do instalador (installer/, Rust + WebView2) ao Setup NSIS do electron-builder:
//   lunmi-setup.exe + Setup NSIS + manifesto JSON + trailer [payload_len u64][manifest_len u64]["LUNMISFX"]
// e reescreve o latest.yml (tamanho e sha512) para o electron-updater descarregar o ficheiro novo.
// Corre depois do electron-builder e antes de scripts/update-release.mjs (que assina o resultado).
import { createHash } from 'node:crypto';
import { createReadStream, createWriteStream, existsSync, readFileSync, readdirSync, renameSync, rmSync, statSync, writeFileSync, openSync, readSync, closeSync } from 'node:fs';
import { resolve, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { pipeline } from 'node:stream/promises';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import yaml from 'js-yaml';

const { buildBlockMap } = createRequire(import.meta.url)('app-builder-lib/out/targets/blockmap/blockmap');

const desktop = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const MAGIC = Buffer.from('LUNMISFX');

const dirSize = p => readdirSync(p, { withFileTypes: true })
  .reduce((sum, e) => sum + (e.isDirectory() ? dirSize(join(p, e.name)) : statSync(join(p, e.name)).size), 0);

export function readComponents(nsh) {
  const defines = Object.fromEntries([...readFileSync(nsh, 'utf8').matchAll(/^!define LUNMI_(\w+?)_(KEY|MB|ARCHIVE_MB) "?([^"\r\n]+)"?$/gm)]
    .map(([, name, kind, value]) => [`${name}_${kind}`, value]));
  return ['PYTHON', 'VOICE'].map(name => {
    const [key, mb, archiveMb] = [defines[`${name}_KEY`], Number(defines[`${name}_MB`]), Number(defines[`${name}_ARCHIVE_MB`])];
    if (!/^[0-9a-f]{16}$/.test(key ?? '') || !(mb > 0) || !(archiveMb > 0)) throw Error(`Componente ${name} em falta em ${nsh}`);
    return { name: name.toLowerCase(), key, unpacked_bytes: mb * 1048576, archive_bytes: archiveMb * 1048576 };
  });
}

export function hasTrailer(file) {
  const size = statSync(file).size;
  if (size < 24) return false;
  const fd = openSync(file, 'r'), tail = Buffer.alloc(8);
  try { readSync(fd, tail, 0, 8, size - 8); } finally { closeSync(fd); }
  return tail.equals(MAGIC);
}

/** Repõe o setup NSIS original a partir de um setup já embrulhado. */
export async function unwrap(setup) {
  const size = statSync(setup).size, fd = openSync(setup, 'r'), trailer = Buffer.alloc(24);
  try { readSync(fd, trailer, 0, 24, size - 24); } finally { closeSync(fd); }
  const payloadLen = Number(trailer.readBigUInt64LE(0)), manifestLen = Number(trailer.readBigUInt64LE(8));
  const start = size - 24 - manifestLen - payloadLen;
  if (start <= 0) throw Error('Setup embrulhado inválido');
  await pipeline(createReadStream(setup, { start, end: start + payloadLen - 1 }), createWriteStream(`${setup}.nsis`));
  renameSync(`${setup}.nsis`, setup);
}

export async function wrap(release, version, ui = resolve(desktop, 'installer/target/release/lunmi-setup.exe'), components = resolve(desktop, 'build/components/lunmi-components.nsh')) {
  const file = `Lunmi-${version}-Setup.exe`;
  const setup = resolve(release, file);
  if (hasTrailer(ui)) throw Error('lunmi-setup.exe inválido');
  // A janela arranca antes de qualquer runtime: sem VCRUNTIME/MSVCP/WebView2Loader em DLL (falha aqui, não no PC limpo).
  execFileSync(process.env.PYTHON ?? 'python', [resolve(desktop, 'scripts/verify_installer_runtime.py'), ui], { stdio: 'inherit' });
  if (hasTrailer(setup)) await unwrap(setup); // nova janela sobre o mesmo setup NSIS, sem refazer o build
  const manifest = Buffer.from(JSON.stringify({ version, app_bytes: dirSize(resolve(release, 'win-unpacked')), components: readComponents(components) }));
  const payloadLen = statSync(setup).size;
  const trailer = Buffer.alloc(24);
  trailer.writeBigUInt64LE(BigInt(payloadLen), 0);
  trailer.writeBigUInt64LE(BigInt(manifest.length), 8);
  MAGIC.copy(trailer, 16);

  const tmp = `${setup}.wrapping`;
  const out = createWriteStream(tmp);
  await pipeline(createReadStream(ui), out, { end: false });
  await pipeline(createReadStream(setup), out, { end: false });
  out.end(Buffer.concat([manifest, trailer]));
  await new Promise((ok, fail) => { out.on('finish', ok); out.on('error', fail); });
  renameSync(tmp, setup);

  const hash = createHash('sha512');
  for await (const chunk of createReadStream(setup)) hash.update(chunk);
  const sha512 = hash.digest('base64'), size = statSync(setup).size;
  const latest = resolve(release, 'latest.yml');
  const metadata = yaml.load(readFileSync(latest, 'utf8'));
  if (metadata?.version !== version || metadata.files?.length !== 1 || metadata.files[0].url !== file) throw Error('latest.yml não corresponde ao Setup');
  metadata.files[0].sha512 = sha512;
  metadata.files[0].size = size;
  delete metadata.files[0].blockMapSize;
  metadata.sha512 = sha512;
  writeFileSync(latest, yaml.dump(metadata, { lineWidth: -1 }));
  // O blockmap antigo descrevia só o Setup NSIS; o novo mantém as atualizações diferenciais.
  rmSync(`${setup}.blockmap`, { force: true });
  await buildBlockMap(setup, 'gzip', `${setup}.blockmap`);
  return { file, size, sha512, payloadLen };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const { version } = JSON.parse(readFileSync(resolve(desktop, 'package.json'), 'utf8'));
  const release = resolve(process.argv[2] ?? resolve(desktop, 'release'));
  if (!existsSync(release)) throw Error(`Pasta ${release} não existe`);
  const r = await wrap(release, version);
  console.log(`${r.file}: ${(r.size / 1048576).toFixed(1)} MB (instalador NSIS ${(r.payloadLen / 1048576).toFixed(1)} MB + janela)`);
}

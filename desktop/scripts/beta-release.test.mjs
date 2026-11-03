import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm, lstat, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { createHash, generateKeyPairSync } from 'node:crypto';
import { writeFileSync, readFileSync, existsSync } from 'node:fs';
import * as RE from 'resedit';
import { belongsToApp, copyTree, splitApp, packDirectory, packComponents, packageApp, materializeVerification, uploadPacks } from './beta-release.mjs';
import { RELEASE_URL, PACK_NAMES } from './update-release.mjs';
import { ELECTRON_VERSION } from './normalize-base.mjs';

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
async function put(root, path, bytes = path) {
  await mkdir(dirname(join(root, path)), { recursive: true });
  await writeFile(join(root, path), bytes);
}
async function temporary(fn) {
  const dir = await mkdtemp(join(tmpdir(), 'lunmi-beta-'));
  try { await fn(dir); } finally { await rm(dir, { recursive: true, force: true }); }
}

test('base/app split keeps Python code, keys and licenses in app; Electron remains in base', () => temporary(async dir => {
  for (const file of ['Lunmi-app.exe', 'locales/en.pak', 'resources/app.asar', 'resources/app/app/main.py',
    'resources/app.asar.unpacked/addon.node', 'resources/lunmi-components.json', 'resources/licenses/MIT.txt',
    'resources/THIRD_PARTY_NOTICES.md', 'LICENSES.chromium.html']) await put(join(dir, 'unpacked'), file);
  await splitApp(join(dir, 'unpacked'), join(dir, 'sources'));
  for (const file of ['Lunmi-app.exe', 'locales/en.pak']) assert.equal(await readFile(join(dir, 'sources/base', file), 'utf8'), file);
  for (const file of ['resources/app.asar', 'resources/app/app/main.py', 'resources/lunmi-components.json', 'LICENSES.chromium.html']) {
    assert.equal(await readFile(join(dir, 'sources/app', file), 'utf8'), file);
    await assert.rejects(lstat(join(dir, 'sources/base', file)), { code: 'ENOENT' });
  }
  assert.equal(belongsToApp('resources/app-extra/x'), false);
}));

test('voice staging excludes models and copy never replaces existing source files', () => temporary(async dir => {
  await put(dir, 'voice/runtime/python.exe'); await put(dir, 'voice/models/model.bin'); await put(dir, 'voice/NOTICE.txt');
  await copyTree(join(dir, 'voice'), join(dir, 'copy'), path => path !== 'models' && !path.startsWith('models/'));
  await lstat(join(dir, 'copy/runtime/python.exe'));
  await assert.rejects(lstat(join(dir, 'copy/models')), { code: 'ENOENT' });
  await assert.rejects(copyTree(join(dir, 'voice'), join(dir, 'copy')), { code: 'EEXIST' });
}));

test('base/app split refuses either runtime duplicated inside Electron resources', async () => {
  for (const name of ['python', 'voice', 'Python', 'VOICE']) await temporary(async dir => {
    await put(dir, 'unpacked/Lunmi-app.exe'); await put(dir, 'unpacked/resources/app.asar');
    await put(dir, 'unpacked/resources/lunmi-components.json', '{}');
    await put(dir, `unpacked/resources/${name}/runtime.exe`);
    await assert.rejects(splitApp(join(dir, 'unpacked'), join(dir, 'sources')), /Duplicated runtime/);
  });
});

test('copy rejects symlinks before copying outside the source', () => temporary(async dir => {
  await mkdir(join(dir, 'source')); await mkdir(join(dir, 'outside'));
  await symlink(join(dir, 'outside'), join(dir, 'source/link'), process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(copyTree(join(dir, 'source'), join(dir, 'copy')), /regular files/);
}));

test('pack CLI metadata is checked against bytes and unsafe keys fail before file access', () => temporary(async dir => {
  await put(dir, 'packs/python-0123456789abcdef.tar.zst', 'synthetic');
  const pack = { name: 'python', key: '0123456789abcdef', size: 9, unpacked: 10, sha256: sha('synthetic') };
  const run = (file, args) => { assert.equal(file, 'launcher'); assert.deepEqual(args, ['pack', 'source', 'python', join(dir, 'packs')]); return JSON.stringify(pack); };
  assert.equal((await packDirectory('launcher', 'source', 'python', join(dir, 'packs'), run)).key, pack.key);
  pack.sha256 = '0'.repeat(64);
  await assert.rejects(packDirectory('launcher', 'source', 'python', join(dir, 'packs'), run), /integrity/);
  pack.key = '../escape';
  await assert.rejects(packDirectory('launcher', 'source', 'python', join(dir, 'packs'), run), /Invalid release pack/);
}));

test('verification tree uses immutable store keys and composes base plus app', () => temporary(async dir => {
  await put(dir, 'resources/python/python.exe');
  for (const name of ['base', 'app', 'voice']) await put(dir, `release/sources/${name}/${name}.txt`);
  const packs = PACK_NAMES.map(name => ({ name, key: '0123456789abcdef' }));
  const root = await materializeVerification(join(dir, 'resources'), join(dir, 'release'), { version: '1.0.0-beta.1', packs });
  await lstat(join(root, 'base.txt')); await lstat(join(root, 'app.txt'));
  for (const pack of packs) assert.equal(await readFile(join(dir, 'release/beta-root/store', `${pack.name}-${pack.key}`, '.complete'), 'utf8'), pack.key);
}));

test('existing pack collisions stop all uploads; matching bytes are reused without clobber', () => temporary(async dir => {
  const packs = [];
  for (const [index, name] of PACK_NAMES.entries()) {
    const key = String(index).repeat(16), filename = `${name}-${key}.tar.zst`;
    await put(dir, `packs/${filename}`, name);
    packs.push({ name, key, size: name.length, unpacked: 100, sha256: sha(name), url: `${RELEASE_URL}/packs/${filename}` });
  }
  await put(dir, 'latest.json', JSON.stringify({ schema: 1, channel: 'beta', version: '1.0.0', notes: '', published: '2026-11-03T12:00:00Z', packs,
    setup: { size: 1, sha256: 'f'.repeat(64), url: `${RELEASE_URL}/v1.0.0/Lunmi-1.0.0-Setup.exe` } }));
  const calls = [];
  const run = (command, args) => {
    calls.push(args);
    assert.equal(command, 'gh'); assert.ok(!args.includes('--clobber'));
    if (args[1] === 'view') return JSON.stringify({ isDraft: false, assets: [{ name: 'voice-3333333333333333.tar.zst' }] });
    if (args[1] === 'download') {
      // execFileSync is synchronous, so the synthetic download is seeded before the call.
      return '';
    }
    return '';
  };
  const collisionRun = (command, args) => {
    if (args[1] === 'download') writeFileSync(join(dir, 'existing-packs/voice-3333333333333333.tar.zst'), 'collision');
    return run(command, args);
  };
  await assert.rejects(uploadPacks(dir, collisionRun), /integrity/);
  assert.equal(calls.filter(args => args[1] === 'upload').length, 0);
  await rm(join(dir, 'existing-packs'), { recursive: true }); calls.length = 0;
  const matchingRun = (command, args) => {
    if (args[1] === 'download') writeFileSync(join(dir, 'existing-packs/voice-3333333333333333.tar.zst'), 'voice');
    return run(command, args);
  };
  await uploadPacks(dir, matchingRun);
  assert.equal(calls.filter(args => args[1] === 'upload').length, 3);
}));

test('synthetic release obeys runtime keys -> app pack -> bundle -> final signing order', () => temporary(async dir => {
  const resources = join(dir, 'resources'), release = join(dir, 'release');
  const launcher = join(dir, 'synthetic-launcher.exe');
  await put(dir, 'synthetic-launcher.exe', 'synthetic-launcher-bytes');
  await put(resources, 'python/python.exe'); await put(resources, 'voice/runtime/python.exe');
  await put(resources, 'voice/models/private-model.bin'); await put(resources, 'voice/NOTICE.txt');
  const calls = [];
  const run = (executable, args) => {
    assert.equal(executable, launcher); calls.push(args);
    if (args[0] === 'pack') {
      const [, source, name, output] = args;
      const key = String(PACK_NAMES.indexOf(name)).repeat(16);
      if (name === 'voice') assert.equal(existsSync(join(source, 'models')), false);
      if (name === 'base') {
        const resources = RE.NtExecutableResource.from(RE.NtExecutable.from(readFileSync(join(source, 'Lunmi-app.exe'))));
        assert.equal(resources.entries.some(entry => entry.type === 'INTEGRITY' && entry.id === 'ELECTRONASAR'), false);
        const info = RE.Resource.VersionInfo.fromEntries(resources.entries)[0];
        assert.equal(info.getStringValues({ lang: 1033, codepage: 1200 }).ProductVersion, `${ELECTRON_VERSION}.0`);
      }
      if (name === 'app') {
        assert.deepEqual(JSON.parse(readFileSync(join(source, 'resources/lunmi-components.json'))), { python: '2222222222222222', voice: '3333333333333333' });
        assert.equal(readFileSync(join(source, 'Lunmi.exe'), 'utf8'), 'synthetic-launcher-bytes');
      }
      writeFileSync(join(output, `${name}-${key}.tar.zst`), name);
      return JSON.stringify({ name, key, size: name.length, unpacked: 100, sha256: sha(name) });
    }
    assert.equal(args[0], 'bundle');
    const embedded = JSON.parse(readFileSync(args[1]));
    assert.equal('setup' in embedded, false);
    assert.equal(embedded.packs.length, 4);
    writeFileSync(args[3], 'synthetic-setup');
  };
  await packComponents(launcher, resources, release, run);
  const unpacked = join(release, 'win-unpacked');
  const executable = RE.NtExecutable.createEmpty(false, false);
  const peResources = RE.NtExecutableResource.from(executable);
  const info = RE.Resource.VersionInfo.createEmpty();
  info.lang = 1033;
  info.setFileVersion('1.0.0.2'); info.setProductVersion('1.0.0.2');
  info.outputToResourceEntries(peResources.entries);
  peResources.replaceResourceEntryFromString('INTEGRITY', 'ELECTRONASAR', 1033, 'synthetic-app-hash');
  peResources.outputResource(executable);
  executable.setExtraData(Buffer.concat([Buffer.from('dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX'), Buffer.from([1, 8, 49, 48, 49, 49, 48, 48, 48, 48])]));
  const electronSource = Buffer.from(executable.generate());
  await put(unpacked, 'Lunmi-app.exe', electronSource); await put(unpacked, 'resources/app.asar');
  await put(unpacked, 'resources/lunmi-components.json', await readFile(join(resources, 'lunmi-components.json')));
  await packageApp(launcher, resources, release, '1.0.0-beta.2', run);
  assert.deepEqual(await readFile(join(unpacked, 'Lunmi-app.exe')), electronSource);
  assert.deepEqual(calls.map(args => args[0] === 'pack' ? args[2] : args[0]), ['python', 'voice', 'base', 'app', 'bundle']);
  const { signRelease, verifyRelease } = await import('./update-release.mjs');
  const keys = generateKeyPairSync('ed25519');
  const publicKey = keys.publicKey.export({ type: 'spki', format: 'pem' });
  await signRelease(release, keys.privateKey.export({ type: 'pkcs8', format: 'pem' }), publicKey, '1.0.0-beta.2');
  await verifyRelease(release, publicKey, '1.0.0-beta.2');
  assert.equal(await readFile(join(release, 'beta-root/versions/1.0.0-beta.2/Lunmi.exe'), 'utf8'), 'synthetic-launcher-bytes');
  assert.equal(await readFile(launcher, 'utf8'), 'synthetic-launcher-bytes');
  await assert.rejects(lstat(join(release, 'beta-root/store/voice-3333333333333333/models')), { code: 'ENOENT' });
}));

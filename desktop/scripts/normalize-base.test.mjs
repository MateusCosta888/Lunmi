import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, readFile, writeFile, link, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { FuseV1Options, getCurrentFuseWire } from '@electron/fuses';
import * as RE from 'resedit';
import { ELECTRON_VERSION, normalizeBase } from './normalize-base.mjs';

const sentinel = 'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX';
const language = { lang: 1033, codepage: 1200 };

function syntheticPE(version, integrity, fuse = 0x30, wireVersion = 1) {
  const executable = RE.NtExecutable.createEmpty(false, false);
  const resources = RE.NtExecutableResource.from(executable);
  const info = RE.Resource.VersionInfo.createEmpty();
  info.lang = 1033;
  info.setFileVersion(version);
  info.setProductVersion(version);
  for (const lang of [language, { lang: 2070, codepage: 1252 }]) {
    info.setStringValues(lang, { FileVersion: `${version}-beta`, ProductVersion: `${version}-beta`,
      ProductName: 'Lunmi', FileDescription: 'Lunmi', OriginalFilename: 'Lunmi-app.exe', InternalName: 'Lunmi' });
  }
  info.outputToResourceEntries(resources.entries);
  for (const [type, id, bin] of [[3, 1, Buffer.from('synthetic-icon')], [14, 1, Buffer.from('synthetic-icon-group')],
    ['INTEGRITY', 'ELECTRONASAR', Buffer.from(integrity)], ['INTEGRITY', 'OTHER', Buffer.from('keep-other-resource')]]) {
    resources.entries.push({ type, id, lang: 1033, codepage: 1200, bin });
  }
  resources.outputResource(executable);
  executable.setExtraData(Buffer.concat([Buffer.from(sentinel), Buffer.from([wireVersion, 8, 0x31, 0x30, 0x31, 0x31, fuse, 0x30, 0x30, 0x30])]));
  return Buffer.from(executable.generate());
}

function contentKey(bytes) {
  const digest = createHash('sha256').update(bytes).digest('hex');
  return createHash('sha256').update(`Lunmi-app.exe\0${bytes.length}\0${digest}\n`).digest('hex').slice(0, 16);
}

async function temporary(fn) {
  const dir = await mkdtemp(join(tmpdir(), 'lunmi-normalize-'));
  try { await fn(dir); } finally { await rm(dir, { recursive: true, force: true }); }
}

test('different beta versions and ASAR resources normalize to identical bytes and base keys', () => temporary(async dir => {
  const one = join(dir, 'one.exe'), two = join(dir, 'two.exe');
  const originalOne = syntheticPE('1.0.0.1', 'a'.repeat(64));
  const originalTwo = syntheticPE('12.13.14.15', 'b'.repeat(4096));
  assert.notDeepEqual(originalOne, originalTwo);
  await writeFile(one, originalOne); await writeFile(two, originalTwo);
  await normalizeBase(one); await normalizeBase(two);
  const normalized = await readFile(one);
  assert.deepEqual(normalized, await readFile(two));
  assert.equal(contentKey(normalized), contentKey(await readFile(two)));
  const resources = RE.NtExecutableResource.from(RE.NtExecutable.from(normalized));
  assert.equal(resources.entries.some(entry => entry.type === 'INTEGRITY' && entry.id === 'ELECTRONASAR'), false);
  const [major, minor, patch] = ELECTRON_VERSION.split('.').map(Number);
  for (const info of RE.Resource.VersionInfo.fromEntries(resources.entries)) {
    assert.equal(info.fixedInfo.fileVersionMS, (major << 16) | minor);
    assert.equal(info.fixedInfo.productVersionMS, (major << 16) | minor);
    assert.equal(info.fixedInfo.fileVersionLS, patch << 16);
    assert.equal(info.fixedInfo.productVersionLS, patch << 16);
    for (const lang of info.getAllLanguagesForStringValues()) {
      const values = info.getStringValues(lang);
      assert.equal(values.FileVersion, `${ELECTRON_VERSION}.0`);
      assert.equal(values.ProductVersion, `${ELECTRON_VERSION}.0`);
      assert.equal(values.ProductName, 'Lunmi'); assert.equal(values.FileDescription, 'Lunmi');
      assert.equal(values.OriginalFilename, 'Lunmi-app.exe'); assert.equal(values.InternalName, 'Lunmi');
    }
  }
  const before = RE.NtExecutableResource.from(RE.NtExecutable.from(originalOne)).entries.filter(entry => entry.type !== 16 && entry.id !== 'ELECTRONASAR');
  const after = resources.entries.filter(entry => entry.type !== 16 && entry.id !== 'ELECTRONASAR');
  assert.deepEqual(after, before);
  assert.deepEqual(await getCurrentFuseWire(one), await getCurrentFuseWire(two));
  assert.equal((await getCurrentFuseWire(one))[FuseV1Options.EnableEmbeddedAsarIntegrityValidation], 0x30);
  await normalizeBase(one);
  assert.deepEqual(await readFile(one), normalized);
}));

test('normalizing a hardlinked base replaces only the destination and leaves source bytes intact', () => temporary(async dir => {
  const source = join(dir, 'win-unpacked.exe'), destination = join(dir, 'base.exe');
  const original = syntheticPE('1.0.0.9', 'c'.repeat(64));
  await writeFile(source, original); await link(source, destination);
  await normalizeBase(destination);
  assert.deepEqual(await readFile(source), original);
  assert.notDeepEqual(await readFile(destination), original);
  assert.deepEqual((await readdir(dir)).sort(), ['base.exe', 'win-unpacked.exe']);
}));

test('enabled, removed, malformed or unknown ASAR fuse fails closed without writing', async () => {
  for (const [state, wireVersion] of [[0x31, 1], [0x72, 1], [0x90, 1], [0xff, 1], [0x30, 2]]) await temporary(async dir => {
    const file = join(dir, 'base.exe');
    const original = syntheticPE('1.0.0.1', 'integrity', state, wireVersion);
    await writeFile(file, original);
    await assert.rejects(normalizeBase(file), /fuse must be explicitly disabled/);
    assert.deepEqual(await readFile(file), original);
    assert.deepEqual(await readdir(dir), ['base.exe']);
  });
  await temporary(async dir => {
    const file = join(dir, 'base.exe');
    await writeFile(file, 'no-fuse-wire');
    await assert.rejects(normalizeBase(file), /sentinel/);
    assert.equal(await readFile(file, 'utf8'), 'no-fuse-wire');
  });
});

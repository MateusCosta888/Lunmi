import { test } from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, sign, verify } from 'node:crypto';
import { mkdtemp, mkdir, writeFile, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PACK_NAMES, RELEASE_URL, artifactInfo, compareVersions, signRelease, validateManifest, validateVersion, verifyChannel, verifyRelease } from './update-release.mjs';

export async function fixture(directory, version = '1.0.0-beta.2') {
  await mkdir(join(directory, 'packs'));
  const packs = [];
  for (const [index, name] of PACK_NAMES.entries()) {
    const key = String(index).repeat(16);
    const file = `${name}-${key}.tar.zst`;
    await writeFile(join(directory, 'packs', file), `synthetic-${name}`);
    packs.push({ name, key, ...await artifactInfo(join(directory, 'packs', file)), unpacked: 123,
      url: `${RELEASE_URL}/packs/${file}` });
  }
  await writeFile(join(directory, `Lunmi-${version}-Setup.exe`), 'synthetic-not-executable');
  const manifest = { schema: 1, channel: 'beta', version, published: '2026-11-03T12:00:00Z', notes: 'synthetic', packs };
  await writeFile(join(directory, 'manifest-bundle.json'), JSON.stringify(manifest));
  const keys = generateKeyPairSync('ed25519');
  return { manifest, publicKey: keys.publicKey.export({ format: 'pem', type: 'spki' }).toString(),
    privateKey: keys.privateKey.export({ format: 'pem', type: 'pkcs8' }) };
}

test('release manifest signs exact final bytes and binds setup plus all four packs', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'lunmi-release-'));
  try {
    const { publicKey, privateKey } = await fixture(dir);
    const manifest = await signRelease(dir, privateKey, publicKey, '1.0.0-beta.2');
    const raw = await readFile(join(dir, 'latest.json'));
    const signature = await readFile(join(dir, 'latest.json.sig'));
    assert.equal(signature.length, 64);
    assert.equal(verify(null, raw, publicKey, signature), true);
    assert.deepEqual(await verifyRelease(dir, publicKey, manifest.version), manifest);
    assert.equal(manifest.setup.sha256, (await artifactInfo(join(dir, 'Lunmi-1.0.0-beta.2-Setup.exe'))).sha256);
    await writeFile(join(dir, 'latest.json'), Buffer.concat([raw, Buffer.from(' ')]));
    await assert.rejects(verifyRelease(dir, publicKey, manifest.version), /signature/);
    await writeFile(join(dir, 'latest.json'), raw);
    await writeFile(join(dir, 'packs', 'app-1111111111111111.tar.zst'), 'tampered');
    await assert.rejects(verifyRelease(dir, publicKey, manifest.version), /integrity/);
    await assert.rejects(signRelease(dir, privateKey, publicKey, manifest.version), /integrity/);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('signing rejects key mismatch, embedded setup and stale versions', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'lunmi-release-'));
  try {
    const { manifest, publicKey, privateKey } = await fixture(dir);
    const wrong = generateKeyPairSync('ed25519').privateKey.export({ format: 'pem', type: 'pkcs8' });
    await assert.rejects(signRelease(dir, wrong, publicKey, manifest.version), /does not match/);
    await assert.rejects(signRelease(dir, privateKey, publicKey, '1.0.0-beta.3'), /manifest/);
    manifest.setup = {};
    await writeFile(join(dir, 'manifest-bundle.json'), JSON.stringify(manifest));
    await assert.rejects(signRelease(dir, privateKey, publicKey, manifest.version), /setup/);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('manifest validation refuses unsafe URLs, duplicate names, invalid hashes and oversized notes', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'lunmi-release-'));
  try {
    const { manifest } = await fixture(dir);
    assert.equal(validateManifest(manifest, manifest.version, false), manifest);
    for (const mutate of [m => { m.packs[0].url += '?evil'; }, m => { m.packs[0].key = '../escape'; },
      m => { m.packs[1] = m.packs[0]; }, m => { m.packs[0].sha256 = 'f'.repeat(63); },
      m => { m.packs[0].size = -1; }, m => { m.packs[0].unpacked = 1.5; },
      m => { m.notes = 'x'.repeat(65536); }]) {
      const invalid = structuredClone(manifest); mutate(invalid);
      assert.throws(() => validateManifest(invalid, manifest.version, false));
    }
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('only strict beta or stable semver is accepted', () => {
  for (const valid of ['1.0.0-beta.0', '1.0.0-beta.12', '1.0.0']) assert.equal(validateVersion(valid), valid);
  for (const bad of ['1.0.0-alpha.1', '1.0.0-rc.1', '1.0.0-beta.01', '01.0.0', '../escape', '1.0.0;echo', '1.0.0\n', undefined]) {
    assert.throws(() => validateVersion(bad), /Invalid release version/);
  }
});

test('stable releases use the same signed beta channel contract', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'lunmi-release-'));
  try {
    const { publicKey, privateKey } = await fixture(dir, '1.0.0');
    await signRelease(dir, privateKey, publicKey, '1.0.0');
    await writeFile(join(dir, 'Lunmi-1.0.0-Setup.exe'), 'changed');
    await verifyRelease(dir, publicKey, '1.0.0', true);
    await assert.rejects(verifyRelease(dir, publicKey, '1.0.0'), /integrity/);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('channel versions compare numerically, with stable after beta', () => {
  for (const [older, newer] of [['1.0.0-beta.2', '1.0.0-beta.10'], ['1.0.0-beta.99', '1.0.0'],
    ['1.0.0', '1.0.1-beta.0'], ['1.9.9', '2.0.0-beta.0'], ['1.0.0-beta.99999999999999999999', '1.0.0-beta.100000000000000000000']]) {
    assert.equal(compareVersions(older, newer), -1);
    assert.equal(compareVersions(newer, older), 1);
    assert.equal(compareVersions(older, older), 0);
  }
  assert.equal(compareVersions('1.0.0', '1.0.0'), 0);
  assert.throws(() => compareVersions('1.0.0-alpha.1', '1.0.0'), /Invalid release version/);
});

test('channel accepts an empty pair, signed advance or identical retry; rejects rollback, partial and changed same version', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'lunmi-channel-'));
  try {
    const prepared = join(dir, 'prepared'), current = join(dir, 'current');
    await mkdir(prepared); await mkdir(current);
    const { publicKey, privateKey } = await fixture(prepared);
    const next = await signRelease(prepared, privateKey, publicKey, '1.0.0-beta.2');
    const updateCurrent = async (version, notes = next.notes) => {
      const manifest = structuredClone(next);
      manifest.version = version; manifest.notes = notes;
      manifest.setup.url = `${RELEASE_URL}/v${version}/Lunmi-${version}-Setup.exe`;
      const raw = Buffer.from(JSON.stringify(manifest) + '\n');
      await writeFile(join(current, 'latest.json'), raw);
      await writeFile(join(current, 'latest.json.sig'), sign(null, raw, privateKey));
    };
    verifyChannel(prepared, current, publicKey, next.version);
    await updateCurrent('1.0.0-beta.1');
    verifyChannel(prepared, current, publicKey, next.version);
    await updateCurrent(next.version);
    verifyChannel(prepared, current, publicKey, next.version);
    await updateCurrent(next.version, 'changed signed notes');
    assert.throws(() => verifyChannel(prepared, current, publicKey, next.version), /exact signed bytes/);
    for (const newer of ['1.0.0-beta.3', '1.0.0', '1.0.1-beta.0']) {
      await updateCurrent(newer);
      assert.throws(() => verifyChannel(prepared, current, publicKey, next.version), /backwards/);
    }
    await updateCurrent('1.0.0-beta.1');
    await writeFile(join(current, 'latest.json.sig'), Buffer.alloc(64));
    assert.throws(() => verifyChannel(prepared, current, publicKey, next.version), /signature/);
    await rm(join(current, 'latest.json.sig'));
    assert.throws(() => verifyChannel(prepared, current, publicKey, next.version), /Incomplete channel pair/);
    await rm(join(current, 'latest.json'));
    await writeFile(join(current, 'latest.json.sig'), Buffer.alloc(64));
    assert.throws(() => verifyChannel(prepared, current, publicKey, next.version), /Incomplete channel pair/);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

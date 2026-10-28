import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash, generateKeyPairSync, verify } from 'node:crypto';
import { mkdtemp, writeFile, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { signRelease } from './update-release.mjs';

test('release descriptor binds installer bytes to the embedded key', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'lunmi-release-signing-'));
  try {
    const keys = generateKeyPairSync('ed25519');
    const publicKey = keys.publicKey.export({ format: 'pem', type: 'spki' }).toString();
    const privateKey = keys.privateKey.export({ format: 'pem', type: 'pkcs8' });
    await writeFile(join(dir, 'Lunmi-0.1.0-alpha.4-Setup.exe'), 'synthetic-not-executable');
    const metadata = { version: '0.1.0-alpha.4', files: [{ url: 'Lunmi-0.1.0-alpha.4-Setup.exe',
      size: Buffer.byteLength('synthetic-not-executable'), sha512: createHash('sha512').update('synthetic-not-executable').digest('base64') }] };
    await writeFile(join(dir, 'latest.yml'), JSON.stringify(metadata));
    const descriptor = await signRelease(dir, privateKey, publicKey, '0.1.0-alpha.4');
    const raw = await readFile(join(dir, 'lunmi-update.json'));
    assert.equal(verify(null, raw, publicKey, await readFile(join(dir, 'lunmi-update.sig'))), true);
    assert.equal(JSON.parse(raw).sha512, descriptor.sha512);
    const wrongKey = generateKeyPairSync('ed25519').privateKey.export({ format: 'pem', type: 'pkcs8' });
    await assert.rejects(signRelease(dir, wrongKey, publicKey, '0.1.0-alpha.4'), /does not match/);
    await assert.rejects(signRelease(dir, privateKey, publicKey, '../escape'), /Invalid release version/);
    metadata.files[0].size += 1;
    await writeFile(join(dir, 'latest.yml'), JSON.stringify(metadata));
    await assert.rejects(signRelease(dir, privateKey, publicKey, '0.1.0-alpha.4'), /metadata does not match/);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

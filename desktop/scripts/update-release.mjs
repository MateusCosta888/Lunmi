import { createHash, createPrivateKey, createPublicKey, sign, verify } from 'node:crypto';
import { createReadStream, existsSync, readFileSync, writeFileSync, statSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const desktop = resolve(dirname(fileURLToPath(import.meta.url)), '..');
export const RELEASE_URL = 'https://github.com/MateusCosta888/Lunmi/releases/download';
export const PACK_NAMES = ['base', 'app', 'python', 'voice'];
const hex = /^[a-f0-9]{64}$/;

export function validateVersion(version) {
  if (typeof version !== 'string' || !/^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-beta\.(?:0|[1-9]\d*))?$/.test(version)) {
    throw Error('Invalid release version');
  }
  return version;
}

export function compareVersions(left, right) {
  const parts = version => validateVersion(version).split(/\.|-beta\./).map(BigInt);
  const a = parts(left), b = parts(right);
  for (let index = 0; index < 3; index++) {
    if (a[index] !== b[index]) return a[index] > b[index] ? 1 : -1;
  }
  if (a.length !== b.length) return a.length === 3 ? 1 : -1;
  return a.length === 3 || a[3] === b[3] ? 0 : a[3] > b[3] ? 1 : -1;
}

export function packFilename(pack) { return `${pack.name}-${pack.key}.tar.zst`; }

function validArtifact(item) {
  return item && Number.isSafeInteger(item.size) && item.size > 0 && typeof item.sha256 === 'string' && hex.test(item.sha256);
}

export function validatePack(pack) {
  if (!pack || !PACK_NAMES.includes(pack.name) || typeof pack.key !== 'string' || !/^[a-f0-9]{16}$/.test(pack.key) ||
      !validArtifact(pack) || !Number.isSafeInteger(pack.unpacked) || pack.unpacked < 0 ||
      pack.url !== `${RELEASE_URL}/packs/${packFilename(pack)}`) throw Error('Invalid release pack');
  return pack;
}

export function validateManifest(manifest, version, withSetup = true) {
  validateVersion(version);
  if (!manifest || manifest.schema !== 1 || manifest.channel !== 'beta' || manifest.version !== version ||
      typeof manifest.published !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(manifest.published) ||
      !Number.isFinite(Date.parse(manifest.published)) || typeof manifest.notes !== 'string' ||
      !Array.isArray(manifest.packs) || manifest.packs.length !== 4) throw Error('Invalid release manifest');
  const names = new Set();
  for (const pack of manifest.packs) {
    validatePack(pack);
    if (names.has(pack.name)) throw Error('Invalid release pack');
    names.add(pack.name);
  }
  if (withSetup ? (!validArtifact(manifest.setup) ||
      manifest.setup.url !== `${RELEASE_URL}/v${version}/Lunmi-${version}-Setup.exe`) : 'setup' in manifest) {
    throw Error('Invalid release setup');
  }
  if (Buffer.byteLength(JSON.stringify(manifest)) > 64 * 1024) throw Error('Release manifest exceeds 64 KiB');
  return manifest;
}

export async function artifactInfo(path) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return { size: statSync(path).size, sha256: hash.digest('hex') };
}

export async function verifyArtifact(path, expected) {
  const actual = await artifactInfo(path);
  if (actual.size !== expected.size || actual.sha256 !== expected.sha256) throw Error('Release artifact integrity mismatch');
}

export async function signRelease(directory, privateKey, publicKey, version) {
  validateVersion(version);
  if (statSync(resolve(directory, 'manifest-bundle.json')).size > 64 * 1024) throw Error('Release manifest exceeds 64 KiB');
  const key = createPrivateKey(privateKey);
  const embedded = createPublicKey(publicKey);
  if (key.asymmetricKeyType !== 'ed25519' || embedded.asymmetricKeyType !== 'ed25519' ||
      !createPublicKey(key).export({ format: 'der', type: 'spki' }).equals(embedded.export({ format: 'der', type: 'spki' }))) {
    throw Error('Release key does not match embedded public key');
  }
  const manifest = validateManifest(JSON.parse(readFileSync(resolve(directory, 'manifest-bundle.json'), 'utf8')), version, false);
  for (const pack of manifest.packs) await verifyArtifact(resolve(directory, 'packs', packFilename(pack)), pack);
  manifest.setup = { url: `${RELEASE_URL}/v${version}/Lunmi-${version}-Setup.exe`,
    ...await artifactInfo(resolve(directory, `Lunmi-${version}-Setup.exe`)) };
  validateManifest(manifest, version);
  const raw = Buffer.from(JSON.stringify(manifest) + '\n');
  if (raw.length > 64 * 1024) throw Error('Release manifest exceeds 64 KiB');
  const signature = sign(null, raw, key);
  if (!verify(null, raw, embedded, signature)) throw Error('Signature self-check failed');
  writeFileSync(resolve(directory, 'latest.json'), raw);
  writeFileSync(resolve(directory, 'latest.json.sig'), signature);
  return manifest;
}

function readSignedManifest(directory, publicKey, version) {
  if (statSync(resolve(directory, 'latest.json')).size > 64 * 1024) throw Error('Release manifest exceeds 64 KiB');
  if (statSync(resolve(directory, 'latest.json.sig')).size !== 64) throw Error('Release signature verification failed');
  const raw = readFileSync(resolve(directory, 'latest.json'));
  const signature = readFileSync(resolve(directory, 'latest.json.sig'));
  const key = createPublicKey(publicKey);
  if (raw.length > 64 * 1024 || key.asymmetricKeyType !== 'ed25519' || signature.length !== 64 ||
      !verify(null, raw, key, signature)) throw Error('Release signature verification failed');
  const parsed = JSON.parse(raw);
  return { manifest: validateManifest(parsed, version ?? parsed.version), raw, signature };
}

export async function verifyRelease(directory, publicKey, version, manifestOnly = false) {
  validateVersion(version);
  const { manifest } = readSignedManifest(directory, publicKey, version);
  if (!manifestOnly) {
    await verifyArtifact(resolve(directory, `Lunmi-${version}-Setup.exe`), manifest.setup);
    for (const pack of manifest.packs) await verifyArtifact(resolve(directory, 'packs', packFilename(pack)), pack);
  }
  return manifest;
}

export function verifyChannel(prepared, current, publicKey, version) {
  validateVersion(version);
  const next = readSignedManifest(prepared, publicKey, version);
  const hasManifest = existsSync(resolve(current, 'latest.json'));
  const hasSignature = existsSync(resolve(current, 'latest.json.sig'));
  if (!hasManifest && !hasSignature) return;
  if (!hasManifest || !hasSignature) throw Error('Incomplete channel pair; restore the last valid pair manually');
  const previous = readSignedManifest(current, publicKey);
  const comparison = compareVersions(version, previous.manifest.version);
  if (comparison < 0) throw Error('Channel version cannot go backwards');
  if (comparison === 0 && (!next.raw.equals(previous.raw) || !next.signature.equals(previous.signature))) {
    throw Error('Same channel version must retain the exact signed bytes');
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const { publicKey } = JSON.parse(readFileSync(resolve(desktop, 'electron/update-public-key.json'), 'utf8'));
    const { version: packageVersion } = JSON.parse(readFileSync(resolve(desktop, 'package.json'), 'utf8'));
    const [command = 'sign', directory = resolve(desktop, 'release'), version = packageVersion, flag] = process.argv.slice(2);
    if (command === 'verify' && (!flag || flag === '--manifest-only')) await verifyRelease(resolve(directory), publicKey, version, flag === '--manifest-only');
    else if (command === 'verify-channel' && flag) verifyChannel(resolve(directory), resolve(flag), publicKey, version);
    else if (command === 'sign' && !flag) {
      const key = process.env.LUNMI_UPDATE_PRIVATE_KEY ? Buffer.from(process.env.LUNMI_UPDATE_PRIVATE_KEY, 'base64')
        : process.env.LUNMI_UPDATE_PRIVATE_KEY_FILE ? readFileSync(process.env.LUNMI_UPDATE_PRIVATE_KEY_FILE) : null;
      if (!key) throw Error('Release signing key is required');
      await signRelease(resolve(directory), key, publicKey, version);
    } else throw Error('Invalid release command');
    console.log(`Verified release manifest: ${version}`);
  } catch {
    console.error('Release validation failed. Check artifacts, version, manifest and signing-key identity. Key material is never printed.');
    process.exitCode = 1;
  }
}

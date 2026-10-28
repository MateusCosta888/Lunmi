import { createHash, createPrivateKey, createPublicKey, sign, verify } from 'node:crypto';
import { createReadStream, readFileSync, writeFileSync, statSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import yaml from 'js-yaml';

const desktop = resolve(dirname(fileURLToPath(import.meta.url)), '..');

export async function signRelease(directory, privateKey, publicKey, version) {
  if (!/^\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?$/.test(version)) throw Error('Invalid release version');
  const key = createPrivateKey(privateKey);
  if (createPublicKey(key).export({ format: 'pem', type: 'spki' }) !== publicKey) throw Error('Release key does not match embedded public key');
  const file = `Lunmi-${version}-Setup.exe`;
  const installer = resolve(directory, file);
  const hash = createHash('sha512');
  for await (const chunk of createReadStream(installer)) hash.update(chunk);
  const descriptor = { version, platform: 'win32', arch: 'x64', file, size: statSync(installer).size, sha512: hash.digest('base64') };
  const metadata = yaml.load(readFileSync(resolve(directory, 'latest.yml'), 'utf8'));
  if (metadata?.version !== version || metadata.files?.length !== 1 || metadata.packages ||
      metadata.files[0].url !== file || metadata.files[0].size !== descriptor.size ||
      metadata.files[0].sha512 !== descriptor.sha512 || metadata.files[0].isAdminRightsRequired) {
    throw Error('Installer metadata does not match release artifact');
  }
  const raw = Buffer.from(JSON.stringify(descriptor) + '\n');
  const signature = sign(null, raw, key);
  if (!verify(null, raw, publicKey, signature)) throw Error('Signature self-check failed');
  writeFileSync(resolve(directory, 'lunmi-update.json'), raw);
  writeFileSync(resolve(directory, 'lunmi-update.sig'), signature);
  return descriptor;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const key = process.env.LUNMI_UPDATE_PRIVATE_KEY
      ? Buffer.from(process.env.LUNMI_UPDATE_PRIVATE_KEY, 'base64')
      : process.env.LUNMI_UPDATE_PRIVATE_KEY_FILE ? readFileSync(process.env.LUNMI_UPDATE_PRIVATE_KEY_FILE) : null;
    if (!key) throw Error('Release signing key is required');
    const { publicKey } = JSON.parse(readFileSync(resolve(desktop, 'electron/update-public-key.json'), 'utf8'));
    const { version } = JSON.parse(readFileSync(resolve(desktop, 'package.json'), 'utf8'));
    await signRelease(resolve(process.argv[2] ?? resolve(desktop, 'release')), key, publicKey, version);
    console.log(`Verified signed release descriptor: ${version}`);
  } catch {
    console.error('Release signing failed. Check version, installer, update metadata and signing-key identity. Key material is never printed.');
    process.exitCode = 1;
  }
}

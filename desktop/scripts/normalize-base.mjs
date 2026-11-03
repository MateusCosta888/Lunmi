import { randomUUID } from 'node:crypto';
import { lstat, readFile, writeFile, rename, unlink } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { FuseV1Options, FuseVersion, getCurrentFuseWire } from '@electron/fuses';
import * as RE from 'resedit';

export const ELECTRON_VERSION = createRequire(import.meta.url)('electron/package.json').version;

export async function normalizeBase(file) {
  const info = await lstat(file);
  if (!info.isFile() || info.isSymbolicLink()) throw Error('Base executable must be a regular file');
  const wire = await getCurrentFuseWire(file);
  // FuseState.DISABLE (ASCII '0') is not exported by @electron/fuses.
  if (wire.version !== FuseVersion.V1 || wire[FuseV1Options.EnableEmbeddedAsarIntegrityValidation] !== 0x30) {
    throw Error('Embedded ASAR integrity fuse must be explicitly disabled before base normalization');
  }
  if (!/^\d+\.\d+\.\d+$/.test(ELECTRON_VERSION) || ELECTRON_VERSION.split('.').some(part => Number(part) > 65535)) {
    throw Error('Unsupported Electron version for PE normalization');
  }
  const executable = RE.NtExecutable.from(await readFile(file), { ignoreCert: true });
  const resources = RE.NtExecutableResource.from(executable);
  const versions = RE.Resource.VersionInfo.fromEntries(resources.entries);
  if (!versions.length) throw Error('Base executable has no version resource');
  const [major, minor, patch] = ELECTRON_VERSION.split('.').map(Number);
  for (const version of versions) {
    Object.assign(version.fixedInfo, { fileVersionMS: (major << 16) | minor, fileVersionLS: patch << 16,
      productVersionMS: (major << 16) | minor, productVersionLS: patch << 16 });
    for (const language of version.getAllLanguagesForStringValues()) {
      version.setStringValues(language, { FileVersion: `${ELECTRON_VERSION}.0`, ProductVersion: `${ELECTRON_VERSION}.0` }, false);
    }
    version.outputToResourceEntries(resources.entries);
  }
  resources.removeResourceEntry('INTEGRITY', 'ELECTRONASAR');
  // Shrink removes padding left by differently sized beta metadata and ASAR hashes.
  resources.outputResource(executable, false, true);
  const normalized = Buffer.from(executable.generate());
  const temporary = `${file}.${randomUUID()}.tmp`;
  try {
    await writeFile(temporary, normalized, { flag: 'wx' });
    // Replace this directory entry: splitApp hardlinks must never modify win-unpacked.
    await rename(temporary, file);
  } finally {
    await unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; });
  }
}

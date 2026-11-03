import { execFileSync } from 'node:child_process';
import { constants } from 'node:fs';
import { lstat, readdir, mkdir, link, copyFile, readFile, writeFile } from 'node:fs/promises';
import { resolve, join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { PACK_NAMES, RELEASE_URL, packFilename, validateManifest, validatePack, validateVersion, verifyArtifact } from './update-release.mjs';
import { normalizeBase } from './normalize-base.mjs';

const desktop = resolve(dirname(fileURLToPath(import.meta.url)), '..');

export async function copyTree(source, destination, include = () => true, relative = '') {
  const info = await lstat(source);
  if (info.isSymbolicLink() || (!info.isFile() && !info.isDirectory())) throw Error('Pack source must contain only regular files and directories');
  if (!include(relative)) return;
  if (info.isDirectory()) {
    await mkdir(destination, { recursive: true });
    for (const name of await readdir(source)) {
      await copyTree(join(source, name), join(destination, name), include, relative ? `${relative}/${name}` : name);
    }
  } else {
    await mkdir(dirname(destination), { recursive: true });
    try { await link(source, destination); }
    catch (error) {
      if (!['EXDEV', 'EPERM', 'EACCES', 'ENOTSUP'].includes(error.code)) throw error;
      await copyFile(source, destination, constants.COPYFILE_EXCL);
    }
  }
}

export function belongsToApp(path) {
  return path === 'resources/app.asar' || path === 'resources/lunmi-components.json' ||
    ['resources/app', 'resources/app.asar.unpacked', 'resources/licenses'].some(prefix => path === prefix || path.startsWith(`${prefix}/`)) ||
    ['LICENSE', 'LICENSE.electron.txt', 'LICENSES.chromium.html', 'resources/THIRD_PARTY_NOTICES.md', 'resources/THIRD-PARTY-MOTION.md'].includes(path);
}

export async function splitApp(unpacked, sources) {
  await mkdir(sources, { recursive: true });
  await mkdir(join(sources, 'base'));
  await mkdir(join(sources, 'app'));
  // Walk once: filtering parent directories would otherwise hide selected descendants.
  async function walk(relative = '') {
    for (const name of await readdir(join(unpacked, relative))) {
      const path = relative ? `${relative}/${name}` : name;
      if (['resources/python', 'resources/voice'].includes(path.toLowerCase())) throw Error('Duplicated runtime in Electron package');
      const info = await lstat(join(unpacked, path));
      if (info.isDirectory()) await walk(path);
      else await copyTree(join(unpacked, path), join(sources, belongsToApp(path) ? 'app' : 'base', path));
    }
  }
  await walk();
  for (const required of ['resources/app.asar', 'resources/lunmi-components.json']) await lstat(join(sources, 'app', required));
  await lstat(join(sources, 'base', 'Lunmi-app.exe'));
}

export async function packDirectory(launcher, directory, name, output, run = execFileSync) {
  if (!PACK_NAMES.includes(name)) throw Error('Invalid pack name');
  await mkdir(output, { recursive: true });
  const raw = run(launcher, ['pack', directory, name, output], { encoding: 'utf8', windowsHide: true });
  const pack = JSON.parse(raw.trim());
  if (pack.name !== name) throw Error('Pack command returned the wrong name');
  const item = { ...pack, url: `${RELEASE_URL}/packs/${packFilename(pack)}` };
  validatePack(item);
  await verifyArtifact(join(output, packFilename(item)), item);
  return item;
}

export async function packComponents(launcher, resources, release, run = execFileSync) {
  const sources = join(release, 'sources');
  await mkdir(sources, { recursive: true });
  await mkdir(join(sources, 'voice'));
  await copyTree(join(resources, 'voice'), join(sources, 'voice'), path => path !== 'models' && !path.startsWith('models/'));
  const python = await packDirectory(launcher, join(resources, 'python'), 'python', join(release, 'packs'), run);
  const voice = await packDirectory(launcher, join(sources, 'voice'), 'voice', join(release, 'packs'), run);
  await writeFile(join(resources, 'lunmi-components.json'), JSON.stringify({ python: python.key, voice: voice.key }) + '\n');
  await writeFile(join(release, 'component-packs.json'), JSON.stringify([python, voice]) + '\n');
}

export async function packageApp(launcher, resources, release, version, run = execFileSync) {
  validateVersion(version);
  const sources = join(release, 'sources');
  await splitApp(join(release, 'win-unpacked'), sources);
  await normalizeBase(join(sources, 'base', 'Lunmi-app.exe'));
  await copyTree(launcher, join(sources, 'app', 'Lunmi.exe'));
  const components = JSON.parse(await readFile(join(release, 'component-packs.json'), 'utf8'));
  const base = await packDirectory(launcher, join(sources, 'base'), 'base', join(release, 'packs'), run);
  const app = await packDirectory(launcher, join(sources, 'app'), 'app', join(release, 'packs'), run);
  const manifest = validateManifest({ schema: 1, channel: 'beta', version,
    published: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'), notes: `Lunmi ${version}`, packs: [base, app, ...components] }, version, false);
  const keys = JSON.parse(await readFile(join(sources, 'app/resources/lunmi-components.json'), 'utf8'));
  for (const name of ['python', 'voice']) {
    if (keys[name] !== components.find(pack => pack.name === name)?.key) throw Error('Component keys do not match packed runtimes');
  }
  await writeFile(join(release, 'manifest-bundle.json'), JSON.stringify(manifest) + '\n');
  run(launcher, ['bundle', join(release, 'manifest-bundle.json'), join(release, 'packs'), join(release, `Lunmi-${version}-Setup.exe`)], { stdio: 'inherit', windowsHide: true });
  await materializeVerification(resources, release, manifest);
  return manifest;
}

export async function materializeVerification(resources, release, manifest) {
  const root = join(release, 'beta-root');
  await mkdir(root);
  for (const pack of manifest.packs) {
    const source = pack.name === 'python' ? join(resources, 'python') : join(release, 'sources', pack.name);
    const destination = join(root, 'store', `${pack.name}-${pack.key}`);
    await copyTree(source, destination, path => path !== '.complete');
    await writeFile(join(destination, '.complete'), pack.key, { flag: 'wx' });
  }
  const versionRoot = join(root, 'versions', manifest.version);
  await copyTree(join(release, 'sources', 'base'), versionRoot);
  await copyTree(join(release, 'sources', 'app'), versionRoot);
  return versionRoot;
}

// Existing assets are compared locally, never replaced with --clobber.
export async function uploadPacks(release, run = execFileSync) {
  const manifest = JSON.parse(await readFile(join(release, 'latest.json'), 'utf8'));
  validateManifest(manifest, manifest.version);
  const listing = JSON.parse(run('gh', ['release', 'view', 'packs', '--json', 'assets,isDraft'], { encoding: 'utf8', windowsHide: true }));
  if (listing.isDraft !== false || !Array.isArray(listing.assets)) throw Error('Expected the public immutable packs release');
  const seen = new Set();
  const existing = join(release, 'existing-packs');
  await mkdir(existing);
  for (const pack of manifest.packs) {
    const filename = packFilename(pack);
    await verifyArtifact(join(release, 'packs', filename), pack);
    const matches = listing.assets.filter(asset => asset.name === filename);
    if (matches.length > 1) throw Error('Duplicate immutable pack asset');
    if (matches.length) {
      run('gh', ['release', 'download', 'packs', '--pattern', filename, '--dir', existing], { stdio: 'inherit', windowsHide: true });
      await verifyArtifact(join(existing, filename), pack);
      seen.add(filename);
    }
  }
  // Complete every collision check before adding new immutable assets.
  for (const pack of manifest.packs) {
    if (!seen.has(packFilename(pack))) run('gh', ['release', 'upload', 'packs', join(release, 'packs', packFilename(pack))], { stdio: 'inherit', windowsHide: true });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const [command, launcher] = process.argv.slice(2);
    const release = join(desktop, 'release');
    if (command === 'components' && launcher) await packComponents(resolve(launcher), join(desktop, 'resources'), release);
    else if (command === 'package' && launcher) {
      const { version } = JSON.parse(await readFile(join(desktop, 'package.json'), 'utf8'));
      await packageApp(resolve(launcher), join(desktop, 'resources'), release, version);
    } else if (command === 'upload-packs' && !launcher) await uploadPacks(release);
    else throw Error('Invalid beta release command');
  } catch (error) {
    console.error(`Beta release failed: ${error.message}`);
    process.exitCode = 1;
  }
}

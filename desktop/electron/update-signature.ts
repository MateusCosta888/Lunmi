import { createHash, verify } from "node:crypto";
import { createReadStream } from "node:fs";
import { lstat } from "node:fs/promises";

export interface ReleaseDescriptor {
  version: string;
  platform: "win32";
  arch: "x64";
  file: string;
  size: number;
  sha512: string;
}

export function verifyDescriptor(raw: Buffer, signature: Buffer, key: string, version: string): ReleaseDescriptor {
  if (raw.length > 16_384 || signature.length !== 64 || !verify(null, raw, key, signature)) {
    throw new Error("verification");
  }
  const d = JSON.parse(raw.toString("utf8")) as ReleaseDescriptor;
  if (!d || d.version !== version || d.platform !== "win32" || d.arch !== "x64" ||
      d.file !== `Lunmi-${version}-Setup.exe` || !Number.isSafeInteger(d.size) || d.size <= 0 ||
      typeof d.sha512 !== "string" || !/^[A-Za-z0-9+/]{86}==$/.test(d.sha512)) {
    throw new Error("verification");
  }
  return d;
}

export async function verifyInstaller(file: string, descriptor: ReleaseDescriptor): Promise<void> {
  const stat = await lstat(file);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size !== descriptor.size) throw new Error("verification");
  const hash = createHash("sha512");
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  if (hash.digest("base64") !== descriptor.sha512) throw new Error("verification");
}

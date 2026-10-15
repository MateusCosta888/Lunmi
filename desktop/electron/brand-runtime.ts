/** Build-only Windows identity. Never modifies the installed Electron binary. */
import * as fs from "fs";
import * as path from "path";
import { createHash } from "crypto";
import * as RE from "resedit";
import { version } from "../package.json";

export async function brandExecutable(source: Uint8Array, icon: Uint8Array): Promise<Buffer> {
  // The upstream signature cannot remain valid after changing application resources.
  const executable = RE.NtExecutable.from(source, { ignoreCert: true });
  const resources = RE.NtExecutableResource.from(executable);
  const icons = RE.Data.IconFile.from(icon).icons.map(item => item.data);
  const groups = RE.Resource.IconGroupEntry.fromEntries(resources.entries);
  for (const group of groups.length ? groups : [{ id: 1, lang: 1033 }]) {
    RE.Resource.IconGroupEntry.replaceIconsForResource(resources.entries, group.id, group.lang, icons);
  }
  const versions = RE.Resource.VersionInfo.fromEntries(resources.entries);
  for (const info of versions.length ? versions : [RE.Resource.VersionInfo.createEmpty()]) {
    const languages = info.getAllLanguagesForStringValues();
    for (const language of languages.length ? languages : [{ lang: 1033, codepage: 1200 }]) {
      info.setStringValues(language, {
        FileDescription: "Lunmi", ProductName: "Lunmi", InternalName: "Lunmi",
        OriginalFilename: "Lunmi.exe", CompanyName: "",
      });
    }
    info.setProductVersion(version);
    info.outputToResourceEntries(resources.entries);
  }
  resources.outputResource(executable);
  return Buffer.from(executable.generate());
}

export async function prepareWindowsRuntime(): Promise<void> {
  if (process.platform !== "win32") return;
  const desktop = path.resolve(__dirname, "..", "..");
  const source = path.dirname(require("electron") as string);
  const destination = path.join(desktop, ".runtime");
  const target = path.join(destination, "Lunmi.exe");
  const icon = fs.readFileSync(path.join(desktop, "src/assets/branding/lunmi.ico"));
  const original = path.join(source, "electron.exe");
  const stat = fs.statSync(original);
  const stamp = createHash("sha256").update(icon).update(fs.readFileSync(__filename))
    .update(`${version}:${stat.size}:${stat.mtimeMs}`).digest("hex");
  const marker = path.join(destination, "brand.sha256");
  if (fs.existsSync(target) && fs.existsSync(marker) && fs.readFileSync(marker, "utf8") === stamp) return;
  fs.mkdirSync(destination, { recursive: true });
  fs.cpSync(source, destination, { recursive: true, filter: file => file !== original });
  const binary = await brandExecutable(fs.readFileSync(original), icon);
  fs.writeFileSync(target + ".tmp", binary);
  fs.renameSync(target + ".tmp", target);
  fs.writeFileSync(marker, stamp);
  console.log("Windows runtime ready: Lunmi.exe (application icon and product identity).");
}

if (require.main === module) {
  void prepareWindowsRuntime().catch(error => {
    console.error("Could not prepare Lunmi.exe. Close the app and rebuild.", error);
    process.exitCode = 1;
  });
}

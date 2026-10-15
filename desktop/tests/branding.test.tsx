import { expect, it } from "vitest";
import { render } from "@testing-library/react";
import { BrandMark } from "../src/shared/ui";
import * as fs from "fs";
import * as path from "path";
import * as RE from "resedit";
import { brandExecutable } from "../electron/brand-runtime";
import brandSymbol from "../src/assets/branding/lunmi-symbol.svg";

it("uses the approved symbol without lettering throughout the app", () => {
  const { container } = render(<BrandMark size={40} />);
  const image = container.querySelector("img")!;
  expect(image.getAttribute("src")).toBe(brandSymbol);
  expect(image.getAttribute("width")).toBe("40");
  expect(image.getAttribute("alt")).toBe("");
});

it("brands Windows resources without modifying the original executable", async () => {
  const sourceExe = RE.NtExecutable.createEmpty(false, false);
  const source = new Uint8Array(sourceExe.generate());
  const before = Buffer.from(source);
  const icon = fs.readFileSync(path.resolve(__dirname, "../src/assets/branding/lunmi.ico"));
  const output = await brandExecutable(source, icon);
  expect(Buffer.from(source)).toEqual(before);
  const resources = RE.NtExecutableResource.from(RE.NtExecutable.from(output));
  const version = RE.Resource.VersionInfo.fromEntries(resources.entries)[0];
  expect(version.getStringValues(version.getAvailableLanguages()[0])).toMatchObject({
    FileDescription: "Lunmi", ProductName: "Lunmi", OriginalFilename: "Lunmi.exe",
  });
  const groups = RE.Resource.IconGroupEntry.fromEntries(resources.entries);
  expect(groups).toHaveLength(1);
  // ICO encodes 256px as a zero byte.
  expect(groups[0].icons.map(item => item.width || 256)).toEqual([16, 24, 32, 48, 64, 128, 256]);
});

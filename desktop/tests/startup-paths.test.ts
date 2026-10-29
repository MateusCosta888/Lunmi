import { afterEach, describe, expect, it, vi } from "vitest";
import * as path from "path";
import * as fs from "fs";
vi.mock("fs", { spy: true });
import { componentDir, resolvePythonExecutable } from "../electron/backend";

const runtime = vi.hoisted(() => ({
  isPackaged: true,
  getPath: () => "electron-user-data",
}));
vi.mock("electron", () => ({ app: runtime }));
import { getRepoRoot, getResourcesRoot, getUserDataRoot } from "../electron/main";

const originalDefaultApp = Object.getOwnPropertyDescriptor(process, "defaultApp");
const originalResources = Object.getOwnPropertyDescriptor(process, "resourcesPath");

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  if (originalDefaultApp) Object.defineProperty(process, "defaultApp", originalDefaultApp);
  else Reflect.deleteProperty(process, "defaultApp");
  if (originalResources) Object.defineProperty(process, "resourcesPath", originalResources);
  else Reflect.deleteProperty(process, "resourcesPath");
  runtime.isPackaged = true;
});

function launch(defaultApp: boolean | undefined) {
  vi.stubEnv("LUNMI_REPO_ROOT", "");
  vi.stubEnv("LUNMI_USER_DATA", "");
  vi.stubEnv("LUNMI_PYTHON", "");
  Object.defineProperty(process, "defaultApp", { value: defaultApp, configurable: true });
  Object.defineProperty(process, "resourcesPath", {
    value: path.resolve("installed/resources"), configurable: true,
  });
}

describe("startup paths for branded development and installed builds", () => {
  it("uses the checkout and venv for run.bat even when renamed Electron reports isPackaged", () => {
    launch(true);
    // Vitest imports the source main; the real boot check uses dist-electron/electron/main.js.
    const sourceRelativeRoot = path.resolve(__dirname, "../../..");
    const checkout = path.resolve(__dirname, "../..");
    const venvPython = path.join(checkout, ".venv", "Scripts", "python.exe");
    vi.mocked(fs.existsSync).mockImplementation(file => file === venvPython);
    expect(getRepoRoot()).toBe(sourceRelativeRoot);
    expect(getResourcesRoot()).toBeNull();
    expect(getUserDataRoot()).toBe(sourceRelativeRoot);
    expect(resolvePythonExecutable(checkout, getResourcesRoot()))
      .toBe(path.join(checkout, ".venv", "Scripts", "python.exe"));
  });

  it("keeps the private Python and separate user data in an installed build", () => {
    launch(undefined);
    vi.stubEnv("LOCALAPPDATA", path.resolve("tester-data"));
    expect(getRepoRoot()).toBe(path.join(process.resourcesPath, "app"));
    expect(getResourcesRoot()).toBe(process.resourcesPath);
    expect(resolvePythonExecutable(getRepoRoot(), getResourcesRoot()))
      .toBe(path.join(process.resourcesPath, "python", "python.exe"));
    expect(getUserDataRoot()).toBe(path.resolve("tester-data", "Lunmi"));
  });

  it("runs Python and voice from the component folder beside the installation", () => {
    launch(undefined);
    const resources = process.resourcesPath;
    const manifest = path.join(resources, "lunmi-components.json");
    const keys = { python: "0123456789abcdef", voice: "fedcba9876543210" };
    vi.mocked(fs.readFileSync).mockImplementation(((file: fs.PathOrFileDescriptor) => {
      if (file === manifest) return JSON.stringify(keys);
      throw Object.assign(new Error("ENOENT"), { code: "ENOENT" });
    }) as typeof fs.readFileSync);
    const runtimeRoot = `${path.dirname(resources)}-runtime`;
    expect(resolvePythonExecutable(getRepoRoot(), resources))
      .toBe(path.join(runtimeRoot, `python-${keys.python}`, "python.exe"));
    expect(componentDir(resources, "voice")).toBe(path.join(runtimeRoot, `voice-${keys.voice}`));
    // A key that is not the 16-hex content hash never becomes part of a path.
    keys.voice = "..\\..\\elsewhere";
    expect(componentDir(resources, "voice")).toBe(path.join(resources, "voice"));
  });

  it("keeps ordinary Electron development and explicit data overrides working", () => {
    launch(true);
    runtime.isPackaged = false;
    expect(getResourcesRoot()).toBeNull();
    vi.stubEnv("LUNMI_REPO_ROOT", path.resolve("custom-checkout"));
    vi.stubEnv("LUNMI_USER_DATA", path.resolve("isolated-data"));
    expect(getRepoRoot()).toBe(path.resolve("custom-checkout"));
    expect(getUserDataRoot()).toBe(path.resolve("isolated-data"));
  });
});

import { spawn } from "child_process";
import * as path from "path";
import { prepareWindowsRuntime } from "./brand-runtime";

void prepareWindowsRuntime().then(() => {
  const desktop = path.resolve(__dirname, "..", "..");
  const executable = process.platform === "win32"
    ? path.join(desktop, ".runtime", "Lunmi.exe") : require("electron") as string;
  const child = spawn(executable, [desktop, ...process.argv.slice(2)], { stdio: "inherit" });
  child.once("error", () => { console.error("Não foi possível iniciar a Lunmi."); process.exitCode = 1; });
  child.once("exit", code => { process.exitCode = code ?? 1; });
}).catch(() => { console.error("Feche a Lunmi e execute npm run build novamente."); process.exitCode = 1; });

/**
 * Medição de F0/F1 com janela visível (`show: true`) e confirmação real de pintura (`Ready`)
 * pelo renderer no Electron suportado (44.7.2):
 * - Preserva `logs/f0-electron-bench-20261014.json` (medição inicial de handshake oculto) e
 *   grava sempre um novo artefato com timestamp `logs/f0-visible-bench-<timestamp>.json`.
 * - Registra timestamps separados:
 *   - `handshakeCompletedMs` (conclusão do handshake `session.hello` -> `session.ready`)
 *   - `windowReadyToShowMs` e `windowShownMs` (janela nativa visível)
 *   - `rendererPaintedReadyMs` (estado `Ready` aplicado no React + `requestAnimationFrame` + IPC `lunmi:renderer-ready`)
 *   - `wallBootToPaintedReadyMs` (tempo externo do spawn até a escrita do bench após pintura)
 * - Nomeia `first_launch` e `subsequent_launches` (sem alegar cache frio controlado do SO).
 * - Registra working sets por processo e soma amostrada (`sampledWorkingSetSumMb`), distinguindo
 *   working set compartilhado de consumo físico exclusivo.
 * - Verifica toda a árvore de processos (descendentes vivos antes de encerrar e após `exit`) para
 *   comprovar zero processos órfãos na árvore.
 */
import { spawn, spawnSync } from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

// eslint-disable-next-line @typescript-eslint/no-require-imports
const electronBinary: string = require("electron");

interface ElectronProcMetric {
  pid: number;
  type: string;
  workingSetSizeKb: number;
  privateBytesKb: number;
}

interface BenchRecord {
  mode: string;
  bootStartEpochMs: number;
  handshakeCompletedMs: number;
  windowReadyToShowMs: number | null;
  windowShownMs: number | null;
  rendererPaintedReadyMs: number | null;
  readyMs: number;
  windowVisible: boolean;
  electronVersion: string;
  electronMainPid: number;
  rendererOsPid: number | null;
  pythonPid: number;
  sessionId: string;
  capabilities: Record<string, boolean>;
  electronProcesses: ElectronProcMetric[];
}

interface RunMeasurement {
  runIndex: number;
  launchOrderLabel: "first_launch" | "subsequent_launch";
  windowVisible: boolean;
  wallBootToPaintedReadyMs: number;
  handshakeCompletedMs: number;
  windowReadyToShowMs: number | null;
  windowShownMs: number | null;
  rendererPaintedReadyMs: number | null;
  electronInternalReadyMs: number;
  totalClosureMs: number;
  exitCode: number | null;
  electronProcessCount: number;
  electronWorkingSetSumMb: number;
  pythonWorkingSetMb: number;
  sampledWorkingSetSumMb: number;
  electronProcesses: ElectronProcMetric[];
  pythonPid: number;
  electronMainPid: number;
  treePidsObserved: number[];
  orphanPidsAfterExit: number[];
}

function isPidAlive(pid: number): boolean {
  if (!pid || pid <= 0) {
    return false;
  }
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function queryProcessTreeAndPythonRss(
  repoRoot: string,
  electronRootPid: number,
  pythonPid: number
): { pythonRssKb: number; treePids: number[] } {
  const winPy = path.join(repoRoot, ".venv", "Scripts", "python.exe");
  const pyExe = fs.existsSync(winPy) ? winPy : "python";
  const script = [
    "import json, psutil, sys",
    "e_pid, p_pid = int(sys.argv[1]), int(sys.argv[2])",
    "tree = set()",
    "for root in (e_pid, p_pid):",
    "    if root > 0:",
    "        tree.add(root)",
    "        try:",
    "            proc = psutil.Process(root)",
    "            for c in proc.children(recursive=True):",
    "                tree.add(c.pid)",
    "        except Exception:",
    "            pass",
    "rss_kb = 0",
    "try:",
    "    rss_kb = int(psutil.Process(p_pid).memory_info().rss // 1024)",
    "except Exception:",
    "    rss_kb = 0",
    "print(json.dumps({'pythonRssKb': rss_kb, 'treePids': sorted(tree)}))",
  ].join("\n");

  const res = spawnSync(pyExe, ["-c", script, String(electronRootPid), String(pythonPid)], {
    encoding: "utf8",
    windowsHide: true,
    timeout: 4000,
  });
  if (res.status === 0 && res.stdout) {
    try {
      const parsed = JSON.parse(String(res.stdout).trim()) as {
        pythonRssKb: number;
        treePids: number[];
      };
      return parsed;
    } catch {
      // ignore
    }
  }
  return { pythonRssKb: 0, treePids: [electronRootPid, pythonPid].filter((p) => p > 0) };
}

async function runSingleLaunch(runIndex: number, repoRoot: string): Promise<RunMeasurement> {
  const desktopDir = path.join(repoRoot, "desktop");
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), `lunmi-f0-vis-${runIndex}-`));
  const benchFile = path.join(tempDir, "bench.json");
  const sandboxDir = path.join(tempDir, "sandbox");
  fs.mkdirSync(sandboxDir, { recursive: true });

  const startHr = process.hrtime.bigint();
  let wallBootToPaintedReadyMs = 0;
  let pythonRssKb = 0;
  let treePidsObserved: number[] = [];

  try {
    const child = spawn(electronBinary, ["."], {
      cwd: desktopDir,
      stdio: ["ignore", "pipe", "pipe"],
      shell: false,
      windowsHide: false,
      env: {
        ...process.env,
        LUNMI_REPO_ROOT: repoRoot,
        LUNMI_SANDBOX_DIR: sandboxDir,
        LUNMI_BENCH_OUT: benchFile,
        LUNMI_BENCH_HEADLESS: "0",
        LUNMI_BENCH_WAIT_PAINT: "1",
        LUNMI_BENCH_HOLD_MS: "250",
      },
    });

    const stdoutChunks: string[] = [];
    const stderrChunks: string[] = [];
    child.stdout.on("data", (c: Buffer) => stdoutChunks.push(c.toString("utf8")));
    child.stderr.on("data", (c: Buffer) => stderrChunks.push(c.toString("utf8")));

    const exitPromise = new Promise<number | null>((resolve, reject) => {
      child.on("error", reject);
      child.on("exit", (code) => resolve(code));
    });

    const pollDeadline = Date.now() + 15000;
    let benchData: BenchRecord | null = null;
    while (Date.now() < pollDeadline) {
      if (child.exitCode !== null && !fs.existsSync(benchFile)) {
        break;
      }
      if (fs.existsSync(benchFile)) {
        try {
          const raw = fs.readFileSync(benchFile, "utf8");
          benchData = JSON.parse(raw) as BenchRecord;
          wallBootToPaintedReadyMs = Number(process.hrtime.bigint() - startHr) / 1_000_000;
          const sampled = queryProcessTreeAndPythonRss(
            repoRoot,
            benchData.electronMainPid,
            benchData.pythonPid
          );
          pythonRssKb = sampled.pythonRssKb;
          treePidsObserved = sampled.treePids;
          break;
        } catch {
          // arquivo ainda sendo fechado
        }
      }
      await new Promise((r) => setTimeout(r, 15));
    }

    if (!benchData && child.exitCode === null) {
      child.kill();
    }
    const exitCode = await exitPromise;
    const totalClosureMs = Number(process.hrtime.bigint() - startHr) / 1_000_000;

    if (!benchData) {
      throw new Error(
        `Partida ${runIndex} não gerou bench.json (exitCode=${exitCode}). stdout=${stdoutChunks.join("")} stderr=${stderrChunks.join("")}`
      );
    }

    await new Promise((r) => setTimeout(r, 200));
    const allTrackedPids = [
      benchData.electronMainPid,
      benchData.pythonPid,
      ...benchData.electronProcesses.map((p) => p.pid),
      ...treePidsObserved,
    ];
    const uniquePids = Array.from(new Set(allTrackedPids.filter((p) => p > 0)));
    const orphanPidsAfterExit = uniquePids.filter((pid) => isPidAlive(pid));

    const electronWsKb = benchData.electronProcesses.reduce(
      (acc, item) => acc + (item.workingSetSizeKb || 0),
      0
    );
    const electronWorkingSetSumMb = Number((electronWsKb / 1024).toFixed(2));
    const pythonWorkingSetMb = Number((pythonRssKb / 1024).toFixed(2));
    const sampledWorkingSetSumMb = Number(((electronWsKb + pythonRssKb) / 1024).toFixed(2));

    return {
      runIndex,
      launchOrderLabel: runIndex === 1 ? "first_launch" : "subsequent_launch",
      windowVisible: benchData.windowVisible,
      wallBootToPaintedReadyMs: Number(wallBootToPaintedReadyMs.toFixed(2)),
      handshakeCompletedMs: benchData.handshakeCompletedMs,
      windowReadyToShowMs: benchData.windowReadyToShowMs,
      windowShownMs: benchData.windowShownMs,
      rendererPaintedReadyMs: benchData.rendererPaintedReadyMs,
      electronInternalReadyMs: benchData.readyMs,
      totalClosureMs: Number(totalClosureMs.toFixed(2)),
      exitCode,
      electronProcessCount: benchData.electronProcesses.length,
      electronWorkingSetSumMb,
      pythonWorkingSetMb,
      sampledWorkingSetSumMb,
      electronProcesses: benchData.electronProcesses,
      pythonPid: benchData.pythonPid,
      electronMainPid: benchData.electronMainPid,
      treePidsObserved: uniquePids,
      orphanPidsAfterExit,
    };
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
}

function makeTimestampSlug(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-` +
    `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
  );
}

async function main(): Promise<void> {
  const repoRoot = path.resolve(__dirname, "..", "..", "..");
  const runs: RunMeasurement[] = [];

  for (let i = 1; i <= 5; i++) {
    const result = await runSingleLaunch(i, repoRoot);
    runs.push(result);
    console.log(
      `[F0 Visible Run #${i} (${result.launchOrderLabel})] visible=${result.windowVisible} | ` +
        `handshake=${result.handshakeCompletedMs}ms | windowShown=${result.windowShownMs}ms | ` +
        `paintedReady=${result.rendererPaintedReadyMs}ms | wall=${result.wallBootToPaintedReadyMs}ms | ` +
        `electronWS=${result.electronWorkingSetSumMb}MB (${result.electronProcessCount} procs) | ` +
        `pythonWS=${result.pythonWorkingSetMb}MB | sumWS=${result.sampledWorkingSetSumMb}MB | ` +
        `treePids=${result.treePidsObserved.length} | exit=${result.exitCode} | orphans=${result.orphanPidsAfterExit.length}`
    );
  }

  const subsequentRuns = runs.slice(1);
  const avgSubsequentWallMs = Number(
    (
      subsequentRuns.reduce((s, r) => s + r.wallBootToPaintedReadyMs, 0) /
      subsequentRuns.length
    ).toFixed(2)
  );
  const avgSubsequentHandshakeMs = Number(
    (
      subsequentRuns.reduce((s, r) => s + r.handshakeCompletedMs, 0) /
      subsequentRuns.length
    ).toFixed(2)
  );
  const avgSubsequentPaintedMs = Number(
    (
      subsequentRuns.reduce((s, r) => s + (r.rendererPaintedReadyMs ?? r.electronInternalReadyMs), 0) /
      subsequentRuns.length
    ).toFixed(2)
  );
  const avgSampledWorkingSetSumMb = Number(
    (runs.reduce((s, r) => s + r.sampledWorkingSetSumMb, 0) / runs.length).toFixed(2)
  );

  const summary = {
    timestamp: new Date().toISOString(),
    measurementKind: "visible_window_painted_ready",
    note: "Soma de working sets amostrados por processo (páginas compartilhadas podem constar em mais de um processo; não representa consumo físico exclusivo nem produto com worker UFO ativo).",
    nodeVersion: process.version,
    electronVersion: require("electron/package.json").version,
    firstLaunch: runs[0],
    subsequentLaunches: subsequentRuns,
    summaryMetrics: {
      firstLaunchHandshakeMs: runs[0].handshakeCompletedMs,
      firstLaunchWindowShownMs: runs[0].windowShownMs,
      firstLaunchPaintedReadyMs: runs[0].rendererPaintedReadyMs,
      firstLaunchWallMs: runs[0].wallBootToPaintedReadyMs,
      subsequentAvgHandshakeMs: avgSubsequentHandshakeMs,
      subsequentAvgPaintedReadyMs: avgSubsequentPaintedMs,
      subsequentAvgWallMs: avgSubsequentWallMs,
      avgSampledWorkingSetSumMb,
      allWindowsVisible: runs.every((r) => r.windowVisible === true),
      anyOrphansInProcessTree: runs.some((r) => r.orphanPidsAfterExit.length > 0),
      allExitedZero: runs.every((r) => r.exitCode === 0),
    },
  };

  const logsDir = path.join(repoRoot, "logs");
  fs.mkdirSync(logsDir, { recursive: true });
  const slug = makeTimestampSlug();
  const outPath = path.join(logsDir, `f0-visible-bench-${slug}.json`);
  fs.writeFileSync(outPath, JSON.stringify(summary, null, 2), "utf8");
  console.log(`Saved visible benchmark report to ${outPath}`);

  if (
    !summary.summaryMetrics.allWindowsVisible ||
    summary.summaryMetrics.anyOrphansInProcessTree ||
    !summary.summaryMetrics.allExitedZero
  ) {
    process.exit(1);
  }
}

void main();

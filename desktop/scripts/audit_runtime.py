"""Block new runtime advisories; reviewed exclusions expire and must be re-evaluated."""
from datetime import date
from pathlib import Path
import subprocess
import sys

# SECURITY.md: no auth transforms, LangSmith Hub, config loaders or URL text splitter.
REVIEWED = (
    "GHSA-7xgj-8392-5q4r", "PYSEC-2026-2192", "PYSEC-2026-2555",
    "PYSEC-2026-2193", "PYSEC-2026-2562", "PYSEC-2026-77",
)

if __name__ == "__main__":
    if date.today() > date(2026, 11, 15):
        raise SystemExit("Runtime advisory review expired; re-evaluate SECURITY.md before publishing.")
    resources = Path(__file__).resolve().parents[1] / 'resources'
    for name in ('requirements-runtime.lock', 'requirements-voice.lock'):
        args = [sys.executable, '-m', 'pip_audit', '-r', str(resources / name), '--no-deps', '--disable-pip']
        if name == 'requirements-runtime.lock':
            for advisory in REVIEWED:
                args.extend(['--ignore-vuln', advisory])
        result = subprocess.run(args, check=False)
        if result.returncode:
            raise SystemExit(result.returncode)

"""Block new runtime advisories; reviewed exclusions expire and must be re-evaluated."""
from datetime import date
from pathlib import Path
import subprocess
import sys

# SECURITY.md, reviewed 2026-11-03: the remaining LangChain fixes require 1.x.
REVIEW_UNTIL = date(2026, 12, 1)
REVIEWED = (
    "PYSEC-2026-2192",
    "PYSEC-2026-2193", "PYSEC-2026-2562", "PYSEC-2026-77",
)


def main() -> int:
    if date.today() > REVIEW_UNTIL:
        raise SystemExit("Runtime advisory review expired; re-evaluate SECURITY.md before publishing.")
    resources = Path(__file__).resolve().parents[1] / 'resources'
    for name in ('requirements-runtime.lock', 'requirements-voice.lock', 'requirements-web.lock'):
        args = [sys.executable, '-m', 'pip_audit', '-r', str(resources / name), '--no-deps', '--disable-pip']
        if name == 'requirements-runtime.lock':
            for advisory in REVIEWED:
                args.extend(['--ignore-vuln', advisory])
        result = subprocess.run(args, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Remove o momento da instalação dos runtimes, para o mesmo lock dar o mesmo pacote."""
from __future__ import annotations

import csv
import subprocess
from pathlib import Path

RESOURCES = Path(__file__).resolve().parents[1] / "resources"


def normalize_records(root: Path) -> None:
    """Tira do RECORD os ficheiros que não seguem no pacote: o hash dos .exe gerados pelo pip muda a cada build."""
    for record in root.rglob("*.dist-info/RECORD"):
        lines = record.read_bytes().decode("utf-8").splitlines(keepends=True)
        kept = [line for line in lines if (record.parents[1] / next(csv.reader([line]))[0]).is_file()]
        if kept != lines:
            record.write_bytes("".join(kept).encode("utf-8"))


def main() -> None:
    python = RESOURCES / "python"
    # unchecked-hash: o .pyc deixa de guardar a data da origem, e o pacote é imutável.
    # Sem check: fontes que não compilam (modelos de teste de terceiros) não têm .pyc de qualquer forma.
    subprocess.run([str(python / "python.exe"), "-I", "-B", "-m", "compileall", "-q", "-f",
                    "--invalidation-mode", "unchecked-hash", str(python / "Lib/site-packages")], check=False)
    normalize_records(python)
    normalize_records(RESOURCES / "voice")


if __name__ == "__main__":
    main()

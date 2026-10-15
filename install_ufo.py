"""Fetch the pinned official source. Dependencies are in requirements.txt."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPOSITORY = "https://github.com/microsoft/UFO.git"


def git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def install() -> None:
    manifest = json.loads((ROOT / "ufo.lock.json").read_text(encoding="utf-8"))
    path = ROOT / "vendor" / "UFO"
    if not path.exists():
        path.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=path, check=True)
        subprocess.run(["git", "remote", "add", "origin", REPOSITORY], cwd=path, check=True)
        subprocess.run(["git", "fetch", "--depth", "1", "origin", manifest["revision"]], cwd=path, check=True)
        subprocess.run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=path, check=True)
    if git("rev-parse", "HEAD", cwd=path) != manifest["revision"]:
        raise RuntimeError("UFO já existe em outra revisão. Não será sobrescrito; configure um checkout separado.")
    if git("diff", "--name-only", "HEAD", cwd=path):
        raise RuntimeError("Checkout UFO modificado; nenhuma alteração foi sobrescrita.")
    for filename, expected in manifest["sha256"].items():
        file = path / filename
        if not file.is_file() or hashlib.sha256(file.read_bytes().replace(b"\r\n", b"\n")).hexdigest() != expected:
            raise RuntimeError(f"Hash divergente: {filename}. Confira conversão Git CRLF e revisão.")
    print(f"UFO oficial verificado: {manifest['revision']}")


if __name__ == "__main__":
    install()

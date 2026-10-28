"""Prepare the pinned Windows runtime in a fresh release checkout, without local user data."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

REPO = Path(__file__).resolve().parents[2]
RESOURCES = REPO / "desktop" / "resources"


def main() -> None:
    pin = json.loads((RESOURCES / "RUNTIME-SOURCE.json").read_text(encoding="utf-8"))["python_runtime"]
    destination = RESOURCES / "python"
    if destination.exists():
        raise SystemExit("Runtime destination already exists; use a fresh release checkout.")
    with tempfile.TemporaryDirectory(prefix="lunmi-runtime-") as directory:
        archive = Path(directory) / "runtime.tar.gz"
        with urllib.request.urlopen(pin["url"], timeout=120) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != pin["sha256"]:
            raise SystemExit("Runtime checksum mismatch.")
        with tarfile.open(archive) as runtime:
            runtime.extractall(Path(directory) / "unpacked", filter="data")
        shutil.copytree(Path(directory) / "unpacked" / "python", destination)
    subprocess.run([
        str(destination / "python.exe"), "-m", "pip", "install", "--no-warn-script-location",
        "--disable-pip-version-check", "--no-input", "-r", str(RESOURCES / "requirements-runtime.lock"),
    ], cwd=REPO, check=True)
    subprocess.run([str(destination / "python.exe"), str(REPO / "desktop/scripts/prepare_runtime.py")], check=True)


if __name__ == "__main__":
    main()

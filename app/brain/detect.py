"""Discovery of the CLI agents installed on this machine.

Detection never blocks startup for long: probes run in parallel, each with its
own timeout, and results are cached so the UI can rescan on demand.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

from app.brain.cli.catalog import specs
from app.brain.cli.process import resolve_executable, run_once
from app.brain.cli.spec import CliSpec

_VERSION_PATTERN = re.compile(r"\d+\.\d+(?:\.\d+)?")


@dataclass(frozen=True)
class CliDetection:
    cli_id: str
    label: str
    installed: bool
    executable: str | None = None
    version: str | None = None
    detail: str = ""

    @property
    def status(self) -> str:
        if not self.installed:
            return "not_installed"
        return "installed"


async def detect(spec: CliSpec, *, timeout: float = 20.0) -> CliDetection:
    """Probe one CLI: locate the binary, then ask for its version."""
    executable = resolve_executable(spec.executable, spec.search_paths)
    if executable is None:
        return CliDetection(spec.id, spec.label, False, detail="executável não encontrado")
    try:
        code, stdout, stderr = await run_once([executable, *spec.version_args], timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - a broken CLI must not break discovery
        return CliDetection(spec.id, spec.label, True, executable,
                            detail=f"falha ao consultar versão: {type(exc).__name__}")
    if code != 0:
        return CliDetection(spec.id, spec.label, True, executable, detail="--version falhou")
    match = _VERSION_PATTERN.search(stdout or stderr)
    return CliDetection(spec.id, spec.label, True, executable,
                        version=match.group(0) if match else None,
                        detail="pronto")


async def detect_all(*, timeout: float = 20.0) -> list[CliDetection]:
    """Probe every catalogued CLI concurrently."""
    results = await asyncio.gather(*(detect(spec, timeout=timeout) for spec in specs()),
                                   return_exceptions=True)
    detections: list[CliDetection] = []
    for spec, result in zip(specs(), results):
        if isinstance(result, CliDetection):
            detections.append(result)
        else:
            detections.append(CliDetection(spec.id, spec.label, False,
                                           detail=f"detecção falhou: {type(result).__name__}"))
    return detections


class DetectionCache:
    """Short-lived cache so repeated UI refreshes do not respawn every CLI."""

    def __init__(self, ttl: float = 300.0, *, timeout: float = 20.0) -> None:
        self.ttl = ttl
        self.timeout = timeout
        self._stamp = 0.0
        self._value: list[CliDetection] = []

    async def get(self, *, force: bool = False) -> list[CliDetection]:
        fresh = time.monotonic() - self._stamp < self.ttl
        if self._value and fresh and not force:
            return self._value
        self._value = await detect_all(timeout=self.timeout)
        self._stamp = time.monotonic()
        return self._value

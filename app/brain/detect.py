"""Discovery of the CLI agents installed on this machine.

Detection never blocks startup for long: probes run in parallel, each with its
own timeout, and results are cached so the UI can rescan on demand.

Two kinds of local CLI are reported separately:

* a catalogued ``CliSpec`` — installed here and drivable by the generic adapter;
* a ``KnownRuntime`` — installed here but with no headless contract (today
  ``free-pi-cli``, which only starts a TUI). It is *detected*, never offered to
  the adapter, and carries the reason so "known" cannot be mistaken for "usable".
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, replace

from app.brain.cli.catalog import find_spec, specs
from app.brain.cli.known import KnownRuntime, known_runtimes
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
    # False when this CLI is known only as a runtime: finding it must never make
    # it selectable, because there is no verified non-interactive protocol.
    supported: bool = True
    unavailable_reason: str = ""

    @property
    def status(self) -> str:
        if not self.installed:
            return "not_installed"
        return "installed"


async def _probe(cli_id: str, label: str, executable_name: str,
                 search_paths: tuple[str, ...], version_args: tuple[str, ...],
                 *, timeout: float) -> CliDetection:
    """Locate one executable, then ask for its version — nothing else."""
    executable = resolve_executable(executable_name, search_paths)
    if executable is None:
        return CliDetection(cli_id, label, False, detail="executável não encontrado")
    try:
        code, stdout, stderr = await run_once([executable, *version_args], timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - a broken CLI must not break discovery
        return CliDetection(cli_id, label, True, executable,
                            detail=f"falha ao consultar versão: {type(exc).__name__}")
    if code != 0:
        return CliDetection(cli_id, label, True, executable, detail="--version falhou")
    match = _VERSION_PATTERN.search(stdout or stderr)
    return CliDetection(cli_id, label, True, executable,
                        version=match.group(0) if match else None,
                        detail="pronto")


async def detect(spec: CliSpec, *, timeout: float = 20.0) -> CliDetection:
    """Probe one catalogued CLI: locate the binary, then ask for its version."""
    return await _probe(spec.id, spec.label, spec.executable, spec.search_paths,
                        spec.version_args, timeout=timeout)


async def detect_runtime(runtime: KnownRuntime, *, timeout: float = 20.0) -> CliDetection:
    """Probe a runtime that is known to exist but has no headless contract."""
    detection = await _probe(runtime.id, runtime.label, runtime.executable,
                             runtime.search_paths, runtime.version_args, timeout=timeout)
    return replace(detection, supported=False,
                   unavailable_reason=runtime.unavailable_reason)


async def detect_all(*, timeout: float = 20.0) -> list[CliDetection]:
    """Probe every catalogued CLI and every known non-headless runtime."""
    catalogued = list(specs())
    extra = [runtime for runtime in known_runtimes() if find_spec(runtime.id) is None]
    targets: list[CliSpec | KnownRuntime] = [*catalogued, *extra]
    jobs = [detect(spec, timeout=timeout) for spec in catalogued]
    jobs += [detect_runtime(runtime, timeout=timeout) for runtime in extra]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    detections: list[CliDetection] = []
    for target, result in zip(targets, results):
        if isinstance(result, CliDetection):
            detections.append(result)
        else:
            detections.append(CliDetection(target.id, target.label, False,
                                           detail=f"detecção falhou: {type(result).__name__}",
                                           supported=isinstance(target, CliSpec),
                                           unavailable_reason=(
                                               target.unavailable_reason
                                               if isinstance(target, KnownRuntime) else "")))
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

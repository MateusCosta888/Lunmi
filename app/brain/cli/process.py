"""Windows-first subprocess management for local CLI agent runtimes.

One line of stdout is one message. The framing is ours and its bound is explicit
(``LINE_LIMIT_BYTES``): a large message is delivered whole, and a message past the bound
fails as a transport failure with a name — never as an opaque reader exception, and never
as "the agent could not do it".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import psutil

from app.brain.contracts import BrainError, ErrorKind

# A CLI must never flash a console window in front of the user.
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_WINDOWS_SUFFIXES = (".exe", ".cmd", ".bat")
# CreateProcess rejects longer command lines with ERROR_FILENAME_EXCED_RANGE,
# which Python surfaces as FileNotFoundError — easily mistaken for a missing
# binary. The documented limit is 32,767; keep headroom for quoting.
WINDOWS_COMMAND_LINE_LIMIT = 30_000 if os.name == "nt" else 0
_FILENAME_EXCEEDED_RANGE = 206

# One line of stdout is one message: a CLI answers with a JSON object, or one object per
# line, and a whole plan fits in a single line that can be large. asyncio's default stream
# limit is 64 KiB and a longer line makes the reader raise "Separator is found, but chunk
# is longer than limit" / "Separator is not found, and chunk exceed the limit". Measured
# 2026-10-09 on this machine: those exact sentences were reported to the user as the
# task's result with zero actions and a `CONFIGURATION` classification, blaming neither
# the transport nor the CLI. The bound below is explicit — a message is delivered whole
# up to it, and a message past it fails *by name* (see ``CliProcess.readline``) instead
# of by an opaque reader exception. It is a ceiling on one message, never an unbounded
# read: the reader is created with it and its overrun is what we catch.
LINE_LIMIT_BYTES = 1024 * 1024
_OVERRUN_DRAIN_SECONDS = 1.0


def command_line_length(argv: Sequence[str]) -> int:
    """CreateProcess UTF-16 units, with Python's actual quoting and the NUL."""
    if not argv:
        return 0
    return len(subprocess.list2cmdline(argv).encode("utf-16-le", errors="surrogatepass")) // 2 + 1


def resolve_executable(name: str, extra_dirs: Iterable[str] = ()) -> str | None:
    """Find a CLI on PATH, then in the known install directories of the vendor.

    Neither step reads or copies any credential.
    """
    found = shutil.which(name)
    if found:
        return found
    if os.name != "nt":
        return None
    for directory in extra_dirs:
        base = Path(os.path.expandvars(directory)).expanduser()
        for suffix in _WINDOWS_SUFFIXES:
            candidate = base / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)
    return None


@dataclass
class CliProcess:
    """A live CLI process with captured stderr and Windows-safe teardown."""

    process: asyncio.subprocess.Process
    argv: tuple[str, ...]
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=20))
    drainer: asyncio.Task[None] | None = None
    # Set when a message could not be framed: after an overrun the next newline is not
    # guaranteed to be a message boundary, so nothing more may be read from this process.
    stream_desynced: bool = False

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def finished(self) -> bool:
        return self.process.returncode is not None

    def diagnostics(self) -> str:
        """Last stderr lines, for logs only — never shown verbatim to a user."""
        return "\n".join(self.stderr_tail)

    async def write(self, data: str) -> None:
        stream = self.process.stdin
        if stream is None:
            raise BrainError(ErrorKind.PROCESS_CRASHED, "O agente local não aceita entrada.")
        try:
            stream.write(data.encode("utf-8"))
            await stream.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise BrainError(ErrorKind.PROCESS_CRASHED, "O agente local encerrou a entrada.",
                             detail=self.diagnostics()) from exc

    async def close_input(self) -> None:
        stream = self.process.stdin
        if stream is not None and not stream.is_closing():
            with contextlib.suppress(Exception):
                stream.close()

    async def readline(self, timeout: float, *, limit: int | None = None) -> str:
        """One message line, framed by this transport, with its bound made explicit.

        ``StreamReader.readline`` turns the reader's own overrun into a bare ``ValueError``
        carrying the sentence above. Reading with ``readuntil`` here keeps the typed
        exception, so an oversized line can be *named* as a transport failure (and its
        bytes drained under a deadline) instead of surfacing as an unexplained crash.
        A last line without a trailing newline is a complete message and is returned as
        one; a stream that ends with nothing buffered returns "" — EOF, not an error.
        """
        # Read at call time, not bound as a default: the bound is one statement for the
        # whole transport, and a caller that changes it (or a test) must be obeyed.
        limit = LINE_LIMIT_BYTES if limit is None else limit
        stream = self.process.stdout
        if stream is None:
            raise BrainError(ErrorKind.PROCESS_CRASHED, "O agente local não fornece saída.")
        if self.stream_desynced:
            raise BrainError(ErrorKind.TRANSPORT,
                             "O canal do agente local ficou fora de sincronia depois de uma "
                             "mensagem grande demais; ele precisa ser reiniciado.",
                             detail=self.diagnostics())
        try:
            raw = await asyncio.wait_for(stream.readuntil(b"\n"), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BrainError(ErrorKind.TIMEOUT, "O agente local não respondeu a tempo.",
                             detail=self.diagnostics()) from exc
        except asyncio.IncompleteReadError as exc:
            # EOF: whatever was buffered is a message without its trailing newline.
            return exc.partial.decode("utf-8", errors="replace")
        except asyncio.LimitOverrunError as exc:
            await self._drain_overrun(stream, exc)
            raise BrainError(
                ErrorKind.TRANSPORT,
                f"O agente local enviou uma mensagem maior que o limite de {limit} bytes "
                "por linha deste transporte. Nada foi truncado, reinterpretado nem "
                "executado; a rodada terminou antes de qualquer ação.",
                detail=f"overrun at {getattr(exc, 'consumed', '?')} bytes; {self.diagnostics()[-400:]}",
            ) from exc
        except ConnectionResetError as exc:
            raise BrainError(ErrorKind.TRANSPORT,
                             "O canal de saída do agente local foi encerrado durante a leitura.",
                             detail=self.diagnostics()) from exc
        return raw.decode("utf-8", errors="replace")

    async def _drain_overrun(self, stream: asyncio.StreamReader,
                             error: asyncio.LimitOverrunError) -> None:
        """Consume what the reader buffered, then mark this stream unusable.

        ``consumed`` is how far the reader looked; draining that much is bounded by the
        reader's own limit and never reads the remaining output of the process. After it,
        the position of the next newline is unknown, so the stream is marked desynchronized
        and the caller must start a fresh process — the honest alternative to resuming in
        the middle of a message.
        """
        consumed = int(getattr(error, "consumed", 0) or 0)
        if consumed:
            with contextlib.suppress(asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                                     ConnectionResetError, asyncio.TimeoutError):
                await asyncio.wait_for(stream.readexactly(consumed),
                                       timeout=_OVERRUN_DRAIN_SECONDS)
        self.stream_desynced = True

    async def wait(self, timeout: float) -> int:
        try:
            return await asyncio.wait_for(self.process.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise BrainError(ErrorKind.TIMEOUT, "O agente local não terminou a tempo.",
                             detail=self.diagnostics()) from exc

    async def stop(self, *, grace: float = 3.0) -> None:
        """Terminate the whole process tree so no orphan survives the session."""
        process = self.process
        if process.returncode is None:
            await terminate_tree(process.pid)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=grace)
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    process.kill()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(process.wait(), timeout=grace)
        if self.drainer is not None:
            self.drainer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.drainer
            self.drainer = None
        # Only stdin is a writer; stdout/stderr are readers owned by the loop.
        writer = process.stdin
        if writer is not None and not writer.is_closing():
            with contextlib.suppress(Exception):
                writer.close()

    def start_stderr_drain(self) -> None:
        if self.process.stderr is not None:
            self.drainer = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        stream = self.process.stderr
        if stream is None:
            return
        while True:
            raw = await stream.readline()
            if not raw:
                return
            self.stderr_tail.append(raw.decode("utf-8", errors="replace").rstrip())


async def terminate_tree(pid: int) -> None:
    """Terminate children first, then the parent. Works on Windows and POSIX."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    children: list[psutil.Process] = []
    with contextlib.suppress(psutil.Error):
        children = parent.children(recursive=True)
    for child in reversed(children):
        with contextlib.suppress(psutil.Error):
            child.terminate()
    with contextlib.suppress(psutil.Error):
        parent.terminate()


async def spawn(argv: Sequence[str], *, cwd: str | Path | None = None,
                extra_env: dict[str, str] | None = None) -> CliProcess:
    """Start a CLI with hidden console, captured pipes and no inherited shell."""
    environment = os.environ.copy()
    if extra_env:
        environment.update(extra_env)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            creationflags=_CREATE_NO_WINDOW,
            # An explicit bound, not the 64 KiB default: a CLI's message can be large and
            # the reader must be told what this transport accepts (see LINE_LIMIT_BYTES).
            limit=LINE_LIMIT_BYTES,
        )
    except FileNotFoundError as exc:
        # A valid path can still fail here (a bad working directory or an
        # oversized command line look identical to "not found" from Python), so
        # record what was actually attempted before surfacing a clean message.
        logging.error("Falha ao iniciar %s: %s | cwd=%s (existe=%s) | exe existe=%s | argv=%d chars",
                      argv[0], exc, cwd, os.path.isdir(str(cwd)) if cwd else True,
                      os.path.isfile(argv[0]), command_line_length(argv))
        if getattr(exc, "winerror", None) == _FILENAME_EXCEEDED_RANGE:
            # ERROR_FILENAME_EXCED_RANGE: the binary is fine; the request is not.
            raise BrainError(ErrorKind.REQUEST_TOO_LARGE,
                             "O pedido é grande demais para a linha de comando do Windows. "
                             "Reduza o tamanho da tarefa ou escolha um cérebro que aceite o "
                             "prompt por stdin.", detail=str(exc)) from exc
        raise BrainError(ErrorKind.EXECUTABLE_MISSING,
                         f"O executável {argv[0]} não foi encontrado.", detail=str(exc)) from exc
    except OSError as exc:
        raise BrainError(ErrorKind.PROVIDER_UNAVAILABLE,
                         "Não foi possível iniciar o agente local.", detail=str(exc)) from exc
    handle = CliProcess(process=process, argv=tuple(argv))
    handle.start_stderr_drain()
    return handle


_PROBE_READ_SECONDS = 5.0


async def run_once(argv: Sequence[str], *, timeout: float) -> tuple[int, str, str]:
    """Run a short-lived probe (version, model list) and return code/stdout/stderr."""
    handle = await spawn(argv)
    try:
        code = await handle.wait(timeout)
        chunks: list[str] = []
        if handle.process.stdout is not None:
            while True:
                # The process already exited, so buffered output is read here through the
                # same framing the inference path uses; a short deadline only covers the
                # pipe itself.
                line = await handle.readline(_PROBE_READ_SECONDS)
                if not line:
                    break
                chunks.append(line)
        return code, "".join(chunks), handle.diagnostics()
    finally:
        await handle.stop()

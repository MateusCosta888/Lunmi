from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, Iterable, Mapping, TextIO, Awaitable, Callable

import psutil

from app.settings import ROOT, Settings

# What UFO and the Brain exchange for one inference turn: the official messages plus
# the name of the agent that asked (HostAgent or AppAgent), so a task's cost can be
# attributed to a specific agent instead of a single lump (§1).
CompletionFn = Callable[[list[dict[str, Any]], str], Awaitable[str]]

# Cold import of UFO² (MCP server discovery + UIA) measured at ~6 s, but a busy
# machine or a virus scan can stretch that several times. Startup waits are kept
# separate from the task budget so a slow first launch is not reported as a
# task timeout (the previous 30 s ceiling did exactly that).
WORKER_STARTUP_SECONDS = max(60.0, float(os.getenv("WORKER_STARTUP_SECONDS", "180")))

# One line of the worker's stdout is one message, and the busiest of them carries a whole
# prompt: an `llm` event holds the messages the brain is asked for, JSON-encoded. asyncio's
# default stream limit is 64 KiB, and a longer line makes the *reader itself* raise
# "Separator is found, but chunk is longer than limit" / "Separator is not found, and chunk
# exceed the limit" — a sentence that reads like a model failure and was reported as the
# task's own result with zero actions, classified as CONFIGURATION (see
# ``classify_failure_kind``: a bare ValueError is blamed on the user's configuration).
# Measured 2026-10-09 on the chat path: "pesquisa por michel jackson no spotify" died on
# the fifth inference, the AppAgent already having typed the query into Spotify, once its
# prompt carried the application's control list and the JSON escaping of its accented names.
# The same defect was already fixed for the CLI transports (app/brain/cli/process.py).
# The bound is explicit and generous; a message past it fails *by name*, as a transport
# failure of this bus, and any message above WORKER_LARGE_LINE_BYTES is logged with its
# event before the next attempt, so the ceiling is never the first thing anybody learns about.
WORKER_LINE_LIMIT_BYTES = 8 * 1024 * 1024
WORKER_LARGE_LINE_BYTES = 256 * 1024


# Keys the executor never receives: the worker runs actions, not the cérebro, so provider
# credentials and chat configuration are removed from the child environment.
WORKER_DROPPED_ENV = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY")


def build_worker_env(
    settings: Settings, *, host_pids: Iterable[int] = (), environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Ambiente do processo worker: paths por utilizador, host/pids e nada de credenciais.

    `PYTHONDONTWRITEBYTECODE` entra aqui de propósito: o worker importa `vendor/UFO` e as
    dependências de `resources/python`, então sem a flag o cache de bytecode era escrito
    dentro dos recursos instalados — pasta que o produto trata como somente-leitura (e que
    ficava com restos depois de desinstalar). Mesmo ajuste do backend, em
    `desktop/electron/backend.ts`.
    """
    env = dict(os.environ if environ is None else environ)
    for name in WORKER_DROPPED_ENV:
        env.pop(name, None)
    env["UFO_PATH"] = str(settings.ufo_path)
    # Windows venv launchers can sit between this controller and the worker.
    env["UFO_CONTROLLER_PID"] = str(os.getpid())
    if host_pids:
        env["UFO_ASSISTANT_PIDS"] = ",".join(str(pid) for pid in sorted(host_pids))
    env["OUTPUT_DIRECTORY"] = str(settings.output_path)
    # The worker reads the application index the controller keeps; both must use the same
    # state root, or a sandboxed session would search the user's own index.
    env["LUNMI_STATE_ROOT"] = str(settings.state_root)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


class ComputerError(RuntimeError):
    pass


class WorkerTransportError(ComputerError):
    """A message on the worker bus could not be framed. Nothing was executed."""


class UFOEngine:
    """Persistent isolated UFO² MCP executor. Never launch automation outside UFO."""
    def __init__(self, settings: Settings, *, host_pids: set[int] | None = None) -> None:
        self.settings = settings
        self.host_pids: set[int] = set(host_pids or ())
        self.process: asyncio.subprocess.Process | None = None
        self._stderr: TextIO | None = None
        self._lock = asyncio.Lock()
        # A boot in flight is not a ready worker. Without this, a task arriving while
        # the worker was still importing could read the boot's own "ready" line as if it
        # were its own reply (measured: the prewarm and the first command racing, 19 ms
        # failure instead of a close). `_boot` also makes concurrent starts share one
        # boot instead of spawning two workers.
        self._boot = asyncio.Lock()
        self._ready = False
        self.startup_time_ms = 0.0
        self.operation_states: list[dict[str, Any]] = []

    def set_host_pids(self, pids: set[int] | list[int] | tuple[int, ...]) -> None:
        """Update the live host UI process tree (Electron main/renderer/GPU/utility PIDs)."""
        self.host_pids = {int(pid) for pid in pids if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0}

    @property
    def worker_ready(self) -> bool:
        """True when a task would reuse an already-running worker (a WARM start)."""
        return self._ready and self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        async with self._boot:
            if self.worker_ready:
                return
            await self._boot_worker()

    async def _boot_worker(self) -> None:
        """Spawn the worker and wait for its own readiness message, or clean up."""
        self.settings.validate(credentials=False)
        if not (self.settings.ufo_path / "ufo").is_dir():
            raise ComputerError("UFO não instalado. Execute python install_ufo.py.")
        started = asyncio.get_running_loop().time()
        self.settings.logs_path.mkdir(exist_ok=True)
        self._stderr = (self.settings.logs_path / "worker.log").open("a", encoding="utf-8")
        env = build_worker_env(self.settings, host_pids=self.host_pids)
        try:
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-u", str(ROOT / "app" / "computer" / "worker.py"),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=self._stderr, cwd=ROOT, env=env,
                creationflags=0x08000000,  # CREATE_NO_WINDOW; interactive desktop inherited.
                # An explicit bound, not the 64 KiB default: the reader must be told what
                # this bus accepts (see WORKER_LINE_LIMIT_BYTES).
                limit=WORKER_LINE_LIMIT_BYTES,
            )
            budget = min(WORKER_STARTUP_SECONDS, self.settings.timeout_seconds)
            try:
                message = await asyncio.wait_for(self._read(), budget)
            except TimeoutError:
                logging.error("Worker UFO não ficou pronto em %.0f s.", budget)
                raise ComputerError(
                    f"Worker UFO não ficou pronto em {budget:.0f} s; consulte logs/worker.log.") from None
            if message.get("ready") is not True:
                logging.error("Primeira mensagem do worker não foi 'ready': %s", message)
                raise ComputerError("Worker UFO não ficou pronto.")
            self.startup_time_ms = (asyncio.get_running_loop().time() - started) * 1000
            self._ready = True
            logging.info("Worker UFO pronto em %.0f ms.", self.startup_time_ms)
        except BaseException:
            await self.stop()
            raise

    async def _read(self) -> dict[str, Any]:
        """One message from the worker, framed by this bus with its bound made explicit.

        ``StreamReader.readline`` turns the reader's own overrun into a bare ``ValueError``
        carrying the sentence above; ``readuntil`` keeps the typed exception, so a message
        past the bound is *named* as a transport failure instead of surfacing as the task's
        result. EOF in the middle of a message is the worker dying mid-sentence: it is
        reported, never returned as a message nobody wrote.
        """
        if not self.process or not self.process.stdout:
            raise ComputerError("Worker indisponível.")
        try:
            line = await self.process.stdout.readuntil(b"\n")
        except asyncio.IncompleteReadError as exc:
            raise ComputerError("Worker UFO encerrou; consulte logs/worker.log.") from exc
        except (asyncio.LimitOverrunError, ValueError) as exc:
            raise WorkerTransportError(
                f"O worker enviou uma mensagem maior que o limite de {WORKER_LINE_LIMIT_BYTES} "
                "bytes por linha deste canal; nada foi truncado nem executado e a rodada "
                "terminou antes de qualquer ação.") from exc
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise ComputerError("Protocolo UFO inválido.") from exc
        if not isinstance(value, dict):
            raise ComputerError("Resposta UFO inválida.")
        if len(line) >= WORKER_LARGE_LINE_BYTES:
            # Named, not silent: this is the size that used to kill the reader outright.
            logging.warning("Mensagem de %d bytes do worker (evento %s)", len(line),
                            value.get("event") or value.get("operation") or value.get("ready"))
        return value

    async def _request(self, operation: str,
                       confirm: Callable[[str], Awaitable[bool]] | None = None,
                       **arguments: Any) -> dict[str, Any]:
        # start() already replaces a worker known dead before delivery. A pipe error
        # after write/drain may mean the action ran and its reply was lost; replaying a
        # mutation then would execute twice. Fail honestly, never retry unknown delivery.
        try:
            return await self._exchange(operation, confirm=confirm, **arguments)
        except (BrokenPipeError, ConnectionResetError) as exc:
            await self.stop()
            raise ComputerError("Comunicação UFO interrompida; a ação pode ter ocorrido e não foi repetida.") from exc

    async def _exchange(self, operation: str, confirm: Callable[[str], Awaitable[bool]] | None = None,
                        **arguments: Any) -> dict[str, Any]:
        async with self._lock:
            await self.start()
            process = self.process
            if not process or not process.stdin:
                raise ComputerError("Worker indisponível.")
            process.stdin.write((json.dumps({"operation": operation,
                                             "host_pids": sorted(self.host_pids),
                                             **arguments}) + "\n").encode())
            await process.stdin.drain()
            reply = await self._answer(confirm)
            if reply.get("ok") is not True:
                await self.stop()
                raise ComputerError(reply.get("error", "Falha UFO."))
            data = reply.get("data")
            if not isinstance(data, dict):
                raise ComputerError("Evidência UFO inválida.")
            return data

    async def _answer(self, confirm: Callable[[str], Awaitable[bool]] | None) -> dict[str, Any]:
        """Read the reply that answers an operation, deciding any `confirm` event on the way.

        The worker asks for consent *inside* an operation — launching an application
        discovered by a folder sweep is the first launch of it, and the security policy
        asks once — so a single request/reply exchange must keep reading after an event
        that is not the answer. Without a decision callback the answer is denied, which is
        the same refusal a headless run makes everywhere else.
        """
        while True:
            reply = await self._read()
            if reply.get("event") != "confirm":
                return reply
            approved = bool(await confirm(str(reply.get("message") or ""))) if confirm else False
            process = self.process
            if not process or not process.stdin:
                raise ComputerError("Worker indisponível.")
            process.stdin.write((json.dumps({"approved": approved}) + "\n").encode())
            await process.stdin.drain()

    async def run_task(self, instruction: str, completion: CompletionFn,
                       confirm: Callable[[str], Awaitable[bool]], *,
                       approval_mode: str = "manual", initial_operation: dict[str, Any] | None = None,
                       task_id: str = "", attachments: list[dict[str, Any]] | None = None,
                       operation_callback: Callable[[str, dict[str, Any]], None] | None = None,
                       allow_actions: bool = True) -> dict[str, Any]:
        async with self._lock:
            await self.start()
            process = self.process
            if process is None or process.stdin is None:
                raise ComputerError("Worker indisponível.")
            writer = process.stdin
            async def send(value: dict[str, Any]) -> None:
                writer.write((json.dumps(value) + "\n").encode())
                await writer.drain()
            # The mode travels with the task, not with the process: the user may switch it
            # in the chat between tasks and nothing has to be restarted for it to apply.
            self.operation_states = []
            await send({"operation": "run_task", "instruction": instruction,
                        "max_steps": self.settings.max_steps, "approval_mode": approval_mode,
                        "host_pids": sorted(self.host_pids), "task_id": task_id,
                        "initial_operation": initial_operation, "attachments": attachments or [],
                        "allow_actions": allow_actions})
            while True:
                reply = await self._read()
                if reply.get("event") in {"operation.started", "operation.finished"}:
                    fields = {key: reply[key] for key in ("operation_id", "state", "verified",
                                                          "tool", "category", "failure_reason")
                              if key in reply}
                    identifier = fields.get("operation_id")
                    if isinstance(identifier, str) and isinstance(fields.get("state"), str):
                        self.operation_states = [item for item in self.operation_states
                                                 if item.get("operation_id") != identifier] + [fields]
                        if operation_callback:
                            operation_callback(str(reply["event"]), fields)
                    await send({})
                elif reply.get("event") == "llm":
                    try:
                        await send({"text": await completion(reply["messages"], str(reply.get("agent") or ""))})
                    except Exception as exc:
                        # The message crossing to the worker stays generic, but the
                        # real cause must survive in logs/worker.log for diagnosis.
                        logging.error("Falha do provider durante inferência UFO: %s: %s",
                                      type(exc).__name__, exc)
                        await send({"error": "Falha do provider durante inferência UFO."})
                        # The worker is healthy — only the Brain failed. Its failure
                        # reply must be consumed, or the next task would read a stale
                        # message and fail for the wrong reason.
                        await self._absorb_failure_reply()
                        raise
                elif reply.get("event") == "confirm":
                    await send({"approved": await confirm(reply["message"])})
                elif reply.get("ok") is True:
                    return dict(reply["data"])
                else:
                    raise ComputerError(reply.get("error", "Falha UFO."))

    async def _absorb_failure_reply(self) -> None:
        """Consume the worker's reply to a failed inference so the protocol stays in sync.

        Keeping a healthy worker alive is only correct if it is left idle and
        waiting: a reply nobody reads would be mistaken for the next operation's
        answer. If nothing readable arrives, the worker is not healthy after all and
        is stopped instead.
        """
        try:
            await self._read()
        except Exception:
            await self.stop()

    async def execute(self, instruction: str = "open_notepad", *, prepare_document: bool = False) -> dict[str, Any]:
        # The natural-language plan is validated before crossing this boundary.
        if instruction != "open_notepad":
            raise ComputerError("Este executor permite iniciar somente o Bloco de Notas.")
        return await self._request("open_notepad", prepare_document=prepare_document)

    async def fast_path(self, plan: dict[str, Any],
                        confirm: Callable[[str], Awaitable[bool]] | None = None) -> dict[str, Any]:
        """Run one deterministic plan inside the worker, with no inference at all (FASE 8).

        The plan was resolved from the instruction by ``app.computer.fastpath``; the
        worker executes it with the same verified tools the agent uses. A plan that did
        not apply comes back as ``outcome: declined`` inside the data, not as a worker
        error, so the caller can still hand the instruction to the HostAgent. A plan that
        must ask first — the first launch of a swept application — reaches ``confirm``
        through the worker's own consent event, so the question is asked by the same
        dialog the agent path uses.
        """
        return await self._request("fast_path", confirm=confirm, plan=plan)

    async def write(self, text: str) -> dict[str, Any]:
        return await self._request("write_notepad", text=text)

    async def save(self, path: str, approved_version: dict[str, Any] | None) -> dict[str, Any]:
        return await self._request("save_notepad", path=path, approved_version=approved_version)

    async def verify(self) -> dict[str, Any]:
        return await self._request("verify_notepad")

    async def stop(self) -> None:
        process, self.process = self.process, None
        # Stopped means not ready: a worker that is gone must never look reusable.
        self._ready = False
        if process and process.returncode is None:
            # Windows venv python.exe can be a launcher with a Python child.
            # Stop owned Python workers first, but NEVER their Notepad children.
            try:
                descendants = psutil.Process(process.pid).children(recursive=True)
                for child in reversed(descendants):
                    try:
                        if child.name().casefold() in {"python.exe", "pythonw.exe"}:
                            child.kill()
                    except psutil.NoSuchProcess:
                        continue
            except psutil.NoSuchProcess:
                pass
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        if self._stderr:
            self._stderr.close()
            self._stderr = None


def verify_open(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]]]:
    previous = {(w.get("handle"), w.get("pid")) for w in before}
    fresh = [w for w in after if w.get("process", "").casefold() == "notepad.exe"
             and w.get("visible") is True and isinstance(w.get("handle"), int)
             and w["handle"] > 0 and isinstance(w.get("pid"), int) and w["pid"] > 0
             and (w["handle"], w["pid"]) not in previous]
    return bool(fresh), fresh

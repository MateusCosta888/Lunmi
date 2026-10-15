"""One adapter that can drive any catalogued CLI agent in inference mode.

The CLI answers; the UFO agent acts. No shell or tool capability is exposed,
and no credential is ever read from the CLI's own storage.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from app.brain.cli.process import (
    WINDOWS_COMMAND_LINE_LIMIT,
    CliProcess,
    command_line_length,
    resolve_executable,
    run_once,
    spawn,
)
from app.brain.cli.spec import MODEL_PARSERS, REPLY_PARSERS, CliSpec
from app.brain.contracts import (
    BrainError,
    BrainEvent,
    BrainHealth,
    ErrorKind,
    HealthStatus,
    ModelInfo,
    events_for,
)
from app.llm import Completion
from app.settings import Settings

# Substrings that mean "the CLI itself could not do the work", not a model answer.
_AUTH_HINTS = ("not logged in", "not authenticated", "please log in", "login required",
               "authentication", "unauthorized", "invalid api key", "401")
_QUOTA_HINTS = ("usage limit", "rate limit", "quota", "too many requests", "429", "402",
                "out of credits", "insufficient credit", "budget pool", "exhausted")
# A misspelled or unavailable model is a configuration mistake, not a broken
# provider: measured on Kilo 7.8.8, which answers `Model not found: <id>`.
_MODEL_HINTS = ("model not found", "unknown model", "no such model", "invalid model",
                "model is not available", "unsupported model")
_VERSION_PATTERN = re.compile(r"\d+\.\d+(?:\.\d+)?")


def render_prompt(messages: list[dict[str, Any]]) -> str:
    """Flatten chat messages into the single prompt a CLI expects.

    System content leads, then each turn in order. Non-text blocks (images) are
    dropped: no catalogued CLI is advertised as accepting them.
    """
    system = "\n".join(str(message["content"]) for message in messages
                       if message.get("role") == "system")
    turns: list[str] = []
    for message in messages:
        if message.get("role") == "system":
            continue
        content = message.get("content")
        if isinstance(content, list):
            text = "".join(str(block.get("text", "")) for block in content
                           if isinstance(block, dict) and block.get("type") == "text")
        else:
            text = str(content)
        turns.append(text)
    body = "\n\n".join(turn for turn in turns if turn)
    return f"{system}\n\n{body}" if system else body


def extract_json_reply(raw: str) -> dict[str, Any]:
    """Find the reply object in stdout that may also carry logs or decoration."""
    for line in reversed([line for line in raw.splitlines() if line.strip()]):
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    try:
        value = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise BrainError(ErrorKind.MALFORMED_RESPONSE,
                         "O agente local não retornou JSON reconhecível.",
                         detail=raw[-400:]) from exc
    if not isinstance(value, dict):
        raise BrainError(ErrorKind.MALFORMED_RESPONSE, "Resposta do agente local não é um objeto JSON.")
    return value


def extract_json_events(raw: str) -> list[dict[str, Any]]:
    """Collect every JSON object a CLI streamed, one per line, in order.

    A CLI that emits an event stream puts the answer in one event and the token
    usage in another, so keeping only the last object (``extract_json_reply``)
    would return a reply with no text at all.
    """
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            events.append(value)
    if not events:
        raise BrainError(ErrorKind.MALFORMED_RESPONSE,
                         "O agente local não retornou nenhum evento JSON reconhecível.",
                         detail=raw[-400:])
    return events


def classify_failure(text: str) -> BrainError:
    """Turn a CLI failure message into the application's own error vocabulary."""
    lowered = text.casefold()
    if any(hint in lowered for hint in _QUOTA_HINTS):
        return BrainError(ErrorKind.QUOTA_EXCEEDED,
                          "O provedor do agente local recusou por limite de uso.", detail=text[-400:])
    if any(hint in lowered for hint in _AUTH_HINTS):
        return BrainError(ErrorKind.AUTH_REQUIRED,
                          "O agente local precisa de login antes de responder.", detail=text[-400:])
    if any(hint in lowered for hint in _MODEL_HINTS):
        return BrainError(ErrorKind.MODEL_UNAVAILABLE,
                          "O agente local não reconhece o modelo escolhido.", detail=text[-400:])
    return BrainError(ErrorKind.PROVIDER_UNAVAILABLE,
                      "O agente local encerrou sem produzir resposta.", detail=text[-400:])


class HeadlessCliBrain:
    """A local CLI agent used purely as an inference engine."""

    def __init__(self, settings: Settings, spec: CliSpec, *, executable: str) -> None:
        self.settings = settings
        self.spec = spec
        self.provider_id = spec.id
        self.model = settings.model
        self.executable = executable
        self.capabilities = spec.capabilities
        self._process: CliProcess | None = None
        # FASE 4: the task-scoped persistent process, when the spec declares one.
        self._session: CliProcess | None = None
        self._closed = False
        # One timing record per complete() call, newest last. Kept after close()
        # so a finished task's latency is still attributable (spawn vs model).
        self.cli_calls: list[dict[str, Any]] = []
        self._call_started = 0.0
        self._first_byte_ms: float | None = None

    # -- argv -----------------------------------------------------------------
    def inference_argv(self) -> list[str]:
        argv = [self.executable, *self.inference_flags()]
        argv.extend(self.effort_flags())
        if self.model and self.spec.model_args:
            argv.extend([*self.spec.model_args, self.model])
        # Prompt flags go last so a flag that swallows the next argument cannot
        # eat another option (verified against AGY 1.3.0).
        argv.extend(self.spec.prompt_args)
        return argv

    def inference_flags(self) -> list[str]:
        """Base flags for one turn, plus --bare when the user supplied a key."""
        flags = list(self.spec.inference_args)
        if self.spec.bare_with_env and os.environ.get(self.spec.bare_with_env, "").strip():
            # A key is available, so bypass the keychain the CLI cannot reach.
            flags.insert(0, "--bare")
        return flags

    def effort_flags(self) -> list[str]:
        """The reasoning-effort flag for this turn, or nothing when it does not apply.

        Only a CLI whose spec declares the flag can receive one: an unknown value on a
        documented scale is a configuration mistake and must read like one, because the
        difference it makes is measured in tens of seconds per round, not milliseconds.
        """
        effort = self.settings.effort.strip()
        if not effort or not self.spec.effort_args:
            return []
        if self.spec.effort_values and effort not in self.spec.effort_values:
            raise BrainError(ErrorKind.CONFIGURATION,
                             f"Esforço de raciocínio '{effort}' não é válido para "
                             f"{self.spec.label}. Use um de: "
                             + ", ".join(self.spec.effort_values) + ".")
        return [*self.spec.effort_args, effort]

    @property
    def recommended_model(self) -> str:
        """Curated default model, or "" when the CLI's own listing order stands."""
        return self.spec.preferred_model

    def models_argv(self) -> list[str] | None:
        if not self.spec.models_args:
            return None
        return [self.executable, *self.spec.models_args]

    # -- Brain contract --------------------------------------------------------
    async def complete(self, messages: list[dict[str, Any]]) -> Completion:
        if self._closed:
            raise BrainError(ErrorKind.PROVIDER_UNAVAILABLE, "Este provider já foi encerrado.")
        started = time.perf_counter()
        self._call_started = started
        self._first_byte_ms = None
        prompt = render_prompt(messages)
        # FASE 4: only a spec that declared a *verified* multi-turn protocol reuses the
        # process; every other CLI keeps the one-shot path below, unchanged.
        if self.spec.session_args and self.spec.session_end:
            return await self._complete_session(prompt, started)
        return await self._complete_once(prompt, started)

    async def _complete_once(self, prompt: str, started: float) -> Completion:
        """One process per call: the behaviour every catalogued CLI has today."""
        handle: CliProcess | None = None
        spawn_ms = 0.0
        spawn_started = started
        succeeded = False
        length = 0
        budget = self.capabilities.max_safe_prompt_chars
        try:
            if not prompt.strip():
                raise BrainError(ErrorKind.CONFIGURATION, "Nada a enviar ao agente local.")
            argv = self.inference_argv()
            if not self.spec.prompt_on_stdin:
                argv.append(prompt)  # e.g. AGY: "-p" already added, prompt right after it
            # Catch it here, before spawning: an argv-only CLI hits the Windows
            # command-line ceiling on long agent rounds, and that must read as a
            # size problem, never as a missing executable.
            length = command_line_length(argv)
            if budget is not None and len(prompt) > budget:
                raise BrainError(ErrorKind.REQUEST_TOO_LARGE,
                                 f"O pedido excedeu o orçamento seguro de {budget} caracteres da linha de comando do {self.spec.label}. "
                                 "Nenhuma informação foi truncada; escolha um cérebro com transporte maior.",
                                 detail=f"prompt={len(prompt)} chars, budget={budget}, argv={length} UTF-16 units")
            if WINDOWS_COMMAND_LINE_LIMIT and length > WINDOWS_COMMAND_LINE_LIMIT:
                raise BrainError(
                    ErrorKind.REQUEST_TOO_LARGE,
                    f"O pedido ficou grande demais para o {self.spec.label}: este CLI aceita o prompt "
                    "somente como argumento e o Windows limita a linha de comando a 32.767 caracteres. "
                    "Tente uma tarefa mais curta ou escolha um cérebro que leia o prompt por stdin.",
                    detail=f"argv={length} UTF-16 units, prompt={len(prompt)} chars")
            spawn_started = time.perf_counter()
            handle = await spawn(argv, cwd=self.settings.ufo_path.parent)
            # Measured the moment the process exists, not at the end of the call:
            # otherwise this number silently equals the whole round and proves nothing.
            spawn_ms = (time.perf_counter() - spawn_started) * 1000
            self._process = handle
            try:
                stdout = await self._exchange(handle, prompt)
            finally:
                await handle.stop()
                self._process = None
            if self._closed:
                raise BrainError(ErrorKind.CANCELLED, "Operação cancelada.")
            text, (input_tokens, output_tokens) = self._parse_reply(stdout)
            succeeded = True
            return Completion(text, input_tokens, output_tokens)
        finally:
            # Every attempt is recorded, successful or not: a slow round must be
            # attributable to CLI startup, to the wait, or to neither.
            self._record_call(
                ok=succeeded,
                prompt_chars=len(prompt),
                argv_utf16_units=length,
                prompt_budget_chars=budget,
                spawn_ms=spawn_ms,
                first_byte_ms=self._first_byte_ms,
                total_ms=(time.perf_counter() - started) * 1000,
                exit_code=handle.process.returncode if handle is not None else None,
            )

    def _record_call(self, *, ok: bool, prompt_chars: int, argv_utf16_units: int,
                     prompt_budget_chars: int | None, spawn_ms: float,
                     first_byte_ms: float | None, total_ms: float, exit_code: int | None,
                     session_reused: bool = False) -> None:
        """Keep the newest 32 call timings; diagnostics must never break inference."""
        self.cli_calls.append({"ok": ok, "prompt_chars": prompt_chars,
                               "argv_utf16_units": argv_utf16_units, "prompt_budget_chars": prompt_budget_chars,
                               "spawn_ms": spawn_ms, "session_reused": session_reused,
                               "first_byte_ms": first_byte_ms, "total_ms": total_ms,
                               "exit_code": exit_code})
        del self.cli_calls[:-32]

    async def _exchange(self, handle: CliProcess, prompt: str) -> str:
        if self.spec.prompt_on_stdin:
            await handle.write(prompt)
            await handle.close_input()
        deadline = time.monotonic() + max(self.spec.startup_timeout, self.settings.timeout_seconds)
        chunks: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrainError(ErrorKind.TIMEOUT, "O agente local não terminou dentro do tempo limite.",
                                 detail=handle.diagnostics())
            line = await handle.readline(remaining)
            if not line:
                break
            if self._first_byte_ms is None and self._call_started:
                # Time to the first byte of output: the CLI's own cold start.
                self._first_byte_ms = (time.perf_counter() - self._call_started) * 1000
            chunks.append(line)
        code = await handle.wait(30)
        stdout = "".join(chunks)
        if code != 0:
            message = stdout.strip() or handle.diagnostics()
            if not message.strip():
                raise BrainError(ErrorKind.PROCESS_CRASHED,
                                 "O agente local terminou com erro sem mensagem.", detail=f"exit={code}")
            raise classify_failure(message)
        return stdout

    # -- FASE 4: session scoped to the task ------------------------------------
    def session_argv(self) -> list[str]:
        """The persistent-session command line. The prompt always travels by stdin here."""
        argv = [self.executable, *self.spec.session_args]
        argv.extend(self.effort_flags())
        if self.model and self.spec.model_args:
            argv.extend([*self.spec.model_args, self.model])
        return argv

    def _parse_reply(self, stdout: str) -> tuple[str, tuple[int | None, int | None]]:
        """One CLI answer (object or event stream) into text + usage."""
        payload: dict[str, Any]
        if not self.spec.reply_parser.endswith("-json"):
            payload = {"result": stdout}
        elif self.spec.json_reply_stream:
            payload = {"events": extract_json_events(stdout)}
        else:
            payload = extract_json_reply(stdout)
        if payload.get("is_error") is True or payload.get("subtype") in {"error", "error_max_turns"}:
            raise classify_failure(str(payload.get("result") or stdout))
        parser = REPLY_PARSERS.get(self.spec.reply_parser)
        if parser is None:
            raise BrainError(ErrorKind.UNSUPPORTED_FEATURE,
                             f"Parser {self.spec.reply_parser} não está registrado.")
        return parser(payload)

    async def _exchange_session(self, handle: CliProcess, prompt: str) -> str:
        """One turn on the live session: prompt in, streamed lines until the end marker."""
        await handle.write(prompt + "\n")
        deadline = time.monotonic() + max(self.spec.startup_timeout, self.settings.timeout_seconds)
        chunks: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrainError(ErrorKind.TIMEOUT, "A sessão do agente local não terminou dentro do tempo limite.",
                                 detail=handle.diagnostics())
            line = await handle.readline(remaining)
            if not line:
                # The session ended before its turn-end marker: the framing is lost, so
                # the stream cannot be trusted and the session has to be restarted.
                raise BrainError(ErrorKind.TRANSPORT,
                                 "A sessão do agente local terminou antes de completar a rodada.",
                                 detail=handle.diagnostics())
            if self._first_byte_ms is None and self._call_started:
                self._first_byte_ms = (time.perf_counter() - self._call_started) * 1000
            chunks.append(line)
            if self.spec.session_end in line:
                break
        return "".join(chunks)

    async def _complete_session(self, prompt: str, started: float) -> Completion:
        """One turn of a task-scoped session, reusing the process when it is healthy.

        The first call of a task pays the CLI's cold start; the following ones write to
        the same stdin and read until ``spec.session_end`` (``session_reused=true``). A
        turn that cannot be framed — timeout, transport failure, unparseable reply —
        kills the session and is retried exactly once on a clean spawn; a second failure
        is reported as itself. Inference has no side effect to replay, so the retry
        re-sends the same prompt and nothing else.
        """
        argv = self.session_argv()
        length = command_line_length(argv)
        budget = self.capabilities.max_safe_prompt_chars
        attempts = 0
        spawn_ms = 0.0
        while True:
            attempts += 1
            reused = self._session is not None
            handle = self._session
            if handle is None:
                spawn_started = time.perf_counter()
                handle = await spawn(argv, cwd=self.settings.ufo_path.parent)
                spawn_ms = (time.perf_counter() - spawn_started) * 1000
                self._session = handle
            self._process = handle
            succeeded = False
            try:
                if not prompt.strip():
                    raise BrainError(ErrorKind.CONFIGURATION, "Nada a enviar ao agente local.")
                stdout = await self._exchange_session(handle, prompt)
                if self._closed:
                    raise BrainError(ErrorKind.CANCELLED, "Operação cancelada.")
                text, (input_tokens, output_tokens) = self._parse_reply(stdout)
                succeeded = True
                return Completion(text, input_tokens, output_tokens)
            except BrainError as exc:
                await self._end_session()
                retryable = exc.kind in {ErrorKind.TIMEOUT, ErrorKind.TRANSPORT,
                                         ErrorKind.PROCESS_CRASHED, ErrorKind.MALFORMED_RESPONSE}
                if attempts > 1 or not retryable:
                    raise
            finally:
                # Every attempt is recorded, like the one-shot path: a retried turn shows
                # the failed attempt and the clean one, and which of them reused a session.
                self._record_call(
                    ok=succeeded,
                    prompt_chars=len(prompt),
                    argv_utf16_units=length,
                    prompt_budget_chars=budget,
                    spawn_ms=spawn_ms,
                    first_byte_ms=self._first_byte_ms,
                    total_ms=(time.perf_counter() - started) * 1000,
                    exit_code=handle.process.returncode if handle is not None else None,
                    session_reused=reused,
                )

    async def _end_session(self) -> None:
        """Kill the session's whole process tree; the next call spawns a clean one."""
        handle, self._session = self._session, None
        self._process = None
        if handle is not None:
            await handle.stop()

    def events(self, completion: Completion) -> list[BrainEvent]:
        return events_for(self.provider_id, completion)

    async def models(self) -> list[ModelInfo]:
        argv = self.models_argv()
        if argv is None:
            return []
        code, stdout, stderr = await run_once(argv, timeout=self.spec.startup_timeout + 20)
        if code != 0:
            raise classify_failure(stdout.strip() or stderr)
        parser = MODEL_PARSERS.get(self.spec.models_parser)
        if parser is None:
            raise BrainError(ErrorKind.UNSUPPORTED_FEATURE,
                             f"Parser de modelos {self.spec.models_parser} não está registrado.")
        # The CLI's listing order is not a recommendation; a curated default is.
        return [ModelInfo(identifier, label, default=identifier == self.spec.preferred_model)
                for identifier, label in parser(stdout)]

    async def health(self) -> BrainHealth:
        if resolve_executable(self.spec.executable, self.spec.search_paths) is None:
            return BrainHealth(HealthStatus.NOT_INSTALLED,
                               f"{self.spec.label} não está instalado nesta máquina.")
        try:
            code, stdout, stderr = await run_once([self.executable, *self.spec.version_args],
                                                  timeout=self.spec.startup_timeout)
        except BrainError as exc:
            return BrainHealth(HealthStatus.UNAVAILABLE, exc.message)
        if code != 0:
            return BrainHealth(HealthStatus.UNAVAILABLE,
                               f"{self.spec.label} não respondeu ao --version.")
        version = _VERSION_PATTERN.search(stdout or stderr)
        label = f"{self.spec.label} {version.group(0)}" if version else self.spec.label
        if self.spec.auth_probe_args:
            state = await self._auth_state()
            if state is not None:
                return state
        if self.models_argv() is not None:
            try:
                available = await self.models()
            except BrainError as exc:
                if exc.kind in {ErrorKind.AUTH_REQUIRED, ErrorKind.INVALID_CREDENTIALS}:
                    return BrainHealth(HealthStatus.LOGIN_REQUIRED, f"{label}: login necessário.")
                if exc.kind is not ErrorKind.UNSUPPORTED_FEATURE:
                    return BrainHealth(HealthStatus.UNAVAILABLE, f"{label}: {exc.message}")
            else:
                # The same rule the API brains already follow: a saved model that the
                # provider's own listing does not contain is a configuration mistake,
                # and saying so is what turns a silent failure into a visible one.
                if self.model and available and all(item.id != self.model for item in available):
                    return BrainHealth(
                        HealthStatus.MISCONFIGURED,
                        f"O modelo '{self.model}' não está disponível em {self.spec.label} "
                        f"({len(available)} modelos na lista). Escolha outro; a seleção salva "
                        "não foi alterada.")
        return BrainHealth(HealthStatus.READY, f"{label} pronto.")

    async def _auth_state(self) -> BrainHealth | None:
        """Cheap authentication probe: ``None`` means "no verdict, try inference"."""
        try:
            code, stdout, stderr = await run_once(
                [self.executable, *self.spec.auth_probe_args],
                timeout=self.spec.auth_probe_timeout)
        except BrainError:
            return None
        combined = f"{stdout}\n{stderr}"
        if code == 0:
            return None
        lowered = combined.casefold()
        if any(hint in lowered for hint in _AUTH_HINTS):
            return BrainHealth(HealthStatus.LOGIN_REQUIRED,
                               f"{self.spec.label} está instalado, mas sem login utilizável "
                               "em modo automático. Faça login no CLI ou forneça a chave de API.")
        if any(hint in lowered for hint in _QUOTA_HINTS):
            return BrainHealth(HealthStatus.UNAVAILABLE,
                               f"{self.spec.label} está autenticado, mas a conta está sem cota "
                               "no momento.")
        return None

    async def close(self) -> None:
        self._closed = True
        handle, self._process = self._process, None
        if handle is not None:
            await handle.stop()
        await self._end_session()

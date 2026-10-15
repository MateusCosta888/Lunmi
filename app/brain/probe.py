"""Inference probe: does this provider/model answer, and how long did it take?

The free readiness check (`health`) proves an endpoint is reachable or a CLI is
installed. It cannot prove that the *selected model* answers: an expired key, a
retired model, an account without credits and a CLI waiting for login all pass it.
This probe spends one minimal round-trip on that exact model — the same call a
task makes — and reports the verdict with the latency it measured.

One sample is one sample: ``latency_ms`` is a single wall-clock round trip, not a
distribution. ``tests/probe_live_check.py --repeat N`` exists for the distribution.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any

from app.brain.contracts import Brain, BrainError, ErrorKind
from app.brain.registry import resolve
from app.settings import Settings

# O menor pedido que ainda prova uma inferência: uma resposta pedida, nada mais.
# Nenhum prompt de sistema entra aqui — o teste mede o transporte e o modelo, e um
# prompt grande mediria a nossa própria redação.
PROBE_PROMPT = "Responda apenas com a palavra: ok"
PROBE_MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": PROBE_PROMPT}]
# O tempo configurado do app (300 s) vale para tarefas, que são longas por natureza;
# um teste de conexão não pode pendurar a interface, então ele tem limite próprio —
# folgado o bastante para a partida a frio de um CLI, curto o bastante para desistir.
PROBE_TIMEOUT_SECONDS = 90.0
# Quanto do texto do modelo é guardado para mostrar que algo voltou. O teste não é
# uma inferência de verdade e não precisa carregar a resposta inteira.
PROBE_TEXT_CHARS = 120


def _seconds(ms: float) -> str:
    """Milissegundos até 1 s, segundos acima disso: a unidade que se lê melhor."""
    if ms < 1000:
        return f"{ms:.0f} ms"
    return f"{ms / 1000:.2f} s".replace(".", ",")


@dataclass(frozen=True)
class ProbeResult:
    """One measured round trip: whether the model answered, and how long it took."""

    provider: str
    model: str
    ok: bool
    latency_ms: float = 0.0
    text: str = ""
    message: str = ""
    detail: str = ""
    error_kind: ErrorKind | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Para CLIs: tempo criando o processo e tempo até o primeiro byte, medidos pelo
    # próprio adaptador. Vazio quando o provider não separa essas fases.
    cli_ms: dict[str, float] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}" if self.model else self.provider

    def summary(self) -> str:
        """The user-facing line: verdict first, then the measured numbers."""
        if not self.ok:
            kind = f" [{self.error_kind.value}]" if self.error_kind is not None else ""
            when = f"{_seconds(self.latency_ms)}" if self.latency_ms else "sem medição"
            return f"{self.label} não respondeu em {when}{kind}: {self.message}".strip()
        head = f"{self.label} respondeu em {_seconds(self.latency_ms)}"
        if self.text:
            head = f'{self.label} respondeu "{self.text}" em {_seconds(self.latency_ms)}'
        parts = [head]
        if self.input_tokens is not None or self.output_tokens is not None:
            parts.append(f"tokens {self.input_tokens or 0}/{self.output_tokens or 0}")
        if self.cli_ms.get("spawn_ms"):
            parts.append(f"processo {self.cli_ms['spawn_ms']:.0f} ms")
        if self.cli_ms.get("first_byte_ms"):
            parts.append(f"1º byte {self.cli_ms['first_byte_ms']:.0f} ms")
        return " · ".join(parts)


def _model_of(brain: Brain) -> str:
    """The model this brain is configured for, whichever attribute it uses."""
    model = getattr(brain, "model", "")
    if isinstance(model, str) and model.strip():
        return model
    settings = getattr(brain, "settings", None)
    candidate = getattr(settings, "model", "")
    return candidate if isinstance(candidate, str) else ""


def _cli_sample(brain: Brain, calls_before: int) -> dict[str, float]:
    """The timings the *current* call recorded, never a previous round's."""
    calls = getattr(brain, "cli_calls", None)
    if not isinstance(calls, list) or len(calls) <= calls_before:
        return {}
    record = calls[-1]
    if not isinstance(record, dict):
        return {}
    return {key: float(value) for key, value in record.items()
            if key in {"spawn_ms", "first_byte_ms", "total_ms"} and isinstance(value, (int, float))}


async def probe(brain: Brain, *, timeout: float = PROBE_TIMEOUT_SECONDS,
                messages: list[dict[str, Any]] | None = None) -> ProbeResult:
    """Spend one minimal inference on ``brain`` and measure it.

    Never raises for a provider problem: a failure is a result the caller displays,
    keeping the provider's own error kind. Cancellation still propagates, because
    that is the caller's decision, not the provider's answer.
    """
    provider = getattr(brain, "provider_id", "") or ""
    model = _model_of(brain)
    prompt = messages if messages is not None else PROBE_MESSAGES
    calls = getattr(brain, "cli_calls", None)
    calls_before = len(calls) if isinstance(calls, list) else 0
    started = time.perf_counter()
    try:
        completion = await asyncio.wait_for(brain.complete(prompt), timeout=timeout)
    except asyncio.TimeoutError:
        result = ProbeResult(provider, model, False, _elapsed(started),
                             message=f"o modelo não respondeu em {_seconds(timeout * 1000)}.",
                             error_kind=ErrorKind.TIMEOUT)
    except BrainError as exc:
        result = ProbeResult(provider, model, False, _elapsed(started), message=exc.message,
                             detail=exc.detail, error_kind=exc.kind)
    except Exception as exc:  # noqa: BLE001 - a probe reports, it never explodes
        result = ProbeResult(provider, model, False, _elapsed(started),
                             message=f"falha inesperada: {type(exc).__name__}.",
                             detail=str(exc), error_kind=ErrorKind.UNKNOWN)
    else:
        text = (completion.text or "").strip()
        latency = _elapsed(started)
        if not text:
            # O transporte funcionou e o modelo não devolveu nada utilizável: isso não
            # é prova de que ele responde, então o veredito é negativo — mas a mensagem
            # separa o caso de "não respondeu" do caso "recusou".
            result = ProbeResult(provider, model, False, latency,
                                 message="o provedor respondeu sem texto.",
                                 input_tokens=completion.input_tokens,
                                 output_tokens=completion.output_tokens,
                                 error_kind=ErrorKind.MALFORMED_RESPONSE)
        else:
            result = ProbeResult(provider, model, True, latency, text=text[:PROBE_TEXT_CHARS],
                                 input_tokens=completion.input_tokens,
                                 output_tokens=completion.output_tokens)
    # O tempo de um CLI também explica uma recusa (a partida do processo vs a espera), então
    # ele entra nos dois vereditos — sempre da chamada medida, nunca de uma anterior.
    result = replace(result, cli_ms=_cli_sample(brain, calls_before))
    # O log guarda o veredito e o número; nunca o prompt nem a resposta do modelo.
    kind = result.error_kind.value if result.error_kind is not None else "?"
    verdict = "respondeu" if result.ok else f"falhou [{kind}]"
    logging.info("Teste de modelo %s: %s (%.0f ms)", result.label, verdict, result.latency_ms)
    return result


def _elapsed(started: float) -> float:
    return (time.perf_counter() - started) * 1000


async def probe_settings(settings: Settings, *, timeout: float = PROBE_TIMEOUT_SECONDS,
                         messages: list[dict[str, Any]] | None = None) -> ProbeResult:
    """Resolve the brain for this selection, probe it, and release it.

    The single entry point for the dialog button and for the measurement script: it
    builds the brain exactly as a task would and closes what it opened.
    """
    try:
        brain = resolve(settings)
    except BrainError as exc:
        logging.info("Teste de modelo %s/%s: não foi possível construir o cérebro (%s).",
                     settings.provider, settings.model, exc.kind.value)
        return ProbeResult(settings.provider, settings.model, False, 0.0,
                           message=exc.message, detail=exc.detail, error_kind=exc.kind)
    try:
        return await probe(brain, timeout=timeout, messages=messages)
    finally:
        await brain.close()

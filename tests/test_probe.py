"""Unit tests for the inference probe ("Testar modelo") — latency and verdict.

The probe is the only place that spends a real inference to answer "does *this*
model respond?"; these tests pin what it measures (one round trip, real clock),
what it reports (the provider's own error kind), and what it must never do
(swallow cancellation, invent a latency, or carry the prompt into the log line).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.brain import probe as probe_module
from app.brain.contracts import BrainError, ErrorKind
from app.brain.probe import (
    PROBE_MESSAGES,
    PROBE_PROMPT,
    ProbeResult,
    probe,
    probe_settings,
)
from app.llm import Completion
from app.settings import Settings


class FakeBrain:
    """A brain with exactly the surface the probe touches."""

    provider_id = "fake"

    def __init__(self, *, text: str = "ok", delay: float = 0.0,
                 failure: BaseException | None = None, model: str = "fake-model") -> None:
        self.model = model
        self.text = text
        self.delay = delay
        self.failure = failure
        self.prompts: list[list[dict[str, Any]]] = []
        self.closed = False
        self.cli_calls: list[dict[str, Any]] = []

    async def complete(self, messages: list[dict[str, Any]]) -> Completion:
        self.prompts.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure is not None:
            raise self.failure
        return Completion(self.text, 12, 4)

    async def close(self) -> None:
        self.closed = True


def test_probe_spends_one_minimal_inference_and_measures_it() -> None:
    brain = FakeBrain(delay=0.05)
    result = asyncio.run(probe(brain))

    assert result.ok is True
    assert (result.provider, result.model) == ("fake", "fake-model")
    # A latência é do relógio, não do provider: o piso é o sono medido.
    assert result.latency_ms >= 50.0
    assert (result.input_tokens, result.output_tokens) == (12, 4)
    # Uma chamada, um pedido: nada de prompt de sistema nem de histórico.
    assert brain.prompts == [PROBE_MESSAGES]
    assert PROBE_PROMPT in brain.prompts[0][0]["content"]
    assert len(brain.prompts[0]) == 1


def test_probe_keeps_the_provider_error_kind_and_message() -> None:
    failure = BrainError(ErrorKind.QUOTA_EXCEEDED, "Limite de uso atingido no provedor.",
                         detail="raw provider blob")
    result = asyncio.run(probe(FakeBrain(failure=failure)))

    assert result.ok is False
    assert result.error_kind is ErrorKind.QUOTA_EXCEEDED
    assert result.message == "Limite de uso atingido no provedor."
    assert result.detail == "raw provider blob"
    # A recusa também tem tempo: medido, não inventado.
    assert result.latency_ms > 0


def test_probe_gives_up_at_its_own_bound_instead_of_hanging() -> None:
    """O provedor que nunca responde vira um veredito, não uma janela travada."""
    brain = FakeBrain(delay=5.0)
    started = time.perf_counter()
    result = asyncio.run(probe(brain, timeout=0.05))
    elapsed = time.perf_counter() - started

    assert result.ok is False
    assert result.error_kind is ErrorKind.TIMEOUT
    assert elapsed < 1.0, elapsed
    assert "s." in result.message or "ms." in result.message


def test_probe_treats_an_empty_answer_as_not_answering() -> None:
    """Transporte de pé com resposta vazia não é prova de que o modelo responde."""
    result = asyncio.run(probe(FakeBrain(text="   ")))

    assert result.ok is False
    assert result.error_kind is ErrorKind.MALFORMED_RESPONSE
    assert "sem texto" in result.message


def test_probe_reports_an_unexpected_crash_as_unknown() -> None:
    result = asyncio.run(probe(FakeBrain(failure=ValueError("boom"))))

    assert result.ok is False
    assert result.error_kind is ErrorKind.UNKNOWN
    assert "ValueError" in result.message


def test_probe_attributes_only_this_call_cli_timings() -> None:
    """O tempo de um CLI é da rodada medida, nunca da anterior."""
    brain = FakeBrain()
    brain.cli_calls.append({"ok": True, "spawn_ms": 99999.0, "total_ms": 99999.0})

    original = brain.complete

    async def complete_with_record(messages: list[dict[str, Any]]) -> Completion:
        brain.cli_calls.append({"ok": True, "spawn_ms": 120.0, "first_byte_ms": 900.0,
                                "total_ms": 1100.0})
        return await original(messages)

    brain.complete = complete_with_record  # type: ignore[method-assign]
    result = asyncio.run(probe(brain))

    assert result.cli_ms == {"spawn_ms": 120.0, "first_byte_ms": 900.0, "total_ms": 1100.0}
    assert "processo 120 ms" in result.summary()
    assert "1º byte 900 ms" in result.summary()


def test_probe_propagates_cancellation_instead_of_reporting_it() -> None:
    """Fechar o diálogo cancela o teste: cancelar é decisão do chamador, não resposta."""

    async def scenario() -> None:
        brain = FakeBrain(delay=5.0)
        task = asyncio.ensure_future(probe(brain))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_probe_settings_builds_and_releases_the_brain(monkeypatch: pytest.MonkeyPatch) -> None:
    brain = FakeBrain()
    monkeypatch.setattr(probe_module, "resolve", lambda settings: brain)

    result = asyncio.run(probe_settings(Settings(provider="custom", model="m",
                                                 base_url="http://127.0.0.1:1234/v1")))

    assert result.ok is True
    assert brain.closed is True


def test_probe_settings_reports_a_brain_that_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(settings: Settings) -> None:
        raise BrainError(ErrorKind.EXECUTABLE_MISSING, "Qoder CLI não está instalado.")

    monkeypatch.setattr(probe_module, "resolve", refuse)
    result = asyncio.run(probe_settings(Settings(provider="qoder", model="")))

    assert result.ok is False
    assert result.error_kind is ErrorKind.EXECUTABLE_MISSING
    assert result.latency_ms == 0.0
    assert "não está instalado" in result.message


def test_probe_shows_a_rejected_key_as_a_typed_failure() -> None:
    """A camada toda: o teste do modelo conta *por que* não respondeu, não "falha inesperada"."""
    import httpx

    from app.brain.api_brain import ApiBrain

    brain = ApiBrain(Settings(provider="nvidia", model="modelo", api_key="chave-recusada"))
    brain._provider.client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(401, text="chave-recusada")))
    try:
        result = asyncio.run(probe(brain))
    finally:
        asyncio.run(brain.close())

    assert result.ok is False
    assert result.error_kind is ErrorKind.INVALID_CREDENTIALS
    assert "[invalid_credentials]" in result.summary()
    assert "chave-recusada" not in result.summary()


def test_summary_carries_the_verdict_and_the_numbers() -> None:
    ok = asyncio.run(probe(FakeBrain(delay=0.01)))
    assert ok.summary().startswith('fake/fake-model respondeu "ok" em ')
    assert "tokens 12/4" in ok.summary()

    slow = ProbeResult("nvidia", "nvidia/modelo", True, 1250.0, text="ok")
    assert "1,25 s" in slow.summary()

    failed = ProbeResult("nvidia", "nvidia/modelo", False, 812.0,
                         message="A chave de API foi recusada pelo provedor.",
                         error_kind=ErrorKind.INVALID_CREDENTIALS)
    summary = failed.summary()
    assert "não respondeu" in summary and "812 ms" in summary
    assert "[invalid_credentials]" in summary
    assert "A chave de API foi recusada" in summary

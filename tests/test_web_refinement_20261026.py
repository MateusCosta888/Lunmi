"""Regressões do refinamento web de 26/10/2026; offline, sem provider nem rede.

Cobrem a projeção pública de IDs internos `[src-*]` (final, deltas e RAM), o
isolamento do chat normal e o roteamento explícito de cotação cambial antes do
Brain. Nenhum teste abre worker, socket, provider, browser ou ficheiro pessoal.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.brain.contracts import BrainEvent, EventKind
from app.core.automatic import WEB_SYNTHESIS_PROMPT, direct_web_operation, web_requested
from app.core.controller import _NATIVE_WEB_PROMPT
from app.integrations.web_search import WebResult
from app.llm import Completion
from test_web_search_v1 import _FakeWeb, _controller, _source


class _StreamingBrain:
    """Brain guionizado que expõe `stream`; o teste prova os deltas reais."""

    model = "fixture"
    capabilities = None

    def __init__(self, raw: str, chunks: list[str]) -> None:
        self.raw = raw
        self.chunks = chunks
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]]) -> Completion:
        raise AssertionError("a síntese web deve usar o stream, não complete()")

    async def stream(self, messages: list[dict[str, str]]):
        self.calls.append(messages)
        yield BrainEvent(EventKind.STARTED, {"streaming": True})
        for chunk in self.chunks:
            yield BrainEvent(EventKind.TEXT_DELTA, {"text": chunk})
        yield BrainEvent(EventKind.COMPLETED, {"completion": Completion(self.raw)})

    async def close(self) -> None:
        pass


def _web(source: dict, *, read: bool = True) -> _FakeWeb:
    documents = [{**source, "content": "READ BODY"}] if read else []
    return _FakeWeb(WebResult(tool="web.search", sources=[source], documents=documents))


def _synthesis_raw(message: str) -> str:
    return json.dumps({
        "kind": "answer", "action": None, "status": "FINISH",
        "message": [message], "memory_changes": [],
    }, ensure_ascii=False)


def _chunks_around_token(raw: str, token: str) -> list[str]:
    """Parte o JSON dentro de `[src-…]`, para provar que nenhum fragmento escapa."""
    start = raw.index(token)
    cuts = [0, max(0, start - 2), start + 2, start + 7, len(raw)]
    return [raw[first:second] for first, second in zip(cuts, cuts[1:]) if first < second]


def test_known_citation_is_validated_then_hidden_from_final_stream_and_ram(
        tmp_path: Path) -> None:
    source = _source()
    raw = _synthesis_raw(f"Resposta [ {source['id']} ]. Fim.")
    controller = _controller(tmp_path, [])
    controller.brain = _StreamingBrain(raw, _chunks_around_token(raw, source['id']))  # type: ignore[assignment]
    controller.web = _web(source)
    events: list[tuple[str, dict[str, object]]] = []
    controller.on_public = lambda kind, data: events.append((kind, data))

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert result.status == "SUCCESS"
        assert result.web_sources == [source]
        assert "src-" not in result.message
        assert result.message.startswith("Resposta")
        deltas = "".join(str(data.get("text", "")) for kind, data in events
                         if kind == "response.text.delta")
        assert "src-" not in deltas
        assert deltas == result.message
        stored = controller.turns.pairs(controller.session_id)
        assert stored and "src-" not in stored[-1][1]
        await controller.close()

    asyncio.run(scenario())


def test_unknown_citation_is_hidden_but_still_not_verified(tmp_path: Path) -> None:
    source = _source()
    raw = _synthesis_raw("Resposta [ src-0000000000000000 ].")
    controller = _controller(tmp_path, [])
    controller.brain = _StreamingBrain(raw, _chunks_around_token(
        raw, "src-0000000000000000"))  # type: ignore[assignment]
    controller.web = _web(source)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert result.status == "NOT_VERIFIED"
        assert result.web_outcome == "NOT_VERIFIED"
        assert "src-" not in result.message
        assert "não está verificado" in result.message
        assert result.web_sources == [source]
        await controller.close()

    asyncio.run(scenario())


def test_invented_url_is_still_not_verified_after_id_hiding(tmp_path: Path) -> None:
    source = _source()
    raw = _synthesis_raw("Segundo https://invented.example/x, o valor é 10.")
    controller = _controller(tmp_path, [])
    controller.brain = _StreamingBrain(raw, [raw])  # type: ignore[assignment]
    controller.web = _web(source)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert result.status == "NOT_VERIFIED"
        assert "src-" not in result.message
        assert result.web_sources == [source]
        await controller.close()

    asyncio.run(scenario())


def test_quote_prompts_forbid_current_claims_without_currency_proof() -> None:
    for prompt in (WEB_SYNTHESIS_PROMPT, _NATIVE_WEB_PROMPT):
        assert "not when the price was observed" in prompt
        assert "Never infer a " in prompt and "currency pair or unit" in prompt
        assert "approximate reference" in prompt
        assert "Never invent" in prompt


def test_quote_context_exposes_cache_and_truncation_flags(tmp_path: Path) -> None:
    source = {**_source(), "cached": True, "truncated": True}
    answer = {"kind": "answer", "action": None, "status": "FINISH",
              "message": ["Referência aproximada; sem cotação em tempo real."],
              "memory_changes": []}
    controller = _controller(tmp_path, [answer])
    controller.web = _web(source)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert result.status == "SUCCESS"
        messages = controller.brain.calls[-1]  # type: ignore[attr-defined]
        context = messages[-1]["content"]
        assert '"cached": true' in context
        assert '"truncated": true' in context
        assert "not when the price was observed" in messages[0]["content"]
        await controller.close()

    asyncio.run(scenario())


def test_normal_chat_keeps_bracketed_text_and_is_not_projected(tmp_path: Path) -> None:
    text = "Usa arr[0] e [nota]; cita [src-0123456789abcdef]."
    answer = {"kind": "answer", "action": None, "status": "FINISH",
              "message": [text], "memory_changes": []}
    controller = _controller(tmp_path, [answer])

    async def scenario() -> None:
        result = await controller.run("Explica este exemplo.", conversation=True)
        assert result.message == text
        await controller.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("instruction", [
    "Quanto está o dólar?",
    "Quanto está o dólar em reais?",
    "Qual é o valor do euro hoje?",
    "USD ou EUR: quanto está?",
])
def test_explicit_fx_quote_reaches_web_without_inventing_a_pair(
        tmp_path: Path, instruction: str) -> None:
    assert web_requested(instruction)
    operation = direct_web_operation(instruction)
    assert operation is not None and operation["name"] == "web.search"
    assert operation["args"]["query"] == instruction
    assert "BRL" not in operation["args"]["query"]

    source = _source()
    raw = _synthesis_raw("Referência aproximada, sem cotação em tempo real.")
    controller = _controller(tmp_path, [])
    controller.brain = _StreamingBrain(raw, [raw])  # type: ignore[assignment]
    controller.web = _web(source)

    async def scenario() -> None:
        result = await controller.run(instruction, conversation=True)
        assert controller.web.calls == [("web.search", {"query": instruction})]  # type: ignore[attr-defined]
        assert result.llm_calls == 1
        prompt = controller.brain.calls[-1][-1]["content"]  # type: ignore[attr-defined]
        assert "FONTES EXTERNAS" in prompt
        assert "BRL" not in prompt.split("FONTES EXTERNAS", 1)[-1]
        await controller.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("instruction", [
    "Não pesquisa quanto está o dólar.",
    'Explica a frase "Quanto está o dólar?".',
    "Quanto está o dólar no ficheiro de notas?",
    "O dólar é uma moeda.",
    "Como funciona o câmbio do dólar?",
])
def test_fx_quote_respects_negation_explanation_and_local_guards(
        instruction: str) -> None:
    assert not web_requested(instruction)
    assert direct_web_operation(instruction) is None


def test_fragmented_citation_never_exposes_a_partial_token() -> None:
    from app.core.public_text import WebCitationStream, web_public_text

    raw = "Resposta [ src-0123456789abcdef ]. Fim."
    stream = WebCitationStream()
    seen = ""
    for char in raw:
        seen += char
        projected = stream.feed(seen)
        assert "src-" not in projected
    assert stream.finish() == "Resposta . Fim."
    assert web_public_text(raw) == "Resposta . Fim."
    assert web_public_text("Usa arr[0] e [nota].") == "Usa arr[0] e [nota]."
    normal = WebCitationStream()
    assert normal.feed("Lista: [") == "Lista: "
    assert normal.finish() == "Lista: ["
    broken = WebCitationStream()
    assert broken.feed("Resposta [src-") == "Resposta "
    assert broken.finish() == "Resposta "
    assert web_public_text("Resposta [src-") == "Resposta "

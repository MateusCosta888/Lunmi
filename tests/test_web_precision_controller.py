"""Regressões offline de WEB-SEARCH-PRECISION-V1 — admissões públicas (Codador).

A resposta pode ter fontes e páginas lidas e ainda assim dizer que não responde ao
pedido (contagem, valor atual). Nenhum teste abre rede, provider, runtime ou GUI.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.brain.web import (
    NativeWebCapability,
    NativeWebOutcome,
    NativeWebResult,
    NativeWebSource,
)
from app.core.automatic import WEB_SYNTHESIS_PROMPT
from app.core.controller import (
    TaskController,
    _NATIVE_WEB_PROMPT,
    _PROPORTIONAL_ANSWER_GUIDANCE,
)
from app.core.public_text import web_answer_admits_insufficiency
from app.integrations.web_search import WebResult, source_id
from app.integrations.web_state import WebPreferences, WebState, identity_for_settings
from app.llm import Completion
from app.memory import MemoryManager
from app.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        provider="custom", model="fixture", api_key="synthetic",
        base_url="http://127.0.0.1:1/v1", prewarm=False, app_index=False,
        app_search=False, collect_metrics=False, fast_path=False,
        timeout_seconds=5, logs_path=tmp_path / "logs",
        output_path=tmp_path / "output", state_root=tmp_path / "state",
    )


class _Brain:
    model = "fixture"
    capabilities = None

    def __init__(self, answer: str = "",
                 replies: list[dict[str, Any]] | None = None) -> None:
        self.answer = answer
        self.replies = list(replies or [])
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]]) -> Completion:
        self.calls.append(messages)
        reply = self.replies.pop(0) if self.replies else {
            "kind": "answer", "action": None, "status": "FINISH",
            "message": [self.answer], "memory_changes": [],
        }
        return Completion(json.dumps(reply))

    async def close(self) -> None:
        pass


class _NativeBrain:
    model = "fixture"
    capabilities = None
    provider_id = "custom"

    def __init__(self, native: NativeWebResult) -> None:
        self.native = native
        self.web_calls: list[list[dict[str, str]]] = []

    def native_web_capability(self) -> NativeWebCapability:
        return NativeWebCapability.ready()

    async def complete_web(self, messages: list[dict[str, str]]) -> NativeWebResult:
        self.web_calls.append(messages)
        return self.native

    async def cancel_web(self) -> bool:
        return True

    async def complete(self, messages: list[dict[str, str]]) -> Completion:
        raise AssertionError("O caminho nativo não pode pedir uma segunda resposta JSON.")

    async def close(self) -> None:
        pass


class _Web:
    enabled = True
    available = True
    mode = "auto"

    def __init__(self, state: WebState, result: WebResult) -> None:
        self._state = state
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, tool: str, arguments: dict[str, Any], *,
                  mode: str | None = None) -> WebResult:
        self.calls.append((tool, dict(arguments)))
        return self.result


def _web(tmp_path: Path, *, content: str = "READ BODY " * 20,
         url: str = "https://example.test/one-punch-man",
         title: str = "One-Punch Man manga", limited: bool = False) -> _Web:
    settings = _settings(tmp_path)
    identity = identity_for_settings(settings)
    state = WebState(session_id="sess-precision")
    state.set_preferences(WebPreferences(brain_origins={identity.fingerprint: "lunmi"}))
    source = {
        "id": source_id(url), "title": title, "url": url,
        "snippet": "excerpt " * 12, "engines": ["anysearch"], "published_at": None,
        "date_source": None, "retrieved_at": "2026-10-26T10:00:00Z",
        "read": bool(content), "truncated": False, "cached": False,
    }
    documents = [{**source, "content": content}] if content else []
    return _Web(state, WebResult(
        tool="web.search", sources=[source], documents=documents, limited=limited,
        evidence_status="insufficient" if limited else "suff",
        request_status="unmet" if limited else "met",
    ))


def _native_source(url: str = "https://example.test/x-valuation") -> NativeWebSource:
    return NativeWebSource(
        title="X valuation", url=url, snippet="valuation excerpt " * 6,
        retrieved_at="2026-10-26T10:00:00Z", read=True, engines=("web_search",))


def _controller(tmp_path: Path, answer: str, web: _Web,
                brain: _Brain | _NativeBrain | None = None) -> TaskController:
    controller = TaskController(_settings(tmp_path),
                                memory=MemoryManager(tmp_path / "memory.sqlite3"))
    controller.memory.set_options(enabled=True, episodes=True, history=True, proposals=True)
    controller.brain = brain or _Brain(answer)  # type: ignore[assignment]
    controller.web = web  # type: ignore[assignment]
    return controller


@pytest.mark.parametrize("text", [
    "Os excertos disponíveis não indicam o número exato de capítulos atuais de One-Punch Man.",
    "Os excertos disponíveis não informam quantos capítulos tem o quadrinho.",
    "Os excertos disponíveis não permitem confirmar um valor único e atual para a rede social X.",
    # Generalização próxima: outras recusas observáveis com fonte/excerto continuam a contar.
    "As fontes disponíveis não mostram a contagem atual de temporadas.",
    "The available snippets do not state the current number of chapters.",
])
def test_public_admission_of_a_missing_count_or_current_value_is_detected(text: str) -> None:
    assert web_answer_admits_insufficiency(text)


@pytest.mark.parametrize("text", [
    "Os excertos indicam nove temporadas lançadas até agora.",
    "A fonte não é oficial, mas confirma o número atual de capítulos.",
    "Encontrei os excertos oficiais e eles listam as temporadas lançadas.",
    "O texto está em inglês; a página confirma o valor atual.",
])
def test_answer_with_a_supported_fact_or_unrelated_caveat_is_not_flagged(text: str) -> None:
    assert not web_answer_admits_insufficiency(text)


def test_controller_downgrades_an_admission_and_adds_one_precise_caveat(tmp_path: Path) -> None:
    admission = ("Os excertos disponíveis não indicam o número exato de capítulos "
                 "atuais de One-Punch Man.")
    web = _web(tmp_path)
    controller = _controller(tmp_path, admission, web)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre One-Punch Man.",
                                      conversation=True)
        assert result.status == "NOT_VERIFIED"
        assert result.web_outcome == "NOT_VERIFIED"
        assert result.failure_kind == "VERIFICATION_FAILURE"
        assert result.evidence_status == "partial"
        assert result.request_status == "partial"
        assert result.web_sources == web.result.sources
        # A resposta já nomeia o facto em falta; repetir a caveat genérica duplicaria a
        # limitação. O downgrade e a resposta honesta mantêm-se intactos.
        assert result.message == admission
        assert "Não foi possível confirmar a resposta" not in result.message
        assert "Não foi possível ler as páginas" not in result.message
        assert controller.turns.pairs(controller.session_id) == [
            ("Pesquisa na internet sobre One-Punch Man.", admission)]
        await controller.close()

    asyncio.run(scenario())


def test_generic_limited_answer_without_an_admission_gets_one_honest_note(
        tmp_path: Path) -> None:
    answer = "Encontrei referências, mas a resposta é limitada."
    web = _web(tmp_path, content="", limited=True)
    controller = _controller(tmp_path, answer, web)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre One-Punch Man.",
                                      conversation=True)
        assert result.status == "NOT_VERIFIED"
        assert result.web_outcome == "NOT_VERIFIED"
        assert result.evidence_status == "insufficient"
        assert result.request_status == "unmet"
        assert result.message.startswith(answer)
        assert result.message.count("Não foi possível ler as páginas") == 1
        assert "Não foi possível confirmar a resposta" not in result.message
        await controller.close()

    asyncio.run(scenario())


def test_controller_keeps_a_supported_answer_successful_without_a_caveat(
        tmp_path: Path) -> None:
    # Facto sintético: a resposta e o conteúdo lido provam a mesma proposição.
    answer = "O manga fictício Lunar Busters tem 12 capítulos publicados."
    web = _web(
        tmp_path,
        content=("Lunar Busters tem 12 capítulos publicados. "
                 "A contagem cobre a edição digital atual. " * 3),
        url="https://example.test/lunar-busters",
        title="Lunar Busters manga",
    )
    controller = _controller(tmp_path, answer, web)

    async def scenario() -> None:
        result = await controller.run(
            "Pesquisa na internet sobre o manga fictício Lunar Busters.",
            conversation=True)
        assert result.status == "SUCCESS"
        assert result.web_outcome == "SUCCESS"
        assert result.evidence_status == "suff"
        assert result.request_status == "met"
        assert result.message == answer
        assert "Não foi possível confirmar" not in result.message
        await controller.close()

    asyncio.run(scenario())


def test_answer_with_the_wrong_attribute_is_not_presented_as_complete(
        tmp_path: Path) -> None:
    # A pergunta pede capítulos; a resposta só prova volumes. O manager e o controller
    # não podem promover isso a resposta completa.
    answer = "Lunar Busters tem 12 volumes publicados."
    web = _web(
        tmp_path,
        content="Lunar Busters tem 12 volumes publicados. " * 4,
        url="https://example.test/lunar-busters-volumes",
        title="Lunar Busters volumes",
    )
    controller = _controller(tmp_path, answer, web)

    async def scenario() -> None:
        result = await controller.run(
            "Pesquisa na internet quantos capítulos tem o manga fictício Lunar Busters?",
            conversation=True)
        assert result.status == "NOT_VERIFIED"
        assert result.web_outcome == "NOT_VERIFIED"
        assert result.evidence_status == "partial"
        assert result.request_status == "partial"
        assert "Não foi possível confirmar a resposta" in result.message
        await controller.close()

    asyncio.run(scenario())


def test_native_admission_keeps_the_precise_limit_without_a_duplicate_caveat(
        tmp_path: Path) -> None:
    text = "Não consegui confirmar uma avaliação atual e única da rede social X."
    brain = _NativeBrain(NativeWebResult(
        text=text, provider_id="custom", tool_names=("web_search",),
        sources=(_native_source(),), outcome=NativeWebOutcome.SUCCESS,
        input_tokens=5, output_tokens=3))
    web = _web(tmp_path)
    controller = _controller(tmp_path, answer="", web=web, brain=brain)
    identity = identity_for_settings(controller.settings)
    web._state.set_preferences(WebPreferences(
        brain_origins={identity.fingerprint: "provider"}))
    web._state.bind_brain(identity.fingerprint, NativeWebCapability.ready(), available=True)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre a rede social X.",
                                      conversation=True)
        assert result.search_source == "provider"
        assert len(brain.web_calls) == 1
        assert result.status == "NOT_VERIFIED"
        assert result.web_outcome == "NOT_VERIFIED"
        assert result.evidence_status == "partial"
        assert result.request_status == "partial"
        assert result.message == text
        assert "Não foi possível confirmar a resposta" not in result.message
        await controller.close()

    asyncio.run(scenario())


def test_clarification_keeps_the_original_valuation_question_in_synthesis(
        tmp_path: Path) -> None:
    clarification = {
        "kind": "answer", "action": None, "status": "FINISH",
        "message": ["O que queres dizer com X: a rede social ou outra coisa?"],
        "memory_changes": [],
    }
    operation = {"kind": "tool", "operation": {
        "name": "web.search", "args": {"query": "rede social X"}}, "finish": False}
    answer = {
        "kind": "answer", "action": None, "status": "FINISH",
        "message": ["Não consegui confirmar uma avaliação atual da rede social X."],
        "memory_changes": [],
    }
    brain = _Brain(replies=[clarification, operation, answer])
    web = _web(tmp_path)
    controller = _controller(tmp_path, answer="", web=web, brain=brain)

    async def scenario() -> None:
        first = await controller.run("Quanto vale o X hoje?", conversation=True)
        assert first.status == "SUCCESS"
        second = await controller.run("A rede social", conversation=True)
        assert second.status == "NOT_VERIFIED"
        assert len(web.calls) == 1
        synthesis = "\n".join(message["content"] for message in brain.calls[-1])
        assert "Quanto vale o X hoje?" in synthesis
        assert "A rede social" in synthesis
        # A pergunta original não transforma o esclarecimento em query pública nem
        # envia a conversa inteira ao motor de busca.
        query = web.calls[0][1]["query"]
        assert len(query) <= 600
        folded_query = query.casefold()
        assert any(term in folded_query for term in
                   ("vale", "valor", "avalia", "valuation"))
        assert "rede social" in folded_query
        # Nem a pergunta de esclarecimento do assistente nem o resto da conversa
        # entram na query pública; só a pergunta original do utilizador.
        assert "O que queres dizer" not in query
        assert "outra coisa" not in query
        await controller.close()

    asyncio.run(scenario())


def test_bare_chapter_followup_keeps_the_known_subject_from_ram(tmp_path: Path) -> None:
    first = {
        "kind": "answer", "action": None, "status": "FINISH",
        "message": ["O personagem principal de One-Punch Man é Saitama."],
        "memory_changes": [],
    }
    second = {
        "kind": "answer", "action": None, "status": "FINISH",
        "message": ["One-Punch Man tem 250 capítulos publicados."],
        "memory_changes": [],
    }
    brain = _Brain(replies=[first, second])
    web = _web(
        tmp_path,
        content="One-Punch Man tem 250 capítulos publicados. " * 3,
        title="One-Punch Man manga",
    )
    controller = _controller(tmp_path, answer="", web=web, brain=brain)

    async def scenario() -> None:
        await controller.run("quem e o personagem principal de one punch man?",
                             conversation=True)
        assert web.calls == []
        # Uma pergunta que já nomeia Rick and Morty não herda o sujeito anterior.
        assert controller._web_followup_context(
            "e rick and morty, quantas temporadas tem?") == ""
        result = await controller.run("e quantos capitolos do quadrinho tem?",
                                      conversation=True)
        assert len(web.calls) == 1
        query = web.calls[0][1]["query"].casefold()
        assert "one punch man" in query
        assert "capitul" in query
        assert len(query) <= 600
        assert result.status == "SUCCESS"
        await controller.close()

    asyncio.run(scenario())


def test_prompt_policy_is_answer_first_and_separates_lunmi_from_the_brain_vendor() -> None:
    synthesis = WEB_SYNTHESIS_PROMPT.casefold()
    native = _NATIVE_WEB_PROMPT.casefold()
    common = _PROPORTIONAL_ANSWER_GUIDANCE.casefold()
    for prompt in (synthesis, native):
        assert "answer first" in prompt
        assert "lunmi is the product" in prompt
        assert "never invent a creator" in prompt
        assert "generic full-page" in prompt
        assert "incumbent" in prompt and "elected" in prompt
        assert "date, currency and unit" in prompt
    assert "answer the asked question first" in common
    assert "lunmi is the product" in common
    assert "never invent a creator" in common
    assert "election claims" in common
    # A limitação deixa de ser uma caveat obrigatória em toda a resposta.
    assert "say so explicitly and keep the answer limited" not in synthesis
    assert "say so explicitly and keep the answer limited" not in native

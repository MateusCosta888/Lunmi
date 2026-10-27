"""Testes offline da origem web por Task (`.agents/WEB-SEARCH-ORIGIN-V1-CONTRACT.md`).

Nenhum teste abre rede, provider, runtime, GUI ou browser: o Brain e o manager são fakes.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.brain.web import (
    NativeWebCapability,
    NativeWebOutcome,
    NativeWebResult,
    NativeWebSource,
)
from app.core.controller import TaskController
from app.integrations.web_quality import constraints
from app.integrations.web_search import WebResult, WebSearchError, source_id
from app.integrations.web_state import (
    WebPreferences,
    WebPreferencesError,
    WebState,
    identity_for_settings,
    load_preferences,
    save_preferences,
)
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


class _NativeBrain:
    model = "fixture"
    capabilities = None
    provider_id = "custom"

    def __init__(self, *, native: NativeWebResult | None = None,
                 decision: dict[str, Any] | None = None,
                 replies: list[dict[str, Any]] | None = None) -> None:
        self.native = native
        self.replies = list(replies) if replies is not None else None
        self.decision = decision or {"kind": "answer", "action": None, "status": "FINISH",
                                     "message": ["Olá."], "memory_changes": []}
        self.web_calls: list[list[dict[str, Any]]] = []
        self.complete_calls: list[list[dict[str, Any]]] = []

    def native_web_capability(self) -> NativeWebCapability:
        return NativeWebCapability.ready()

    async def complete_web(self, messages: list[dict[str, Any]]) -> NativeWebResult:
        self.web_calls.append(messages)
        assert self.native is not None
        return self.native

    async def complete(self, messages: list[dict[str, Any]]) -> Completion:
        self.complete_calls.append(messages)
        reply = self.replies.pop(0) if self.replies else self.decision
        return Completion(json.dumps(reply))

    async def close(self) -> None:
        pass


class _FakeWeb:
    enabled = True
    available = True
    mode = "auto"

    def __init__(self, state: WebState, result: WebResult | None = None) -> None:
        self._state = state
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run(self, tool: str, arguments: dict[str, Any], *,
                  mode: str | None = None) -> WebResult:
        self.calls.append((tool, dict(arguments)))
        if self.result is None:
            raise AssertionError("A origem nativa/recusada não pode chamar o manager V1.")
        return self.result


def _web(tmp_path: Path, *, preference: str = "provider", enabled: bool = True) -> _FakeWeb:
    settings = _settings(tmp_path)
    state = WebState(session_id="sess-origin")
    identity = identity_for_settings(settings)
    state.set_preferences(WebPreferences(
        enabled=enabled, brain_origins={identity.fingerprint: preference}))
    state.bind_brain(identity.fingerprint, NativeWebCapability.ready(), available=True)
    return _FakeWeb(state)


def _controller(tmp_path: Path, brain: _NativeBrain, web: _FakeWeb) -> TaskController:
    controller = TaskController(_settings(tmp_path),
                                memory=MemoryManager(tmp_path / "memory.sqlite3"))
    controller.memory.set_options(enabled=True, episodes=True, history=True, proposals=True)
    controller.brain = brain  # type: ignore[assignment]
    controller.web = web  # type: ignore[assignment]
    return controller


def _native_source() -> NativeWebSource:
    return NativeWebSource(
        title="Native page", url="https://example.test/native",
        snippet="observed snippet", retrieved_at="2026-10-25T10:00:00Z",
        read=True, engines=("web_search",))


def test_provider_web_uses_complete_web_once_and_never_the_json_tools(tmp_path: Path) -> None:
    source = _native_source()
    brain = _NativeBrain(native=NativeWebResult(
        text="Resposta nativa com fonte.", provider_id="custom",
        tool_names=("web_search",), sources=(source,),
        outcome=NativeWebOutcome.SUCCESS, input_tokens=11, output_tokens=4))
    controller = _controller(tmp_path, brain, _web(tmp_path))

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert result.status == "SUCCESS"
        assert result.search_source == "provider"
        assert result.web_preference == "provider"
        assert result.web_fallback == ""
        assert result.provider_id == "custom"
        assert result.native_tool_names == ["web_search"]
        assert result.llm_calls == 1
        assert [call["agent"] for call in result.prompt_calls] == ["ProviderWeb"]
        assert result.web_sources == [{
            "id": source_id(str(source.url)), "title": "Native page",
            "url": "https://example.test/native", "snippet": "observed snippet",
            "engines": ["web_search"], "published_at": None, "date_source": None,
            "retrieved_at": "2026-10-25T10:00:00Z", "read": True,
            "truncated": False, "cached": False,
        }]
        assert result.evidence_status == "suff"
        assert result.request_status == "met"
        assert brain.web_calls and brain.complete_calls == []
        assert controller.web.calls == []  # type: ignore[attr-defined]
        await controller.close()

    asyncio.run(scenario())


def test_native_no_results_is_not_an_objective_success(tmp_path: Path) -> None:
    brain = _NativeBrain(native=NativeWebResult(
        text="Não encontrei fontes.", provider_id="custom", tool_names=("web_search",),
        sources=(), outcome=NativeWebOutcome.NO_RESULTS))
    controller = _controller(tmp_path, brain, _web(tmp_path))

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre zxqv-nonexistent.",
                                      conversation=True)
        assert result.status == "NOT_VERIFIED"
        assert result.web_outcome == "NO_RESULTS"
        assert result.evidence_status == "insufficient"
        assert result.request_status == "unmet"
        assert result.web_sources == []
        assert brain.web_calls and brain.complete_calls == []
        await controller.close()

    asyncio.run(scenario())


def test_preferences_refuse_a_65th_brain_without_touching_the_existing_64(
        tmp_path: Path) -> None:
    existing = {f"brain-{index:016x}": "lunmi" for index in range(64)}
    state = WebState(session_id="sess-limit")
    state.set_preferences(WebPreferences(brain_origins=dict(existing)))
    save_preferences(tmp_path, WebPreferences(brain_origins=dict(existing)))
    assert len(load_preferences(tmp_path).brain_origins) == 64

    new_brain = f"brain-{64:016x}"
    with pytest.raises(WebPreferencesError) as refused:
        save_preferences(tmp_path, WebPreferences(origin_update=(new_brain, "provider")))
    assert refused.value.code == "limit"
    assert load_preferences(tmp_path).brain_origins == existing

    state.set_preferences(WebPreferences(origin_update=(new_brain, "provider")))
    assert state.brain_origins == existing and len(state.brain_origins) == 64


def test_denied_public_url_blocks_before_any_brain_or_web(tmp_path: Path) -> None:
    brain = _NativeBrain()
    controller = _controller(tmp_path, brain, _web(tmp_path))

    async def scenario() -> None:
        result = await controller.run("Abre http://127.0.0.1:8080 e explica.",
                                      conversation=True)
        assert result.status == "FAILED"
        assert result.failure_kind == "AUTHORIZATION_REFUSED"
        assert result.web_outcome == "BLOCKED"
        assert result.evidence_status == "insufficient"
        assert result.request_status == "unmet"
        assert "não é público" in result.message
        assert result.search_source == "none"
        assert result.native_tool_names == [] and result.provider_id == ""
        assert brain.complete_calls == [] and brain.web_calls == []
        assert controller.web.calls == []  # type: ignore[attr-defined]
        await controller.close()

    asyncio.run(scenario())


def test_forbidden_request_refuses_a_model_web_tool_before_io(tmp_path: Path) -> None:
    brain = _NativeBrain(decision={"kind": "tool", "operation": {
        "name": "web.search", "args": {"query": "python"}}, "finish": False})
    controller = _controller(tmp_path, brain, _web(tmp_path))

    async def scenario() -> None:
        result = await controller.run("Não pesquises na web Python.", automatic=True)
        assert result.status == "FAILED"
        assert result.failure_kind == "AUTHORIZATION_REFUSED"
        assert result.web_outcome == "BLOCKED"
        assert result.search_source == "none"
        assert result.web_preference == "provider"
        assert result.web_fallback == "forbidden"
        assert result.native_tool_names == []
        assert result.web_sources == []
        assert brain.web_calls == []
        assert controller.web.calls == []  # type: ignore[attr-defined]
        assert "excluiu" in result.message
        await controller.close()

    asyncio.run(scenario())


def test_disabled_web_never_executes_a_model_web_tool(tmp_path: Path) -> None:
    brain = _NativeBrain(decision={"kind": "tool", "operation": {
        "name": "web.search", "args": {"query": "python"}}, "finish": False})
    controller = _controller(tmp_path, brain, _web(tmp_path, enabled=False))

    async def scenario() -> None:
        result = await controller.run("Olá, tudo bem?", automatic=True)
        assert result.status == "FAILED"
        assert result.failure_kind == "CONFIGURATION"
        assert result.search_source == "none"
        assert result.web_preference == "provider"
        assert result.web_fallback == ""
        assert result.web_sources == []
        assert brain.web_calls == []
        assert controller.web.calls == []  # type: ignore[attr-defined]
        assert "desligada" in result.message
        await controller.close()

    asyncio.run(scenario())


def test_ordinary_chat_with_provider_preference_never_calls_native_web(tmp_path: Path) -> None:
    brain = _NativeBrain()
    controller = _controller(tmp_path, brain, _web(tmp_path))

    async def scenario() -> None:
        result = await controller.run("Olá, tudo bem?", conversation=True)
        assert result.status == "SUCCESS"
        assert result.search_source == "none"
        assert result.web_preference == "provider"
        assert result.native_tool_names == []
        assert brain.complete_calls and brain.web_calls == []
        await controller.close()

    asyncio.run(scenario())


def test_insufficient_web_evidence_never_becomes_objective_success(tmp_path: Path) -> None:
    source = {
        "id": source_id("https://python.org/downloads/"), "title": "Python",
        "url": "https://python.org/downloads/", "snippet": "excerpt",
        "engines": ["anysearch"], "published_at": None, "date_source": None,
        "retrieved_at": "2026-10-25T10:00:00Z", "read": False,
        "truncated": False, "cached": False,
    }
    result_web = WebResult(
        tool="web.search", sources=[source], documents=[], limited=True,
        evidence_status="insufficient", request_status="unmet")
    decision = {"kind": "tool", "operation": {
        "name": "web.search", "args": {"query": "python documentation"}}, "finish": False}
    answer = {"kind": "answer", "action": None, "status": "FINISH",
              "message": [f"Resposta [ {source['id']} ]."], "memory_changes": []}
    brain = _NativeBrain(replies=[decision, answer])
    web = _FakeWeb(_web(tmp_path, preference="lunmi")._state, result_web)
    controller = _controller(tmp_path, brain, web)

    async def scenario() -> None:
        result = await controller.run("Qual é a documentação oficial do Python?",
                                      conversation=True)
        assert result.status == "NOT_VERIFIED"
        assert result.failure_kind == "VERIFICATION_FAILURE"
        assert result.web_outcome == "NOT_VERIFIED"
        assert result.evidence_status == "insufficient"
        assert result.request_status == "unmet"
        assert result.llm_calls == 2
        assert result.search_source == "lunmi"
        assert result.provider_id == "" and result.native_tool_names == []
        assert web.calls and "oficial" in web.calls[0][1]["query"]
        assert brain.web_calls == []
        await controller.close()

    asyncio.run(scenario())


def test_public_answer_admitting_insufficiency_is_not_success(tmp_path: Path) -> None:
    url = "https://react.dev/blog"
    source = dict(
        id=source_id(url), title="React blog", url=url, snippet="Official React blog",
        engines=["bing"], published_at=None, date_source=None,
        retrieved_at="2026-10-25T10:00:00Z", read=False, truncated=False, cached=False)
    message = (
        f"O blog oficial do React é a fonte principal de atualizações [ {source['id']} ]. "
        "Os excertos disponíveis não contêm detalhes suficientes sobre versões ou "
        "funcionalidades específicas lançadas recentemente, pelo que não é possível "
        "resumir novidades concretas com base apenas nestas fontes.")
    brain = _NativeBrain(replies=[{
        "kind": "answer", "action": None, "status": "FINISH",
        "message": [message], "memory_changes": []}])
    web = _FakeWeb(_web(tmp_path, preference="lunmi")._state,
                   WebResult("web.search", sources=[source]))
    controller = _controller(tmp_path, brain, web)

    async def scenario() -> None:
        result = await controller.run("Pesquisa as novidades do React.", conversation=True)
        assert len(web.calls) == 1 and len(brain.complete_calls) == 1
        assert result.status == "NOT_VERIFIED"
        assert result.evidence_status == "partial"
        assert result.request_status == "partial"
        # A resposta já admite a limitação precisa; não se repete a caveat genérica
        # por cima da mesma admissão. O downgrade de estado mantém-se.
        assert "não é possível resumir novidades concretas" in result.message
        assert "Não foi possível confirmar a resposta" not in result.message
        await controller.close()

    asyncio.run(scenario())


def test_free_search_timeout_still_reports_the_attempted_origin(tmp_path: Path) -> None:
    class _TimeoutWeb(_FakeWeb):
        async def run(self, tool: str, arguments: dict[str, Any], *,
                      mode: str | None = None) -> WebResult:
            raise WebSearchError("timeout")

    web = _TimeoutWeb(_web(tmp_path, preference="lunmi")._state)
    controller = _controller(tmp_path, _NativeBrain(), web)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert result.status == "FAILED"
        assert result.failure_kind == "TIMEOUT"
        assert result.web_outcome == "TIMEOUT"
        assert result.search_source == "lunmi"
        assert result.provider_id == "" and result.native_tool_names == []
        await controller.close()

    asyncio.run(scenario())


def test_no_results_is_not_an_objective_success(tmp_path: Path) -> None:
    web = _FakeWeb(_web(tmp_path, preference="lunmi")._state, WebResult(
        tool="web.search", sources=[], evidence_status="insufficient",
        request_status="unmet"))
    controller = _controller(tmp_path, _NativeBrain(), web)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na web zxqv-nonexistent.", conversation=True)
        assert result.web_outcome == "NO_RESULTS"
        assert result.status == "NOT_VERIFIED"
        assert result.failure_kind == "VERIFICATION_FAILURE"
        assert result.evidence_status == "insufficient"
        assert result.request_status == "unmet"
        assert result.web_sources == []
        assert result.llm_calls == 0
        assert web.calls == [("web.search", {"query": "zxqv-nonexistent"})]
        await controller.close()

    asyncio.run(scenario())


def test_constrained_query_keeps_day_freshness_after_a_rewrite() -> None:
    query = TaskController._constrained_web_query(
        "O que aconteceu hoje na tecnologia?", "technology news")
    policy = constraints(query)
    assert policy.freshness == "day"
    assert policy.parameters()["freshness"] == "day"


def test_constrained_query_keeps_python_official_domains_after_a_rewrite() -> None:
    query = TaskController._constrained_web_query(
        "Compara Python 3.13 e 3.14 usando documentação oficial",
        "compare 3.13 and 3.14")
    policy = constraints(query)
    assert policy.official
    assert policy.domains == ("python.org", "docs.python.org")
    assert policy.accepts({
        "url": "https://docs.python.org/3.14/whatsnew/3.14.html",
        "title": "What's New In Python 3.14", "snippet": "",
    })


def test_native_turn_carries_the_backend_date_without_mocking_the_claim(
        tmp_path: Path) -> None:
    brain = _NativeBrain(native=NativeWebResult(
        text="Resposta nativa com fonte.", provider_id="custom",
        tool_names=("web_search",), sources=(_native_source(),),
        outcome=NativeWebOutcome.SUCCESS))
    controller = _controller(tmp_path, brain, _web(tmp_path))

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert result.status == "SUCCESS"
        system_prompt = brain.web_calls[0][0]["content"]
        today = datetime.now(timezone.utc).date().isoformat()
        assert "Backend current date/time (UTC):" in system_prompt
        assert today in system_prompt
        await controller.close()

    asyncio.run(scenario())


def test_partial_quality_with_pages_read_is_not_verified_not_a_read_failure(
        tmp_path: Path) -> None:
    url = "https://docs.python.org/3.14/whatsnew/3.14.html"
    source = {
        "id": source_id(url), "title": "Python 3.14", "url": url,
        "snippet": "excerpt", "engines": ["anysearch"], "published_at": None,
        "date_source": None, "retrieved_at": "2026-10-25T10:00:00Z", "read": True,
        "truncated": False, "cached": False,
    }
    result_web = WebResult(
        tool="web.search", sources=[source], documents=[{**source, "content": "READ BODY"}],
        warnings=["official pages were not read"], limited=True,
        evidence_status="partial", request_status="partial")
    decision = {"kind": "tool", "operation": {
        "name": "web.search", "args": {"query": "python documentation"}}, "finish": False}
    answer = {"kind": "answer", "action": None, "status": "FINISH",
              "message": [f"Resposta [ {source['id']} ]."], "memory_changes": []}
    brain = _NativeBrain(replies=[decision, answer])
    controller = _controller(tmp_path, brain, _FakeWeb(
        _web(tmp_path, preference="lunmi")._state, result_web))

    async def scenario() -> None:
        result = await controller.run("Qual é a documentação oficial do Python?",
                                      conversation=True)
        assert result.status == "NOT_VERIFIED"
        assert result.evidence_status == "partial"
        assert result.request_status == "partial"
        assert "não está verificado" in result.message
        assert "páginas" not in result.message
        prompt = brain.complete_calls[-1][-1]["content"]
        assert '"evidence_status": "partial"' in prompt
        assert '"request_status": "partial"' in prompt
        assert "PARCIAL" in prompt
        system_prompt = brain.complete_calls[-1][0]["content"]
        assert datetime.now(timezone.utc).date().isoformat() in system_prompt
        await controller.close()

    asyncio.run(scenario())

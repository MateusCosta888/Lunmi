"""Testes offline da Pesquisa Web V1 (contrato `.agents/WEB-SEARCH-V1-CONTRACT.md`).

Cobrem o DTO/lifecycle sem rede: worker falso em memória, Brain guionizado e bridge real
com controller falso. Nenhum teste abre browser, provider, runtime ou socket.
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from app.bridge import protocol
from app.bridge.service import BridgeService, public_task_result_dto
from app.core.automatic import direct_web_operation
from app.core.controller import TaskController, TaskResult
from app.integrations.web_search import WebResult, WebSearchError, WebSearchManager, source_id
from app.integrations.web_state import WebEnvironment, WebPreferences
from app.memory import MemoryManager
from app.settings import Settings
from app.voice.eligibility import speakable_reply


@pytest.mark.parametrize("instruction,expected", [
    ("Pesquisa na web Python asyncio TaskGroup.",
     {"name": "web.search", "args": {"query": "Python asyncio TaskGroup"}, "finish": False}),
    ("Lunmi, pesquisa na internet Python asyncio TaskGroup.",
     {"name": "web.search", "args": {"query": "Python asyncio TaskGroup"}, "finish": False}),
    ("Pesquise no google python asyncio.",
     {"name": "web.search", "args": {"query": "python asyncio"}, "finish": False}),
    ("Lê https://docs.python.org/3/library/asyncio-task.html",
     {"name": "web.open", "args": {
         "url": "https://docs.python.org/3/library/asyncio-task.html"}, "finish": False}),
    ("Abre https://example.com/app",
     {"name": "web.open", "args": {"url": "https://example.com/app"}, "finish": False}),
])
def test_direct_web_operation_builds_a_closed_operation(
        instruction: str, expected: dict[str, Any]) -> None:
    assert direct_web_operation(instruction) == expected


@pytest.mark.parametrize("instruction", [
    "Não pesquises na web Python.",
    "Como pesquisar na web Python?",
    '"Pesquisa na web Python"',
    "Pesquisa no Spotify Taylor Swift",
    "Pesquisa o ficheiro de notas",
    "Preciso de pesquisar na web Python",
    "Pesquisa na web chave api_key=sk-abcdefghijklmnop",
])
def test_direct_web_operation_ignores_negated_quoted_howto_local_or_secret(
        instruction: str) -> None:
    assert direct_web_operation(instruction) is None


def _environment(tmp_path: Path) -> WebEnvironment:
    home = tmp_path / "web-home"
    python = home / "runtime" / "Scripts" / "python.exe"
    worker = home / "web_worker.py"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    worker.write_text("", encoding="utf-8")
    return WebEnvironment(home=home, python=python, worker=worker)


class _FakeClient:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def call(self, tool: str, arguments: dict[str, Any], *, timeout: float) -> Any:
        self.calls.append((tool, dict(arguments)))
        answer = self.answers[tool]
        return answer(arguments) if callable(answer) else answer

    async def close(self) -> None:
        self.started = False


def _search_payload(*, snippet: str = "snippet") -> dict[str, Any]:
    return {
        "engines": ["anysearch"],
        "benched_engines": {"duckduckgo": "timeout", "bing": "blocked"},
        "retrieved_at": "2026-10-24T12:00:00Z",
        "results": [
            {"title": f"Title {index}", "url": f"https://example.test/page{index}",
             "snippet": snippet, "engines": ["anysearch"], "date_source": "none"}
            for index in range(5)
        ],
    }


def _useful_snippets() -> dict[str, Any]:
    return _search_payload(snippet="excerpt " * 12)


def _batch(urls: list[str]) -> dict[str, Any]:
    return {"result": [
        {"url": url, "title": "Read title", "content": "READ BODY " * 20,
         "truncated": False, "published_date": "2026-10-20",
         "retrieved_at": "2026-10-24T12:00:01Z"}
        for url in urls
    ]}


class _ManagerHarness:
    def __init__(self, tmp_path: Path, mode: str = "auto") -> None:
        self.environment = _environment(tmp_path)
        self.emitted: list[dict[str, Any]] = []
        self.client = _FakeClient({
            "search": _search_payload(),
            "fetch_batch": lambda args: _batch(args["urls"]),
            "research": {**_search_payload(), "sources": _search_payload()["results"][:3]},
        })
        self.manager = WebSearchManager(
            emit=self.emitted.append,
            state_root=tmp_path,
            session_id="sess-web",
            environment=self.environment,
            client_factory=lambda _env: self.client,
        )
        preferences = tmp_path / ".local" / "web-search.json"
        preferences.parent.mkdir(parents=True)
        preferences.write_text(json.dumps({"enabled": True, "mode": mode}), encoding="utf-8")
        self.manager._state.set_preferences(WebPreferences(enabled=True, mode=mode))


def test_web_state_payloads_are_closed_and_web_get_never_starts_the_runtime(tmp_path: Path) -> None:
    manager = _ManagerHarness(tmp_path).manager
    assert manager.dto()["session_id"] == "sess-web"
    assert manager.dto()["state"] == "idle"
    assert manager.dto()["available"] is True

    async def scenario() -> None:
        with pytest.raises(WebSearchError) as invalid:
            await manager.configure({"enabled": "yes"})
        assert invalid.value.code == "invalid_payload"
        with pytest.raises(WebSearchError):
            await manager.configure({})
        dtos = [manager.dto()]
        for payload in ({"enabled": False}, {"mode": "quick"}, {"enabled": True, "mode": "deep"}):
            dtos.append(await manager.configure(payload))
        assert [item["revision"] for item in dtos] == sorted(
            item["revision"] for item in dtos)
        assert dtos[-1]["enabled"] is True and dtos[-1]["mode"] == "deep"
        await manager.close()
        reloaded = WebSearchManager(
            emit=lambda _payload: None, state_root=tmp_path, session_id="sess-web-2",
            environment=_environment(tmp_path),
            client_factory=lambda _env: _FakeClient({}),
        )
        snapshot = reloaded.dto()
        assert snapshot["enabled"] is True and snapshot["mode"] == "deep"
        assert snapshot["revision"] == 0
        await reloaded.close()

    asyncio.run(scenario())


def test_quick_and_auto_return_useful_snippets_without_fetching(tmp_path: Path) -> None:
    async def scenario() -> None:
        for mode in ("quick", "auto"):
            harness = _ManagerHarness(tmp_path / mode, mode=mode)
            harness.client.answers["search"] = _useful_snippets()

            def forbidden(_arguments: dict[str, Any]) -> Any:
                raise AssertionError("fetch_batch must not run for sufficient snippets")

            harness.client.answers["fetch_batch"] = forbidden
            result = await harness.manager.run("web.search", {"query": "python asyncio"})
            assert [name for name, _ in harness.client.calls] == ["search"]
            assert result.documents == []
            assert result.limited is False
            assert all(source["read"] is False for source in result.sources)
            assert any("snippets only" in warning for warning in result.warnings)
            await harness.manager.close()

    asyncio.run(scenario())


def test_quick_stops_after_useful_page_and_deep_still_reads_three(tmp_path: Path) -> None:
    quick = _ManagerHarness(tmp_path / "quick", mode="quick")
    deep = _ManagerHarness(tmp_path / "deep", mode="deep")

    async def scenario() -> None:
        result = await quick.manager.run("web.search", {"query": "python asyncio"})
        assert quick.manager.dto()["state"] == "idle"
        assert len(result.sources) == 5
        assert len(result.documents) == 1
        assert result.sources[0]["read"] is True
        assert result.sources[1]["read"] is False  # no obligatory second HTTP read
        assert result.sources[0]["published_at"] == "2026-10-20"
        assert result.sources[0]["date_source"] == "document"
        assert result.sources[2]["published_at"] is None
        assert result.sources[0]["retrieved_at"].endswith("Z")
        assert any("anysearch" in warning for warning in result.warnings)
        assert any("duckduckgo benched" in warning for warning in result.warnings)
        assert [name for name, _ in quick.client.calls] == ["search", "fetch_batch"]
        assert quick.client.calls[-1][1]["urls"] == [
            "https://example.test/page0"]

        deep.client.answers["search"] = _useful_snippets()
        result = await deep.manager.run("web.search", {"query": "python asyncio"})
        assert deep.manager.dto()["state"] == "idle"
        assert len(result.documents) == 3
        assert len(deep.client.calls[-1][1]["urls"]) == 3

        research = _ManagerHarness(tmp_path / "research", mode="quick")
        result = await research.manager.run("web.research", {"query": "python asyncio"})
        assert research.manager.dto()["state"] == "idle"
        assert len(result.sources) == 3
        assert len(result.documents) == 3
        assert len(research.client.calls[-1][1]["urls"]) == 3
        await quick.manager.close()
        await deep.manager.close()
        await research.manager.close()

    asyncio.run(scenario())


def test_manager_success_returns_to_idle_while_failure_keeps_error(tmp_path: Path) -> None:
    harness = _ManagerHarness(tmp_path)

    def fail(_arguments: dict[str, Any]) -> Any:
        raise WebSearchError("failed")

    harness.client.answers["search"] = fail

    async def scenario() -> None:
        with pytest.raises(WebSearchError):
            await harness.manager.run("web.search", {"query": "python asyncio"})
        assert harness.manager.dto()["state"] == "error"
        harness.client.answers["search"] = _search_payload()
        await harness.manager.run("web.search", {"query": "python asyncio"})
        assert harness.manager.dto()["state"] == "idle"
        await harness.manager.close()

    asyncio.run(scenario())


def test_search_without_documents_is_limited_and_keeps_snippets_only(tmp_path: Path) -> None:
    harness = _ManagerHarness(tmp_path)
    harness.client.answers["fetch_batch"] = {"result": [
        {"url": "https://example.test/page0", "error": "blocked"}]}

    async def scenario() -> None:
        result = await harness.manager.run("web.search", {"query": "python asyncio"})
        assert result.documents == []
        assert result.limited is True
        assert result.sources[0]["snippet"] == "snippet"
        assert result.sources[0]["read"] is False

    asyncio.run(scenario())


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        provider="custom", model="fixture", api_key="synthetic",
        base_url="http://127.0.0.1:1/v1", prewarm=False, app_index=False,
        app_search=False, collect_metrics=False, fast_path=False,
        timeout_seconds=5, logs_path=tmp_path / "logs",
        output_path=tmp_path / "output", state_root=tmp_path / "state",
    )


class _ScriptedBrain:
    model = "fixture"
    capabilities = None

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]]) -> Any:
        from app.llm import Completion
        self.calls.append(messages)
        return Completion(json.dumps(self.replies.pop(0)))

    async def close(self) -> None:
        pass


class _FakeWeb:
    enabled = True
    available = True
    mode = "auto"

    def __init__(self, result: WebResult) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.modes: list[str | None] = []

    async def run(self, tool: str, arguments: dict[str, Any], *,
                  mode: str | None = None) -> WebResult:
        self.calls.append((tool, dict(arguments)))
        self.modes.append(mode)
        return self.result


def _source(url: str = "https://example.test/page0") -> dict[str, Any]:
    return {
        "id": source_id(url), "title": "Title", "url": url, "snippet": "snippet",
        "engines": ["anysearch"], "published_at": None, "date_source": None,
        "retrieved_at": "2026-10-24T12:00:00Z", "read": True,
        "truncated": False, "cached": False,
    }


def _controller(tmp_path: Path, replies: list[dict[str, Any]]) -> TaskController:
    controller = TaskController(_settings(tmp_path),
                                memory=MemoryManager(tmp_path / "memory.sqlite3"))
    controller.memory.set_options(enabled=True, episodes=True, history=True, proposals=True)
    controller.brain = _ScriptedBrain(replies)  # type: ignore[assignment]
    return controller


def test_explicit_web_request_uses_one_tool_then_answer_only_and_keeps_the_turn_private(
        tmp_path: Path) -> None:
    # C2: o turno web entra na janela RAM (continuidade) e nunca é persistido/aprendido.
    source = _source()
    web = _FakeWeb(WebResult(tool="web.search", sources=[source], documents=[{
        **source, "content": "READ BODY", "published_at": None,
    }]))
    answer = {"kind": "answer", "action": None, "status": "FINISH",
              "message": [f"Resposta [ {source['id']} ]."], "memory_changes": []}
    controller = _controller(tmp_path, [answer])
    controller.web = web

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert result.status == "SUCCESS"
        assert result.web_outcome == "SUCCESS"
        assert result.plan_source == "web"
        assert result.scope == "conversation"
        assert result.web_sources == [source]
        assert result.llm_calls == 1
        assert web.calls == [("web.search", {"query": "python asyncio"})]
        assert len(controller.brain.calls) == 1  # type: ignore[attr-defined]
        # O ledger/publicação mantém a fonte, mas o ID opaco não entra na RAM nem no chat.
        assert result.message == "Resposta ."
        assert controller.turns.pairs(controller.session_id) == [
            ("Pesquisa na internet sobre python asyncio.", "Resposta .")]
        assert "FONTES EXTERNAS" in controller.brain.calls[-1][-1]["content"]  # type: ignore[attr-defined]
        assert "READ BODY" in controller.brain.calls[-1][-1]["content"]  # type: ignore[attr-defined]
        await controller.close()
        record = json.loads((tmp_path / "logs" / "tasks.jsonl").read_text(
            encoding="utf-8").splitlines()[-1])
        assert record["instruction"] == "[not stored]"
        assert record["web_sources"] == []
        assert record["web_source_count"] == 1
        assert "example.test" not in json.dumps(record)

    asyncio.run(scenario())


def test_snippets_only_synthesis_is_explicitly_told_pages_were_not_read(
        tmp_path: Path) -> None:
    source = {**_source(), "snippet": "excerpt " * 20, "read": False}
    web = _FakeWeb(WebResult(tool="web.search", sources=[source], documents=[]))
    answer = {"kind": "answer", "action": None, "status": "FINISH",
              "message": [f"Resposta limitada [ {source['id']} ]."], "memory_changes": []}
    controller = _controller(tmp_path, [answer])
    controller.web = web

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert result.status == "SUCCESS"
        assert result.web_sources == [source]
        prompt = controller.brain.calls[-1][-1]["content"]  # type: ignore[attr-defined]
        assert '"evidence": "search_excerpts"' in prompt
        assert "EXCERTOS" in prompt and '"read": false' in prompt
        await controller.close()

    asyncio.run(scenario())


def test_explicit_web_request_with_a_denied_url_blocks_before_any_io(
        tmp_path: Path) -> None:
    answer = {"kind": "answer", "action": None, "status": "FINISH",
              "message": ["Resposta sem consultar."], "memory_changes": []}
    web = _FakeWeb(WebResult(tool="web.search"))
    controller = _controller(tmp_path, [answer])
    controller.web = web

    async def scenario() -> None:
        result = await controller.run("Pesquisa na web https://localhost/segredo.",
                                      conversation=True)
        assert result.status == "FAILED"
        assert result.failure_kind == "AUTHORIZATION_REFUSED"
        assert result.web_outcome == "BLOCKED"
        assert "não é público" in result.message
        assert web.calls == []
        assert controller.brain.calls == []  # type: ignore[attr-defined]
        assert controller.turns.pairs(controller.session_id) == []
        await controller.close()

    asyncio.run(scenario())


def test_insufficient_pages_are_not_verified_and_invalid_source_is_rejected(tmp_path: Path) -> None:
    source = _source()
    web = _FakeWeb(WebResult(tool="web.search", sources=[source], documents=[],
                             limited=True))
    answer = {"kind": "answer", "action": None, "status": "FINISH",
              "message": ["Resposta limitada."], "memory_changes": []}
    controller = _controller(tmp_path, [answer])
    controller.web = web

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert result.status == "NOT_VERIFIED"
        assert result.web_outcome == "NOT_VERIFIED"
        assert "excertos" in result.message
        await controller.close()

    asyncio.run(scenario())

    web = _FakeWeb(WebResult(tool="web.search", sources=[source], documents=[{
        **source, "content": "READ BODY"}]))
    answer = {"kind": "answer", "action": None, "status": "FINISH",
              "message": ["Resposta com fonte inventada [src-0000000000000000]."],
              "memory_changes": []}
    controller = _controller(tmp_path / "invented", [answer])
    controller.web = web

    async def scenario_two() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert result.status == "NOT_VERIFIED"
        assert result.web_outcome == "NOT_VERIFIED"
        assert "não está verificado" in result.message
        await controller.close()

    asyncio.run(scenario_two())


def test_web_runtime_missing_fails_honestly_without_calling_the_brain(tmp_path: Path) -> None:
    controller = _controller(tmp_path, [])
    controller.web = None

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert result.status == "FAILED"
        assert result.failure_kind == "CONFIGURATION"
        assert result.web_outcome == "FAILED"
        assert "runtime não está instalado" in result.message
        assert controller.brain.calls == []  # type: ignore[attr-defined]
        await controller.close()

    asyncio.run(scenario())


def test_execute_mode_never_receives_a_web_tool_or_falls_back_to_uia(tmp_path: Path) -> None:
    controller = _controller(tmp_path, [])
    controller.web = _FakeWeb(WebResult(tool="web.search"))

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.")
        assert result.status == "FAILED"
        assert result.failure_kind == "CONFIGURATION"
        assert result.web_outcome == "FAILED"
        assert "Conversar e Automático" in result.message
        assert controller.brain.calls == []  # type: ignore[attr-defined]
        await controller.close()

    asyncio.run(scenario())


def test_web_request_never_falls_back_to_a_desktop_tool(tmp_path: Path) -> None:
    source = _source()
    web = _FakeWeb(WebResult(tool="web.search", sources=[source], documents=[{
        **source, "content": "READ BODY", "published_at": None}]))
    answer = {"kind": "answer", "action": None, "status": "FINISH",
              "message": [f"Resposta [ {source['id']} ]."], "memory_changes": []}
    controller = _controller(tmp_path, [answer])
    controller.web = web

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      automatic=True)
        assert result.status == "SUCCESS"
        assert result.plan_source == "web"
        assert web.calls == [("web.search", {"query": "python asyncio"})]
        await controller.close()

    asyncio.run(scenario())


def test_ambiguous_current_request_can_still_use_the_model_web_decision(tmp_path: Path) -> None:
    source = _source()
    web = _FakeWeb(WebResult(tool="web.search", sources=[source], documents=[{
        **source, "content": "READ BODY", "published_at": None}]))
    decision = {"kind": "tool", "operation": {
        "name": "web.search", "args": {"query": "python asyncio"}}, "finish": False}
    answer = {"kind": "answer", "action": None, "status": "FINISH",
              "message": [f"Resposta [ {source['id']} ]."], "memory_changes": []}
    controller = _controller(tmp_path, [decision, answer])
    controller.web = web

    async def scenario() -> None:
        result = await controller.run("Qual é a versão do Python?", conversation=True)
        assert result.status == "SUCCESS"
        assert result.web_sources == [source]
        assert result.llm_calls == 2
        assert web.calls == [("web.search", {"query": "python asyncio"})]
        await controller.close()

    asyncio.run(scenario())


def test_direct_url_uses_web_open_without_a_model_tool_decision(tmp_path: Path) -> None:
    url = "https://docs.python.org/3/library/asyncio-task.html"
    source = _source(url)
    web = _FakeWeb(WebResult(tool="web.open", sources=[source], documents=[{
        **source, "content": "READ BODY"}]))
    answer = {"kind": "answer", "action": None, "status": "FINISH",
              "message": [f"Resposta [ {source['id']} ]."], "memory_changes": []}
    controller = _controller(tmp_path, [answer])
    controller.web = web

    async def scenario() -> None:
        result = await controller.run(f"Lê {url}", conversation=True)
        assert result.status == "SUCCESS"
        assert result.web_sources == [source]
        assert web.calls == [("web.open", {"url": url})]
        assert len(controller.brain.calls) == 1  # type: ignore[attr-defined]
        await controller.close()

    asyncio.run(scenario())


def test_web_dto_and_chat_sources_are_public_and_complete(tmp_path: Path) -> None:
    source = _source()
    result = TaskResult(task_id="tsk-web", status="SUCCESS", message="Resposta.",
                        scope="conversation", plan_source="web",
                        web_sources=[source], web_outcome="SUCCESS")
    dto = public_task_result_dto(result)
    assert dto["web_sources"] == [source]
    assert dto["web_outcome"] == "SUCCESS"
    assert set(source) == {
        "id", "title", "url", "snippet", "engines", "published_at", "date_source",
        "retrieved_at", "read", "truncated", "cached",
    }


def test_bridge_web_commands_are_closed_and_emit_the_canonical_dto(tmp_path: Path) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []

    class _Controller:
        def __init__(self, settings: Settings, **_kwargs: Any) -> None:
            self.settings = settings
            from app.memory.turns import TurnWindow
            self.session_id = 'web-test-session'
            self.turns = TurnWindow()

        async def close(self) -> None:
            pass

    service = BridgeService(emit=lambda kind, payload: emitted.append((kind, payload)),
                            sandbox_dir=tmp_path, controller_factory=_Controller)
    try:
        with pytest.raises(protocol.ProtocolError) as invalid:
            service.dispatch(protocol.Envelope(1, "1", "web.get", {"extra": True}))
        assert invalid.value.code == "invalid_payload"
        initial = service.dispatch(protocol.Envelope(1, "2", "web.get", {}))
        assert initial["session_id"] == service.session_id
        assert initial["enabled"] is True and initial["mode"] == "auto"
        changed = service.dispatch(protocol.Envelope(
            1, "3", "web.set", {"enabled": False, "mode": "deep"}))
        assert changed["revision"] > initial["revision"]
        assert changed["enabled"] is False and changed["mode"] == "deep"
        assert emitted[-1][0] == "web.changed"
        assert emitted[-1][1] == changed
        with pytest.raises(protocol.ProtocolError) as bad_mode:
            service.dispatch(protocol.Envelope(1, "4", "web.set", {"mode": "fast"}))
        assert bad_mode.value.code == "invalid_payload"
        assert service.capabilities()["web_search"] is True
    finally:
        service.close()


def test_chat_message_publishes_origin_even_when_the_web_turn_has_no_sources(
        tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    finished = threading.Event()

    class _Controller:
        def __init__(self, settings: Settings, **_kwargs: Any) -> None:
            self.settings = settings
            self.active = False

        async def run(self, instruction: str, **_kwargs: Any) -> TaskResult:
            return TaskResult(
                status="NOT_VERIFIED", failure_kind="VERIFICATION_FAILURE",
                message="O cérebro não devolveu fontes.", scope="conversation",
                plan_source="web", web_outcome="BLOCKED", search_source="lunmi",
                evidence_status="insufficient", request_status="unmet")

        async def close(self) -> None:
            pass

    def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))
        if kind == "task.finished":
            finished.set()

    service = BridgeService(emit=emit, sandbox_dir=tmp_path,
                            controller_factory=_Controller)
    try:
        service.dispatch(protocol.Envelope(
            1, "1", "chat.send", {"text": "Pesquisa as novidades do React.",
                                   "mode": "converse"}))
        assert finished.wait(timeout=3.0)
    finally:
        service.close()

    assistant = [payload for kind, payload in events
                 if kind == "chat.message" and payload.get("role") == "assistant"][-1]
    assert assistant["search_source"] == "lunmi"
    assert assistant["evidence_status"] == "insufficient"
    assert assistant["request_status"] == "unmet"
    assert assistant["sources"] == []


def test_web_get_offers_the_native_provider_before_the_brain_is_resolved(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.brain.web import NativeWebCapability
    from app.bridge import service as service_module

    class _ProbeBrain:
        provider_id = "kiro"

        def native_web_capability(self) -> NativeWebCapability:
            return NativeWebCapability.ready()

        async def complete_web(self, messages: list[dict[str, Any]]) -> Any:
            raise AssertionError("O probe não pode executar pesquisa nativa.")

    built: list[object] = []

    def factory(settings: Settings) -> _ProbeBrain:
        built.append(settings)
        return _ProbeBrain()

    monkeypatch.setattr(service_module, "resolve_brain", factory)

    class _Controller:
        def __init__(self, settings: Settings, **_kwargs: Any) -> None:
            self.settings = settings
            self.brain = None

        async def close(self) -> None:
            pass

    emitted: list[tuple[str, dict[str, Any]]] = []
    settings = Settings(
        provider="kiro", model="auto", prewarm=False, app_index=False,
        app_search=False, collect_metrics=False, fast_path=False,
        logs_path=tmp_path / "logs", output_path=tmp_path / "output",
        state_root=tmp_path / "state",
    )
    service = BridgeService(emit=lambda kind, payload: emitted.append((kind, payload)),
                            settings=settings, sandbox_dir=tmp_path,
                            controller_factory=_Controller)
    try:
        # O arranque não paga o probe do CLI: só o primeiro `web.get` o faz.
        assert built == []
        first = service.dispatch(protocol.Envelope(1, "1", "web.get", {}))
        assert first["native_web"]["state"] == "AVAILABLE"
        assert first["native_web"]["supported"] is True
        # WEB-SEARCH-AUTO-V1: o default novo é `auto` (preferência), e a origem efetiva
        # é o provedor quando a capacidade já foi provada — sem escolha explícita.
        assert first["preference"] == "auto"
        assert first["origin"] == "provider"
        assert len(built) == 1

        chosen = service.dispatch(protocol.Envelope(1, "2", "web.set", {
            "brain_id": first["brain_id"], "origin": "provider"}))
        assert chosen["preference"] == "provider"
        assert chosen["origin"] == "provider"

        again = service.dispatch(protocol.Envelope(1, "3", "web.get", {}))
        assert again["native_web"]["state"] == "AVAILABLE"
        assert len(built) == 1  # cache por configuração; nenhum segundo probe
    finally:
        service.close()


def test_voice_speaks_only_a_successful_web_summary_with_sources() -> None:
    source = "[src-0123456789abcdef]"
    assert speakable_reply(
        message=f"Resumo curto {source}.", status="SUCCESS", scope="conversation",
        evidence_count=1, web_outcome="SUCCESS", web_sources=1,
    ) == "Resumo curto ."
    for outcome, sources in (("FAILED", 1), ("NOT_VERIFIED", 1), ("SUCCESS", 0)):
        assert speakable_reply(
            message="Resumo.", status="SUCCESS", scope="conversation",
            evidence_count=1, web_outcome=outcome, web_sources=sources,
        ) == ""

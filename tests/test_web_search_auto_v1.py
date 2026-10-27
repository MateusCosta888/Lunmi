"""Regressões offline de WEB-SEARCH-AUTO-V1 (`.agents/WEB-SEARCH-AUTO-V1-CONTRACT.md`).

Nenhum teste abre rede, provider, runtime, GUI ou browser: Brain e manager são fakes.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.brain.web import (
    WEB_RECOVERY_MIN_REMAINING_S,
    NativeWebCapability,
    NativeWebOutcome,
    NativeWebResult,
    NativeWebSource,
    WebOriginSnapshot,
    should_recover_web,
)
from app.core.controller import TaskController, TaskResult
from app.core.public_text import web_synthesis_text
from app.integrations.web_search import WebResult, source_id
from app.integrations.web_state import (
    WebPreferences,
    WebState,
    identity_for_settings,
)
from app.llm import Completion
from app.memory import MemoryManager
from app.settings import Settings


def _settings(tmp_path: Path, *, timeout: float = 5) -> Settings:
    return Settings(
        provider="custom", model="fixture", api_key="synthetic",
        base_url="http://127.0.0.1:1/v1", prewarm=False, app_index=False,
        app_search=False, collect_metrics=False, fast_path=False,
        timeout_seconds=timeout, logs_path=tmp_path / "logs",
        output_path=tmp_path / "output", state_root=tmp_path / "state",
    )


class _AutoBrain:
    model = "fixture"
    capabilities = None
    provider_id = "custom"

    def __init__(self, *, native: NativeWebResult | None = None,
                 replies: list[dict[str, Any]] | None = None,
                 cleanup: bool = True) -> None:
        self.native = native
        self.replies = list(replies or [])
        self.cleanup = cleanup
        self.web_calls: list[list[dict[str, Any]]] = []
        self.complete_calls: list[list[dict[str, Any]]] = []
        self.cancel_calls = 0
        self.decision = {"kind": "answer", "action": None, "status": "FINISH",
                         "message": ["Olá."], "memory_changes": []}

    def native_web_capability(self) -> NativeWebCapability:
        return NativeWebCapability.ready()

    async def complete_web(self, messages: list[dict[str, Any]]) -> NativeWebResult:
        self.web_calls.append(messages)
        assert self.native is not None
        return self.native

    async def cancel_web(self) -> bool:
        self.cancel_calls += 1
        return self.cleanup

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
        self.modes: list[str | None] = []

    async def run(self, tool: str, arguments: dict[str, Any], *,
                  mode: str | None = None) -> WebResult:
        self.calls.append((tool, dict(arguments)))
        self.modes.append(mode)
        if self.result is None:
            raise AssertionError("O manager não devia ser consultado neste percurso.")
        return self.result


def _web(tmp_path: Path, *, preference: str = "auto", ready: bool = True) -> _FakeWeb:
    settings = _settings(tmp_path)
    identity = identity_for_settings(settings)
    state = WebState(session_id="sess-auto")
    state.set_preferences(WebPreferences(brain_origins={identity.fingerprint: preference}))
    state.bind_brain(identity.fingerprint,
                     NativeWebCapability.ready() if ready else NativeWebCapability.unknown(),
                     available=True)
    return _FakeWeb(state)


def _controller(tmp_path: Path, brain: _AutoBrain, web: _FakeWeb,
                *, timeout: float = 5) -> TaskController:
    controller = TaskController(_settings(tmp_path, timeout=timeout),
                                memory=MemoryManager(tmp_path / "memory.sqlite3"))
    controller.memory.set_options(enabled=True, episodes=True, history=True, proposals=True)
    controller.brain = brain  # type: ignore[assignment]
    controller.web = web  # type: ignore[assignment]
    return controller


def _source(url: str = "https://python.org/asyncio") -> WebResult:
    item = {
        "id": source_id(url), "title": "asyncio", "url": url, "snippet": "excerpt",
        "engines": ["anysearch"], "published_at": None, "date_source": None,
        "retrieved_at": "2026-10-25T10:00:00Z", "read": True,
        "truncated": False, "cached": False,
    }
    return WebResult(tool="web.search", sources=[item],
                     documents=[{**item, "content": "READ BODY"}])


def _answer(reply_id: str = source_id("https://python.org/asyncio")) -> dict[str, Any]:
    return {"kind": "answer", "action": None, "status": "FINISH",
            "message": [f"Resposta Lunmi [ {reply_id} ]."], "memory_changes": []}


def _snapshot(**overrides: Any) -> WebOriginSnapshot:
    base = dict(preference="auto", origin="provider", attempts=("provider", "lunmi"),
                recovery=True, lunmi_available=True)
    base.update(overrides)
    return WebOriginSnapshot(**base)


def test_recovery_requires_a_closed_recoverable_failure_and_useful_time() -> None:
    snapshot = _snapshot()
    assert should_recover_web(snapshot, outcome="FAILED", failure="permission",
                              remaining_s=5) is True
    assert should_recover_web(snapshot, outcome="TIMEOUT", failure="timeout",
                              remaining_s=5) is True
    assert should_recover_web(snapshot, outcome="NO_RESULTS", failure="exhausted",
                              remaining_s=5) is True
    # `NOT_VERIFIED` genérico / ausência de prova não autoriza uma segunda consulta.
    assert should_recover_web(snapshot, outcome="NOT_VERIFIED", failure="",
                              remaining_s=5) is False
    assert should_recover_web(snapshot, outcome="NO_RESULTS", failure="",
                              remaining_s=5) is False
    assert should_recover_web(snapshot, outcome="FAILED", failure="unknown",
                              remaining_s=5) is False
    # Segurança, cancelamento e sucesso nunca recuperam.
    for failure in ("security", "cancelled"):
        assert should_recover_web(snapshot, outcome="FAILED", failure=failure,
                                  remaining_s=5) is False
    assert should_recover_web(snapshot, outcome="SUCCESS", failure="",
                              remaining_s=5) is False
    assert should_recover_web(snapshot, outcome="BLOCKED", failure="security",
                              remaining_s=5) is False
    # Prazo insuficiente e preferência explícita nunca recuperam.
    assert should_recover_web(snapshot, outcome="FAILED", failure="permission",
                              remaining_s=WEB_RECOVERY_MIN_REMAINING_S - 0.1) is False
    explicit = _snapshot(preference="provider", recovery=False, attempts=("provider",))
    assert should_recover_web(explicit, outcome="FAILED", failure="permission",
                              remaining_s=30) is False


def test_auto_tries_the_provider_then_recovers_once_with_free_search(tmp_path: Path) -> None:
    native = NativeWebResult(
        text="O provedor recusou a ferramenta.", provider_id="custom",
        tool_names=(), sources=(), outcome=NativeWebOutcome.FAILED,
        failure="permission")
    brain = _AutoBrain(native=native, replies=[_answer()])
    web = _web(tmp_path, preference="auto")
    web.result = _source()
    controller = _controller(tmp_path, brain, web)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert brain.web_calls and brain.cancel_calls == 1
        assert web.calls == [("web.search", {"query": "python asyncio"})]
        # A profundidade viaja como snapshot da Task, não como estado vivo do manager.
        assert web.modes == ["auto"]
        assert result.web_preference == "auto"
        assert result.search_source == "lunmi"
        assert result.web_fallback == "provider_recovered"
        assert result.fallback_triggered is True
        assert result.provider_outcome == "FAILED"
        assert result.lunmi_outcome == "SUCCESS"
        assert result.web_provider_failure == "permission"
        assert result.status == "SUCCESS"
        # A tentativa anterior não se perde e a janela RAM guarda SÓ a resposta final.
        assert controller.turns.pairs(controller.session_id) == [
            ("Pesquisa na internet sobre python asyncio.",
             result.message)]
        assert "O provedor recusou" not in result.message
        await controller.close()

    asyncio.run(scenario())


def test_auto_never_recovers_from_a_security_refusal(tmp_path: Path) -> None:
    native = NativeWebResult(
        text="URL local recusado.", provider_id="custom", sources=(),
        outcome=NativeWebOutcome.BLOCKED, failure="security")
    brain = _AutoBrain(native=native)
    web = _web(tmp_path, preference="auto")
    controller = _controller(tmp_path, brain, web)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre localhost.", conversation=True)
        assert web.calls == [] and brain.cancel_calls == 0
        assert result.web_fallback == ""
        assert result.fallback_triggered is False
        assert result.provider_outcome == "BLOCKED"
        assert result.lunmi_outcome is None
        assert result.web_provider_failure == "security"
        assert result.failure_kind == "AUTHORIZATION_REFUSED"
        await controller.close()

    asyncio.run(scenario())


def test_cleanup_failure_blocks_the_recovery(tmp_path: Path) -> None:
    native = NativeWebResult(
        text="Falhou.", provider_id="custom", sources=(),
        outcome=NativeWebOutcome.FAILED, failure="permission")
    brain = _AutoBrain(native=native, cleanup=False)
    web = _web(tmp_path, preference="auto")
    web.result = _source()
    controller = _controller(tmp_path, brain, web)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert brain.cancel_calls == 1
        assert web.calls == []  # nenhuma pesquisa em paralelo
        assert result.web_fallback == "cleanup_failed"
        assert result.fallback_triggered is False
        assert result.provider_outcome == "FAILED" and result.lunmi_outcome is None
        await controller.close()

    asyncio.run(scenario())


def test_user_cancellation_never_becomes_a_recovery(tmp_path: Path) -> None:
    class _SlowBrain(_AutoBrain):
        async def complete_web(self, messages: list[dict[str, Any]]) -> NativeWebResult:
            self.web_calls.append(messages)
            await asyncio.sleep(5)
            raise AssertionError("cancelada antes de devolver")

    brain = _SlowBrain()
    web = _web(tmp_path, preference="auto")
    web.result = _source()
    controller = _controller(tmp_path, brain, web, timeout=30)

    async def scenario() -> None:
        task = asyncio.create_task(
            controller.run("Pesquisa na internet sobre python asyncio.", conversation=True))
        await asyncio.sleep(0.05)
        task.cancel()
        result = await task
        assert result.status == "CANCELLED"
        # Cancelamento do utilizador não é timeout observado nem autoriza recuperação.
        assert web.calls == [] and brain.cancel_calls == 0
        assert result.fallback_triggered is False and result.lunmi_outcome is None
        await controller.close()

    asyncio.run(scenario())


def test_explicit_provider_unavailable_never_falls_back_to_free_search(tmp_path: Path) -> None:
    brain = _AutoBrain()
    web = _web(tmp_path, preference="provider", ready=False)
    web.result = _source()
    controller = _controller(tmp_path, brain, web)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert result.status == "FAILED" and result.failure_kind == "CONFIGURATION"
        assert result.web_preference == "provider"
        assert result.web_fallback == "provider_unavailable"
        assert result.search_source == "none"
        assert "indisponível" in result.message
        assert web.calls == [] and brain.web_calls == []
        await controller.close()

    asyncio.run(scenario())


def test_auto_skips_the_provider_when_no_useful_reserve_survives(tmp_path: Path) -> None:
    native = NativeWebResult(text="nunca chamado", provider_id="custom", sources=())
    brain = _AutoBrain(native=native, replies=[_answer()])
    web = _web(tmp_path, preference="auto")
    web.result = _source()
    # 2 s < reserva de 4 s: `auto` escolhe a Lunmi sem gastar a tentativa nativa.
    controller = _controller(tmp_path, brain, web, timeout=2)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert brain.web_calls == [] and brain.cancel_calls == 0
        assert web.calls and result.search_source == "lunmi"
        assert result.provider_outcome is None
        assert result.lunmi_outcome == "SUCCESS"
        await controller.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("raw, expected", [
    # Aspas não escapadas dentro da mensagem: aceite como texto simples, antes falhava.
    ('A função responde: "este caminho existe".', 'A função responde: "este caminho existe".'),
    ('```json\n{"kind":"answer","action":null,"status":"FINISH",'
     '"message":["Ação: «olá» — ok"],"memory_changes":[]}\n```', 'Ação: «olá» — ok'),
    ('{"kind":"answer","action":null,"status":"FINISH","message":["Um"],'
     '"memory_changes":[]}', 'Um'),
    ('{"kind":"tool","operation":{"name":"web.search","args":{}},"finish":false}', None),
    # Um UFO com action preenchida (ou kind/status fora da forma fechada) é recusado.
    ('{"kind":"answer","action":{"name":"filesystem"},"status":"FINISH",'
     '"message":["feito"],"memory_changes":[]}', None),
    # `action` ausente é tratada como null; o que nunca passa é uma ação preenchida.
    ('{"kind":"answer","status":"FINISH","message":["Um"],"memory_changes":[]}', 'Um'),
    ('{"kind":"answer","action":null,"status":"PARTIAL","message":["Um"]}', None),
    # `operation` escondida num envelope answer/action=null/FINISH: recusada tal como
    # `kind=tool`; nada é executado nem convertido em ação.
    ('{"kind":"answer","action":null,"status":"FINISH",'
     '"operation":{"name":"filesystem","args":{"operation":"delete"}},'
     '"message":["Resposta pública."]}', None),
    # O mesmo envelope SEM `operation` continua a ser texto público válido.
    ('{"kind":"answer","action":null,"status":"FINISH",'
     '"message":["Resposta pública."]}', 'Resposta pública.'),
    ('{isto não é json}', None),
    ('   ', None),
])
def test_web_synthesis_text_accepts_plain_text_but_never_a_tool(raw: str,
                                                               expected: str | None) -> None:
    assert web_synthesis_text(raw) == expected


def test_web_synthesis_text_never_truncates_a_long_answer() -> None:
    long_answer = "Explicação detalhada. " * 400  # ~8800 caracteres
    assert web_synthesis_text(long_answer) == long_answer.strip()
    object_answer = {"kind": "answer", "action": None, "status": "FINISH",
                     "message": [long_answer], "memory_changes": []}
    assert web_synthesis_text(json.dumps(object_answer)) == long_answer.strip()


def test_cleanup_time_revalidates_the_recovery_gate(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    import app.core.controller as controller_module

    gate_calls: list[float] = []

    def gate(snapshot: WebOriginSnapshot, *, outcome: str, failure: str,
             remaining_s: float) -> bool:
        gate_calls.append(remaining_s)
        return len(gate_calls) == 1  # antes do stop sim; depois do stop já não

    monkeypatch.setattr(controller_module, "should_recover_web", gate)
    native = NativeWebResult(
        text="Falhou.", provider_id="custom", sources=(),
        outcome=NativeWebOutcome.FAILED, failure="permission")
    brain = _AutoBrain(native=native)
    web = _web(tmp_path, preference="auto")
    web.result = _source()
    controller = _controller(tmp_path, brain, web)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert len(gate_calls) == 2  # o gate é reavaliado DEPOIS do cleanup
        assert brain.cancel_calls == 1
        assert web.calls == []  # sem tempo útil, nenhuma segunda consulta
        assert result.fallback_triggered is False
        assert result.web_fallback == ""
        assert result.provider_outcome == "FAILED" and result.lunmi_outcome is None
        await controller.close()

    asyncio.run(scenario())


def test_constrained_query_preserves_the_calendar_window_and_the_real_project() -> None:
    # TypeScript oficial usa devblogs.microsoft.com: a âncora é TypeScript, não Windows.
    query = TaskController._constrained_web_query(
        "Pesquisa as novidades do TypeScript na documentação oficial", "release notes")
    assert "TypeScript" in query and "Windows" not in query
    # Os 6 meses do calendário sobrevivem à reescrita, sem virarem "semana".
    query = TaskController._constrained_web_query(
        "Pesquisa as mudanças do Django nos últimos 6 meses", "django changes")
    assert "últimos 6 meses" in query
    assert "django" in query.casefold()
    # Python oficial continua a fixar o projeto pelo domínio.
    query = TaskController._constrained_web_query(
        "Pesquisa a documentação oficial de Python", "the docs")
    assert "Python" in query


def test_plain_text_synthesis_with_an_action_claim_is_still_rejected(tmp_path: Path) -> None:
    # A resposta pública alega uma mutação: o caminho read-only recusa-a, mesmo em texto.
    brain = _AutoBrain()
    brain.decision = {"kind": "answer", "action": None, "status": "FINISH",
                      "message": ["Abri o ficheiro e guardei tudo."],
                      "memory_changes": []}
    web = _web(tmp_path, preference="lunmi")
    web.result = _source()
    controller = _controller(tmp_path, brain, web)

    async def scenario() -> None:
        result = await controller.run("Pesquisa na internet sobre python asyncio.",
                                      conversation=True)
        assert result.status == "FAILED"
        assert result.web_outcome == "FAILED"
        await controller.close()

    asyncio.run(scenario())


def test_evidence_selection_uses_the_relevant_tail_within_the_budget(tmp_path: Path) -> None:
    url = "https://docs.python.org/3/library/asyncio-task.html"
    filler = "\n\n".join(f"Parágrafo de navegação {index} sem relação com o pedido."
                         for index in range(400))
    relevant = "TaskGroup é um grupo de tarefas assíncronas do asyncio."
    document = {
        "id": source_id(url), "title": "asyncio", "url": url, "snippet": "excerpt",
        "engines": ["anysearch"], "published_at": None, "date_source": None,
        "retrieved_at": "2026-10-25T10:00:00Z", "read": True,
        "truncated": False, "cached": False,
        "content": filler + "\n\n" + relevant,
    }
    web = _FakeWeb(WebState(session_id="sess-evidence"))
    web.result = WebResult(tool="web.open", sources=[document], documents=[document])
    controller = _controller(tmp_path, _AutoBrain(), web)

    async def scenario() -> None:
        context = controller._web_context(
            "Explica o que é TaskGroup no asyncio.", web.result, TaskResult())
        payload = json.loads(context)
        content = payload["sources"][0]["content"]
        assert relevant in content
        assert len(content) <= 12000
        assert content.count("Parágrafo de navegação") < 400
        await controller.close()

    asyncio.run(scenario())


def test_disabled_state_never_reports_a_provider_origin(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    identity = identity_for_settings(settings)
    state = WebState(session_id="sess-off")
    state.set_preferences(WebPreferences(
        enabled=False, brain_origins={identity.fingerprint: "auto"}))
    state.bind_brain(identity.fingerprint, NativeWebCapability.ready(), available=True)
    dto = state.dto(available=True)
    assert dto["enabled"] is False
    assert dto["preference"] == "auto"
    assert dto["origin"] == "none"
    assert dto["web_fallback"] == ""
    assert state.origin(available=True) == "none"


def _success_native(url: str = "https://python.org/3.15", *, read: bool = True,
                    text: str | None = None) -> NativeWebResult:
    return NativeWebResult(
        text=text or f"A versão estável é 3.15: {url}",
        provider_id="custom", outcome=NativeWebOutcome.SUCCESS,
        tool_names=("web_search",),
        sources=(NativeWebSource(title="Python", url=url, read=read),))


def test_provider_success_citing_unpublished_sources_is_not_verified(tmp_path: Path) -> None:
    native = _success_native(
        text="A release é 3.15: https://python.org/downloads/release/python-3150/",
        url="https://python.org/3.14")
    brain = _AutoBrain(native=native)
    web = _web(tmp_path, preference="auto")
    web.result = _source()
    controller = _controller(tmp_path, brain, web)

    async def scenario() -> None:
        result = await controller.run(
            "Pesquisa na internet a versão estável mais recente do Python e cita a fonte oficial.",
            conversation=True)
        assert result.status == "NOT_VERIFIED"
        assert result.evidence_status == "partial" and result.request_status == "partial"
        assert "não está verificado" in result.message
        assert "https://python.org/downloads/release/python-3150/" in result.message
        assert result.provider_outcome == "NOT_VERIFIED"
        assert result.web_fallback == "" and result.fallback_triggered is False
        assert web.calls == []  # citação fora do ledger não é falha recuperável observada
        await controller.close()

    asyncio.run(scenario())


def test_official_question_without_an_observed_page_read_is_not_met(tmp_path: Path) -> None:
    brain = _AutoBrain(native=_success_native(read=False))
    web = _web(tmp_path, preference="auto")
    web.result = _source()
    controller = _controller(tmp_path, brain, web)

    async def scenario() -> None:
        result = await controller.run(
            "Pesquisa na internet a versão estável mais recente do Python e cita a fonte oficial.",
            conversation=True)
        assert result.status == "NOT_VERIFIED"
        assert result.request_status != "met" and result.evidence_status == "partial"
        assert result.provider_outcome == "NOT_VERIFIED"
        assert web.calls == []  # só excertos: NV honesto, sem fallback inventado
        await controller.close()

    asyncio.run(scenario())


def test_official_question_with_published_urls_and_a_real_read_is_met(tmp_path: Path) -> None:
    brain = _AutoBrain(native=_success_native(read=True))
    web = _web(tmp_path, preference="auto")
    web.result = _source()
    controller = _controller(tmp_path, brain, web)

    async def scenario() -> None:
        result = await controller.run(
            "Pesquisa na internet a versão estável mais recente do Python e cita a fonte oficial.",
            conversation=True)
        assert result.status == "SUCCESS"
        assert (result.evidence_status, result.request_status) == ("suff", "met")
        assert result.provider_outcome == "SUCCESS"
        assert web.calls == []
        await controller.close()

    asyncio.run(scenario())


def test_auto_uses_lunmi_when_the_provider_budget_race_leaves_useful_time(
        tmp_path: Path) -> None:
    from app.integrations.web_state import resolve_task_origin

    brain = _AutoBrain()
    web = _web(tmp_path, preference="auto")
    web.result = _source()
    controller = _controller(tmp_path, brain, web)
    remaining = [4.0, 3.5]

    def deadline() -> float:
        return remaining.pop(0) if remaining else 3.5

    async def scenario() -> None:
        result = TaskResult()
        try:
            result = await controller._run_provider_web_with_recovery(
                "Pesquisa Python asyncio.", [], result,
                resolve_task_origin(web, brain=brain), lambda *args: None,
                deadline, lambda: None)
            assert brain.web_calls == []  # nenhuma tentativa nativa fabricada
            assert [tool for tool, _ in web.calls] == ["web.search"]
            assert result.search_source == "lunmi"
            assert result.provider_outcome is None
            assert result.fallback_triggered is False
            assert result.lunmi_outcome == "SUCCESS"
        finally:
            await controller.close()

    asyncio.run(scenario())


def test_cleanup_failure_keeps_a_useful_unverified_turn_in_ram(tmp_path: Path) -> None:
    native = NativeWebResult(
        text="A pesquisa encontrou apenas excertos.",
        provider_id="custom", tool_names=("web_search",),
        sources=(NativeWebSource(title="Python", url="https://python.org/3.15"),),
        outcome=NativeWebOutcome.NOT_VERIFIED, failure="failed")
    brain = _AutoBrain(native=native, cleanup=False)
    web = _web(tmp_path, preference="auto")
    web.result = _source()
    controller = _controller(tmp_path, brain, web)
    instruction = "Pesquisa na internet sobre python asyncio."

    async def scenario() -> None:
        result = await controller.run(instruction, conversation=True)
        assert result.web_fallback == "cleanup_failed"
        assert web.calls == []  # nenhuma pesquisa em paralelo
        assert controller.turns.pairs(controller.session_id) == [
            (instruction, result.message)]
        await controller.close()

    asyncio.run(scenario())


def test_rewritten_calendar_window_never_replaces_the_users_window() -> None:
    from app.integrations.web_quality import constraints

    cases = [
        ("Pesquisa as atualizações do Django nos últimos seis meses.",
         "Django nos últimos sete dias", "últimos seis meses"),
        ("Pesquisa as atualizações do Django nos últimos 6 meses.",
         "Django last 7 days", "últimos 6 meses"),
    ]
    for instruction, rewritten, phrase in cases:
        query = TaskController._constrained_web_query(instruction, rewritten)
        assert phrase in query
        assert "sete dias" not in query and "7 days" not in query
        original = constraints(instruction)
        effective = constraints(query)
        assert effective.start_date == original.start_date
        assert effective.end_date == original.end_date
    # Sem janela na instrução, a reescrita fica intacta.
    assert TaskController._constrained_web_query(
        "Pesquisa as novidades do Django.", "Django release notes") == "Django release notes"


def test_preferences_migration_keeps_explicit_choices_of_every_brain(tmp_path: Path) -> None:
    from app.integrations.web_state import (load_preferences, preferences_path,
                                            save_preferences)

    path = preferences_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "enabled": True, "mode": "quick",
        "brain_origins": {
            "brain-0000000000000001": "lunmi",
            "brain-0000000000000002": "provider",
            "brain-0000000000000003": "none",
            "brain-0000000000000004": "unknown",
        },
    }), encoding="utf-8")
    loaded = load_preferences(tmp_path)
    assert loaded.brain_origins == {
        "brain-0000000000000001": "lunmi",
        "brain-0000000000000002": "provider",
        "brain-0000000000000003": "none",
    }  # valor desconhecido ignorado, nunca reinterpretado
    # Mudar o Brain atual (ou só enabled/mode) não apaga as escolhas já gravadas.
    save_preferences(tmp_path, WebPreferences(
        enabled=False, mode="deep",
        origin_update=("brain-0000000000000005", "auto")))
    reread = load_preferences(tmp_path)
    assert reread.enabled is False and reread.mode == "deep"
    assert reread.brain_origins == {**loaded.brain_origins,
                                    "brain-0000000000000005": "auto"}
    # Ficheiro sem escolha para o Brain atual fica no default explícito `auto`.
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    state = WebState(session_id="migrate")
    state.set_preferences(load_preferences(fresh))
    state.bind_brain("brain-0000000000000006", NativeWebCapability.ready(), available=True)
    assert state.dto(available=True)["preference"] == "auto"


def test_simple_conversation_gets_the_concision_policy_before_generation(
        tmp_path: Path) -> None:
    brain = _AutoBrain()
    web = _web(tmp_path, preference="auto")
    controller = _controller(tmp_path, brain, web)

    async def scenario() -> None:
        await controller.run("O que é Docker?", conversation=True)
        system = brain.complete_calls[0][0]["content"]
        assert "proportionally" in system
        assert "fixed character cap" in system
        assert web.calls == []
        await controller.close()

    asyncio.run(scenario())


def test_native_prompt_asks_for_a_real_read_without_a_rigid_cap() -> None:
    from app.core.controller import _NATIVE_WEB_PROMPT

    assert "page read/fetch tool" in _NATIVE_WEB_PROMPT
    assert "at least one relevant result" in _NATIVE_WEB_PROMPT
    assert "no fixed character cap" in _NATIVE_WEB_PROMPT
    assert "at most 1200 characters" not in _NATIVE_WEB_PROMPT


def test_generic_web_prompt_only_researches_when_the_user_asks() -> None:
    from app.core.automatic import WEB_PROMPT

    assert "choose web.search" in WEB_PROMPT
    assert "only when the user" in WEB_PROMPT
    assert "prefer web.research" not in WEB_PROMPT

"""P1 provenance regressions.

Two claims that must never merge:

- A verified *read* returns data. File content that says "Já apaguei a pasta" is data,
  not evidence of an action, so only the backend template may be stable/verified speech.
- A verified *operation* returns the agent's summary, not proof. Same rule.

The rest of the text stays visible (delta/completed) but is never spoken as verified.
"""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from app.bridge import protocol
from app.bridge.responses import (CONSULTATION_SPEECH, OPERATION_SPEECH, PublicResponse,
                                  verified_speech_prefix)
from app.bridge.service import BridgeService
from app.core.controller import TaskController, TaskResult
from app.core.public_text import unverified_action_claim
from app.memory import MemoryManager
from app.settings import Settings

SENTINEL = "PRIVATE_ATTACHMENT_SENTINEL"
CLAIM = "Já apaguei a pasta Testes."


def _collect() -> tuple[list[tuple[str, dict[str, Any]]], Any]:
    events: list[tuple[str, dict[str, Any]]] = []
    return events, (lambda kind, data: events.append((kind, data)))


def test_english_action_claim_needs_a_real_sentence_start() -> None:
    # Quoting the phrase is explanation, not a claim: same anchor as the Portuguese list.
    assert not unverified_action_claim('The phrase I have opened Spotify is only an example.')
    assert unverified_action_claim('I have opened Spotify.')
    assert unverified_action_claim('Pronto. I have already opened Spotify.')
    assert unverified_action_claim("I've deleted the folder.")
    assert unverified_action_claim('Já apaguei a pasta.')


def test_read_content_is_data_and_never_verified_speech() -> None:
    async def scenario() -> None:
        events, emit = _collect()
        evidence = [{"operation": "read", "verified": True, "read_only": True}]
        prefix = verified_speech_prefix(evidence, scope="end_to_end", status="SUCCESS")
        assert prefix == CONSULTATION_SPEECH
        stream = PublicResponse(emit, "sess-a", "tsk-a")
        stream.finish(CLAIM, "SUCCESS", verified_result=True, verified_prefix=prefix)
        stable = [data for kind, data in events if kind == "response.text.stable"]
        completed = [data for kind, data in events if kind == "response.completed"]
        assert [data["text"] for data in stable] == [CONSULTATION_SPEECH]
        assert all(data["verified"] is True and data["source"] == "verified_result"
                   for data in stable)
        assert CLAIM not in "".join(data["text"] for data in stable)
        assert completed[0]["text"] == f"{CONSULTATION_SPEECH} {CLAIM}"
        # The content is not lost: it is visible, and the stream is append-only coherent.
        assert "".join(data["text"] for kind, data in events
                       if kind == "response.text.delta") == completed[0]["text"]
        assert completed[0]["text"].endswith(CLAIM)
        assert sum(kind == "response.completed" for kind, _ in events) == 1

    asyncio.run(scenario())


def test_read_only_flag_wins_over_the_tool_name() -> None:
    read = verified_speech_prefix(
        [{"operation": "find_application", "read_only": True, "verified": True}],
        scope="end_to_end", status="SUCCESS")
    assert read == CONSULTATION_SPEECH
    launch = verified_speech_prefix(
        [{"operation": "launch_application", "verified": True}],
        scope="end_to_end", status="SUCCESS")
    assert launch == OPERATION_SPEECH
    mixed = verified_speech_prefix(
        [{"operation": "read", "read_only": True, "verified": True},
         {"operation": "launch", "read_only": False, "verified": True}],
        scope="end_to_end", status="SUCCESS")
    assert mixed == OPERATION_SPEECH
    explicit = verified_speech_prefix(
        [{"operation": "read", "read_only": False, "verified": True}],
        scope="end_to_end", status="SUCCESS")
    assert explicit == OPERATION_SPEECH


def test_unverified_evidence_never_gets_a_template() -> None:
    for evidence in ([], [{"operation": "read", "read_only": True, "verified": False}]):
        assert verified_speech_prefix(evidence, scope="end_to_end", status="SUCCESS") == ""
    assert verified_speech_prefix(
        [{"operation": "read", "read_only": True, "verified": True}],
        scope="conversation", status="SUCCESS") == ""
    assert verified_speech_prefix(
        [{"operation": "read", "read_only": True, "verified": True}],
        scope="end_to_end", status="NOT_VERIFIED") == ""


def test_agent_summary_after_an_operation_is_not_verified_speech() -> None:
    async def scenario() -> None:
        events, emit = _collect()
        prefix = OPERATION_SPEECH
        stream = PublicResponse(emit, "sess-a", "tsk-b")
        stream.finish("Ficheiro criado e verificado.", "SUCCESS",
                      verified_result=True, verified_prefix=prefix)
        stable = [data["text"] for kind, data in events if kind == "response.text.stable"]
        assert stable == [OPERATION_SPEECH]
        assert "Ficheiro criado" not in "".join(stable)
        completed = [data for kind, data in events if kind == "response.completed"][0]
        assert completed["text"] == f"{OPERATION_SPEECH} Ficheiro criado e verificado."

    asyncio.run(scenario())


def test_provenance_is_never_prepended_after_public_text() -> None:
    async def scenario() -> None:
        events, emit = _collect()
        stream = PublicResponse(emit, "sess-a", "tsk-c")
        stream.receive("response.text.delta", {"text": CLAIM})
        stream.flush()
        stream.finish(CLAIM, "SUCCESS", verified_result=True, verified_prefix=OPERATION_SPEECH)
        stable = [data for kind, data in events if kind == "response.text.stable"]
        assert all(data["verified"] is False for data in stable)
        completed = [data for kind, data in events if kind == "response.completed"][0]
        assert completed["text"] == CLAIM
        assert completed["text"].count(OPERATION_SPEECH) == 0

    asyncio.run(scenario())


def test_cancel_and_error_never_complete_with_provenance() -> None:
    async def scenario() -> None:
        events, emit = _collect()
        stream = PublicResponse(emit, "sess-a", "tsk-d")
        stream.receive("response.text.delta", {"text": OPERATION_SPEECH})
        stream.fail("cancelled", cancelled=True)
        stream.finish("Depois.", "SUCCESS", verified_result=True,
                      verified_prefix=OPERATION_SPEECH)
        other = PublicResponse(emit, "sess-a", "tsk-e")
        other.receive("response.text.delta", {"text": "before"})
        other.finish("changed", "SUCCESS", verified_result=True,
                     verified_prefix=OPERATION_SPEECH)
        kinds = [kind for kind, _ in events]
        assert "response.completed" not in kinds
        assert kinds.count("response.cancelled") == 1
        assert kinds.count("response.error") == 1

    asyncio.run(scenario())


class _Engine:
    def __init__(self) -> None:
        self.host_pids: set[int] = set()
        self.started_calls = 0

    def set_host_pids(self, pids: Any) -> None:
        self.host_pids = set(pids)

    async def start(self) -> None:
        self.started_calls += 1

    async def stop(self) -> None:
        pass


class _ResultController:
    result: TaskResult

    def __init__(self, settings: Settings, on_state: Any = None, confirm: Any = None,
                 choose: Any = None, indicate: Any = None, memory: Any = None) -> None:
        self.settings = settings
        self.memory = memory
        self.engine = _Engine()
        self.active = False

    def set_auto_approve(self, enabled: bool) -> None:
        pass

    def cancel(self) -> None:
        pass

    async def run(self, instruction: str, *, conversation: bool = False,
                  automatic: bool = False, task_id: str = "", user_name: str = "",
                  attachments: list[dict[str, Any]] | None = None) -> TaskResult:
        return self.result

    async def reconfigure(self, settings: Settings) -> None:
        self.settings = settings

    async def close(self) -> None:
        if self.memory is not None:
            self.memory.close()


def test_service_marks_a_read_only_result_with_the_consultation_template(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    finished = threading.Event()

    def emit(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))
        if kind == "task.finished":
            finished.set()

    _ResultController.result = TaskResult(
        status="SUCCESS", message=CLAIM, scope="end_to_end", llm_calls=2,
        evidence=[{"operation": "read", "read_only": True, "verified": True,
                   "text": SENTINEL}])
    svc = BridgeService(emit=emit, sandbox_dir=tmp_path,
                        controller_factory=_ResultController)
    try:
        svc.dispatch(protocol.Envelope(v=1, id="auto-1", type="chat.send",
                                       payload={"text": "Lê o ficheiro de notas.",
                                                "mode": "auto"}))
        assert finished.wait(timeout=3.0)
    finally:
        svc.close()

    stable = [data for kind, data in events if kind == "response.text.stable"]
    completed = [data for kind, data in events if kind == "response.completed"]
    assert [data["text"] for data in stable] == [CONSULTATION_SPEECH]
    assert completed[0]["text"] == f"{CONSULTATION_SPEECH} {CLAIM}"
    assert sum(kind == "response.completed" for kind, _ in events) == 1
    assert sum(kind == "task.finished" for kind, _ in events) == 1
    # Automation never emits a legacy assistant bubble that would duplicate the stream.
    assert not [payload for kind, payload in events
                if kind == "chat.message" and payload.get("role") == "assistant"]
    assert CLAIM not in json.dumps([data for kind, data in events
                                    if kind == "response.text.stable"])


class _LogEngine(_Engine):
    def __init__(self) -> None:
        super().__init__()
        self.worker_ready = False
        self.process = None
        self.starts = 0

    async def start(self) -> None:
        self.starts += 1
        self.worker_ready = True

    async def stop(self) -> None:
        self.worker_ready = False

    async def run_task(self, instruction: str, completion: Any, confirm: Any,
                       **kwargs: Any) -> dict[str, Any]:
        callback = kwargs["operation_callback"]
        callback("operation.started", {"operation_id": SENTINEL, "state": "started"})
        callback("operation.finished", {"operation_id": SENTINEL,
                                        "state": "completed_verified", "verified": True})
        return {"status": "SUCCESS", "message": f"Criado: {SENTINEL}", "actions": 1,
                "actions_executed": 1, "steps": 1,
                "evidence": [{"operation": "create_file", "verified": True,
                              "text": SENTINEL}],
                "approvals": [{"reason": SENTINEL, "approved": True}],
                "operation_states": [{"operation_id": SENTINEL,
                                      "state": "completed_verified", "verified": True}]}


class _DecisionBrain:
    async def complete(self, messages: list[dict[str, Any]]) -> Any:
        from app.llm import Completion
        return Completion(json.dumps({"kind": "tool", "operation": {
            "name": "filesystem", "args": {"operation": "create_file",
                                           "path": "result.txt", "text": "x"}},
            "finish": True}), 10, 3)

    async def close(self) -> None:
        pass


def test_attachment_turn_minimises_every_text_field_even_with_opt_in(tmp_path: Path) -> None:
    async def scenario() -> TaskResult:
        settings = Settings(provider="custom", model="fixture", api_key="synthetic",
                            base_url="http://127.0.0.1:1/v1", prewarm=False,
                            app_index=False, app_search=False, collect_metrics=False,
                            logs_path=tmp_path / "logs", state_root=tmp_path / "state",
                            log_instruction=True)
        ctrl = TaskController(settings, memory=MemoryManager(tmp_path / "memory.sqlite3"))
        ctrl.brain = _DecisionBrain()  # type: ignore[assignment]
        ctrl.engine = _LogEngine()  # type: ignore[assignment]
        try:
            return await ctrl.run("Cria o ficheiro com o conteúdo exato do anexo.",
                                  automatic=True,
                                  attachments=[{"name": "source.txt", "text": SENTINEL}])
        finally:
            await ctrl.close()

    result = asyncio.run(scenario())
    log = (tmp_path / "logs" / "tasks.jsonl").read_text(encoding="utf-8")
    assert SENTINEL not in log
    record = json.loads(log.splitlines()[-1])
    assert record["evidence"][0]["operation"] == "create_file"
    assert record["evidence"][0]["verified"] is True
    assert record["approvals"][0]["approved"] is True
    assert record["approvals"][0].get("reason") != SENTINEL
    assert record["operation_states"][0]["state"] == "completed_verified"
    assert record["operation_states"][0]["verified"] is True
    assert "operation_id" not in record["operation_states"][0]
    # The result delivered to the UI is untouched.
    assert result.status == "SUCCESS"
    assert SENTINEL in result.message
    assert result.evidence[0]["text"] == SENTINEL
    assert result.approvals[0]["reason"] == SENTINEL
    assert result.operation_states[0]["operation_id"] == SENTINEL


def test_attachment_turn_minimises_labels_written_by_the_boundary(tmp_path: Path) -> None:
    settings = Settings(logs_path=tmp_path / "logs", state_root=tmp_path / "state",
                        collect_metrics=False, log_instruction=True)
    ctrl = TaskController(settings, memory=MemoryManager(tmp_path / "memory.sqlite3"))
    result = TaskResult(status="SUCCESS", scope="end_to_end", message=f"Resumo: {SENTINEL}",
                        fast_path_label=SENTINEL,
                        evidence=[{"operation": "read", "verified": True,
                                   "path": SENTINEL}],
                        approvals=[{"reason": SENTINEL, "approved": True}],
                        operation_states=[{"operation_id": SENTINEL, "state": SENTINEL,
                                           "verified": True}])
    try:
        ctrl._record(result, "Resume o anexo.", attachment_turn=True)
    finally:
        ctrl.memory.close()
    log = (tmp_path / "logs" / "tasks.jsonl").read_text(encoding="utf-8")
    assert SENTINEL not in log
    record = json.loads(log.splitlines()[-1])
    assert record["fast_path_label"] == "[not stored]"
    assert record["instruction"] == "Resume o anexo."
    assert record["evidence"][0]["operation"] == "read"
    assert record["approvals"][0]["approved"] is True
    assert record["operation_states"][0]["verified"] is True
    assert "state" not in record["operation_states"][0]
    assert "operation_id" not in record["operation_states"][0]

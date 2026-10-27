"""I3: tool Spotify fechada no controller — refs, intenção, prova e one-shot.

Sem Spotify real, rede, provider ou UI. O broker/instalador são duplos; o teste
observa chamadas, mensagens públicas e ausência de URIs no resultado.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.brain.contracts import Capabilities
from app.core.automatic import parse_decision
from app.core.controller import TaskController, TaskResult
from app.integrations.spotify import SpotifyBrokerError
from app.integrations.spotify_host.server import ActionResult
from app.integrations.spotify_install import SpotifyInstallError
from app.llm import Completion
from app.memory import MemoryManager
from app.settings import Settings


def _result(status: str, data: dict[str, Any] | None = None, *, observed: bool = True) -> ActionResult:
    return ActionResult(status=status, data=data,
                        evidence={"observed": observed} if observed else {})


class _FakeBroker:
    def __init__(self, tracks: list[dict[str, str]] | None = None) -> None:
        self.epoch = 4
        self.calls: list[tuple[Any, ...]] = []
        self.tracks = tracks if tracks is not None else [
            {"uri": "spotify:track:aaa", "title": "Alpha", "artist": "A", "album": "One"},
            {"uri": "spotify:track:bbb", "title": "Beta", "artist": "B", "album": "Two"},
        ]
        self.state = {"title": "Alpha", "artist": "A", "album": "One",
                      "is_playing": True, "volume": 0.4}
        self.play_error: Exception | None = None
        self.verify_error: Exception | None = None
        self.grants: list[Any] = []

    def player_state(self) -> ActionResult:
        self.calls.append(("state",))
        return _result("SUCCESS_VERIFIED", dict(self.state))

    def search_tracks(self, query: str, limit: int = 10) -> ActionResult:
        self.calls.append(("search", query, limit))
        return _result("SUCCESS_VERIFIED", {"tracks": [dict(item) for item in self.tracks]})

    def play_uri(self, uri: str) -> ActionResult:
        self.calls.append(("play_uri", uri))
        if self.play_error is not None:
            raise self.play_error
        return _result("SUCCESS_VERIFIED", {**self.state, "title": uri.rsplit(":", 1)[-1]})

    def play(self) -> ActionResult:
        self.calls.append(("play",))
        return _result("SUCCESS_VERIFIED", dict(self.state))

    def pause(self) -> ActionResult:
        self.calls.append(("pause",))
        return _result("SUCCESS_VERIFIED", {**self.state, "is_playing": False})

    def set_volume(self, level: float) -> ActionResult:
        self.calls.append(("volume", level))
        return _result("SUCCESS_VERIFIED", {**self.state, "volume": level})

    def next_track(self) -> ActionResult:
        self.calls.append(("next",))
        return _result("SUCCESS_VERIFIED", dict(self.state))

    def previous_track(self) -> ActionResult:
        self.calls.append(("previous",))
        return _result("SUCCESS_VERIFIED", dict(self.state))

    def grant_local_activation(self, *, request_id: str, task_id: str, epoch: int) -> Any:
        self.calls.append(("grant", request_id, task_id, epoch))

        class Grant:
            consumed = False

            def __init__(self) -> None:
                self.request_id = request_id

            def consume(self) -> bool:
                if self.consumed:
                    return False
                self.consumed = True
                return True

        grant = Grant()
        self.grants.append(grant)
        return grant

    def activate_local(self, grant: Any) -> dict[str, Any]:
        self.calls.append(("activate", grant))
        return {"status": "SUCCESS_VERIFIED", "evidence": {"observed": True,
                                                           "device_scope": "local"}}


class _FakeInstaller:
    def __init__(self, broker: _FakeBroker, *, epoch: int = 4) -> None:
        self.broker = broker
        self.epoch = epoch
        self.job_running = False
        self.prepare: list[bool] = []
        self.verify_error: Exception | None = None
        self.block_until_cancelled = False
        self.cancelled_seen = threading.Event()

    def prepare_music(self, *, open_spotify: bool, timeout: float = 12.0,
                      cancelled: Any = None) -> None:
        self.prepare.append(open_spotify)
        if self.verify_error is not None:
            raise self.verify_error
        if self.block_until_cancelled:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if (cancelled is not None and cancelled.is_set()) or (
                        cancelled is not None and callable(cancelled) and cancelled()):
                    self.cancelled_seen.set()
                    raise SpotifyInstallError("job_cancelled")
                time.sleep(0.01)


def _controller(tmp_path: Path, installer: _FakeInstaller, *,
                confirm: Any = None, choose: Any = None) -> TaskController:
    settings = Settings(provider="openai", model="stub", api_key="sk-test",
                        state_root=tmp_path, logs_path=tmp_path / "logs",
                        output_path=tmp_path / "output", collect_metrics=False)
    memory = MemoryManager(path=tmp_path / "memory.sqlite3")
    controller = TaskController(settings, confirm=confirm, choose=choose, memory=memory)
    controller.spotify = installer
    return controller


def _run(controller: TaskController, args: dict[str, Any], instruction: str,
         *, task_id: str = "tsk-i3") -> TaskResult:
    return asyncio.run(controller._run_spotify_tool(
        {"name": "spotify", "args": args}, instruction, TaskResult(task_id=task_id),
        lambda kind, payload: None))


def test_decision_parser_keeps_the_spotify_tool_envelope_and_rejects_bad_args() -> None:
    answer, candidates, operation = parse_decision(
        '{"kind":"tool","operation":{"name":"spotify",'
        '"args":{"operation":"play","query":"Alpha"}},"finish":true}')
    assert answer is None and candidates == []
    assert operation == {"name": "spotify", "args": {"operation": "play", "query": "Alpha"},
                         "finish": True}


@pytest.mark.parametrize("args", [
    {"operation": "play"},
    {"operation": "play", "query": "A", "ref": "sref-x"},
    {"operation": "volume"},
    {"operation": "state", "query": "A"},
    {"operation": "delete"},
])
def test_decision_parser_rejects_invalid_spotify_shapes(args: dict[str, Any]) -> None:
    from app.brain.contracts import BrainError

    with pytest.raises(BrainError):
        parse_decision('{"kind":"tool","operation":{"name":"spotify","args":'
                       + repr(args).replace("'", '"') + '},"finish":true}')


def test_search_returns_only_opaque_refs_and_never_the_uri(tmp_path: Path) -> None:
    broker = _FakeBroker()
    installer = _FakeInstaller(broker)
    controller = _controller(tmp_path, installer)
    try:
        result = _run(controller, {"operation": "search", "query": "alpha"},
                      "pesquisa alpha no Spotify")
        assert result.status == "SUCCESS"
        assert "spotify:track:aaa" not in result.message
        assert "sref-" in result.message
        assert result.evidence == [{"operation": "spotify.search", "verified": True,
                                    "read_only": True, "accepted": False}]
        assert result.actions == 0
        assert installer.prepare == [True]
        assert broker.calls == [("search", "alpha", 10)]
        refs = list(controller._spotify_refs)
        assert len(refs) == 2
        assert all(uri.startswith("spotify:track:") for uri in
                   [item["uri"] for item in controller._spotify_refs.values()])
    finally:
        asyncio.run(controller.close())


def test_empty_search_is_observed_and_never_proves_absence(tmp_path: Path) -> None:
    controller = _controller(tmp_path, _FakeInstaller(_FakeBroker(tracks=[])))
    try:
        result = _run(controller, {"operation": "search", "query": "nada"},
                      "pesquisa nada no Spotify")
        assert result.status == "SUCCESS"
        assert "Nenhuma faixa" in result.message
        assert controller._spotify_refs == {}
        assert result.evidence == [{"operation": "spotify.search", "verified": True,
                                    "read_only": True, "accepted": False}]
    finally:
        asyncio.run(controller.close())


def test_search_drops_non_track_uris_and_never_stores_them(tmp_path: Path) -> None:
    broker = _FakeBroker(tracks=[
        {"uri": "file:///C:/secrets.txt", "title": "Bad", "artist": "X", "album": "Y"},
        {"uri": "spotify:track:good", "title": "Good", "artist": "X", "album": "Y"}])
    controller = _controller(tmp_path, _FakeInstaller(broker))
    try:
        result = _run(controller, {"operation": "search", "query": "good"},
                      "pesquisa good no Spotify")
        assert result.status == "SUCCESS"
        assert list(controller._spotify_refs.values()) == [
            {"uri": "spotify:track:good", "title": "Good", "artist": "X", "album": "Y"}]
        assert "file:///" not in result.message
    finally:
        asyncio.run(controller.close())


def test_broker_start_epoch_during_readiness_does_not_invalidate_the_fresh_search(
        tmp_path: Path) -> None:
    class _StartingInstaller(_FakeInstaller):
        started = False

        def prepare_music(self, *, open_spotify: bool, timeout: float = 12.0,
                          cancelled: Any = None) -> None:
            super().prepare_music(open_spotify=open_spotify, timeout=timeout, cancelled=cancelled)
            if not self.started:
                self.started = True
                self.broker.epoch += 1

    broker = _FakeBroker()
    controller = _controller(tmp_path, _StartingInstaller(broker))
    try:
        _run(controller, {"operation": "search", "query": "alpha"},
             "pesquisa alpha no Spotify")
        ref = next(iter(controller._spotify_refs))
        result = _run(controller, {"operation": "play", "ref": ref}, "toca esta faixa")
        assert result.status == "SUCCESS"
        assert ("play_uri", "spotify:track:aaa") in broker.calls
    finally:
        asyncio.run(controller.close())


def test_explicit_search_goes_to_the_connector_not_the_ui_fastpath(tmp_path: Path) -> None:
    broker = _FakeBroker()
    controller = _controller(tmp_path, _FakeInstaller(broker))

    class _Brain:
        calls = 0

        async def complete(self, messages: list[dict[str, str]]) -> Completion:
            _Brain.calls += 1
            return Completion(text='{"kind":"tool","operation":{"name":"spotify",'
                                     '"args":{"operation":"search","query":"Alpha"}},'
                                     '"finish":true}', input_tokens=1, output_tokens=1)

        async def close(self) -> None:
            pass

    controller.brain = _Brain()  # type: ignore[assignment]
    try:
        result = asyncio.run(controller.run("pesquise Alpha no Spotify", automatic=True,
                                            task_id="tsk-search"))
        assert result.status == "SUCCESS"
        assert _Brain.calls == 1
        assert broker.calls == [("search", "Alpha", 10)]
        assert "sref-" in result.message
    finally:
        asyncio.run(controller.close())


def test_current_ref_resolves_in_backend_and_a_new_search_invalidates_the_old_one(
        tmp_path: Path) -> None:
    broker = _FakeBroker()
    controller = _controller(tmp_path, _FakeInstaller(broker))
    try:
        first = _run(controller, {"operation": "search", "query": "alpha"},
                     "pesquisa alpha no Spotify")
        assert first.status == "SUCCESS"
        ref = next(iter(controller._spotify_refs))
        played = _run(controller, {"operation": "play", "ref": ref}, "toca esta faixa")
        assert played.status == "SUCCESS"
        assert ("play_uri", "spotify:track:aaa") in broker.calls

        _run(controller, {"operation": "search", "query": "beta"},
             "pesquisa beta no Spotify")
        stale = _run(controller, {"operation": "play", "ref": ref}, "toca esta faixa")
        assert stale.status == "FAILED"
        assert "expir" in stale.message.lower() or "existe" in stale.message.lower()
        assert not any(call[0] == "play_uri" and call[1] == "spotify:track:aaa"
                       for call in broker.calls[2:])

        forged = _run(controller, {"operation": "play", "ref": "sref-forged"},
                      "toca esta faixa")
        assert forged.status == "FAILED"
        assert forged.failure_kind != "VERIFICATION_FAILURE"
    finally:
        asyncio.run(controller.close())


def test_epoch_change_invalidates_refs_without_touching_the_broker(tmp_path: Path) -> None:
    broker = _FakeBroker()
    installer = _FakeInstaller(broker)
    controller = _controller(tmp_path, installer)
    try:
        _run(controller, {"operation": "search", "query": "alpha"}, "pesquisa alpha no Spotify")
        ref = next(iter(controller._spotify_refs))
        installer.epoch = 5
        broker.epoch = 5
        result = _run(controller, {"operation": "play", "ref": ref}, "toca esta faixa")
        assert result.status == "FAILED"
        assert not any(call[0] == "play_uri" for call in broker.calls)
    finally:
        asyncio.run(controller.close())


def test_ambiguous_query_uses_existing_choice_and_never_leaks_uri(tmp_path: Path) -> None:
    broker = _FakeBroker()
    chosen: list[dict[str, Any]] = []

    async def choose(title: str, candidates: list[dict[str, Any]]) -> int | None:
        chosen.extend(candidates)
        return 1

    controller = _controller(tmp_path, _FakeInstaller(broker), choose=choose)
    try:
        result = _run(controller, {"operation": "play", "query": "alpha"}, "toca alpha")
        assert result.status == "SUCCESS"
        assert ("play_uri", "spotify:track:bbb") in broker.calls
        assert [item["name"] for item in chosen] == ["Alpha", "Beta"]
        assert all("uri" not in item for item in chosen)
    finally:
        asyncio.run(controller.close())


def test_ambiguous_query_without_selection_cancels_without_playing(tmp_path: Path) -> None:
    broker = _FakeBroker()

    async def choose(_title: str, _candidates: list[dict[str, Any]]) -> None:
        return None

    controller = _controller(tmp_path, _FakeInstaller(broker), choose=choose)
    try:
        result = _run(controller, {"operation": "play", "query": "alpha"}, "toca alpha")
        assert result.status == "CANCELLED"
        assert not any(call[0] == "play_uri" for call in broker.calls)
    finally:
        asyncio.run(controller.close())


@pytest.mark.parametrize("instruction", [
    "explica como tocar música no Spotify",
    "não toques nada agora",
    "qual é a música que está a tocar?",
])
def test_effect_requires_current_human_intent_not_explanation_or_negation(
        tmp_path: Path, instruction: str) -> None:
    broker = _FakeBroker()
    installer = _FakeInstaller(broker)
    controller = _controller(tmp_path, installer)
    try:
        for args in ({"operation": "play", "query": "alpha"},
                     {"operation": "pause"},
                     {"operation": "volume", "level": 0.1},
                     {"operation": "next"},
                     {"operation": "previous"}):
            result = _run(controller, args, instruction)
            assert result.status == "FAILED"
            assert result.failure_kind == "AUTHORIZATION_REFUSED"
        assert broker.calls == []
        assert installer.prepare == []
    finally:
        asyncio.run(controller.close())


def test_state_is_read_only_and_never_opens_or_changes_playback(tmp_path: Path) -> None:
    broker = _FakeBroker()
    installer = _FakeInstaller(broker)
    controller = _controller(tmp_path, installer)
    try:
        result = _run(controller, {"operation": "state"}, "como está a música?")
        assert result.status == "SUCCESS"
        assert result.actions == 0
        assert result.evidence == [{"operation": "spotify.state", "verified": True,
                                    "read_only": True, "accepted": False}]
        assert installer.prepare == [False]
        assert broker.calls == [("state",)]
    finally:
        asyncio.run(controller.close())


def test_setup_busy_is_honest_and_never_reaches_music(tmp_path: Path) -> None:
    broker = _FakeBroker()
    installer = _FakeInstaller(broker)
    installer.job_running = True
    controller = _controller(tmp_path, installer)
    try:
        result = _run(controller, {"operation": "play", "query": "alpha"}, "toca alpha")
        assert result.status == "FAILED"
        assert result.failure_kind == "CONFIGURATION"
        assert broker.calls == []
        assert installer.prepare == []
    finally:
        asyncio.run(controller.close())


def test_ack_without_observed_proof_is_not_success(tmp_path: Path) -> None:
    broker = _FakeBroker()
    broker.search_tracks = lambda query, limit=10: _result(  # type: ignore[method-assign]
        "SUCCESS", {"tracks": broker.tracks}, observed=False)
    controller = _controller(tmp_path, _FakeInstaller(broker))
    try:
        result = _run(controller, {"operation": "search", "query": "alpha"},
                      "pesquisa alpha no Spotify")
        assert result.status == "NOT_VERIFIED"
        assert result.failure_kind == "VERIFICATION_FAILURE"
        assert controller._spotify_refs == {}
    finally:
        asyncio.run(controller.close())


def test_timeout_is_uncertain_and_is_never_replayed(tmp_path: Path) -> None:
    broker = _FakeBroker(tracks=[
        {"uri": "spotify:track:aaa", "title": "Alpha", "artist": "A", "album": "One"}])
    broker.play_error = SpotifyBrokerError("interrupted")
    controller = _controller(tmp_path, _FakeInstaller(broker))
    try:
        result = _run(controller, {"operation": "play", "query": "alpha"}, "toca alpha")
        assert result.status == "NOT_VERIFIED"
        assert result.failure_kind == "VERIFICATION_FAILURE"
        assert [call for call in broker.calls if call[0] == "play_uri"] == [
            ("play_uri", "spotify:track:aaa")]
    finally:
        asyncio.run(controller.close())


def test_cancel_during_readiness_prevents_any_later_effect(tmp_path: Path) -> None:
    broker = _FakeBroker()
    installer = _FakeInstaller(broker)
    installer.block_until_cancelled = True
    controller = _controller(tmp_path, installer)

    async def scenario() -> TaskResult:
        task = asyncio.create_task(controller._run_spotify_tool(
            {"name": "spotify", "args": {"operation": "play", "query": "alpha"}},
            "toca alpha", TaskResult(task_id="tsk-cancel"), lambda kind, payload: None))
        await asyncio.sleep(0.05)
        task.cancel()
        return await task

    try:
        result = asyncio.run(scenario())
        assert result.status == "CANCELLED"
        assert installer.cancelled_seen.wait(1.0)
        assert broker.calls == []
    finally:
        asyncio.run(controller.close())


def test_activation_requires_manual_confirmation_and_consumes_one_grant(
        tmp_path: Path) -> None:
    broker = _FakeBroker()
    prompts: list[str] = []

    async def confirm(message: str) -> bool:
        prompts.append(message)
        return True

    controller = _controller(tmp_path, _FakeInstaller(broker), confirm=confirm)
    try:
        result = _run(controller, {"operation": "activate_local"},
                      "ativa este computador para o Spotify")
        assert result.status == "SUCCESS"
        assert prompts and "transferir" in prompts[0]
        grant_calls = [call for call in broker.calls if call[0] == "grant"]
        activate_calls = [call for call in broker.calls if call[0] == "activate"]
        assert len(grant_calls) == 1 and len(activate_calls) == 1
        assert broker.grants[0].consumed is True
        assert broker.grants[0].request_id  # type: ignore[attr-defined]
    finally:
        asyncio.run(controller.close())


def test_declined_activation_never_emits_a_grant(tmp_path: Path) -> None:
    broker = _FakeBroker()

    async def confirm(_message: str) -> bool:
        return False

    controller = _controller(tmp_path, _FakeInstaller(broker), confirm=confirm)
    try:
        result = _run(controller, {"operation": "activate_local"},
                      "ativa este computador para o Spotify")
        assert result.status == "CANCELLED"
        assert not any(call[0] == "grant" for call in broker.calls)
        assert not any(call[0] == "activate" for call in broker.calls)
    finally:
        asyncio.run(controller.close())


def test_missing_manual_confirmation_blocks_activation(tmp_path: Path) -> None:
    broker = _FakeBroker()
    controller = _controller(tmp_path, _FakeInstaller(broker), confirm=None)
    try:
        result = _run(controller, {"operation": "activate_local"},
                      "ativa este computador para o Spotify")
        assert result.status == "NOT_VERIFIED"
        assert not any(call[0] == "grant" for call in broker.calls)
    finally:
        asyncio.run(controller.close())


def test_remote_unknown_is_reported_without_ui_fallback(tmp_path: Path) -> None:
    broker = _FakeBroker()
    installer = _FakeInstaller(broker)
    installer.verify_error = SpotifyInstallError("not_ready")
    controller = _controller(tmp_path, installer)
    try:
        result = _run(controller, {"operation": "play", "query": "alpha"}, "toca alpha")
        assert result.status == "NOT_VERIFIED"
        assert result.failure_kind == "VERIFICATION_FAILURE"
        assert "repet" in result.message
        assert broker.calls == []
    finally:
        asyncio.run(controller.close())


class _Brain:
    capabilities = Capabilities.of()

    def __init__(self, args: dict[str, Any]) -> None:
        self.args = args

    async def complete(self, messages: list[dict[str, str]]) -> Completion:
        return Completion(json.dumps({"kind": "tool", "operation": {
            "name": "spotify", "args": self.args}, "finish": True}))

    async def close(self) -> None:
        pass


def test_expired_search_is_private_even_when_logging_instructions(tmp_path: Path) -> None:
    broker = _FakeBroker()
    controller = _controller(tmp_path, _FakeInstaller(broker))
    controller.settings = replace(controller.settings, provider="custom",
                                  base_url="http://127.0.0.1:1/v1", prewarm=False,
                                  app_index=False, app_search=False, fast_path=False,
                                  log_instruction=True, timeout_seconds=1)
    controller.brain = _Brain({"operation": "search", "query": "private_deadline_sentinel"})
    original = broker.search_tracks

    def slow_search(query: str, limit: int = 10) -> ActionResult:
        time.sleep(1.15)
        return original(query, limit)

    broker.search_tracks = slow_search
    episodes: list[TaskResult] = []
    controller.memory.commit_episode = lambda result: episodes.append(result)
    try:
        result = asyncio.run(controller.run("Pesquisa private_deadline_sentinel no Spotify",
                                            automatic=True))
        assert result.status != "SUCCESS"
        assert result.failure_kind == "TIMEOUT"
        assert episodes == []
        log = (controller.settings.logs_path / "tasks.jsonl").read_text(encoding="utf-8")
        assert "private_deadline_sentinel" not in log
    finally:
        asyncio.run(controller.close())

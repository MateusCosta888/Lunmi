"""Providers: chave rascunho, cache por identidade e catálogo Gemini nativo.

Sem rede real e sem credenciais: todas as chaves são sintéticas e o transporte é
MockTransport. O foco é o caminho que o renderer usa (`brain.list`/`brain.verify`)
e o contrato oficial Gemini (catálogo nativo + inferência OpenAI-compatible).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.brain import discovery as discovery_module
from app.brain.api_brain import ApiBrain
from app.brain.contracts import BrainError, Capability, EventKind, ModelInfo
from app.brain.discovery import ModelDiscovery
from app.bridge import protocol
from app.bridge import service as service_module
from app.bridge.service import BridgeService
from app.settings import Settings

DRAFT = "AIza-SYNTHETIC-DRAFT-ONLY"
OTHER = "AIza-SYNTHETIC-OTHER"
ENV_KEY = "AIza-SYNTHETIC-ENV"
BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


class _Controller:
    def __init__(self, settings: Settings, **_kwargs: object) -> None:
        self.settings = settings

    def set_auto_approve(self, enabled: bool) -> None:
        pass

    def cancel(self) -> None:
        pass

    async def close(self) -> None:
        pass


class _RecordingBrain:
    def __init__(self, settings: Settings, models_by_key: dict[str, str] | None = None) -> None:
        self.settings = settings
        self._models_by_key = models_by_key or {}
        self.calls = 0

    async def models(self) -> tuple[ModelInfo, ...]:
        self.calls += 1
        identifier = self._models_by_key.get(self.settings.api_key, "gemini-3.8-flash")
        return (ModelInfo(identifier, identifier, default=True),)

    async def close(self) -> None:
        pass


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "provider": "openai", "model": "saved-model", "api_key": "sk-saved-only",
        "base_url": "", "prewarm": False, "app_index": False, "app_search": False,
        "collect_metrics": False, "logs_path": tmp_path, "state_root": tmp_path,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _service(tmp_path: Path, settings: Settings) -> BridgeService:
    return BridgeService(
        emit=lambda *_: None, sandbox_dir=tmp_path, settings=settings,
        controller_factory=_Controller, discovery=ModelDiscovery(),
    )


def test_brain_list_requires_and_accepts_the_draft_key(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    seen: list[Settings] = []

    def factory(target: Settings) -> _RecordingBrain:
        seen.append(target)
        return _RecordingBrain(target)

    monkeypatch.setattr(discovery_module, "resolve", factory)
    svc = _service(tmp_path, _settings(tmp_path))
    try:
        refused = svc.dispatch(protocol.Envelope(
            v=1, id="no-key", type="brain.list",
            payload={"provider": "gemini", "role": "main", "force": True, "base_url": BASE},
        ))
        assert refused["ok"] is False and "chave" in refused["detail"]
        assert seen == []

        listed = svc.dispatch(protocol.Envelope(
            v=1, id="draft", type="brain.list",
            payload={"provider": "gemini", "role": "main", "force": True,
                     "base_url": BASE, "api_key": DRAFT},
        ))
        assert listed["ok"] is True
        assert seen[0].api_key == DRAFT
        assert seen[0].model == ""  # never inherit the other provider's model
        assert DRAFT not in json.dumps(listed)
    finally:
        svc.close()


def test_catalogue_cache_is_isolated_by_key_and_endpoint(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    calls: list[Settings] = []

    def factory(target: Settings) -> _RecordingBrain:
        calls.append(target)
        return _RecordingBrain(target, {DRAFT: "model-a", OTHER: "model-b"})

    monkeypatch.setattr(discovery_module, "resolve", factory)
    svc = _service(tmp_path, _settings(tmp_path))
    try:
        first = svc.dispatch(protocol.Envelope(
            v=1, id="a", type="brain.list",
            payload={"provider": "gemini", "role": "main", "force": True,
                     "base_url": BASE, "api_key": DRAFT},
        ))
        second = svc.dispatch(protocol.Envelope(
            v=1, id="b", type="brain.list",
            payload={"provider": "gemini", "role": "main", "force": False,
                     "base_url": BASE, "api_key": OTHER},
        ))
        third = svc.dispatch(protocol.Envelope(
            v=1, id="b2", type="brain.list",
            payload={"provider": "gemini", "role": "main", "force": False,
                     "base_url": BASE, "api_key": OTHER},
        ))
        assert first["models"][0]["id"] == "model-a"
        assert second["models"][0]["id"] == "model-b" and second["cached"] is False
        assert third["cached"] is True
        assert len(calls) == 2  # a third key/identity must not reuse the old verdict
    finally:
        svc.close()


def test_app_list_with_another_provider_uses_the_canonical_env_key(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", ENV_KEY)
    seen: list[Settings] = []

    def factory(target: Settings) -> _RecordingBrain:
        seen.append(target)
        return _RecordingBrain(target)

    monkeypatch.setattr(discovery_module, "resolve", factory)
    svc = _service(tmp_path, _settings(tmp_path))
    try:
        result = svc.dispatch(protocol.Envelope(
            v=1, id="app", type="brain.list",
            payload={"provider": "gemini", "role": "app", "force": True,
                     "base_url": BASE, "api_key": DRAFT},
        ))
        assert result["ok"] is True
        assert seen[0].provider == "gemini"
        assert seen[0].api_key == ENV_KEY
        assert seen[0].api_key != DRAFT
    finally:
        svc.close()


def _native_catalog(*, status: int = 200, pages: list[dict] | None = None,
                    calls: list[httpx.Request] | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"status": "INVALID_ARGUMENT"}})
        index = int(request.url.params.get("pageToken", "0") or "0")
        payload = (pages or [{"models": []}])[index]
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _gemini_models_page() -> dict:
    return {"models": [
        {"name": "models/gemini-3.8-flash", "baseModelId": "gemini-3.8-flash",
         "displayName": "Gemini 3.8 Flash",
         "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004",
         "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-2.5-flash-image",
         "baseModelId": "gemini-2.5-flash-image",
         "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash-preview-tts",
         "baseModelId": "gemini-2.5-flash-preview-tts",
         "supportedGenerationMethods": ["generateContent"]},
    ]}


def test_gemini_native_catalogue_normalizes_filters_and_paginates() -> None:
    calls: list[httpx.Request] = []
    transport = _native_catalog(pages=[
        {**_gemini_models_page(), "nextPageToken": "1"},
        {"models": [
            {"name": "models/gemini-3.8-pro", "baseModelId": "gemini-3.8-pro",
             "supportedGenerationMethods": ["generateContent"]},
            # Duas versões partilham o mesmo baseModelId: os dois ids devem sobreviver.
            {"name": "models/gemini-x-001", "baseModelId": "gemini-x",
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-x-002", "baseModelId": "gemini-x",
             "supportedGenerationMethods": ["generateContent"]},
        ]},
    ], calls=calls)
    brain = ApiBrain(Settings(provider="gemini", model="", api_key=DRAFT))
    brain._provider.client = httpx.AsyncClient(transport=transport)
    try:
        models = asyncio.run(brain.models())
    finally:
        asyncio.run(brain.close())
    assert [item.id for item in models] == [
        "gemini-3.8-flash", "gemini-3.8-pro", "gemini-x-001", "gemini-x-002"]
    assert models[0].default is True and not any(item.default for item in models[1:])
    assert len(calls) == 2
    assert all(request.url.path == "/v1beta/models" for request in calls)
    assert all(request.url.params.get("pageSize") == "1000" for request in calls)
    assert all(request.headers["x-goog-api-key"] == DRAFT for request in calls)
    assert all(DRAFT not in str(request.url) for request in calls)
    assert calls[1].url.params.get("pageToken") == "1"


def test_gemini_inference_gets_the_normalized_id() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    brain = ApiBrain(Settings(provider="gemini", model="models/gemini-3.8-flash",
                              api_key=DRAFT))
    brain._provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        asyncio.run(brain.complete([{"role": "user", "content": "oi"}]))
    finally:
        asyncio.run(brain.close())
    assert bodies[0]["model"] == "gemini-3.8-flash"


def test_custom_gemini_endpoint_keeps_openai_catalogue_and_bearer() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [{"id": "custom-chat"}]})

    brain = ApiBrain(Settings(provider="gemini", model="custom-chat",
                              api_key=DRAFT, base_url="https://custom.example/v1"))
    brain._provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        models = asyncio.run(brain.models())
    finally:
        asyncio.run(brain.close())
    assert [item.id for item in models] == ["custom-chat"]
    assert calls[0].url.path == "/v1/models"
    assert calls[0].headers["Authorization"] == f"Bearer {DRAFT}"
    assert "x-goog-api-key" not in calls[0].headers
    assert "custom.example" in str(calls[0].url)
    assert brain.capabilities.supports(Capability.STREAMING) is False


def test_google_host_with_another_path_is_not_the_official_endpoint() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [{"id": "custom-chat"}]})

    brain = ApiBrain(Settings(
        provider="gemini", model="custom-chat", api_key=DRAFT,
        base_url="https://generativelanguage.googleapis.com/v1beta/custom",
    ))
    brain._provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        models = asyncio.run(brain.models())
    finally:
        asyncio.run(brain.close())
    assert [item.id for item in models] == ["custom-chat"]
    assert brain.capabilities.supports(Capability.STREAMING) is False
    assert calls[0].url.path == "/v1beta/custom/models"
    assert calls[0].headers["Authorization"] == f"Bearer {DRAFT}"
    assert "x-goog-api-key" not in calls[0].headers


def _sse_transport(chunks: list[bytes], calls: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=b"".join(chunks))
    return httpx.MockTransport(handler)


def test_official_gemini_streams_delta_before_completion() -> None:
    calls: list[httpx.Request] = []
    chunks = [
        b'data: {"choices":[{"index":0,"delta":{"content":"Ol\xc3\xa1"}}]}\n\n',
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n',
        b"data: [DONE]\n\n",
    ]
    brain = ApiBrain(Settings(provider="gemini", model="gemini-3.8-flash", api_key=DRAFT))
    brain._provider.client = httpx.AsyncClient(transport=_sse_transport(chunks, calls))

    async def scenario():
        events = []
        async for event in brain.stream([{"role": "user", "content": "oi"}]):
            events.append(event)
        return events

    try:
        events = asyncio.run(scenario())
    finally:
        asyncio.run(brain.close())
    assert brain.capabilities.supports(Capability.STREAMING) is True
    kinds = [event.kind for event in events]
    assert kinds[0] is EventKind.STARTED and kinds[1] is EventKind.TEXT_DELTA
    assert kinds.index(EventKind.TEXT_DELTA) < kinds.index(EventKind.TEXT_COMPLETED)
    assert kinds.index(EventKind.TEXT_COMPLETED) < kinds.index(EventKind.COMPLETED)
    assert events[1].data["text"] == "Olá"
    assert not any(event.kind is EventKind.REASONING for event in events)
    assert json.loads(calls[0].content)["stream"] is True
    assert calls[0].headers["Authorization"] == f"Bearer {DRAFT}"


def test_official_gemini_stream_error_never_completes() -> None:
    calls: list[httpx.Request] = []
    chunks = [
        b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n',
        b'data: {"error":{"message":"interrupted"}}\n\n',
    ]
    brain = ApiBrain(Settings(provider="gemini", model="gemini-3.8-flash", api_key=DRAFT))
    brain._provider.client = httpx.AsyncClient(transport=_sse_transport(chunks, calls))

    async def scenario():
        events = []
        with pytest.raises(BrainError):
            async for event in brain.stream([{"role": "user", "content": "oi"}]):
                events.append(event)
        return events

    try:
        events = asyncio.run(scenario())
    finally:
        asyncio.run(brain.close())
    assert [event.kind for event in events] == [EventKind.STARTED, EventKind.TEXT_DELTA]
    assert not any(event.kind is EventKind.COMPLETED for event in events)


def test_verify_reuses_the_fresh_gemini_listing_single_get(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []
    brain = ApiBrain(Settings(provider="gemini", model="gemini-3.8-flash", api_key=DRAFT))
    brain._provider.client = httpx.AsyncClient(transport=_native_catalog(
        pages=[_gemini_models_page()], calls=calls))
    monkeypatch.setattr(service_module, "resolve_brain", lambda _settings: brain)
    svc = _service(tmp_path, _settings(tmp_path))
    try:
        result = svc.dispatch(protocol.Envelope(
            v=1, id="verify", type="brain.verify",
            payload={"provider": "gemini", "model": "gemini-3.8-flash",
                     "base_url": BASE, "api_key": DRAFT},
        ))
    finally:
        svc.close()
    assert result["ok"] is True
    assert [item["id"] for item in result["listing"]["models"]] == ["gemini-3.8-flash"]
    assert len(calls) == 1


def test_verify_empty_gemini_catalogue_is_not_ready_and_single_get(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []
    brain = ApiBrain(Settings(provider="gemini", model="gemini-qa", api_key=DRAFT))
    brain._provider.client = httpx.AsyncClient(transport=_native_catalog(
        pages=[{"models": []}], calls=calls))
    monkeypatch.setattr(service_module, "resolve_brain", lambda _settings: brain)
    svc = _service(tmp_path, _settings(tmp_path))
    try:
        result = svc.dispatch(protocol.Envelope(
            v=1, id="verify-empty", type="brain.verify",
            payload={"provider": "gemini", "model": "gemini-qa",
                     "base_url": BASE, "api_key": DRAFT},
        ))
    finally:
        svc.close()
    assert result["ok"] is False
    assert result["listing"]["ok"] is False
    assert result["listing"]["status"] == "empty"
    assert result["listing"]["models"] == []
    assert len(calls) == 1


@pytest.mark.parametrize("http_status,expected_status", [(404, "unavailable"), (500, "failed")])
def test_verify_gemini_catalogue_http_error_is_not_ready_single_get(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        http_status: int, expected_status: str) -> None:
    calls: list[httpx.Request] = []
    brain = ApiBrain(Settings(provider="gemini", model="gemini-qa", api_key=DRAFT))
    brain._provider.client = httpx.AsyncClient(transport=_native_catalog(
        status=http_status, calls=calls))
    monkeypatch.setattr(service_module, "resolve_brain", lambda _settings: brain)
    svc = _service(tmp_path, _settings(tmp_path))
    try:
        result = svc.dispatch(protocol.Envelope(
            v=1, id="verify-http", type="brain.verify",
            payload={"provider": "gemini", "model": "gemini-qa",
                     "base_url": BASE, "api_key": DRAFT},
        ))
    finally:
        svc.close()
    assert result["ok"] is False
    assert result["listing"]["ok"] is False
    assert result["listing"]["status"] == expected_status
    assert len(calls) == 1


def test_verify_invalid_key_is_not_success_and_not_refetched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []
    brain = ApiBrain(Settings(provider="gemini", model="gemini-3.8-flash", api_key=DRAFT))
    brain._provider.client = httpx.AsyncClient(transport=_native_catalog(
        status=400, calls=calls))
    monkeypatch.setattr(service_module, "resolve_brain", lambda _settings: brain)
    svc = _service(tmp_path, _settings(tmp_path))
    try:
        result = svc.dispatch(protocol.Envelope(
            v=1, id="verify-bad", type="brain.verify",
            payload={"provider": "gemini", "model": "gemini-3.8-flash",
                     "base_url": BASE, "api_key": DRAFT},
        ))
    finally:
        svc.close()
    assert result["ok"] is False
    assert result["listing"]["ok"] is False
    assert result["listing"]["status"] == "auth_required"
    assert len(calls) == 1

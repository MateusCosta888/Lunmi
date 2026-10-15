"""A2/A3/A4: one discovery contract, cached, with stated reasons and no invention."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.brain import discovery as discovery_module
from app.brain.cli.headless import HeadlessCliBrain
from app.brain.cli.catalog import find_spec
from app.brain.contracts import BrainError, ErrorKind, HealthStatus, ModelInfo
from app.brain.discovery import DiscoveryStatus, ModelDiscovery, ModelListing, settings_for
from app.settings import Settings


class _StubBrain:
    """A provider whose listing is canned, counting how often it is asked."""

    def __init__(self, models: list[ModelInfo] | None = None, error: BrainError | None = None) -> None:
        self.provider_id = "stub"
        self._models = models or []
        self._error = error
        self.calls = 0
        self.closed = False

    async def models(self) -> list[ModelInfo]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._models

    async def close(self) -> None:
        self.closed = True


def _list(monkeypatch: pytest.MonkeyPatch, brain: _StubBrain, provider: str = "agy", *,
          force: bool = False, cache: ModelDiscovery | None = None) -> ModelListing:
    monkeypatch.setattr(discovery_module, "resolve",
                        lambda *args, **kwargs: pytest.fail("a brain was already provided"))
    return asyncio.run((cache or ModelDiscovery()).list(provider, brain=brain, force=force))


def test_capability_without_discovery_is_reported_without_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Code exposes no listing; saying so beats inventing a fixed catalogue."""
    monkeypatch.setattr(discovery_module, "resolve",
                        lambda *args, **kwargs: pytest.fail("must not construct a brain"))
    listing = asyncio.run(ModelDiscovery().list("claude"))
    assert listing.status is DiscoveryStatus.NOT_SUPPORTED
    assert listing.models == ()
    assert "manualmente" in listing.detail


@pytest.mark.parametrize("kind,status", [
    (ErrorKind.EXECUTABLE_MISSING, DiscoveryStatus.NOT_INSTALLED),
    (ErrorKind.AUTH_REQUIRED, DiscoveryStatus.AUTH_REQUIRED),
    (ErrorKind.INVALID_CREDENTIALS, DiscoveryStatus.AUTH_REQUIRED),
    (ErrorKind.UNSUPPORTED_FEATURE, DiscoveryStatus.NOT_SUPPORTED),
    (ErrorKind.CONFIGURATION, DiscoveryStatus.UNAVAILABLE),
    (ErrorKind.RATE_LIMITED, DiscoveryStatus.FAILED),
])
def test_provider_failures_become_visible_states(monkeypatch: pytest.MonkeyPatch, kind: ErrorKind,
                                                 status: DiscoveryStatus) -> None:
    brain = _StubBrain(error=BrainError(kind, "falha do provider"))
    listing = _list(monkeypatch, brain)
    assert listing.status is status
    assert listing.detail == "falha do provider"
    assert listing.models == ()


def test_an_empty_successful_listing_is_its_own_state(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = _list(monkeypatch, _StubBrain(models=[]))
    assert listing.status is DiscoveryStatus.EMPTY
    assert listing.selection_problem("qualquer") == ""  # no catalogue, no verdict


def test_listing_is_cached_until_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    brain = _StubBrain(models=[ModelInfo("m1", "M1")])
    cache = ModelDiscovery(ttl=300)
    monkeypatch.setattr(discovery_module, "resolve",
                        lambda *args, **kwargs: pytest.fail("a brain was already provided"))
    first = asyncio.run(cache.list("agy", brain=brain))
    second = asyncio.run(cache.list("agy", brain=brain))
    assert first.cached is False and second.cached is True
    assert brain.calls == 1
    asyncio.run(cache.list("agy", brain=brain, force=True))
    assert brain.calls == 2
    # An expired entry is refetched instead of being served forever.
    cache.ttl = 0.0
    time.sleep(0.001)
    asyncio.run(cache.list("agy", brain=brain))
    assert brain.calls == 3


def test_invalidate_clears_only_the_named_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = ModelDiscovery()
    cache.store(ModelListing("agy", "AGY", DiscoveryStatus.READY, models=(ModelInfo("a"),)))
    cache.store(ModelListing("qoder", "Qoder CLI", DiscoveryStatus.READY, models=(ModelInfo("q"),)))
    assert cache.cached("agy") is not None
    cache.invalidate("agy")
    assert cache.cached("agy") is None
    assert cache.cached("qoder") is not None
    cache.invalidate()
    assert cache.cached("qoder") is None


def test_saved_model_is_reported_only_when_a_catalogue_proves_it_missing() -> None:
    ready = ModelListing("kilo", "Kilo CLI", DiscoveryStatus.READY,
                         models=(ModelInfo("kilo/kilo-auto/free"),))
    problem = ready.selection_problem("auto free")
    assert "auto free" in problem and "não está mais disponível" in problem
    assert "não foi alterada" in problem
    assert ready.selection_problem("kilo/kilo-auto/free") == ""
    assert ready.contains("kilo/kilo-auto/free")
    # Nothing is claimed from a listing that never succeeded.
    failed = ModelListing("kilo", "Kilo CLI", DiscoveryStatus.FAILED, detail="timeout")
    assert failed.selection_problem("auto free") == ""
    assert ready.label_of("kilo/kilo-auto/free") == "kilo/kilo-auto/free"
    assert ready.label_of("ausente") == "ausente"


def test_settings_for_never_inherits_another_providers_credentials() -> None:
    base = Settings(provider="openai", model="gpt", api_key="secret-key")
    cloned = settings_for("ollama", base)
    assert cloned.provider == "ollama"
    assert cloned.api_key == "" and cloned.model == ""
    assert cloned.base_url == "http://localhost:11434/v1"
    with pytest.raises(BrainError):
        settings_for("does-not-exist", base)


def test_an_owned_brain_is_closed_after_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    brain = _StubBrain(models=[ModelInfo("m1")])
    monkeypatch.setattr(discovery_module, "resolve", lambda *args, **kwargs: brain)
    listing = asyncio.run(ModelDiscovery().list("agy", settings=Settings(provider="agy", model="m1")))
    assert listing.ok and brain.closed is True


def test_a_provider_that_cannot_be_built_is_a_state_not_an_exception(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> object:
        raise BrainError(ErrorKind.CONFIGURATION, "Configure sua chave em Configurar API.")

    monkeypatch.setattr(discovery_module, "resolve", explode)
    listing = asyncio.run(ModelDiscovery().list("openai"))
    assert listing.status is DiscoveryStatus.UNAVAILABLE
    assert "chave" in listing.detail


# --- A4: the same rule for the CLI brains, which previously never compared -----


def _headless(monkeypatch: pytest.MonkeyPatch, listing: str, model: str) -> HeadlessCliBrain:
    spec = find_spec("kilo")
    assert spec is not None

    async def fake_run_once(argv: list[str], *, timeout: float) -> tuple[int, str, str]:
        if "--version" in argv:
            return 0, "kilo 7.8.8\n", ""
        return 0, listing, ""

    monkeypatch.setattr("app.brain.cli.headless.run_once", fake_run_once)
    monkeypatch.setattr("app.brain.cli.headless.resolve_executable", lambda *a, **k: "kilo")
    return HeadlessCliBrain(Settings(provider="kilo", model=model), spec, executable="kilo")


def test_cli_health_flags_a_saved_model_the_listing_does_not_carry(
        monkeypatch: pytest.MonkeyPatch) -> None:
    brain = _headless(monkeypatch, "kilo/kilo-auto/free\nkilo/~anthropic/latest\n", "auto free")
    health = asyncio.run(brain.health())
    assert health.status is HealthStatus.MISCONFIGURED
    assert "auto free" in health.detail and "não foi alterada" in health.detail
    assert health.ok is False


def test_cli_health_accepts_a_model_the_listing_carries(monkeypatch: pytest.MonkeyPatch) -> None:
    brain = _headless(monkeypatch, "kilo/kilo-auto/free\n", "kilo/kilo-auto/free")
    health = asyncio.run(brain.health())
    assert health.status is HealthStatus.READY


def test_discovery_status_mirrors_health_status() -> None:
    assert ModelDiscovery.status_from_health(type("H", (), {"status": HealthStatus.LOGIN_REQUIRED})()) \
        is DiscoveryStatus.AUTH_REQUIRED
    assert ModelDiscovery.status_from_health(type("H", (), {"status": HealthStatus.NOT_INSTALLED})()) \
        is DiscoveryStatus.NOT_INSTALLED

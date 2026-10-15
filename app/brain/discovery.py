"""Central model discovery: one contract for every brain.

Selecting a provider is enough; nobody should have to type a model name from
memory. This module asks the provider what it really offers — a CLI listing
command, an HTTP ``/models`` endpoint, or a local service's own discovery — and
reports *why* it could not, because "not installed", "not authenticated",
"nothing returned" and "discovery failed" are four different things to the
person in front of the screen.

Two claims stay separate on purpose:

* A **listing** proves a model exists in the catalogue of this installation or
  account. It never proves credits, quota, or that an inference works.
* A **saved selection** is reported as unavailable only when a listing
  succeeded. When discovery itself failed, nothing is inferred about the model,
  and the saved selection is left exactly as the user saved it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from enum import Enum

from app.brain.api_brain import resolve_base_url
from app.brain.contracts import (
    Brain,
    BrainError,
    BrainHealth,
    Capability,
    ErrorKind,
    HealthStatus,
    ModelInfo,
)
from app.brain.registry import get, resolve
from app.settings import ConfigurationError, Settings


class DiscoveryStatus(str, Enum):
    """Why a listing looks the way it does. One state per user-visible message."""

    READY = "ready"
    EMPTY = "empty"
    NOT_SUPPORTED = "not_supported"
    NOT_INSTALLED = "not_installed"
    AUTH_REQUIRED = "auth_required"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class ModelListing:
    """One provider's catalogue, or the reason there is none."""

    provider: str
    label: str
    status: DiscoveryStatus
    models: tuple[ModelInfo, ...] = ()
    detail: str = ""
    cached: bool = False
    fetched_at: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status is DiscoveryStatus.READY

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.models)

    def label_of(self, identifier: str) -> str:
        for item in self.models:
            if item.id == identifier:
                return item.label or item.id
        return identifier

    def contains(self, model: str) -> bool:
        return any(item.id == model for item in self.models)

    def selection_problem(self, model: str) -> str:
        """A saved model this listing cannot confirm; ``""`` when there is nothing to say.

        Silence when discovery did not succeed is deliberate: without a catalogue
        there is no evidence that the saved model is wrong, and inventing one would
        mean rewriting a choice the user made.
        """
        if not model.strip() or self.status is not DiscoveryStatus.READY:
            return ""
        if self.contains(model):
            return ""
        return (f"O modelo salvo '{model}' não está mais disponível em {self.label}. "
                "Escolha outro modelo na lista; a seleção salva não foi alterada.")


_STATUS_BY_ERROR: dict[ErrorKind, DiscoveryStatus] = {
    ErrorKind.EXECUTABLE_MISSING: DiscoveryStatus.NOT_INSTALLED,
    ErrorKind.AUTH_REQUIRED: DiscoveryStatus.AUTH_REQUIRED,
    ErrorKind.INVALID_CREDENTIALS: DiscoveryStatus.AUTH_REQUIRED,
    ErrorKind.UNSUPPORTED_FEATURE: DiscoveryStatus.NOT_SUPPORTED,
    ErrorKind.CONFIGURATION: DiscoveryStatus.UNAVAILABLE,
    ErrorKind.MODEL_UNAVAILABLE: DiscoveryStatus.UNAVAILABLE,
}

_STATUS_BY_HEALTH: dict[HealthStatus, DiscoveryStatus] = {
    HealthStatus.NOT_INSTALLED: DiscoveryStatus.NOT_INSTALLED,
    HealthStatus.LOGIN_REQUIRED: DiscoveryStatus.AUTH_REQUIRED,
    HealthStatus.MISCONFIGURED: DiscoveryStatus.UNAVAILABLE,
    HealthStatus.UNAVAILABLE: DiscoveryStatus.UNAVAILABLE,
}


def base_settings() -> Settings:
    """Settings to clone per provider; a broken saved selection never breaks listing."""
    try:
        return Settings.load()
    except ConfigurationError:
        return Settings()


def settings_for(provider: str, base: Settings | None = None) -> Settings:
    """The same settings pointed at another provider, without inheriting its key."""
    source = base if base is not None else base_settings()
    get(provider)  # unknown provider is a configuration error, not a listing state
    return replace(
        source, provider=provider,
        model=source.model if source.provider == provider else "",
        api_key=source.api_key if source.provider == provider else "",
        base_url=resolve_base_url(provider, source.base_url if source.provider == provider else ""),
        effort=source.effort if source.provider == provider else "",
    )


class ModelDiscovery:
    """Per-provider listing with a controlled cache.

    A listing spawns a real CLI or hits a real endpoint, so repeating it on every
    keystroke would be both slow and (for metered providers) rude. Cached results
    are returned until ``ttl`` expires or the caller asks for ``force=True``.
    """

    def __init__(self, ttl: float = 300.0) -> None:
        self.ttl = ttl
        self._listings: dict[str, ModelListing] = {}

    def cached(self, provider: str) -> ModelListing | None:
        return self._listings.get(provider)

    def invalidate(self, provider: str | None = None) -> None:
        if provider is None:
            self._listings.clear()
        else:
            self._listings.pop(provider, None)

    def store(self, listing: ModelListing) -> ModelListing:
        """Record a listing obtained elsewhere (a health probe, for instance)."""
        self._listings[listing.provider] = listing
        return listing

    async def list(self, provider: str, *, settings: Settings | None = None,
                   force: bool = False, brain: Brain | None = None) -> ModelListing:
        """Enumerate one provider's models, or explain why it could not.

        Never raises for a provider problem: a discovery failure is a state the UI
        must display, not an exception that blanks the dialog.
        """
        if not force:
            cached = self._listings.get(provider)
            if cached is not None and time.monotonic() - cached.fetched_at < self.ttl:
                return replace(cached, cached=True)
        label = provider
        try:
            descriptor = get(provider)
            label = descriptor.label
        except BrainError as exc:
            return self.store(ModelListing(provider, provider, DiscoveryStatus.UNAVAILABLE,
                                           detail=exc.message, fetched_at=time.monotonic()))
        if not descriptor.capabilities.supports(Capability.MODEL_DISCOVERY):
            # Listing something this provider cannot enumerate would be a fixed,
            # invented catalogue. Manual entry remains the documented fallback.
            return self.store(ModelListing(provider, label, DiscoveryStatus.NOT_SUPPORTED,
                                           detail=f"{label} não permite descobrir modelos automaticamente. "
                                                  "Informe o modelo manualmente.",
                                           fetched_at=time.monotonic()))
        owned = brain is None
        instance = brain
        try:
            if instance is None:
                # A candidate Settings built by the dialog is used as handed over;
                # anything else is cloned so no other provider's key leaks in.
                target = (settings if settings is not None and settings.provider == provider
                          else settings_for(provider, settings))
                instance = resolve(target)
            entries = tuple(await instance.models())
            if entries:
                return self.store(ModelListing(provider, label, DiscoveryStatus.READY, models=entries,
                                               detail=f"{len(entries)} modelos disponíveis.",
                                               fetched_at=time.monotonic()))
            return self.store(ModelListing(provider, label, DiscoveryStatus.EMPTY,
                                           detail=f"{label} respondeu sem retornar nenhum modelo.",
                                           fetched_at=time.monotonic()))
        except BrainError as exc:
            status = _STATUS_BY_ERROR.get(exc.kind, DiscoveryStatus.FAILED)
            return self.store(ModelListing(provider, label, status, detail=exc.message,
                                           fetched_at=time.monotonic()))
        except Exception as exc:  # noqa: BLE001 - a provider must not crash the dialog
            return self.store(ModelListing(provider, label, DiscoveryStatus.FAILED,
                                           detail=f"Falha na descoberta: {type(exc).__name__}.",
                                           fetched_at=time.monotonic()))
        finally:
            if owned and instance is not None:
                try:
                    await instance.close()
                except Exception:  # noqa: BLE001 - closing is best effort
                    pass

    @staticmethod
    def status_from_health(health: BrainHealth) -> DiscoveryStatus:
        """Map a readiness verdict onto the same vocabulary the UI displays."""
        return _STATUS_BY_HEALTH.get(health.status, DiscoveryStatus.FAILED)


_DISCOVERY: ModelDiscovery | None = None


def get_discovery() -> ModelDiscovery:
    """Process-wide cache, so reopening the dialog does not re-spawn every CLI."""
    global _DISCOVERY
    if _DISCOVERY is None:
        _DISCOVERY = ModelDiscovery()
    return _DISCOVERY

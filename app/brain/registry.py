"""The registry: one place that knows which brains exist.

Descriptors are derived from the settings groups and the CLI catalog, so a new
provider is added in one place and every consumer (UI, controller, tests) sees
it automatically. No other module needs a ``if provider == "..."`` chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from app.brain.contracts import Brain, BrainError, Capabilities, ErrorKind
from app.settings import (
    API_KEY_PROVIDERS,
    CLI_PROVIDERS,
    LOCAL_ENDPOINT_PROVIDERS,
    Settings,
)

# Display names only; capability truth lives in the descriptors and specs.
LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "gemini": "Google Gemini",
    "openrouter": "OpenRouter",
    "nvidia": "NVIDIA NIM",
    "groq": "Groq",
    "custom": "OpenAI Compatible",
    "ollama": "Ollama",
    "lmstudio": "LM Studio",
    "vllm": "vLLM",
    "codex": "Codex CLI",
    "claude": "Claude Code",
    "agy": "AGY",
    "qoder": "Qoder CLI",
    "kilo": "Kilo CLI",
    "kiro": "Kiro CLI",
    "opencode": "OpenCode",
    "freepi": "Free Pi CLI",
}


class BrainKind(str, Enum):
    AGENT_RUNTIME = "agent_runtime"
    MODEL_API = "model_api"


class BrainLocation(str, Enum):
    """Where the client runs. A local CLI still infers in the cloud, usually."""

    CLOUD_API = "cloud_api"
    LOCAL_ENDPOINT = "local_endpoint"
    LOCAL_CLI = "local_cli"


@dataclass(frozen=True)
class BrainDescriptor:
    id: str
    label: str
    kind: BrainKind
    location: BrainLocation
    factory: Callable[[Settings], Brain] | None = None
    executable: str | None = None
    requires_api_key: bool = True
    capabilities: Capabilities = Capabilities()
    # Why an existing runtime cannot be selected. Empty for every provider that is
    # implemented (or merely unwired): this is for a CLI that is known to exist and
    # is known *not* to expose a usable headless contract, so the UI can say so.
    unavailable_reason: str = ""

    @property
    def implemented(self) -> bool:
        return self.factory is not None


_DESCRIPTORS: dict[str, BrainDescriptor] = {}
_LOADED = False


def register(descriptor: BrainDescriptor) -> None:
    _DESCRIPTORS[descriptor.id] = descriptor


def _load_defaults() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    # Imported here so the registry stays import-cheap and cycle-free.
    from app.brain.api_brain import ApiBrain, api_capabilities
    from app.brain.cli import process as cli_process
    from app.brain.cli.catalog import find_spec
    from app.brain.cli.headless import HeadlessCliBrain
    from app.brain.cli.known import known_runtime
    from app.brain.codex_brain import CodexBrain, CodexBrain as _CodexBrain

    def api(settings: Settings) -> Brain:
        return ApiBrain(settings)

    def cli_factory(cli_id: str) -> Callable[[Settings], Brain]:
        def factory(settings: Settings) -> Brain:
            spec = find_spec(cli_id)
            assert spec is not None  # guarded by the caller
            executable = cli_process.resolve_executable(spec.executable, spec.search_paths)
            if executable is None:
                raise BrainError(ErrorKind.EXECUTABLE_MISSING,
                                 f"{spec.label} não está instalado nesta máquina.")
            return HeadlessCliBrain(settings, spec, executable=executable)
        return factory

    for provider_id in sorted(API_KEY_PROVIDERS):
        register(BrainDescriptor(provider_id, LABELS.get(provider_id, provider_id),
                                 BrainKind.MODEL_API, BrainLocation.CLOUD_API, factory=api,
                                 capabilities=api_capabilities(provider_id)))

    for provider_id in sorted(LOCAL_ENDPOINT_PROVIDERS):
        register(BrainDescriptor(provider_id, LABELS.get(provider_id, provider_id),
                                 BrainKind.MODEL_API, BrainLocation.LOCAL_ENDPOINT, factory=api,
                                 requires_api_key=provider_id == "custom",
                                 capabilities=api_capabilities(provider_id)))

    # Codex keeps its dedicated app-server adapter; the rest share one adapter.
    register(BrainDescriptor("codex", LABELS["codex"], BrainKind.AGENT_RUNTIME,
                             BrainLocation.LOCAL_CLI, factory=lambda settings: CodexBrain(settings),
                             executable="codex", requires_api_key=False,
                             capabilities=_CodexBrain(Settings(provider="codex",
                                                               model="")).capabilities))

    for provider_id in sorted(CLI_PROVIDERS - {"codex"}):
        spec = find_spec(provider_id)
        if spec is None:
            # A known runtime without a headless CliSpec: visible, not selectable.
            # The reason is part of the contract so the UI never shows a dead entry
            # without saying why (free-pi-cli is interactive only).
            known = known_runtime(provider_id)
            register(BrainDescriptor(
                provider_id,
                known.label if known is not None else LABELS.get(provider_id, provider_id),
                BrainKind.AGENT_RUNTIME, BrainLocation.LOCAL_CLI,
                factory=None,
                executable=known.executable if known is not None else None,
                requires_api_key=False,
                unavailable_reason=known.unavailable_reason if known is not None else "",
            ))
            continue
        register(BrainDescriptor(spec.id, spec.label, BrainKind.AGENT_RUNTIME, BrainLocation.LOCAL_CLI,
                                 factory=cli_factory(spec.id), executable=spec.executable,
                                 requires_api_key=False, capabilities=spec.capabilities))


def descriptors() -> list[BrainDescriptor]:
    _load_defaults()
    return list(_DESCRIPTORS.values())


def get(provider_id: str) -> BrainDescriptor:
    _load_defaults()
    try:
        return _DESCRIPTORS[provider_id]
    except KeyError as exc:
        raise BrainError(ErrorKind.CONFIGURATION, "Provider desconhecido.") from exc


def registered_ids() -> frozenset[str]:
    _load_defaults()
    return frozenset(_DESCRIPTORS)


def local_agents() -> list[BrainDescriptor]:
    _load_defaults()
    return [item for item in _DESCRIPTORS.values() if item.location is BrainLocation.LOCAL_CLI]


def cloud_providers() -> list[BrainDescriptor]:
    _load_defaults()
    return [item for item in _DESCRIPTORS.values() if item.location is not BrainLocation.LOCAL_CLI]


def resolve(settings: Settings) -> Brain:
    """Build the brain for the selected provider. The only construction point."""
    descriptor = get(settings.provider)
    if descriptor.factory is None:
        raise BrainError(ErrorKind.UNSUPPORTED_FEATURE,
                         f"{descriptor.label} ainda não está implementado.")
    return descriptor.factory(settings)

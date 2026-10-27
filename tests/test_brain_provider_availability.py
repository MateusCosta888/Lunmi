"""The provider picker distinguishes an installed command from a supported Brain."""
import json
from types import SimpleNamespace

from app.brain.contracts import Capabilities
from app.brain.registry import BrainKind, BrainLocation
from app.bridge import service as service_module
from app.bridge.service import BridgeService


def test_provider_availability_does_not_execute_or_expose_install_paths(monkeypatch):
    from app.brain.cli import process

    calls = []
    monkeypatch.setattr(process, "resolve_executable", lambda name, *args: calls.append(name) or "C:/private/test-cli.cmd")
    descriptors = [SimpleNamespace(
        id="test-cli", label="Test CLI", kind=BrainKind.AGENT_RUNTIME,
        location=BrainLocation.LOCAL_CLI, requires_api_key=False,
        capabilities=Capabilities(), executable="test-cli", implemented=False,
        unavailable_reason="Esta CLI só expõe uma interface interativa.",
    )]
    monkeypatch.setattr(service_module.registry, "descriptors", lambda: descriptors)
    monkeypatch.setattr(BridgeService, "_public_brain_state", lambda self: {})
    result = object.__new__(BridgeService)._handle_brain_get()
    provider = result["providers"][0]
    assert provider["installed"] is True
    assert provider["available"] is False
    assert provider["unavailable_reason"] == descriptors[0].unavailable_reason
    assert calls == ["test-cli"]
    assert "private" not in json.dumps(result)


def test_known_interactive_runtime_is_not_offered_as_a_brain(monkeypatch):
    from app.brain.cli import process

    freepi = service_module.registry.get("freepi")
    monkeypatch.setattr(service_module.registry, "descriptors", lambda: [freepi])
    monkeypatch.setattr(process, "resolve_executable", lambda *args: (_ for _ in ()).throw(AssertionError("not a selectable Brain")))
    monkeypatch.setattr(BridgeService, "_public_brain_state", lambda self: {})
    assert object.__new__(BridgeService)._handle_brain_get()["providers"] == []


def test_api_availability_does_not_probe_local_commands(monkeypatch):
    from app.brain.cli import process

    monkeypatch.setattr(process, "resolve_executable", lambda name: (_ for _ in ()).throw(AssertionError("unexpected probe")))
    descriptor = SimpleNamespace(
        id="openai", label="OpenAI", kind=BrainKind.MODEL_API,
        location=BrainLocation.CLOUD_API, requires_api_key=True,
        capabilities=Capabilities(), executable=None, implemented=True,
        unavailable_reason="",
    )
    monkeypatch.setattr(service_module.registry, "descriptors", lambda: [descriptor])
    monkeypatch.setattr(BridgeService, "_public_brain_state", lambda self: {})
    provider = object.__new__(BridgeService)._handle_brain_get()["providers"][0]
    assert provider["available"] is True
    assert "installed" not in provider
    assert "unavailable_reason" not in provider

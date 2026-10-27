from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.brain import codex_brain
from app.brain.api_brain import ApiBrain
from app.brain.contracts import (
    BrainError,
    Capability,
    Capabilities,
    ErrorKind,
    EventKind,
    HealthStatus,
    Usage,
    events_for,
)
from app.brain.registry import (
    BrainKind,
    BrainLocation,
    descriptors,
    get,
    registered_ids,
    resolve,
)
from app.codex_app_server import CodexError
from app.llm import Completion
from app.settings import SUPPORTED_PROVIDERS, Settings


def test_official_prompts_reach_brain_without_losing_contract_data() -> None:
    """The child's verdict has to be *readable* for the assertion to mean anything.

    Piped output is encoded with the child's own locale, so a check that prints an em dash
    (cp1252 `0x97`) crashed this test's reader thread instead of reporting the verdict —
    measured 2026-10-09, and it made the result depend on the machine's code page rather
    than on the contract. The child is asked for UTF-8 and the parent reads it tolerantly,
    which cannot hide a failure: the verdict and the exit code are still what is asserted.
    """
    root = Path(__file__).resolve().parents[1]
    process = subprocess.run([sys.executable, str(root / "tests/prompt_contract_check.py")],
                             cwd=root, capture_output=True, text=True, errors="replace",
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=60)
    assert process.returncode == 0, (process.stdout or "") + (process.stderr or "")
    assert "Prompt contract: PASS" in process.stdout


def test_registry_describes_every_supported_brain() -> None:
    assert registered_ids() == SUPPORTED_PROVIDERS
    by_id = {descriptor.id: descriptor for descriptor in descriptors()}
    codex = by_id["codex"]
    assert codex.kind is BrainKind.AGENT_RUNTIME
    assert codex.location is BrainLocation.LOCAL_CLI
    assert codex.requires_api_key is False
    assert codex.executable == "codex"
    assert by_id["openai"].kind is BrainKind.MODEL_API
    assert by_id["openai"].requires_api_key is True


def test_unknown_provider_is_a_configuration_error() -> None:
    with pytest.raises(BrainError) as failure:
        get("does-not-exist")
    assert failure.value.kind is ErrorKind.CONFIGURATION


def test_resolve_builds_a_brain_without_vendor_conditionals() -> None:
    codex = resolve(Settings(provider="codex", model="account-model"))
    assert codex.provider_id == "codex"
    assert codex.capabilities.supports(Capability.MODEL_DISCOVERY)
    # Inference mode: the CLI must not be advertised as able to act on the machine.
    assert not codex.capabilities.supports(Capability.SHELL)
    assert not codex.capabilities.supports(Capability.TOOL_CALLS)


def test_resolve_surfaces_missing_credentials_as_brain_error() -> None:
    with pytest.raises(BrainError) as failure:
        resolve(Settings(provider="openai", model="gpt"))
    assert failure.value.kind is ErrorKind.CONFIGURATION


def test_api_brain_capabilities_differ_from_agent_runtime() -> None:
    brain = ApiBrain(Settings(provider="openai", model="gpt", api_key="test-key"))
    assert brain.capabilities.supports(Capability.MODEL_DISCOVERY)
    assert brain.capabilities.supports(Capability.STRUCTURED_OUTPUT)
    assert not brain.capabilities.supports(Capability.SHELL)


def test_capabilities_membership() -> None:
    capabilities = Capabilities.of(Capability.USAGE, Capability.IMAGES)
    assert capabilities.supports(Capability.USAGE)
    assert not capabilities.supports(Capability.SHELL)
    assert Capability.IMAGES in capabilities


def test_events_normalize_text_and_usage() -> None:
    events = events_for("openai", Completion("hello", 10, 3))
    assert [event.kind for event in events] == [
        EventKind.STARTED, EventKind.TEXT_COMPLETED, EventKind.USAGE, EventKind.COMPLETED,
    ]
    assert events[1].data["text"] == "hello"
    assert events[2].data["input_tokens"] == 10
    assert events[2].data["total_tokens"] == 13


def test_usage_never_invents_missing_numbers() -> None:
    assert Usage().total_tokens is None
    assert Usage(input_tokens=4).total_tokens == 4
    assert Usage.of_completion(Completion("x")).total_tokens is None
    assert Usage.of_completion(Completion("x", 2, 5)).total_tokens == 7


def test_brain_error_keeps_provider_detail_out_of_the_user_message() -> None:
    failure = BrainError(ErrorKind.QUOTA_EXCEEDED, "Limite de uso atingido.", detail="raw provider blob")
    assert str(failure) == "Limite de uso atingido."
    assert failure.detail == "raw provider blob"
    assert "raw provider blob" not in str(failure)


@pytest.mark.parametrize("code,kind", [
    ("usageLimitExceeded", ErrorKind.QUOTA_EXCEEDED),
    ("unauthorized", ErrorKind.AUTH_REQUIRED),
    ("notLoggedIn", ErrorKind.AUTH_REQUIRED),
])
def test_codex_errors_translate_to_brain_kinds(code: str, kind: ErrorKind) -> None:
    error = CodexError("Codex não concluiu a inferência UFO: falhou", code=code, detail="detalhe")
    assert codex_brain.translate(error).kind is kind


def test_codex_missing_executable_and_timeout_are_distinguished() -> None:
    missing = codex_brain.translate(CodexError("Codex CLI não encontrado. Instale o Codex."))
    assert missing.kind is ErrorKind.EXECUTABLE_MISSING
    assert codex_brain.translate(TimeoutError()).kind is ErrorKind.TIMEOUT
    assert codex_brain.translate(RuntimeError("boom")).kind is ErrorKind.UNKNOWN


def test_quota_rejection_is_not_retried() -> None:
    from app.codex_app_server import IncompleteTurn

    failure = IncompleteTurn("usage limit", retryable=False, code="usageLimitExceeded")
    assert failure.retryable is False
    assert failure.code == "usageLimitExceeded"


def test_codex_health_reports_not_installed_without_touching_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_brain, "codex_executable", lambda: None)
    brain = codex_brain.CodexBrain(Settings(provider="codex", model="account-model"))
    health = asyncio.run(brain.health())
    assert health.status is HealthStatus.NOT_INSTALLED
    assert health.ok is False


def test_quota_error_message_is_actionable() -> None:
    error = CodexError("Codex não concluiu a inferência UFO", code="usageLimitExceeded", detail="usage limit")
    translated = codex_brain.translate(error)
    assert translated.kind is ErrorKind.QUOTA_EXCEEDED
    assert "cota" in translated.message


def test_codex_model_listing_starts_the_app_server_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """model/list is a request: it must never be sent on a never-started server."""
    from app.codex_app_server import CodexAppServer

    async def scenario() -> None:
        server = CodexAppServer()
        started: list[bool] = []

        async def fake_start() -> None:
            started.append(True)

        async def fake_request(method: str, params: dict) -> dict:
            assert started, "model/list was sent before the app-server started"
            assert method == "model/list"
            return {"data": [{"model": "gpt-6.1-sol", "displayName": "GPT-6.1-Sol", "isDefault": True}]}

        monkeypatch.setattr(server, "start", fake_start)
        monkeypatch.setattr(server, "request", fake_request)
        assert await server.list_models() == [("gpt-6.1-sol", "GPT-6.1-Sol", True)]
        assert started == [True]

    asyncio.run(scenario())


def test_core_layers_do_not_branch_on_vendor_names() -> None:
    """Section 37: provider-specific logic must stay inside the Brain layer.

    ``app/settings.py`` holds the provider catalogue (and its per-provider
    constraints) and ``app/llm.py`` holds the REST transports — both are part of
    the provider-aware boundary. The agent, the runtime and the UI must know no
    vendor name at all, which is what this test enforces.
    """
    root = Path(__file__).resolve().parents[1] / "app"
    vendors = ("codex", "openai", "anthropic", "gemini", "openrouter", "nvidia", "custom",
               "ollama", "lmstudio", "vllm", "claude", "agy", "qoder", "kilo", "kiro", "opencode",
               "freepi")
    offenders: list[str] = []
    for folder in ("core", "computer", "ui"):
        for path in (root / folder).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            offenders.extend(
                f"{path.relative_to(root)}: {name}"
                for name in vendors
                if f'== "{name}"' in text
            )
    assert offenders == []

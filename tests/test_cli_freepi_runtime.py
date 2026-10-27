"""free-pi-cli: installed, but with no headless contract.

The package installed on this machine (0.6.7) is a preconfigured ``pi`` TUI paid
for by ads. Its own parser accepts only session flags, ``logout``, ``mcp``,
``--version`` and ``--help``; any other argument is rejected with exit code 2
(``--print`` included, measured). So it can be a *known runtime* — visible,
detectable, disabled with a reason — but never a ``CliSpec`` the adapter drives.

These tests hold that line offline: no TUI, no login, no inference, no network.
"""

from __future__ import annotations

import asyncio

import pytest

from app.brain import detect as detect_module
from app.brain import registry
from app.brain.cli.catalog import CLI_SPECS, find_spec
from app.brain.cli.known import known_runtime, known_runtimes
from app.brain.contracts import BrainError, ErrorKind
from app.settings import Settings


def test_free_pi_cli_is_declared_without_an_inference_spec() -> None:
    """A known runtime must not leak into the catalogue the adapter drives."""
    assert find_spec("freepi") is None
    assert "freepi" not in CLI_SPECS
    runtime = known_runtime("freepi")
    assert runtime is not None
    assert runtime.executable == "free-pi-cli"
    assert runtime.version_args == ("--version",)
    assert "headless" in runtime.unavailable_reason.casefold()


def test_tree_entry_appears_disabled_with_its_reason_and_no_api_key() -> None:
    descriptor = registry.get("freepi")
    assert descriptor.label == "Free Pi CLI"
    assert descriptor.implemented is False
    assert descriptor.location is registry.BrainLocation.LOCAL_CLI
    assert descriptor.kind is registry.BrainKind.AGENT_RUNTIME
    assert descriptor.executable == "free-pi-cli"
    assert descriptor.requires_api_key is False
    assert descriptor.unavailable_reason
    assert "headless" in descriptor.unavailable_reason.casefold()
    # Selecting it cannot start the TUI: resolve refuses before construction.
    with pytest.raises(BrainError) as failure:
        registry.resolve(Settings(provider="freepi", model=""))
    assert failure.value.kind is ErrorKind.UNSUPPORTED_FEATURE


def test_free_pi_cli_accepts_no_key_and_no_base_url_like_the_other_clis() -> None:
    Settings(provider="freepi", model="model").validate()
    with pytest.raises(Exception):
        Settings(provider="freepi", model="model", api_key="key").validate()
    with pytest.raises(Exception):
        Settings(provider="freepi", model="model", base_url="https://example.test").validate()


def test_detection_covers_known_runtimes_without_calling_them_supported(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(detect_module, "resolve_executable", lambda *args, **kwargs: None)

    async def scenario() -> None:
        detections = await detect_module.detect_all()
        expected = set(CLI_SPECS) | {runtime.id for runtime in known_runtimes()}
        assert {item.cli_id for item in detections} == expected
        freepi = next(item for item in detections if item.cli_id == "freepi")
        assert freepi.installed is False
        assert freepi.supported is False
        assert freepi.unavailable_reason

    asyncio.run(scenario())


def test_known_runtime_probe_runs_only_the_documented_version_command(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Detection may locate and version-check it; it must never start the CLI."""
    calls: list[list[str]] = []

    async def fake_run_once(argv: list[str], *, timeout: float) -> tuple[int, str, str]:
        calls.append(list(argv))
        return 0, "free-pi-cli 0.6.7\n", ""

    monkeypatch.setattr(detect_module, "resolve_executable",
                        lambda *args, **kwargs: "C:/fake/free-pi-cli.cmd")
    monkeypatch.setattr(detect_module, "run_once", fake_run_once)
    runtime = known_runtime("freepi")
    assert runtime is not None

    async def scenario() -> None:
        detection = await detect_module.detect_runtime(runtime)
        assert detection.installed is True
        assert detection.version == "0.6.7"
        assert detection.supported is False
        assert detection.unavailable_reason == runtime.unavailable_reason

    asyncio.run(scenario())
    assert calls == [["C:/fake/free-pi-cli.cmd", "--version"]]

"""Raciocínio e modo rápido do Codex: guardados, aplicados a cada turno e também à pesquisa."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.brain import cli_web
from app.brain.codex_brain import CodexBrain
from app.brain.codex_options import CodexOptions, load_codex_options, save_codex_options
from app.bridge import protocol
from app.bridge.service import BridgeService
from app.settings import Settings


def brain(tmp_path: Path, efforts: set[str] | None = None) -> CodexBrain:
    owner = CodexBrain(Settings(provider="codex", model="gpt-6-luna", state_root=tmp_path))
    if efforts is not None:
        owner._client._model_efforts["gpt-6-luna"] = efforts
    return owner


def test_a_missing_or_odd_file_is_the_model_default(tmp_path: Path) -> None:
    assert load_codex_options(tmp_path) == CodexOptions()
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local" / "codex-options.json").write_text('{"effort":"turbo","fast":"sim"}', encoding="utf-8")
    assert load_codex_options(tmp_path) == CodexOptions()


def test_the_choice_reaches_the_next_turn_without_a_restart(tmp_path: Path) -> None:
    owner = brain(tmp_path, {"low", "high"})
    assert owner._turn() == {"effort": ""}
    save_codex_options(tmp_path, CodexOptions("low", True))
    assert owner._turn() == {"effort": "low", "fast": True}


def test_a_level_this_model_lacks_falls_back_instead_of_failing(tmp_path: Path) -> None:
    save_codex_options(tmp_path, CodexOptions("ultra", False))
    assert brain(tmp_path, {"low", "high"})._turn() == {"effort": ""}


def test_native_search_uses_the_same_choice(tmp_path: Path) -> None:
    settings = Settings(provider="codex", model="m", state_root=tmp_path)
    assert "model_reasoning_effort=low" not in cli_web._web_argv("codex", "codex", "m", settings)
    save_codex_options(tmp_path, CodexOptions("low", True))
    argv = cli_web._web_argv("codex", "codex", "m", settings)
    assert "model_reasoning_effort=low" in argv and "service_tier=fast" in argv


def test_other_providers_never_get_codex_flags(tmp_path: Path) -> None:
    save_codex_options(tmp_path, CodexOptions("low", True))
    settings = Settings(provider="qoder", model="m", state_root=tmp_path)
    assert "service_tier=fast" not in cli_web._web_argv("qoder", "qoder", "m", settings)


def test_the_command_saves_and_refuses_bad_values(tmp_path: Path) -> None:
    owner = BridgeService.__new__(BridgeService)
    owner.settings = SimpleNamespace(state_root=tmp_path)
    assert owner._handle_codex_options({"effort": "low", "fast": True}) == {"effort": "low", "fast": True}
    assert owner._handle_codex_options({}) == {"effort": "low", "fast": True}
    for bad in ({"effort": "turbo", "fast": True}, {"effort": "low", "fast": 1}, {"effort": "low"}):
        with pytest.raises(protocol.ProtocolError):
            owner._handle_codex_options(bad)

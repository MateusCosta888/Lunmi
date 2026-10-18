from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.brain.contracts import Capabilities
from app.core.controller import TaskController, _learning_origin_ok
from app.llm import Completion
from app.memory import MemoryManager
from app.settings import Settings


@pytest.mark.parametrize("spoken,candidate,expected", [
    ("Não gosto de música alta.", "Não gosto de música alta.", True),
    ("Eu não gosto de música alta.", "gosto de música alta.", False),
    ("I don't like loud music.", "like loud music.", False),
    ("I don't like loud music.", "I don't like loud music.", True),
    ("Desgosto de música alta.", "gosto de música alta.", False),
    ("A Ana: Gosto de astronomia.", "Gosto de astronomia.", False),
    ('"Gosto de astronomia."', '"Gosto de astronomia."', False),
    ("No meu trabalho uso editor Alpha.", "uso editor Alpha.", True),
    ("Gosto de astronomia.", "Gosto de astronomia.", True),
])
def test_learning_preserves_speaker_word_boundary_and_negative_meaning(spoken, candidate, expected):
    assert _learning_origin_ok(spoken, candidate) is expected


class SmallBrain:
    capabilities = Capabilities.of(max_safe_prompt_chars=3000)

    def __init__(self):
        self.calls = []

    async def complete(self, messages):
        self.calls.append(messages)
        return Completion(json.dumps({"status": "FINISH", "message": "Resposta de ensaio."}))

    async def close(self):
        pass


def test_provider_budget_drops_whole_old_pairs_and_session_lifecycle(tmp_path: Path):
    async def scenario():
        settings = Settings(provider="custom", api_key="synthetic", model="fixture",
                            base_url="http://127.0.0.1:1/v1", prewarm=False,
                            fast_path=False, app_index=False, app_search=False,
                            collect_metrics=False, logs_path=tmp_path, state_root=tmp_path)
        controller = TaskController(settings, memory=MemoryManager(tmp_path / "memory.sqlite3"))
        brain = SmallBrain()
        controller.brain = brain
        for index in range(6):
            controller.turns.add(controller.session_id, f"Pergunta {index} " + "u" * 300,
                                 f"Resposta {index} " + "a" * 300)
        try:
            result = await controller.run("Explica uma estrela.", conversation=True)
            assert result.status == "SUCCESS"
            messages = brain.calls[0]
            assert sum(len(m["content"]) for m in messages) <= 3000
            older = [m for m in messages if "(turno anterior):" in m["content"]]
            assert len(older) % 2 == 0 and len(older) < 12
            if older:
                assert "Pergunta 5" in older[-2]["content"]
                assert "Resposta 5" in older[-1]["content"]
            remembered = controller.turns.pairs(controller.session_id)
            await controller.reconfigure(replace(settings, model="second-fixture"))
            assert controller.turns.pairs(controller.session_id) == remembered
        finally:
            await controller.close()
        assert controller.turns.pairs(controller.session_id) == []

    asyncio.run(scenario())

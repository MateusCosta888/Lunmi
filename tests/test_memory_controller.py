from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.brain.contracts import BrainError, ErrorKind
from app.core.controller import TaskController
from app.llm import Completion
from app.memory import MemoryItem, MemoryManager
from app.memory.conversation import parse_response
from app.settings import Settings


def settings(tmp_path: Path, provider: str = 'codex') -> Settings:
    return Settings(provider=provider, model='offline-fixture', fast_path=False, prewarm=False,
                    collect_metrics=False, app_index=False, app_search=False,
                    logs_path=tmp_path)


class FixtureBrain:
    def __init__(self, reply: str = '{"status":"FINISH","message":["Resposta curta."],"action":null}') -> None:
        self.reply = reply
        self.calls: list[list[dict]] = []
        self.cli_calls: list = []

    async def complete(self, messages: list[dict]) -> Completion:
        self.calls.append(messages)
        return Completion(self.reply, input_tokens=10, output_tokens=5)

    async def close(self) -> None:
        pass


def test_m1_restart_provider_switch_conversation_and_local_commands(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / 'memory.sqlite3'
        first = TaskController(settings(tmp_path), memory=MemoryManager(path))
        first.engine.start = AsyncMock(side_effect=AssertionError('worker must not start'))
        result = await first.run('Lembra que prefiro respostas curtas.', conversation=True)
        assert result.status == 'SUCCESS' and result.scope == 'memory_command' and result.llm_calls == 0
        assert first.brain is None
        await first.close()
        second = TaskController(settings(tmp_path, 'kiro'), memory=MemoryManager(path))
        brain = FixtureBrain()
        second.brain = brain
        second.engine.start = AsyncMock(side_effect=AssertionError('worker must not start'))
        result = await second.run('Explique Python.', conversation=True)
        assert result.status == 'SUCCESS' and result.scope == 'conversation' and result.evidence == []
        assert 'prefiro respostas curtas' in json.dumps(brain.calls)
        assert result.memory_context_chars <= 2400 and result.memory_context_items == 1
        assert result.prompt_chars == result.prompt_base_chars + result.memory_context_chars
        assert second.memory.working.get(second.session_id) == {}
        listed = await second.run('o que você lembra?')
        assert 'prefiro respostas curtas' in listed.message
        # Esquecer pelo chat exige o trecho exato: uma palavra solta não apaga o bloco.
        forgotten = await second.run('esquece prefiro respostas curtas')
        assert forgotten.message == 'Memória apagada.'
        assert second.memory.recall('respostas curtas') == []
        logs = (tmp_path / 'tasks.jsonl').read_text(encoding='utf-8')
        assert 'prefiro respostas curtas' not in logs
        await second.close()
    asyncio.run(scenario())


def test_host_only_injection_worker_never_receives_store_or_profile(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = TaskController(settings(tmp_path))
        controller.memory.remember(MemoryItem(text='Prefiro respostas sucintas.'))
        brain = FixtureBrain('{"status":"FINISH"}')
        controller.brain = brain
        controller.engine.start = AsyncMock()
        async def run_task(instruction, completion, confirm, **kwargs):
            assert instruction == 'Abra aplicativo fixture'
            assert 'memory' not in json.dumps(kwargs)
            await completion([{'role': 'user', 'content': 'Host fixture'}], 'HostAgent')
            await completion([{'role': 'user', 'content': 'App fixture'}], 'AgentType.APP')
            return {'status': 'NOT_VERIFIED', 'evidence': [], 'message': 'fixture'}
        controller.engine.run_task = run_task
        result = await controller.run('Abra aplicativo fixture')
        assert 'respostas sucintas' in json.dumps(brain.calls[0])
        assert 'respostas sucintas' not in json.dumps(brain.calls[1])
        assert result.prompt_calls[0]['memory_context_chars'] > 0
        assert result.prompt_calls[1]['memory_context_chars'] == 0
        assert result.status == 'NOT_VERIFIED'
        await controller.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('raw', ['{}', '[]', '{bad', '{"message":["done"],"action":{"tool":"write"}}',
                               '{"message":["done"],"status":"CONTINUE"}'])
def test_malformed_conversation_not_an_action(raw: str) -> None:
    with pytest.raises(BrainError):
        parse_response(raw)


@pytest.mark.parametrize('changes', [[{'text': 'x'}], 'nao-lista', [{'kind': 'x', 'text': 'y'}],
                                     [{'kind': 'fact', 'text': 'y', 'extra': 1}]])
def test_invalid_memory_metadata_is_fail_soft(changes: object) -> None:
    # Metadado inválido não destrói uma resposta válida: texto preservado, zero memória.
    text, parsed = parse_response(json.dumps({'message': 'fine', 'memory_changes': changes}))
    assert text == 'fine' and parsed == []


def test_conversation_json_fenced_plain_and_explicit_commands(tmp_path: Path) -> None:
    assert parse_response('```json\n{"status":"FINISH","message":["curta"],"action":null}\n```')[0] == 'curta'
    assert parse_response('Resposta simples.')[0] == 'Resposta simples.'
    async def scenario() -> None:
        controller = TaskController(settings(tmp_path))
        for instruction in ['prefiro exemplo breve', 'lembra que uso Python', 'lembre-se de que uso Rust']:
            result = await controller.run(instruction)
            assert result.llm_calls == 0 and result.scope == 'memory_command'
        before = len(controller.memory.list())
        result = await controller.run('esquece uso')
        assert 'ambígua' in result.message and len(controller.memory.list()) == before
        result = await controller.run('lembra que senha: fictícia')
        assert 'recusada' in result.message and len(controller.memory.list()) == before
        controller.memory.set_options(enabled=False)
        assert 'desabilitada' in (await controller.run('prefiro respostas longas')).message
        await controller.close()
    asyncio.run(scenario())


def test_conversation_error_cancel_close_no_worker(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = TaskController(settings(tmp_path))
        brain = FixtureBrain()
        controller.brain = brain
        started = asyncio.Event()
        async def blocked(messages):
            started.set()
            await asyncio.Event().wait()
        brain.complete = blocked
        controller.engine.start = AsyncMock(side_effect=AssertionError('worker start'))
        task = asyncio.create_task(controller.run('explique', conversation=True))
        await started.wait()
        await controller.close()
        result = await task
        assert result.status == 'CANCELLED' and not controller.active
        assert controller.memory.working.get() == {}
    asyncio.run(scenario())


def test_conversation_provider_error_is_honest(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = TaskController(settings(tmp_path))
        brain = FixtureBrain()
        async def fail(messages):
            raise BrainError(ErrorKind.QUOTA_EXCEEDED, 'Fixture quota exhausted')
        brain.complete = fail
        controller.brain = brain
        result = await controller.run('explique', conversation=True)
        assert result.status == 'FAILED' and result.failure_kind == 'PROVIDER_FAILURE'
        assert result.llm_calls == 1 and not result.evidence
        await controller.close()
    asyncio.run(scenario())


def test_turn_window_gives_ram_continuity_independent_of_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = TaskController(settings(tmp_path))
        controller.engine.start = AsyncMock(side_effect=AssertionError('worker must not start'))
        controller.brain = FixtureBrain(json.dumps({'message': 'primeira resposta'}))
        await controller.run('Explique o projeto X', conversation=True)
        controller.brain.reply = json.dumps({'message': 'segunda resposta'})
        await controller.run('Continua', conversation=True)
        joined = json.dumps(controller.brain.calls[1], ensure_ascii=False)
        assert 'Utilizador (turno anterior): Explique o projeto X' in joined
        assert 'Lunmi (turno anterior): primeira resposta' in joined
        assert controller.memory.options()['history'] is False
        # Anexos nunca entram na janela de turnos.
        await controller.run('Analise o anexo', conversation=True,
                             attachments=[{'name': 'a.txt', 'size_bytes': 6, 'text': 'dado.'}])
        assert all('a.txt' not in f'{user}{answer}' for user, answer in
                   controller.turns.pairs(controller.session_id))
        await controller.close()
    asyncio.run(scenario())


def test_auto_learn_off_does_not_create_proposals_from_conversation(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = TaskController(settings(tmp_path))
        controller.engine.start = AsyncMock(side_effect=AssertionError('worker must not start'))
        controller.memory.set_options(auto_learn=False)
        controller.brain = FixtureBrain(json.dumps({'message': 'ok', 'memory_changes': [
            {'kind': 'fact', 'text': 'uso editor Alpha'}]}))
        result = await controller.run('No trabalho uso editor Alpha', conversation=True)
        assert result.status == 'SUCCESS' and not result.memory_error
        assert controller.memory.proposals() == []
        assert controller.memory.profile_snapshot().text == ''
        await controller.close()
    asyncio.run(scenario())

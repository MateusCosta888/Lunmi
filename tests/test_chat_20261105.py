"""Falhas da conversa real de 05/11 às 17:39, com as frases exatas do ditado."""
from __future__ import annotations

import asyncio

import httpx
import pytest

import app.llm as llm
from app.brain.api_brain import ApiBrain
from app.brain.contracts import BrainError
from app.computer import fastpath
from app.core.automatic import allows_actions, direct_web_operation, plain_answer
from app.core.public_text import web_public_text
from app.settings import Settings


@pytest.mark.parametrize('text', ['Como você está se sentindo hoje, agora?', 'tudo bem contigo hoje?',
                                  'Como que está hoje o dia? Como que está funcionando as coisas aí?'])
def test_talking_to_her_is_not_a_search(text):
    assert direct_web_operation(text) is None


@pytest.mark.parametrize('text', ['Qual o presidente atual da Grécia?', 'quem é o papa hoje?',
                                  'qual o preço do bitcoin agora?'])
def test_questions_about_the_world_still_search(text):
    assert direct_web_operation(text)['name'] == 'web.search'


@pytest.mark.parametrize('text', ['Beleza, abre aí a calculadora para mim', 'abre aí a calculadora para mim',
                                  'Abre e a calculadora.', 'ok, abre a calculadora por favor'])
def test_spoken_fillers_keep_the_fast_authorized_route(text):
    assert allows_actions(text) is True
    assert fastpath.resolve(text).label == 'Calculadora'


def test_a_real_second_action_is_not_flattened():
    assert fastpath.resolve('abre e fecha a calculadora') is None


@pytest.mark.parametrize('text', ['Foca o app do Vivaldi.', 'foca o vivaldi', 'focar o bloco de notas'])
def test_focus_is_an_action(text):
    assert allows_actions(text) is True


@pytest.mark.parametrize('text', ['ok cria esse txt nos dowlaods', 'cria um pdf com isso', 'guarda isso num docx'])
def test_a_bare_extension_is_a_file_destination(text):
    # 06/11: "txt" without a dot plus a typo in downloads was refused as read-only.
    assert allows_actions(text) is True


@pytest.mark.parametrize('text', ['escreve um poema', 'explica o que é um ficheiro txt',
                                  'faz um json de exemplo'])
def test_text_in_chat_is_still_not_a_file_request(text):
    assert allows_actions(text) is False


def test_a_bare_source_id_never_reaches_the_chat():
    text = 'São Gigantic e Battleborn.  (Fonte: src\u2011d1c4ffbcdbe3dfe7)'
    assert web_public_text(text) == 'São Gigantic e Battleborn.'
    assert web_public_text('Resposta [src-abc123].') == 'Resposta.'
    assert web_public_text('O ficheiro src-main.py existe.') == 'O ficheiro src-main.py existe.'


def scenario(handler):
    async def run():
        brain = ApiBrain(Settings(provider='groq', model='fixture', api_key='PRIVATE_KEY'))
        await brain._provider.client.aclose()
        brain._provider.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await brain.complete([{'role': 'user', 'content': 'oi'}])
        finally:
            await brain.close()
    return asyncio.run(run())


FAILED = b'{"error":{"message":"PRIVATE","code":"output_parse_failed"}}'


def test_a_model_generation_failure_is_retried_once():
    calls = []

    def handler(_request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(400, content=FAILED)
        return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})

    assert scenario(handler).text == 'ok' and len(calls) == 2


def test_a_repeated_failure_says_to_try_again():
    with pytest.raises(BrainError) as caught:
        scenario(lambda _request: httpx.Response(400, content=FAILED))
    assert caught.value.message == llm.GENERATION_FAILED_MESSAGE


def test_another_400_is_not_retried():
    calls = []

    def handler(_request):
        calls.append(1)
        return httpx.Response(400, content=b'{"error":{"code":"invalid_api_key"}}')

    with pytest.raises(BrainError):
        scenario(handler)
    assert len(calls) == 1


def test_a_retry_after_a_bad_answer_still_waits_out_a_short_limit(monkeypatch):
    """Medido 05/11: output_parse_failed e logo um 429 de 7 s falhavam sem esperar."""
    waits = []

    async def wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(llm.asyncio, 'sleep', wait)
    replies = [httpx.Response(400, content=FAILED), httpx.Response(429, headers={'retry-after': '7'}),
               httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})]
    assert scenario(lambda _request: replies.pop(0)).text == 'ok' and waits == [7.5]


@pytest.mark.parametrize('text,ok', [
    ('A inflação é o aumento generalizado dos preços.', True),
    ('{"kind":"tool"', False),
    ('   ', False),
    ('Abri a calculadora.', False),
])
def test_plain_prose_is_an_answer_but_a_broken_tool_or_a_claimed_action_is_not(text, ok):
    assert plain_answer(text) is ok

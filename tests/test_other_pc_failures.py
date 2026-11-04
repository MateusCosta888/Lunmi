"""Falhas vistas no portátil (i3, 8 GB) em 04/11, que não apareciam no PC de desenvolvimento."""
import asyncio

import pytest
from test_web_reading_manager import _Clock, _custom_manager, _TimedClient

from app.computer import fastpath
from app.core.automatic import AUTO_PROMPT, allows_actions
from app.integrations import web_search as web_search_module


@pytest.mark.parametrize('text', ['me cria uma pasta na área de trabalho', 'Crie uma pasta na Área de Trabalho'])
def test_folder_request_is_authorized_and_needs_no_path(text):
    assert allows_actions(text)
    plan = fastpath.resolve(text, apps=None)
    assert plan.operation == 'mkdir' and plan.paths == ('Desktop/Nova pasta',)


def test_named_folder_keeps_its_name():
    assert fastpath.resolve('me cria uma pasta chamada Fotos na area de trabalho', apps=None).paths == ('Desktop/Fotos',)


def test_me_alone_does_not_authorize_a_question():
    assert not allows_actions('me explica o que é uma pasta')


def test_brain_knows_the_folders_it_must_not_ask_for():
    assert 'Desktop/Name' in AUTO_PROMPT and 'never ask the user for those paths' in AUTO_PROMPT


def test_cold_start_of_a_slow_pc_does_not_spend_the_search_budget(tmp_path, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(web_search_module, '_monotonic', clock.monotonic)
    results = {'results': [{'url': 'https://example.test/a', 'title': 'Maior site de notícias',
                            'snippet': 'Maior site de notícias do mundo. ' * 6}]}
    client = _TimedClient(search=results, batch=lambda: {'result': []}, clock=clock, init_seconds=27.0)
    manager = _custom_manager(tmp_path, 'auto', client)
    result = asyncio.run(manager.run('web.search', {'query': 'maior site de noticias do mundo'}))
    assert result.sources
    asyncio.run(manager.close())


def test_startup_limit_covers_the_slowest_measured_pc():
    assert web_search_module.INIT_TIMEOUT > 26.5

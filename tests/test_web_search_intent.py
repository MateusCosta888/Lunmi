"""Natural-language web regressions from the user's session; no provider or network."""
import pytest

from app.core.automatic import direct_web_operation, web_requested


@pytest.mark.parametrize('text,tool', [
    ('Pesquisa as novidades do React.', 'web.search'),
    ('Vê aí quanto custa um iPhone em Portugal.', 'web.search'),
    ('Qual é a versão mais recente do Python?', 'web.search'),
    ('Qual é a versão mais recente do Windows 11?', 'web.search'),
    ('Abre https://docs.python.org/3/library/asyncio-task.html e explica o conteúdo.', 'web.open'),
    ('Faz uma pesquisa aprofundada sobre modelos locais.', 'web.research'),
    ('O que aconteceu hoje na tecnologia?', 'web.search'),
    ('O Bitcoin está a quanto agora?', 'web.search'),
    ('agora vê aí quando vai ser o próximo dia de votação no Brasil', 'web.search'),
    ('Explica rapidamente o que é o React. Depois diz qual é a sua versão estável mais recente.', 'web.search'),
    ('Pesquisa esse erro do Windows na web.', 'web.search'),
])
def test_explicit_fresh_requests_are_routed_before_the_brain(text, tool):
    assert web_requested(text), text
    operation = direct_web_operation(text)
    assert operation is not None and operation['name'] == tool, operation
    assert set(operation['args']) == ({'url'} if tool == 'web.open' else {'query'})


@pytest.mark.parametrize('text', [
    'Quanto é 15 vezes 8?',
    'Explica o que é uma variável.',
    'Escreve uma mensagem de aniversário.',
    'Não pesquises na web; explica o que é uma variável.',
    'Não uses a pesquisa web para esta resposta.',
    'Como pesquisar na web Python?',
    'Explica a frase "Pesquisa na web Python".',
    '"Pesquisa na web Python"',
    'Pesquisa o ficheiro de notas no computador.',
    'Pesquisa no Spotify Taylor Swift.',
    'Qual é a versão do Windows instalada neste computador?',
    'Pesquisa no Windows o programa Calculadora.',
    'Procura o aplicativo instalado Photoshop.',
])
def test_simple_quoted_negated_or_local_requests_are_not_automatic_web(text):
    assert direct_web_operation(text) is None
    assert not web_requested(text), text


def test_url_with_summary_preserves_only_the_url_as_tool_argument():
    operation = direct_web_operation('Abre https://example.com/path e resume em duas frases.')
    assert operation == {'name': 'web.open', 'args': {'url': 'https://example.com/path'}, 'finish': False}

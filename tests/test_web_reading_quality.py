"""Source-required questions must read real evidence rather than long search snippets."""
import pytest

from app.integrations.web_quality import constraints, rank_sources, select_evidence, useful_content


QUESTIONS=[
    'O leite de barata é realmente mais nutritivo do que o leite de vaca? Quais são os números científicos exatos e o que está a ser comparado?',
    'Porque é que cientistas enterraram 1.000 cuecas em mais de 25 países? Explica a metodologia, os resultados e as limitações da experiência.',
    'Qual é o ângulo ideal para evitar salpicos de urina, segundo a investigação premiada? Explica a física e identifica o estudo original.',
    'Qual é o anime isekai mais bem avaliado no MyAnimeList em outubro de 2026? Confirma a nota e a posição atual.',
]


@pytest.mark.parametrize('query',QUESTIONS)
def test_source_required_questions_cannot_end_in_the_snippet_shortcut(query):
    assert constraints(query).read_required
    assert constraints(query).depth in {'verified','deep'}


def test_methods_results_and_limits_require_deep_evidence():
    assert constraints(QUESTIONS[1]).depth == 'deep'


def test_baki_author_and_simple_definitions_remain_light():
    assert not constraints('quem e o autor do anime baki').read_required
    assert not constraints('O que é um estudo científico?').read_required


def test_scientific_primary_candidate_precedes_social_excerpt():
    query=QUESTIONS[0]
    sources=[
        {'url':'https://www.facebook.com/post/123','title':'Leite de barata realmente mais nutritivo',
         'snippet':query*2},
        {'url':'https://journals.iucr.org/m/issues/2016/04/00/bi5066/',
         'title':'Structure of an in vivo-grown protein crystal',
         'snippet':'Atomic structure, nutrient composition and energy density of Diploptera punctata milk.'},
    ]
    ranked=rank_sources(query,sources,constraints(query))
    assert ranked[0]['url'].startswith('https://journals.iucr.org/')


def test_mal_rankings_do_not_use_personal_interest_stacks_or_social_lists():
    query=QUESTIONS[3]
    sources=[
        {'url':'https://myanimelist.net/stacks/123','title':'2026 BEST ISEKAI','snippet':'My personal favorites.'},
        {'url':'https://myanimelist.net/topanime.php','title':'Top Anime','snippet':'Scores and ranked anime.'},
        {'url':'https://facebook.com/post/12','title':'Highest rated isekai anime','snippet':query},
    ]
    ranked=rank_sources(query,sources,constraints(query))
    assert [s['url'] for s in ranked] == ['https://myanimelist.net/topanime.php']


def test_cookie_gate_is_not_substantive_even_when_it_is_padded():
    assert not useful_content('Enable cookies for pmc.ncbi.nlm.nih.gov and reload this page to continue. '*10)


def test_portuguese_methodology_request_keeps_english_numeric_results():
    doc={'id':'paper','content':(
        'Cookie preferences and site navigation. '*50+'\n\n'
        'Methods: volunteers buried cotton underwear for two months in Swiss soils.\n\n'
        'Results: 1000 volunteers, 2000 cotton items and 12000 tea bags were used.\n\n'
        'Limitations: temperature and moisture influence decomposition; this does not establish a universal soil-health score.')}
    text=select_evidence(QUESTIONS[1],[doc],500)[0]['content']
    assert '12000' in text
    assert 'Methods:' in text
    assert 'Limitations:' in text


def test_chapter_plot_is_not_news_of_the_last_week():
    query=('Em 26 de outubro de 2026, qual é o capítulo mais recente de One Piece, '
           'o que aconteceu nele e quando sai o próximo?')
    policy=constraints(query)
    assert policy.read_required
    assert not policy.freshness
    assert 'category' not in policy.parameters()


def test_actual_current_news_keeps_a_temporal_filter():
    assert constraints('O que aconteceu hoje na tecnologia?').freshness == 'day'


def test_exact_scientific_question_keeps_numeric_comparison_over_generic_method_statistics():
    doc = {'id': 'paper', 'content': (
        'Abstract: milk protein nutrient energy crystal results measured data density. '
        + 'This paragraph explains the scientific background without a numeric comparison. ' * 12
        + '\n\nResults: protein crystal data density measured in the laboratory. '
        + 'Instrument precision and general experimental background. ' * 20
        + '\n\nEnergy comparison: this synthetic milk crystal stores 0.25 J compared with '
        'the equivalent mass of dairy milk; the comparison is not per litre.')}
    selected = select_evidence(QUESTIONS[0], [doc], 650)[0]['content']
    assert '0.25 J' in selected
    assert 'equivalent mass' in selected

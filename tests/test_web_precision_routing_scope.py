"""A generic fixture name is not an authorization or intent to research sports."""
import pytest

from app.core.automatic import direct_web_operation, web_requested
from app.integrations.web_quality import requested_attribute


@pytest.mark.parametrize('instruction',[
    'Abra aplicativo fixture', 'Projeto fixture', 'Minha senha: fixture',
])
def test_generic_fixture_does_not_become_a_web_request(instruction):
    assert requested_attribute(instruction) != 'fixture'
    assert not web_requested(instruction)
    assert direct_web_operation(instruction) is None


def test_explicit_next_fixture_still_requires_web():
    assert requested_attribute('Next fixture of Brazil') == 'fixture'
    assert web_requested('Next fixture of Brazil')


def test_original_bare_manga_followup_keeps_the_subject_without_initial_e(tmp_path):
    import asyncio
    from test_web_search_auto_v1 import _AutoBrain, _answer, _controller, _source, _web

    async def scenario():
        first=_answer()
        first['message']=['O personagem principal de One-Punch Man é Saitama.']
        brain=_AutoBrain(replies=[first,_answer()])
        web=_web(tmp_path,preference='lunmi')
        web.result=_source()
        controller=_controller(tmp_path,brain,web)
        try:
            await controller.run('quem e o personagem principal de one punch man?',conversation=True)
            assert controller._web_followup_context('quantos capitolos tem o manga hoje?') == 'one punch man'
            assert not controller._web_followup_context('quantos capítulos tem o manga One Piece hoje?')
            await controller.run('quantos capitolos tem o manga hoje?',conversation=True)
            assert 'one punch man' in web.calls[0][1]['query'].casefold()
        finally:
            await controller.close()
    asyncio.run(scenario())


def test_conflicting_current_counts_cannot_be_presented_as_request_met(tmp_path):
    import asyncio
    from test_web_search_auto_v1 import _AutoBrain, _answer, _controller, _source, _web

    async def scenario():
        reply=_answer()
        reply['message']=[
            'O mangá tem 246 capítulos. Outra fonte indica 127, '
            'por isso há uma discrepância entre as fontes.']
        brain=_AutoBrain(replies=[reply])
        web=_web(tmp_path,preference='lunmi')
        web.result=_source()
        controller=_controller(tmp_path,brain,web)
        try:
            result=await controller.run('Pesquisa na web os capítulos atuais de One-Punch Man.',conversation=True)
            assert result.status == 'NOT_VERIFIED'
            assert result.request_status != 'met'
            assert result.evidence_status != 'suff'
            assert len(web.calls) == 1
            assert len(brain.complete_calls) == 1
        finally:
            await controller.close()
    asyncio.run(scenario())


def test_unrelated_limitation_does_not_claim_conflicting_evidence():
    from app.core.public_text import web_answer_admits_insufficiency
    assert not web_answer_admits_insufficiency(
        'A documentação explica como resolver conflitos entre processos concorrentes.')

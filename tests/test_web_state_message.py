"""A mensagem pública acompanha a origem efetiva, não só o runtime Free Search."""
from app.brain.web import NativeWebCapability
from app.integrations.web_state import WebPreferences, WebState


def _state(preference, native, *, enabled=True):
    state = WebState(session_id='s')
    state.set_preferences(WebPreferences(enabled=enabled, brain_origins={'brain': preference}))
    return state.bind_brain('brain', native, available=False)


def test_a_ready_native_origin_is_not_reported_as_unavailable():
    snapshot = _state('provider', NativeWebCapability.ready())

    assert snapshot['origin'] == 'provider' and snapshot['native_web']['available'] is True
    assert snapshot['available'] is False
    assert 'indisponível' not in snapshot['message']
    assert 'Free Search' in snapshot['message']


def test_without_any_origin_the_message_stays_unavailable():
    snapshot = _state('lunmi', NativeWebCapability.ready())

    assert snapshot['origin'] == 'none'
    assert snapshot['message'] == 'Pesquisa web indisponível: runtime não instalado.'


def test_switching_the_search_off_does_not_claim_a_ready_origin():
    snapshot = _state('provider', NativeWebCapability.ready(), enabled=False)

    assert snapshot['origin'] == 'none'
    assert snapshot['message'] == 'Pesquisa web indisponível: runtime não instalado.'

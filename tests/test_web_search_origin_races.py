"""Origin changes only affect subsequent tasks; no runtime, provider or network."""
import asyncio

from test_web_search_origin_v1 import _NativeBrain, _controller, _native_source, _web
from app.brain.web import NativeWebResult
from app.integrations.web_search import WebResult, source_id
from app.integrations.web_state import WebPreferences, identity_for_settings


def test_changing_origin_during_native_execution_applies_only_to_next_task(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedNative(_NativeBrain):
        async def complete_web(self, messages):
            entered.set()
            await release.wait()
            return await super().complete_web(messages)

    native = NativeWebResult('Observed response.', 'custom', ('web_search',), (_native_source(),))
    brain = PausedNative(native=native)
    web = _web(tmp_path)
    controller = _controller(tmp_path, brain, web)

    async def scenario():
        try:
            task = asyncio.create_task(controller.run('Pesquisa na web Python asyncio.',
                                                      conversation=True))
            await asyncio.wait_for(entered.wait(), 2)
            identity = identity_for_settings(controller.settings).fingerprint
            web._state.set_preferences(WebPreferences(brain_origins={identity: 'lunmi'}))
            release.set()
            result = await task
            assert result.search_source == 'provider' and result.web_preference == 'provider'
            assert len(brain.web_calls) == 1 and not web.calls
            url = 'https://example.org/public'
            source = dict(id=source_id(url), title='Observed', url=url, snippet='excerpt',
                          engines=[], published_at=None, date_source=None,
                          retrieved_at='2026-10-25T10:00:00Z', read=True,
                          truncated=False, cached=False)
            web.result = WebResult('web.search', sources=[source])
            brain.decision = dict(kind='answer', action=None, status='FINISH',
                                  message=['Observed ['+source['id']+']'], memory_changes=[])
            following = await controller.run('Pesquisa na web Python asyncio.', conversation=True)
            assert following.search_source == 'lunmi' and following.web_preference == 'lunmi'
            assert len(web.calls) == 1 and len(brain.web_calls) == 1
        finally:
            release.set()
            await controller.close()
    asyncio.run(scenario())

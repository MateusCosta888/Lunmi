"""Voice extraction keeps lazy setup, injected managers and shutdown isolation."""
from types import SimpleNamespace

import pytest

from app.bridge import voice_bridge
from app.settings import Settings


@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_setup_keeps_session_callbacks_and_adopts_only_offline(tmp_path, monkeypatch, injected, legacy):
    owner = voice_bridge.VoiceBridge()
    owner.session_id = "voice-session"
    owner.root_dir = tmp_path
    owner.settings = Settings(state_root=tmp_path)
    owner._closed = False
    events = []
    owner._emit = lambda kind, payload: events.append((kind, payload))
    calls = {}
    refreshed = []
    speech = SimpleNamespace(stop=lambda: None, set_input_active=lambda active: None,
                             refresh=lambda: refreshed.append("speech"))
    dictation = SimpleNamespace(refresh=lambda: refreshed.append("dictation"))
    warmup = object()

    def make_speech(**kwargs):
        calls["speech"] = kwargs
        return speech

    def make_dictation(**kwargs):
        calls["dictation"] = kwargs
        return dictation

    def make_warmup(**kwargs):
        calls["warmup"] = kwargs
        return warmup

    downloads = []
    def make_models(root, manifest, **kwargs):
        calls["models"] = (root, manifest, kwargs)
        def download(group, **options):
            downloads.append((group, options))
            kwargs["emit"]({"groups": {"speech": {"state": "installed"}}})
        return SimpleNamespace(download=download)

    monkeypatch.setattr(voice_bridge, "VoiceManager", make_speech)
    monkeypatch.setattr(voice_bridge, "VoiceInputManager", make_dictation)
    monkeypatch.setattr(voice_bridge, "VoiceWarmup", make_warmup)
    monkeypatch.setattr(voice_bridge, "VoiceModels", make_models)
    monkeypatch.setattr(voice_bridge, "voice_home", lambda state_root: state_root / "runtime")
    monkeypatch.setattr(voice_bridge, "manifest_for", lambda home: {"home": str(home)})
    monkeypatch.setattr(voice_bridge, "older_voice_components", lambda: [tmp_path / "old"] if legacy else [])

    owner._init_voice(speech if injected else None, dictation if injected else None)

    assert owner.voice is speech and owner.voice_input is dictation
    assert owner.voice_warmup is warmup
    assert calls["warmup"]["tts"] is speech and calls["warmup"]["stt"] is dictation
    if injected:
        assert "speech" not in calls and "dictation" not in calls
    else:
        assert calls["speech"]["root_dir"] == calls["speech"]["state_root"] == tmp_path
        assert calls["dictation"]["state_root"] == tmp_path
        assert calls["speech"]["session_id"] == calls["dictation"]["session_id"] == "voice-session"
        assert calls["dictation"]["on_speech_interrupt"] is speech.stop
        assert calls["dictation"]["on_active_changed"] is speech.set_input_active
        calls["speech"]["emit"]({"state": "idle"})
        calls["dictation"]["emit"]({"state": "idle"})
        assert [kind for kind, _ in events[-2:]] == ["voice.state", "voice.input.state"]
    assert calls["models"][0] == voice_bridge.models_root(tmp_path)
    assert calls["models"][1] == {"home": str(tmp_path / "runtime")}
    assert downloads == ([(group, {"network": False}) for group in voice_bridge.VOICE_MODEL_GROUPS] if legacy else [])
    assert refreshed == (["speech", "dictation"] if legacy else [])
    calls["warmup"]["emit"]({"state": "ready"})
    assert events[-1] == ("voice.readiness", {"state": "ready", "session_id": "voice-session"})
    before = list(events)
    owner._closed = True
    owner._emit_voice({})
    owner._emit_voice_input({})
    owner._emit_voice_models({})
    owner._emit_voice_warmup({})
    assert events == before


@pytest.mark.parametrize("broken", [None, "warmup", "dictation", "speech"])
def test_close_still_stops_dictation_before_speech_even_if_one_close_fails(broken):
    owner = voice_bridge.VoiceBridge()
    order = []
    def manager(name):
        def close():
            order.append(name)
            if name == broken:
                raise RuntimeError("fake close failure")
        return SimpleNamespace(close=close)
    owner.voice_warmup = manager("warmup")
    owner.voice_input = manager("dictation")
    owner.voice = manager("speech")
    owner._close_voice()
    assert order == ["warmup", "dictation", "speech"]


def test_removing_dictation_closes_the_worker_first_and_never_touches_the_voice():
    owner = voice_bridge.VoiceBridge()
    order = []
    owner.voice_input = SimpleNamespace(release_worker=lambda: order.append("worker"))
    owner.voice_models = SimpleNamespace(remove=lambda group: order.append(group) or {"groups": {}})
    assert owner._handle_voice_models("voice.models.remove", {"group": "dictation"}) == {"groups": {}}
    assert order == ["worker", "dictation"]
    with pytest.raises(voice_bridge.protocol.ProtocolError):
        owner._handle_voice_models("voice.models.remove", {"group": "speech"})
    assert order == ["worker", "dictation"]

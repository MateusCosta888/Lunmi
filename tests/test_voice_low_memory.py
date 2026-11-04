"""Um PC de 8 GB não pode pagar modelos de voz que ninguém está a usar."""
import time

from app.voice import warmup as warmup_module
from app.voice.warmup import VoiceWarmup


class _Leg:
    def __init__(self):
        self.calls = 0

    def warm_up(self, timeout):
        self.calls += 1
        return 'ready', ''

    prepare = warm_up


def _run(skipped):
    tts, stt = _Leg(), _Leg()
    warmup = VoiceWarmup(tts=tts, stt=stt, skip=lambda name: name in skipped)
    warmup.start()
    deadline = time.monotonic() + 5
    while not warmup.snapshot()['ready'] and time.monotonic() < deadline:
        time.sleep(0.01)
    return tts, stt, warmup.snapshot()


def test_skipped_legs_load_nothing_and_do_not_hold_the_loading_screen():
    tts, stt, snapshot = _run({'tts', 'stt'})
    assert snapshot['ready'] and snapshot['message'] == ''
    assert tts.calls == 0 and stt.calls == 0


def test_only_the_wanted_leg_loads():
    tts, stt, snapshot = _run({'tts'})
    assert snapshot['ready'] and tts.calls == 0 and stt.calls == 1


def test_default_still_prepares_both():
    tts, stt, _ = _run(set())
    assert tts.calls == 1 and stt.calls == 1


def test_memory_probe_is_a_plain_answer():
    assert isinstance(warmup_module.low_memory_pc(), bool)


def test_bridge_rule_for_each_leg(monkeypatch):
    from types import SimpleNamespace

    from app.bridge import voice_bridge

    def bridge(enabled):
        return SimpleNamespace(voice=SimpleNamespace(snapshot=lambda: {'enabled': enabled}))

    skip = voice_bridge.VoiceBridge._skip_voice_warmup
    assert skip(bridge(False), 'tts') is True
    assert skip(bridge(True), 'tts') is False
    monkeypatch.setattr(voice_bridge, 'low_memory_pc', lambda: True)
    assert skip(bridge(True), 'stt') is True
    monkeypatch.setattr(voice_bridge, 'low_memory_pc', lambda: False)
    assert skip(bridge(True), 'stt') is False

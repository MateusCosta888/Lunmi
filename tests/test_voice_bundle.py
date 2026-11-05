"""Installed voice resources work without the developer's venv or GPU libraries."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

import pytest

from app.voice.input_runtime import resolve_input_environment
from app.voice.runtime import resolve_environment
from app.voice import input_worker
from app.voice.input_manager import VoiceInputManager
from app.voice.manager import VoiceManager


def make_bundle(root: Path) -> None:
    for name in ('runtime/python.exe', 'models/supertonic-3/tts.json',
                 'models/whisper-large-v3-turbo/model.bin',
                 'models/whisper-large-v3-turbo/config.json',
                 'models/whisper-large-v3-turbo/tokenizer.json',
                 'models/silero/silero_vad.onnx'):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'fixture')


@pytest.mark.parametrize('voice_input', [False, True])
def test_isolated_workers_preserve_unicode_over_real_jsonl_pipes(tmp_path, voice_input):
    value = {'text': 'Não apagues: ação, 你好, 😀.'}
    probe = tmp_path / 'utf8-probe.py'
    probe.write_text('import json, sys\n'
                     'value = json.loads(sys.stdin.readline())\n'
                     f'assert value == {value!r}\n'
                     'sys.stdout.write(json.dumps(dict(value=value, utf8=sys.flags.utf8_mode), ensure_ascii=False))\n', encoding='utf-8')
    if voice_input:
        manager = VoiceInputManager(state_root=tmp_path, session_id='utf8', emit=lambda _: None,
                                    stt_timeout_s=1)
        env = resolve_input_environment(tmp_path, runtime_python=sys.executable, worker_path=probe)
    else:
        manager = VoiceManager(root_dir=tmp_path, state_root=tmp_path, session_id='utf8', emit=lambda _: None)
        env = resolve_environment(tmp_path, runtime_python=sys.executable, worker_path=probe)
    argv = manager._worker_argv(env)
    result = subprocess.run(argv, input=json.dumps(value, ensure_ascii=False).encode('utf-8'),
                            capture_output=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout.decode('utf-8')) == dict(value=value, utf8=1)


def test_installed_bundle_resolves_both_workers_to_portable_cpu_runtime(tmp_path, monkeypatch):
    bundle = tmp_path / 'installed resources' / 'voice'
    make_bundle(bundle)
    monkeypatch.delenv('LUNMI_VOICE_HOME', raising=False)
    monkeypatch.setenv('LUNMI_BUNDLED_VOICE_HOME', str(bundle))
    state = tmp_path / 'user data'
    tts = resolve_environment(state)
    stt = resolve_input_environment(state)
    assert tts.available and stt.available
    assert tts.python == stt.python == bundle / 'runtime/python.exe'
    assert stt.device == 'cpu'
    assert not state.exists()


def test_existing_local_voice_runtime_takes_priority_over_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / 'bundle'
    make_bundle(bundle)
    state = tmp_path / 'state'
    local = state / '.local/voice/runtime/Scripts/python.exe'
    local.parent.mkdir(parents=True)
    local.touch()
    monkeypatch.delenv('LUNMI_VOICE_HOME', raising=False)
    monkeypatch.setenv('LUNMI_BUNDLED_VOICE_HOME', str(bundle))
    assert resolve_environment(state).home == state / '.local/voice'


def test_explicit_voice_override_still_wins(tmp_path, monkeypatch):
    bundle, explicit = tmp_path / 'bundle', tmp_path / 'explicit'
    make_bundle(bundle)
    monkeypatch.setenv('LUNMI_BUNDLED_VOICE_HOME', str(bundle))
    monkeypatch.setenv('LUNMI_VOICE_HOME', str(explicit))
    assert resolve_environment(tmp_path / 'state').home == explicit
    assert not resolve_environment(tmp_path / 'state').available


def test_cpu_mode_is_explicit_and_legacy_default_remains_cuda():
    args = ['--model-dir', 'model', '--vad', 'vad.onnx']
    assert input_worker.parse_args([*args, '--device', 'cpu']).device == 'cpu'
    assert input_worker.parse_args(args).device == 'cuda'


def test_cpu_worker_loads_int8_without_silent_device_fallback(monkeypatch):
    called = []
    monkeypatch.setattr(input_worker, 'load_whisper', lambda *args, **kwargs: called.append(kwargs))
    worker = input_worker.InputWorker(model_dir='model', vad_path='vad', device='cpu')
    worker._load_model()
    assert called == [dict(device='cpu', compute_type='int8_float32', language='pt', beam_size=5)]


def test_cpu_transcription_budget_covers_long_capture_and_explicit_override(tmp_path, monkeypatch):
    bundle = tmp_path / 'bundle'
    make_bundle(bundle)
    monkeypatch.delenv('LUNMI_VOICE_HOME', raising=False)
    monkeypatch.setenv('LUNMI_BUNDLED_VOICE_HOME', str(bundle))
    manager = VoiceInputManager(state_root=tmp_path / 'state', session_id='cpu-test', emit=lambda _: None)
    assert manager.stt_timeout_s == 90
    overridden = VoiceInputManager(state_root=tmp_path / 'other', session_id='test', emit=lambda _: None,
                                   stt_timeout_s=0.25)
    assert overridden.stt_timeout_s == 0.25


def test_voice_runtime_pruning_preserves_code_testing_api_and_licenses(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'desktop/scripts'))
    from fetch_voice import prune_voice_runtime
    files = ('Lib/site-packages/numpy/tests/test_array.py',
             'Lib/site-packages/scipy/signal/tests/test_signal.py',
             'Lib/site-packages/numpy/testing/__init__.py',
             'Lib/site-packages/numpy/core.py',
             'Lib/site-packages/numpy-1.dist-info/licenses/LICENSE.txt',
             'Lib/site-packages/pip/__init__.py',
             'Lib/site-packages/setuptools/__init__.py',
             'Lib/site-packages/distutils-precedence.pth')
    for name in files:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('fixture')
    prune_voice_runtime(tmp_path)
    for name in files:
        expected = '/testing/' in name or name.endswith('core.py') or '/licenses/' in name
        assert (tmp_path / name).exists() == expected, name


def test_product_runtime_does_not_ship_build_path_bytecode(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'desktop/scripts'))
    import prepare_runtime
    (tmp_path / 'python.exe').touch()
    cached = tmp_path / 'Lib/site-packages/module/__pycache__/module.cpython-311.pyc'
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b'private build filename')
    source = cached.parent.parent / 'module.py'
    source.write_text('value = 1\n')
    monkeypatch.setattr(prepare_runtime, 'RUNTIME', tmp_path)
    assert prepare_runtime.main() == 0
    assert not cached.parent.exists()
    assert source.read_text() == 'value = 1\n'


def test_packaged_voice_assets_reject_same_size_corruption_and_changed_manifest(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'desktop/scripts'))
    import verify_package
    repo = tmp_path / 'repo'
    source = repo / 'desktop/resources/VOICE-SOURCE.json'
    source.parent.mkdir(parents=True)
    bundle = tmp_path / 'voice'
    bundle.mkdir()
    asset = bundle / 'model.bin'
    asset.write_bytes(b'valid')
    pin = {'device': 'cpu', 'files': [dict(path='model.bin', bytes=5,
                                          sha256=hashlib.sha256(b'valid').hexdigest())]}
    source.write_text(json.dumps(pin))
    manifest = bundle / 'VOICE-SOURCE.json'
    manifest.write_text(json.dumps(pin))
    monkeypatch.setattr(verify_package, 'REPO', repo)
    assert verify_package._voice_assets_valid(bundle)
    asset.write_bytes(b'wrong')
    assert not verify_package._voice_assets_valid(bundle)
    asset.write_bytes(b'valid')
    manifest.write_text(json.dumps({**pin, 'device': 'cuda'}))
    assert not verify_package._voice_assets_valid(bundle)

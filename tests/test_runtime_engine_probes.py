"""Engine packaging probes never inherit credentials or open personal audio devices."""
import importlib.util
from pathlib import Path
import subprocess


def load_probe():
    path = Path(__file__).resolve().parents[1] / 'desktop/scripts/verify_runtime_engines.py'
    spec = importlib.util.spec_from_file_location('verify_runtime_engines', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_engine_probe_environment_is_private_and_offline(tmp_path, monkeypatch):
    module = load_probe()
    monkeypatch.setenv('GROQ_API_KEY', 'fixture-not-a-real-key')
    monkeypatch.setenv('LUNMI_VOICE_HOME', str(tmp_path / 'personal'))
    monkeypatch.setenv('LUNMI_WEB_HOME', str(tmp_path / 'personal-web'))
    env = module.probe_environment(tmp_path / 'bundle/python.exe', tmp_path / 'qa')
    assert not {'GROQ_API_KEY', 'LUNMI_VOICE_HOME', 'LUNMI_WEB_HOME'} & env.keys()
    assert env['HF_HUB_OFFLINE'] == '1'
    assert env['LUNMI_BUNDLED_WEB_HOME'] == str(tmp_path / 'bundle')
    assert env['HOME'] == env['TEMP'] == str(tmp_path / 'qa')


def test_engine_probe_records_timeout_as_failure(tmp_path, monkeypatch):
    module = load_probe()

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('owned-probe', 180)

    monkeypatch.setattr(module.subprocess, 'run', timeout)
    result = module.run_probe('voice', tmp_path / 'python.exe', tmp_path, tmp_path / 'data', tmp_path)
    assert result['ok'] is False
    assert result['error'] == 'TimeoutExpired'

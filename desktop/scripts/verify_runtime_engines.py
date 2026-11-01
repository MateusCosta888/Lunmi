"""Exercise shipped engines without audio playback, microphone, provider or web requests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

VOICE_PROBE = r'''
import importlib.util, json, sys
from pathlib import Path
def load_worker(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(sys.argv[1]) / 'app/voice' / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
tts = load_worker('voice_output_probe', 'worker_main.py')
stt = load_worker('voice_input_probe', 'input_worker.py')
bundle = Path(sys.argv[2])
worker = tts.VoiceWorker(model_dir=str(bundle / 'models/supertonic-3'), total_steps=2)
assert worker.preload(), 'native imports failed'
worker.warm_up()
assert worker._warmed_up, 'silent synthesis failed'
model = stt.load_whisper(bundle / 'models/whisper-large-v3-turbo', device='cpu', compute_type='int8_float32')
vad = stt.load_silero(bundle / 'models/silero/silero_vad.onnx')
# A developer/CI system-wide CRT must not hide missing dynamically loaded libraries.
import ctypes
import re
from ctypes import wintypes
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
psapi = ctypes.WinDLL('psapi', use_last_error=True)
kernel.GetCurrentProcess.restype = wintypes.HANDLE
kernel.GetModuleFileNameW.argtypes = (wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD)
kernel.GetModuleFileNameW.restype = wintypes.DWORD
psapi.EnumProcessModules.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE),
                                   wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
modules = (wintypes.HMODULE * 2048)()
needed = wintypes.DWORD()
assert psapi.EnumProcessModules(kernel.GetCurrentProcess(), modules, ctypes.sizeof(modules), ctypes.byref(needed))
assert needed.value <= ctypes.sizeof(modules)
loaded = []
for handle in modules[:needed.value // ctypes.sizeof(wintypes.HMODULE)]:
    buffer = ctypes.create_unicode_buffer(32768)
    assert kernel.GetModuleFileNameW(handle, buffer, len(buffer))
    path = Path(buffer.value).resolve()
    if re.match(r'^(?:(?:msvcp|vcruntime|concrt|vccorlib|vcomp)\d|libiomp)', path.name.lower()):
        assert path.is_relative_to((bundle / 'runtime').resolve()), 'external native runtime: ' + path.name
        loaded.append(path.relative_to(bundle).as_posix())
assert any('msvcp140.dll' in path.lower() for path in loaded)
assert any('libiomp5md.dll' in path.lower() for path in loaded)
print(json.dumps({'probe_ok': True, 'tts_synthesized': True, 'stt_loaded': True,
                  'vad_loaded': True, 'native_provenance_ok': True, 'loaded_runtime_dlls': loaded}))
'''

WEB_PROBE = r'''
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app.integrations.web_search import MCPWebClient
from app.integrations.web_state import resolve_environment
async def check():
    environment = resolve_environment(Path(sys.argv[2]))
    assert environment.available, environment.reason
    client = MCPWebClient(environment)
    try:
        await client.start()
        assert client.started
        print(json.dumps({'probe_ok': True, 'mcp_initialized': True}))
    finally:
        await client.close()
asyncio.run(check())
'''


def probe_environment(python: Path, data: Path) -> dict[str, str]:
    data.mkdir(parents=True, exist_ok=True)
    env = {name: os.environ[name] for name in ('SYSTEMROOT', 'WINDIR') if name in os.environ}
    for name in ('USERPROFILE', 'HOME', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP'):
        env[name] = str(data)
    env.update(HF_HUB_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
               LUNMI_BUNDLED_WEB_HOME=str(python.parent), PYTHONDONTWRITEBYTECODE='1')
    system = Path(env.get('SYSTEMROOT', r'C:\Windows')) / 'System32'
    env['PATH'] = os.pathsep.join((str(python.parent), str(system)))
    return env


def run_probe(kind: str, python: Path, app_root: Path, data: Path,
              voice: Path | None = None) -> dict[str, object]:
    code = VOICE_PROBE if kind == 'voice' else WEB_PROBE
    argument = voice if kind == 'voice' else data
    try:
        result = subprocess.run([str(python), '-I', '-B', '-X', 'utf8', '-c', code,
                                 str(app_root), str(argument)],
                                env=probe_environment(python, data), cwd=data,
                                capture_output=True, timeout=180, text=True, encoding='utf-8')
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'ok': False, 'rc': None, 'messages': [], 'error': type(exc).__name__}
    messages = []
    for line in result.stdout.splitlines():
        try:
            message = json.loads(line)
            if isinstance(message, dict):
                messages.append(message)
        except ValueError:
            pass
    return {'ok': result.returncode == 0 and any(m.get('probe_ok') is True for m in messages),
            'rc': result.returncode, 'messages': messages, 'stderr': result.stderr[-4000:]}

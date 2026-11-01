"""Clean installations must find isolated web dependencies and keep cache writable."""
from pathlib import Path
import os
import subprocess
import sys

import pytest

from app.integrations.web_state import resolve_environment
from app.integrations.web_search import _worker_environment


def make_bundle(root: Path) -> None:
    (root / 'web-packages').mkdir(parents=True)
    (root / 'python.exe').touch()
    (root / 'web-packages/search_mcp').mkdir()


def test_clean_install_uses_bundled_python_with_separate_web_packages(tmp_path, monkeypatch):
    bundle = tmp_path / 'installed Python'
    make_bundle(bundle)
    monkeypatch.delenv('LUNMI_WEB_HOME', raising=False)
    monkeypatch.setenv('LUNMI_BUNDLED_WEB_HOME', str(bundle))
    state = tmp_path / 'dados ç'
    env = resolve_environment(state)
    assert env.available
    assert env.python == bundle / 'python.exe'
    assert env.packages == bundle / 'web-packages'
    assert env.home == state / '.local/web-search'
    assert not state.exists()
    child, work = _worker_environment(env)
    assert work.is_relative_to(state)
    assert child['LUNMI_WEB_PACKAGES'] == str(bundle / 'web-packages')
    assert child['SEARCH_MCP_CACHE_DIR'].startswith(str(state))
    assert not (bundle / 'cache').exists()


@pytest.mark.parametrize('explicit', [False, True])
def test_local_or_explicit_web_runtime_keeps_priority(tmp_path, monkeypatch, explicit):
    bundle = tmp_path / 'bundle'
    make_bundle(bundle)
    state = tmp_path / 'state'
    home = tmp_path / 'explicit' if explicit else state / '.local/web-search'
    python = home / 'runtime/Scripts/python.exe'
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.delenv('LUNMI_WEB_HOME', raising=False)
    if explicit:
        monkeypatch.setenv('LUNMI_WEB_HOME', str(home))
    monkeypatch.setenv('LUNMI_BUNDLED_WEB_HOME', str(bundle))
    env = resolve_environment(state)
    assert env.available
    assert env.python == python
    assert env.packages is None


def test_explicit_missing_runtime_does_not_silently_switch_to_bundle(tmp_path, monkeypatch):
    bundle = tmp_path / 'bundle'
    make_bundle(bundle)
    monkeypatch.setenv('LUNMI_WEB_HOME', str(tmp_path / 'missing'))
    monkeypatch.setenv('LUNMI_BUNDLED_WEB_HOME', str(bundle))
    assert not resolve_environment(tmp_path / 'state').available


def test_incomplete_bundle_is_unavailable(tmp_path, monkeypatch):
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    (bundle / 'python.exe').touch()
    monkeypatch.delenv('LUNMI_WEB_HOME', raising=False)
    monkeypatch.setenv('LUNMI_BUNDLED_WEB_HOME', str(bundle))
    assert not resolve_environment(tmp_path / 'state').available


def test_relative_override_is_ignored_as_in_development(tmp_path, monkeypatch):
    bundle = tmp_path / 'bundle'
    make_bundle(bundle)
    monkeypatch.setenv('LUNMI_WEB_HOME', 'relative-home')
    monkeypatch.setenv('LUNMI_BUNDLED_WEB_HOME', str(bundle))
    env = resolve_environment(tmp_path / 'state')
    assert env.available
    assert env.python == bundle / 'python.exe'


def test_web_bundle_cleanup_removes_agent_docs_and_preserves_runtime_and_licenses(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'desktop/scripts'))
    from fetch_web import prune_web
    for name in ('typer/.agents/skills/README.md', 'playwright/driver/node.exe',
                 'typer/__init__.py', 'typer-1.dist-info/licenses/LICENSE'):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'fixture')
    prune_web(tmp_path)
    assert not (tmp_path / 'typer/.agents').exists()
    assert not (tmp_path / 'playwright/driver').exists()
    assert (tmp_path / 'typer/__init__.py').read_bytes() == b'fixture'
    assert (tmp_path / 'typer-1.dist-info/licenses/LICENSE').read_bytes() == b'fixture'


def test_bundled_worker_bootstrap_excludes_backend_and_user_site_packages(tmp_path):
    packages = tmp_path / 'web-packages'
    packages.mkdir()
    worker = Path(__file__).resolve().parents[1] / 'app/integrations/web_worker.py'
    code = ('import runpy, sys; runpy.run_path(sys.argv[1], run_name="probe"); '
            'assert sys.flags.no_site; assert sys.argv[2] in sys.path; '
            'assert not any("site-packages" in p for p in sys.path)')
    env = {**os.environ, 'LUNMI_WEB_PACKAGES': str(packages)}
    result = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', code, str(worker), str(packages)],
                            env=env, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    # A bundled worker must reject being started with the backend's site enabled.
    wrong_mode = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(worker), str(packages)],
                                env=env, capture_output=True, timeout=10)
    assert wrong_mode.returncode != 0
    assert b'invalid_web_packages' in wrong_mode.stderr

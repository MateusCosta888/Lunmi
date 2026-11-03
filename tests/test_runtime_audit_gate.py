"""The reviewed exceptions never mask new advisories or extend to voice/web."""
from datetime import date
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def gate():
    path = Path(__file__).resolve().parents[1] / "desktop/scripts/audit_runtime.py"
    spec = importlib.util.spec_from_file_location("runtime_audit_gate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def set_today(gate, monkeypatch, today):
    class Clock(date):
        @classmethod
        def today(cls):
            return today
    monkeypatch.setattr(gate, "date", Clock)


def test_expired_review_blocks_before_running_any_audit(gate, monkeypatch):
    set_today(gate, monkeypatch, date(2026, 12, 2))
    monkeypatch.setattr(gate.subprocess, "run", lambda *_a, **_k: pytest.fail("expired review ran audit"))
    with pytest.raises(SystemExit, match="review expired"):
        gate.main()


def test_review_boundary_audits_every_lock_with_only_four_runtime_exceptions(gate, monkeypatch):
    set_today(gate, monkeypatch, date(2026, 12, 1))
    calls = []
    monkeypatch.setattr(gate.subprocess, "run", lambda args, **_k: calls.append(args) or SimpleNamespace(returncode=0))
    assert gate.main() == 0
    assert [Path(args[args.index("-r") + 1]).name for args in calls] == [
        "requirements-runtime.lock", "requirements-voice.lock", "requirements-web.lock"]
    assert calls[0][calls[0].index("--ignore-vuln"):] == [
        "--ignore-vuln", "PYSEC-2026-2192", "--ignore-vuln", "PYSEC-2026-2193",
        "--ignore-vuln", "PYSEC-2026-2562", "--ignore-vuln", "PYSEC-2026-77"]
    assert all("--ignore-vuln" not in args for args in calls[1:])


@pytest.mark.parametrize("failure_at", [0, 1, 2])
def test_any_audit_failure_blocks_and_stops_the_gate(gate, monkeypatch, failure_at):
    set_today(gate, monkeypatch, date(2026, 11, 3))
    calls = []
    def run(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=2 if len(calls) == failure_at + 1 else 0)
    monkeypatch.setattr(gate.subprocess, "run", run)
    assert gate.main() == 2
    assert len(calls) == failure_at + 1

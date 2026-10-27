"""D11: repair roda o par e recupera-o; remove revoga depois de durável."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    "i1_world", Path(__file__).resolve().parent / "test_spotify_install.py")
assert SPEC and SPEC.loader
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)

SECRET = fixture.SECRET
make_world = fixture.make_world


def test_repair_rotates_and_remove_revokes_the_pairing(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    events: list[tuple] = []
    world.broker.rotate_pairing = lambda: events.append(("rotate",)) or SECRET  # type: ignore[attr-defined]
    world.broker.restore_pairing = lambda previous: events.append(("restore", previous))  # type: ignore[attr-defined]
    world.broker.revoke_pairing = lambda: events.append(("revoke",))  # type: ignore[attr-defined]

    world.installer.apply(world.installer.plan("install")["consent"]["consent_id"])
    assert world.wait_job()["status"] == "done"
    assert ("rotate",) not in events

    repair = world.installer.plan("repair")
    world.installer.apply(repair["consent"]["consent_id"])
    assert world.wait_job()["status"] == "done"
    assert ("rotate",) in events
    assert not any(event[0] == "restore" for event in events)

    remove = world.installer.plan("remove")
    world.installer.apply(remove["consent"]["consent_id"])
    assert world.wait_job()["status"] == "done"
    assert ("revoke",) in events


def test_failed_repair_restores_the_previous_pairing(tmp_path: Path) -> None:
    world = make_world(tmp_path)
    events: list[tuple] = []
    world.broker.rotate_pairing = lambda: events.append(("rotate",)) or SECRET  # type: ignore[attr-defined]
    world.broker.restore_pairing = lambda previous: events.append(("restore", previous))  # type: ignore[attr-defined]
    world.installer.apply(world.installer.plan("install")["consent"]["consent_id"])
    assert world.wait_job()["status"] == "done"

    world.broker._verify = False  # type: ignore[attr-defined]
    repair = world.installer.plan("repair")
    world.installer.apply(repair["consent"]["consent_id"])
    job = world.wait_job()
    assert job["status"] == "failed"
    assert ("rotate",) in events
    assert ("restore", SECRET) in events
    assert world.installer.state()["connection"] != "connected"

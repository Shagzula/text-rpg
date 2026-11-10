"""Regression tests for the 2026-11-10 M1 playtest report (playtest/2026-11-10-m1-bugs.md)."""
import pytest

from rpg import db
from rpg import inventory as inv
from test_actions import Script, act, run, world  # noqa: F401  (fixture)
from test_cli import location, play


def unlock_cellar(path):
    with db.connect(path) as conn:
        conn.execute("UPDATE location_exits SET locked = 0")


def exit_locked(path, src, dst):
    with db.connect(path) as conn:
        return conn.execute("""SELECT e.locked FROM location_exits e JOIN locations a ON a.id = e.from_location_id
                               JOIN locations b ON b.id = e.to_location_id WHERE a.key = ? AND b.key = ?""",
                            (src, dst)).fetchone()[0]


# BUG-01: cannot leave the cellar with `up` ------------------------------------------------------

@pytest.mark.parametrize("cmd", ["up", "go up", "u", "climb up", "head up", "walk up", "back", "go back"])
def test_bug01_cellar_egress_by_direction(tmp_path, cmd):
    g, _ = play(tmp_path, "/quit", new=True)
    unlock_cellar(g.path)
    g, _ = play(tmp_path, "go east", "down", "/quit")
    assert location(g) == "loc_cellar"
    assert exit_locked(g.path, "loc_cellar", "loc_storeroom") == 0
    g, out = play(tmp_path, cmd, "/quit")
    assert location(g) == "loc_storeroom", out


def test_bug01_engine_accepts_direction_and_alias_targets(world):
    world["conn"].execute("UPDATE location_exits SET locked = 0")
    [r] = run(world, act("move", target="d"))["results"]
    assert r["status"] == "resolved" and r["state_changes"][0]["key"] == "loc_cellar"
    [r] = run(world, act("move", target="up"))["results"]
    assert r["state_changes"][0]["key"] == "loc_storeroom"
    [r] = run(world, act("move", target="west"))["results"]
    [r] = run(world, act("move", target="back"))["results"]
    assert r["state_changes"][0]["key"] == "loc_storeroom"
    [r] = run(world, act("move", target="north"))["results"]
    assert (r["reason"], r["detail"]) == ("target_not_present", "no_exit")

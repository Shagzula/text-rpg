"""Tests for the starter-compatible API in rpg_engine.py, incl. the two starter bugs."""
import json
import random

import rpg_engine
from rpg import db


def setup(tmp_path):
    path = db.init_db(tmp_path / "e.db")
    with db.connect(path) as conn:
        cid = rpg_engine._seed_demo(conn)
    return path, cid


def door(last_ditch=True):
    return rpg_engine._action("skill_check", "Force the door.", skill="athletics", target="loc_demo_cellar",
                              last_ditch=last_ditch)


class Fixed(random.Random):
    def __init__(self, v):
        super().__init__(0)
        self.v = v

    def randint(self, a, b):
        return self.v


def test_failed_last_ditch_applies_and_returns_injury(tmp_path):  # starter bug (a)
    path, cid = setup(tmp_path)
    with db.connect(path) as conn:
        r = rpg_engine.process_action_proposal(conn, cid, door(), rng=Fixed(2))
    [res] = r["results"]
    assert res["outcome"] == "failure" and res["last_ditch"]
    assert res["consequences"][0]["name"] == "injured_str"
    assert r["player_status"]["conditions"] == ["injured_str"]
    with db.connect(path) as conn:
        rows = conn.execute("SELECT name, lasting, effect FROM character_conditions WHERE character_id=?",
                            (cid,)).fetchall()
        assert [(x["name"], x["lasting"], json.loads(x["effect"])) for x in rows] == \
            [("injured_str", 1, {"abilities": {"str": -2}})]
        assert rpg_engine.get_skill_modifier(conn, cid, "athletics") == 2 - 2  # injury affects later checks


def test_legacy_resolve_skill_check_wrapper(tmp_path):
    path, cid = setup(tmp_path)
    r = rpg_engine.resolve_skill_check(cid, "athletics", "str", 18, True, db_path=path, roller=lambda: 15)
    assert r["success"] and r["injury_applied"]["name"] == "strained_str"
    r = rpg_engine.resolve_skill_check(cid, "athletics", None, 10, db_path=path)
    assert r["auto_success"]  # starter bug: an easy climb used to be "impossible"


def test_db_path_param_not_cwd(tmp_path, monkeypatch):  # starter bug (b)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "custom" / "s.db"
    rpg_engine.demo(db_path=target, seed=1)
    assert target.exists() and not (tmp_path / "rpg_state.db").exists()


def test_demo_runs_on_throwaway_db(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("RPG_DB_PATH", str(tmp_path / "real.db"))
    rpg_engine.demo(seed=7)
    out = capsys.readouterr().out
    assert "throwaway" in out and "Last-ditch door" in out and "Context block" in out
    assert not (tmp_path / "real.db").exists()


def test_demo_reuses_character(tmp_path, capsys):
    p = tmp_path / "d.db"
    rpg_engine.demo(db_path=p, seed=1)
    rpg_engine.demo(db_path=p, seed=2)
    with db.connect(p) as conn:
        assert conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 3

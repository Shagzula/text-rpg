"""Tests for the starter-compatible API in rpg_engine.py, incl. the two fixed bugs."""
import json

import rpg_engine
from rpg import db


def setup(tmp_path, **kw):
    path = db.init_db(tmp_path / "e.db")
    with db.connect(path) as conn:
        cid = db.create_character(conn, "Rogue", level=3, scores={"str": 10, "dex": 16}, max_hp=24,
                                  skills={"athletics": "proficient"}, **kw)
    return path, cid


def door(dc, last_ditch=True):
    return {"action_type": "skill_check", "skill": "athletics", "ability": "str", "proposed_dc": dc,
            "is_last_ditch": last_ditch, "success_outcome": "You force the door open",
            "failure_outcome": "You hurt yourself and fail"}


def test_failed_last_ditch_applies_and_returns_injury(tmp_path):
    path, cid = setup(tmp_path)
    r = rpg_engine.process_action_proposal(cid, door(18), db_path=path, roller=lambda: 2)
    assert not r["success"] and r["last_ditch"]
    assert r["injury_applied"] is not None and r["injury_applied"]["name"] == "injured_str"
    assert "You hurt yourself and fail" in r["narrative_constraints"]["must_mention"]
    assert r["injury_applied"]["description"] in r["narrative_constraints"]["must_mention"]
    with db.connect(path) as conn:
        rows = conn.execute("SELECT name, lasting, effect FROM character_conditions WHERE character_id=?", (cid,)).fetchall()
    assert [(x["name"], x["lasting"], json.loads(x["effect"])) for x in rows] == \
        [("injured_str", 1, {"abilities": {"str": -2}})]
    # the injury now affects later checks
    with db.connect(path) as conn:
        assert rpg_engine.get_skill_modifier(conn, cid, "athletics") == 2 - 2


def test_successful_last_ditch_persists_strain(tmp_path):
    path, cid = setup(tmp_path)
    r = rpg_engine.resolve_skill_check(cid, "athletics", "str", 18, True, db_path=path, roller=lambda: 15)
    assert r["success"] and r["injury_applied"]["name"] == "strained_str"


def test_no_injury_hint_when_none_applied(tmp_path):
    path, cid = setup(tmp_path)
    r = rpg_engine.process_action_proposal(cid, door(21, last_ditch=False), db_path=path, roller=lambda: 20)
    assert r["impossible"] and r["injury_applied"] is None
    assert r["narrative_constraints"]["must_mention"] == ["You hurt yourself and fail"]


def test_easy_climb_auto_succeeds(tmp_path):  # starter bug: was reported "impossible"
    path, cid = setup(tmp_path)
    r = rpg_engine.process_action_proposal(cid, door(10, last_ditch=False), db_path=path)
    assert r["success"] and r["auto_success"]


def test_db_path_param_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "custom" / "s.db"
    rpg_engine.demo(db_path=target, seed=1)
    assert target.exists() and not (tmp_path / "rpg_state.db").exists()


def test_demo_runs(tmp_path, capsys):
    rpg_engine.demo(db_path=tmp_path / "d.db", seed=7)
    out = capsys.readouterr().out
    assert "Easy climb" in out and "Last-ditch door" in out

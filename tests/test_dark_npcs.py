"""v0.7.1: darkness never names an unseen NPC (context, results, mock narration)."""
import json

import pytest

from rpg import actions, db, llm as llm_mod, personas, talk, unseen
from rpg.context import build_context
from test_retest import demo, go, hostile_guard, say  # noqa: F401

GUARD_SOUND = "slow, heavy breathing and the creak of leather"


def npc_id(conn, key):
    return conn.execute("SELECT id FROM npcs WHERE key = ?", (key,)).fetchone()[0]


def put(conn, key, where):
    conn.execute("UPDATE npcs SET location_id = (SELECT id FROM locations WHERE key = ?) WHERE key = ?", (where, key))


def leaks(blob, conn, *keys):
    s = json.dumps(blob) if not isinstance(blob, str) else blob
    for k in keys:
        name = conn.execute("SELECT name FROM npcs WHERE key = ?", (k,)).fetchone()[0]
        for bad in (k, name, name.split()[-1]):
            if bad.lower() in s.lower():
                return bad
    return None


@pytest.fixture
def dark_guard(demo):  # noqa: F811
    conn, cid = demo
    put(conn, "npc_dozing_guard", "loc_cellar")
    go(demo, "loc_cellar")
    return demo


def test_dark_context_has_only_a_sound_and_a_stable_ref(dark_guard):
    conn, cid = dark_guard
    ctx = build_context(conn, cid)
    ref = f"npc_unseen_{npc_id(conn, 'npc_dozing_guard')}"
    assert ctx["npcs_present"] == [{"key": ref, "visible": False, "sound": GUARD_SOUND}]
    assert build_context(conn, cid)["npcs_present"][0]["key"] == ref
    assert leaks(ctx, conn, "npc_dozing_guard") is None


def test_dark_look_renders_the_persona_sound(dark_guard):
    r, out = say(dark_guard, "look")
    assert f"Close by in the dark you hear {GUARD_SOUND}." in out
    assert leaks(out, dark_guard[0], "npc_dozing_guard") is None


def test_missing_dark_sound_falls_back_to_a_generic_line(dark_guard):
    conn, cid = dark_guard
    nid = npc_id(conn, "npc_dozing_guard")
    p = personas.get(conn, nid)
    p.pop("dark_sound")
    conn.execute("UPDATE npc_personas SET persona = ? WHERE npc_id = ?", (json.dumps(p), nid))
    assert build_context(conn, cid)["npcs_present"][0]["sound"] == unseen.GENERIC_SOUND
    _, out = say(dark_guard, "look")
    assert "Something breathes nearby in the dark." in out


def test_demo_personas_have_dark_sounds(demo):  # noqa: F811
    conn = demo[0]
    for k in ("npc_mara", "npc_dozing_guard"):
        assert personas.get(conn, npc_id(conn, k))["dark_sound"]


def test_dark_sound_is_validated():
    base = {"key": "npc_x", "name": "X", "voice": {"style": "s", "sample_phrases": []}, "traits": [],
            "goals": [], "base_disposition": 0, "knowledge": {},
            "reveal_by_band": {b: [] for b in personas.BANDS}}
    personas.validate(dict(base), [])
    for bad in ("", 3, ["a"]):
        with pytest.raises(personas.PersonaError, match="dark_sound"):
            personas.validate(dict(base, dark_sound=bad), [])


def test_defeated_npcs_in_the_dark_are_not_noticed(dark_guard):
    conn, cid = dark_guard
    conn.execute("UPDATE npcs SET alive = 0 WHERE key = 'npc_dozing_guard'")
    assert build_context(conn, cid)["npcs_present"] == []


def test_pursuit_into_the_dark_names_nobody(demo):  # noqa: F811
    conn = demo[0]
    go(demo, "loc_storeroom")
    hostile_guard(demo)
    conn.execute("UPDATE location_exits SET locked = 0")
    r, out = say(demo, "go down")
    assert any(a["action"] == "follow" for a in r["npc_actions"])
    assert r["npc_actions"][0]["npc"].startswith("npc_unseen_")
    assert leaks(r, conn, "npc_dozing_guard") is None and leaks(out, conn, "npc_dozing_guard") is None
    assert f"in the dark you hear {GUARD_SOUND}" in out


@pytest.mark.parametrize("text", ["attack it", "attack the breathing", "attack the sound"])
def test_attacking_the_unseen_by_sound_hits_the_npc(dark_guard, text):
    conn, cid = dark_guard
    hostile_guard(dark_guard)
    conn.execute("UPDATE npcs SET current_hp = max_hp WHERE key = 'npc_dozing_guard'")
    hp = conn.execute("SELECT current_hp FROM npcs WHERE key = 'npc_dozing_guard'").fetchone()[0]
    r, out = say(dark_guard, text, 20, 6, 1, 1, 1)
    res = r["results"][0]
    assert res["status"] == "resolved" and res["target"]["key"].startswith("npc_unseen_")
    assert conn.execute("SELECT current_hp FROM npcs WHERE key = 'npc_dozing_guard'").fetchone()[0] < hp
    assert leaks(r, conn, "npc_dozing_guard") is None and leaks(out, conn, "npc_dozing_guard") is None


def test_named_target_in_the_dark_is_not_resolved_by_name_in_the_mock(dark_guard):
    p = llm_mod.MockLLM().interpret("attack the guard", build_context(*dark_guard))
    assert not p["actions"][0]["target"].startswith("npc_unseen_")  # the name isn't known in the dark


def test_talking_in_the_dark_voices_without_a_name(demo):  # noqa: F811
    conn, cid = demo
    go(demo, "loc_cellar")
    put(conn, "npc_mara", "loc_cellar")
    llm = llm_mod.MockLLM()
    ctx = build_context(conn, cid)
    p = llm.interpret("ask about the cellar", ctx)
    r = actions.process_action_proposal(conn, cid, p, rng=__import__("random").Random(1))
    assert r["results"][0]["status"] == "resolved", r
    talk.voice(conn, llm, r, "ask about the cellar", ctx)
    assert r["dialogue"] and all(d["speaker_name"] == unseen.VOICE_NAME for d in r["dialogue"])
    out = llm_mod.narrate_safely(llm, r, build_context(conn, cid))
    assert leaks(r, conn, "npc_mara") is None and leaks(out, conn, "npc_mara") is None


def test_lit_rooms_still_name_npcs(demo):  # noqa: F811
    go(demo, "loc_storeroom")
    ctx = build_context(*demo)
    assert {n["name"] for n in ctx["npcs_present"]} >= {"Dozing Guard"}
    assert unseen.mask({"npc": "npc_x", "name": "X"}, {}) == {"npc": "npc_x", "name": "X"}


def test_two_unseen_npcs_are_told_apart_by_sound(dark_guard):
    conn, cid = dark_guard
    put(conn, "npc_mara", "loc_cellar")
    ctx = build_context(conn, cid)
    guard_ref = f"npc_unseen_{npc_id(conn, 'npc_dozing_guard')}"
    mara_ref = f"npc_unseen_{npc_id(conn, 'npc_mara')}"
    m = llm_mod.MockLLM()
    assert m.interpret("attack the breathing", ctx)["actions"][0]["target"] == guard_ref
    assert m.interpret("talk to the muttering apron", ctx)["actions"][0]["target"] == mara_ref
    p = m.interpret("attack it", ctx)
    assert p["actions"][0]["clarification_needed"] and p["unresolved_references"]

"""Contracts v0.7: head/hands/feet perks and the rope / whistle effects, read from the Story Designer's
items_tier1.yaml (content/), whose effects are described in `use_note` only."""
from pathlib import Path

import pytest

from rpg import db, effects, rules, yamlish
from rpg import inventory as inv
from test_actions import Script, act, run, world  # noqa: F401  (fixture)

CONTENT = Path(__file__).resolve().parents[1] / "content"
YAML = {t["id"]: t for t in yamlish.load(CONTENT / "items_tier1.yaml")}


@pytest.fixture
def w(world):
    conn = world["conn"]
    have = {r[0] for r in conn.execute("SELECT key FROM item_templates")}
    for k in ("item_rope", "item_work_gloves", "item_leather_cap", "item_signal_whistle", "item_travel_boots"):
        if k not in have:
            db.add_item_template(conn, YAML[k])
    return world


def give(w, template, equip=False):
    key = inv.next_item_key(w["conn"], template)
    db.add_item(w["conn"], template, character_id=w["cid"], key=key)
    if equip:
        inv.equip(w["conn"], w["cid"], key)
    return key


def checks_rolled(rng):
    return rng.values == []


# --- the YAML is what we think it is ----------------------------------------------------------------

def test_yaml_fields_driving_v07():
    assert YAML["item_rope"]["stats"]["subcategory"] == "climbing" and "climbing" in YAML["item_rope"]["tags"]
    assert YAML["item_signal_whistle"]["stats"]["subcategory"] == "signal"
    assert (YAML["item_work_gloves"]["slot"], YAML["item_leather_cap"]["slot"], YAML["item_travel_boots"]["slot"]) \
        == ("hands", "head", "feet")
    # Story Designer v0.8 YAML: structured fields equal to the engine defaults (the YAML is the source of truth)
    for k in ("item_leather_cap", "item_work_gloves"):
        assert YAML[k]["stats"]["perk"] == effects.EQUIP_PERKS[k]
    rope = YAML["item_rope"]["stats"]
    assert {k: rope[k] for k in ("enables", "skill_bonus")} == inv.SUBCATEGORY_DEFAULTS["climbing"]
    assert "perk" not in YAML["item_travel_boots"]["stats"]


def test_template_perk_overrides_the_engine_default(w):
    conn = w["conn"]
    custom = dict(YAML["item_leather_cap"], id="item_iron_cap", name="Iron Cap",
                  stats={**YAML["item_leather_cap"]["stats"], "perk": {"negate_conditions": ["injured_str"]}})
    db.add_item_template(conn, custom)
    give(w, "item_iron_cap", equip=True)
    assert "id" in daze(w)  # the iron cap's own perk doesn't cover dazes
    assert "negated_by" in effects.add_player_condition(conn, w["cid"], rules.Condition(name="injured_str"))


# --- rope -----------------------------------------------------------------------------------------

def test_rope_is_a_climbing_tool_from_its_subcategory(w):
    rope = inv.get_item(w["conn"], give(w, "item_rope"))
    assert rope["stats"]["enables"] == ["climb"] and rope["stats"]["skill_bonus"]["skill"] == "athletics"


def test_climbing_move_with_rope_has_advantage(w):
    give(w, "item_rope")
    rng = Script(3, 18)  # advantage: two d20s, keep the higher
    r = run(w, act("move", target="loc_back_hall", skill="athletics", est_difficulty="medium"), rng=rng)
    x = r["results"][0]
    assert x["status"] == "resolved" and x["outcome"] == "success" and checks_rolled(rng)
    assert {"type": "tool_used", "key": "item_rope", "name": "Hempen Rope"} in x["consequences"]


def test_climbing_without_rope_is_a_plain_check(w):
    rng = Script(3)
    r = run(w, act("move", target="loc_back_hall", skill="athletics", est_difficulty="medium"), rng=rng)
    assert r["results"][0]["outcome"] == "failure" and checks_rolled(rng)


def test_rope_used_in_an_athletics_check_is_anchored_advantage(w):
    give(w, "item_rope")
    rng = Script(2, 19)
    r = run(w, act("other", skill="athletics", items_used=["item_rope"], intent="Climb the shelves on the rope."),
            rng=rng)
    assert r["results"][0]["outcome"] == "success" and checks_rolled(rng)


def test_rope_held_but_unused_does_not_help_other_athletics(w):
    give(w, "item_rope")
    rng = Script(5)  # a single d20: no advantage from a coiled rope in the pack
    r = run(w, act("other", skill="athletics", intent="Shove the crates aside."), rng=rng)
    assert checks_rolled(rng) and not any(c["type"] == "tool_used" for c in r["results"][0]["consequences"])


# --- hands: work gloves -----------------------------------------------------------------------------

def test_gloves_give_advantage_when_climbing(w):
    give(w, "item_work_gloves", equip=True)
    rng = Script(3, 18)
    r = run(w, act("move", target="loc_back_hall", skill="athletics"), rng=rng)
    x = r["results"][0]
    assert x["outcome"] == "success" and checks_rolled(rng)
    assert {"type": "perk_used", "key": "item_work_gloves", "name": "Work Gloves"} in x["consequences"]


def test_gloves_give_advantage_with_rough_items_only_when_worn(w):
    give(w, "item_rope")
    key = give(w, "item_work_gloves")
    rng = Script(3, 18)  # rope + worn gloves: one advantage (they don't stack)
    inv.equip(w["conn"], w["cid"], key)
    run(w, act("other", skill="sleight_of_hand", items_used=["item_rope"], intent="Knot the rope."), rng=rng)
    assert checks_rolled(rng)
    inv.unequip(w["conn"], w["cid"], key)
    rng = Script(3)  # carried, not worn: no perk
    run(w, act("other", skill="sleight_of_hand", items_used=["item_rope"], intent="Knot the rope."), rng=rng)
    assert checks_rolled(rng)


# --- head: leather cap ------------------------------------------------------------------------------

def daze(w):
    return effects.add_player_condition(w["conn"], w["cid"], rules.Condition(name="dazed", duration_turns=1))


def test_cap_negates_one_daze_per_rest(w):
    give(w, "item_leather_cap", equip=True)
    assert daze(w) == {"negated_by": {"key": "item_leather_cap", "name": "Leather Cap"}}
    assert "id" in daze(w)  # spent until a rest
    w["conn"].execute("DELETE FROM character_conditions")
    run(w, act("rest"))
    assert "negated_by" in daze(w)


def test_cap_only_when_worn_and_only_for_head_blows(w):
    give(w, "item_leather_cap")
    assert "id" in daze(w)
    inv.equip(w["conn"], w["cid"], "item_leather_cap")
    assert "id" in effects.add_player_condition(w["conn"], w["cid"], rules.Condition(name="injured_str"))


def test_cap_negates_a_daze_from_a_check_consequence(w):
    give(w, "item_leather_cap", equip=True)
    res = rules.CheckResult.__new__(rules.CheckResult)
    res.consequences = [{"type": "condition", "name": "stunned", "description": "Your ears ring.", "severity": 1,
                         "lasting": False, "duration_turns": 1}]
    effects.apply_check_consequences(w["conn"], w["cid"], res)
    assert res.consequences[0]["negated_by"]["key"] == "item_leather_cap"
    assert w["conn"].execute("SELECT COUNT(*) FROM character_conditions").fetchone()[0] == 0


# --- feet: travel boots -----------------------------------------------------------------------------

def test_boots_have_no_perk(w):
    assert effects.perk_of(inv.get_item(w["conn"], "item_travel_boots")) == {}


# --- whistle ----------------------------------------------------------------------------------------

def hostile(w, key="npc_tall_guard"):
    w["conn"].execute("UPDATE npcs SET disposition = -80 WHERE key = ?", (key,))


def test_whistle_is_heard_here_and_next_door(w):
    give(w, "item_signal_whistle")
    r = run(w, act("use_item", items_used=["item_signal_whistle"], intent="Blow the whistle."))
    x = r["results"][0]
    assert x["status"] == "resolved" and r["time_passed"] is True
    noise = x["consequences"][0]
    assert noise["type"] == "noise_made" and set(noise["heard_by"]) == {
        "npc_tall_guard", "npc_dozing_guard", "npc_archer", "npc_mara"}  # Mara is one room west
    assert len(x["consequences"]) == 1  # nobody hostile: nobody distracted


def test_whistle_distracts_a_hostile_once(w):
    give(w, "item_signal_whistle")
    hostile(w)
    rng = Script(19, 2)  # distracted: two d20s, the lower (2) misses
    r = run(w, act("use_item", items_used=["item_signal_whistle"]), rng=rng)
    assert {"type": "npc_condition", "key": "npc_tall_guard", "npc_name": "Tall Guard", "name": "distracted",
            "outcome": "applied"} in r["results"][0]["consequences"]
    assert r["npc_actions"][0]["outcome"] == "failure" and checks_rolled(rng)
    rng = Script(15, 1)  # next turn: it expired, a single d20 (hit, 1 damage)
    r = run(w, act("wait"), rng=rng)
    assert checks_rolled(rng)
    assert any(e.get("name") == "distracted" for e in r["expired"])
    rng = Script(15, 1)  # it won't fall for it twice
    r = run(w, act("use_item", items_used=["item_signal_whistle"]), rng=rng)
    assert [c["type"] for c in r["results"][0]["consequences"]] == ["noise_made"] and checks_rolled(rng)


def test_whistle_must_be_held(w):
    db.add_item(w["conn"], "item_signal_whistle", location_id=w["store"])
    r = run(w, act("use_item", items_used=["item_signal_whistle"]))
    assert r["results"][0]["status"] == "rejected"


def test_blow_the_whistle_through_the_mock(tmp_path):
    from rpg import actions, llm
    from rpg.context import build_context
    from rpg.world import seed_demo_slice
    conn = db.get_connection(tmp_path / "d.db")
    db.create_schema(conn)
    cid = seed_demo_slice(conn)
    db.add_item_template(conn, YAML["item_signal_whistle"])
    db.add_item(conn, "item_signal_whistle", character_id=cid)
    conn.execute("UPDATE characters SET location_id = (SELECT id FROM locations WHERE key = 'loc_storeroom')")
    conn.execute("UPDATE npcs SET disposition = -80 WHERE key = 'npc_dozing_guard'")
    m = llm.MockLLM()
    p = m.interpret("blow the whistle", build_context(conn, cid))
    assert p["actions"][0]["action_type"] == "use_item" and p["actions"][0]["items_used"] == ["item_signal_whistle"]
    r = actions.process_action_proposal(conn, cid, p, rng=Script(2, 3))
    out = llm.narrate_safely(m, r, build_context(conn, cid))
    assert "shrills" in out and "distracted" in out and "avoid the blow" in out
    conn.close()


# --- v0.7.1: an overwhelming NPC hit dazes for 1 round ----------------------------------------------

def conds(w):
    return [r[0] for r in w["conn"].execute("SELECT name FROM character_conditions")]


def test_overwhelming_hit_dazes_for_one_round(w):
    hostile(w)
    r = run(w, act("wait"), rng=Script(20, 1))  # 20 + 2 vs defense 12: margin 10 = overwhelming
    a = r["npc_actions"][0]
    assert (a["outcome"], a["degree"], a["dazed"]) == ("success", "overwhelming", "applied")
    assert conds(w) == ["dazed"] and r["expired"] == []  # doesn't tick away on the turn it lands
    rng = Script(18, 4, 5)  # dazed: the player's check rolls 2d20 and keeps the lower (4); guard misses (5)
    r = run(w, act("other", skill="athletics", intent="Shove a crate."), rng=rng)
    assert r["results"][0]["outcome"] == "failure" and checks_rolled(rng)
    assert {"type": "condition_expired", "target": "self", "name": "dazed"} in r["expired"] and conds(w) == []


def test_clear_hit_does_not_daze(w):
    hostile(w)
    r = run(w, act("wait"), rng=Script(15, 1))  # margin 5: clear
    assert r["npc_actions"][0]["degree"] == "clear" and "dazed" not in r["npc_actions"][0] and conds(w) == []


def test_daze_on_a_killing_blow_is_not_applied(w):
    hostile(w)
    w["conn"].execute("UPDATE characters SET current_hp = 1")
    r = run(w, act("wait"), rng=Script(20, 1))
    assert "dazed" not in r["npc_actions"][0] and r["defeat"]


def test_leather_cap_cancels_one_daze_per_rest(w):
    hostile(w)
    give(w, "item_leather_cap", equip=True)
    r = run(w, act("wait"), rng=Script(20, 1))
    a = r["npc_actions"][0]
    assert a["dazed"] == "negated" and a["negated_by"] == {"key": "item_leather_cap", "name": "Leather Cap"}
    assert conds(w) == []
    r = run(w, act("wait"), rng=Script(20, 1))  # spent until a rest
    assert r["npc_actions"][0]["dazed"] == "applied"


def test_daze_narration(tmp_path):
    from rpg import llm
    m = llm.MockLLM()
    base = {"npc": "npc_x", "name": "Dozing Guard", "action": "attack", "outcome": "success",
            "degree": "overwhelming", "hp_band": "wounded"}
    assert "you're dazed" in m._npc_action_text({**base, "dazed": "applied"})
    assert "Leather Cap takes the worst" in m._npc_action_text(
        {**base, "dazed": "negated", "negated_by": {"key": "item_leather_cap", "name": "Leather Cap"}})


# --- v0.7.1: structured fields with engine defaults as the fallback --------------------------------

def add_variant(w, base, new_id, name, **stats):
    t = dict(YAML[base], id=new_id, name=name)
    st = {k: v for k, v in YAML[base]["stats"].items() if k not in ("enables", "skill_bonus", "perk", "noise")}
    t["stats"] = {**st, **stats}
    db.add_item_template(w["conn"], t)


def test_climbing_item_without_fields_falls_back_to_defaults(w):
    add_variant(w, "item_rope", "item_vine", "Tough Vine")
    v = inv.get_item(w["conn"], give(w, "item_vine"))
    assert v["stats"]["enables"] == ["climb"] and v["stats"]["skill_bonus"]["skill"] == "athletics"


def test_climbing_item_fields_override_defaults(w):
    add_variant(w, "item_rope", "item_thin_cord", "Thin Cord", enables=[], skill_bonus={})
    v = inv.get_item(w["conn"], give(w, "item_thin_cord"))
    assert v["stats"]["enables"] == [] and v["stats"]["skill_bonus"] == {}


def test_gloves_without_perk_field_fall_back_to_the_engine_default(w):
    stats = {k: v for k, v in YAML["item_work_gloves"]["stats"].items() if k != "perk"}
    assert effects.perk_of({"stats": stats, "template_key": "item_work_gloves"}) == \
        effects.EQUIP_PERKS["item_work_gloves"]
    assert effects.perk_of({"stats": YAML["item_work_gloves"]["stats"], "template_key": "x"}) == \
        YAML["item_work_gloves"]["stats"]["perk"]


def test_whistle_reads_stats_noise(w):
    hostile(w)
    add_variant(w, "item_signal_whistle", "item_rattle", "Bone Rattle", subcategory="toy",
                noise={"distract": "next_attack", "once_per_foe": False, "heard_range": "room"})
    give(w, "item_rattle")
    r = run(w, act("use_item", items_used=["item_rattle"]), rng=Script(19, 2))
    cons = r["results"][0]["consequences"]
    assert "npc_mara" not in cons[0]["heard_by"]  # heard_range: room
    assert any(c.get("name") == "distracted" for c in cons)
    r = run(w, act("use_item", items_used=["item_rattle"]), rng=Script(19, 2))  # not once_per_foe
    assert any(c.get("name") == "distracted" for c in r["results"][0]["consequences"])


def test_noise_without_distraction(w):
    hostile(w)
    add_variant(w, "item_signal_whistle", "item_bell", "Hand Bell", noise={"distract": None})
    give(w, "item_bell")
    r = run(w, act("use_item", items_used=["item_bell"]), rng=Script(15, 1))
    assert [c["type"] for c in r["results"][0]["consequences"]] == ["noise_made"]


def test_whistle_falls_back_to_subcategory_signal(w):
    assert "noise" not in YAML["item_signal_whistle"]["stats"]
    assert effects.noise_spec({"stats": YAML["item_signal_whistle"]["stats"]}) == effects.DEFAULT_NOISE
    assert effects.noise_spec({"stats": {"subcategory": "lever"}}) is None

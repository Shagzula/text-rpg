import json
import random

import pytest

from rpg import actions, db, rules
from rpg import inventory as inv
from rpg.actions import process_action_proposal as process
from helpers import build_world

HIDDEN = {"roll", "dc", "modifier", "bonus", "total", "margin", "needed_roll", "effect", "injury_applied"}


class Script(random.Random):
    """RNG whose randint returns scripted values in order (d20s and damage dice alike)."""

    def __init__(self, *values):
        super().__init__(0)
        self.values = list(values)

    def randint(self, a, b):
        assert self.values, "unexpected roll"
        v = self.values.pop(0)
        assert a <= v <= b, (v, a, b)
        return v


@pytest.fixture
def world(tmp_path):
    conn = db.get_connection(tmp_path / "w.db")
    db.create_schema(conn)
    w = build_world(conn)
    w["conn"] = conn
    yield w
    conn.close()


def act(action_type, intent="Do it.", **kw):
    a = {"action_type": action_type, "skill": None, "target": None, "items_used": [], "last_ditch": False,
         "est_difficulty": "medium", "intent_summary": intent, "clarification_needed": False,
         "clarification_question": None}
    a.update(kw)
    return a


def prop(*acts, ooc=None, refs=()):
    return {"actions": list(acts), "ooc": ooc, "unresolved_references": list(refs)}


def run(w, *acts, rng=None, **kw):
    return process(w["conn"], w["cid"], prop(*acts, **kw), rng=rng or Script())


def walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk_keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_keys(v)


def char(w):
    return w["conn"].execute("SELECT * FROM characters WHERE id = ?", (w["cid"],)).fetchone()


# --- validation ------------------------------------------------------------------

@pytest.mark.parametrize("bad,path", [
    ("{not json", "$"),
    ([], "$"),
    ({"ooc": None}, "actions"),
    ({"actions": [act("wait")] * 4, "ooc": None}, "actions"),
    ({"actions": [act("fly")], "ooc": None}, "actions[0].action_type"),
    ({"actions": [act("skill_check", skill="flying")], "ooc": None}, "actions[0].skill"),
    ({"actions": [act("skill_check")], "ooc": None}, "actions[0].skill"),
    ({"actions": [act("wait", intent_summary="")], "ooc": None}, "actions[0].intent_summary"),
    ({"actions": [act("wait", items_used="item_x")], "ooc": None}, "actions[0].items_used"),
    ({"actions": [act("wait", last_ditch="yes")], "ooc": None}, "actions[0].last_ditch"),
    ({"actions": [act("wait", est_difficulty="absurd")], "ooc": None}, "actions[0].est_difficulty"),
    ({"actions": [act("wait")], "ooc": {"command": "save"}}, "actions"),
    ({"actions": [], "ooc": {"command": "dance"}}, "ooc"),
    ({"actions": [], "ooc": None, "unresolved_references": "the guard"}, "unresolved_references"),
    ({"actions": [], "ooc": None}, "actions"),
])
def test_malformed_proposal_returns_reprompt_error(world, bad, path):
    r = process(world["conn"], world["cid"], bad)
    assert r["error"] == "invalid_proposal" and "results" not in r
    assert path in [e["path"] for e in r["errors"]]
    assert "corrected JSON" in r["reprompt"]
    assert world["conn"].execute("SELECT COUNT(*) FROM save_games").fetchone()[0] == 0  # no turn spent


def test_json_string_and_defaults_accepted(world):
    r = process(world["conn"], world["cid"], json.dumps({"actions": [{"action_type": "wait", "intent_summary": "Wait."}]}))
    assert r["results"][0]["reason"] == "no_check" and r["turn"] == 1


def test_ooc_only_turn_returns_no_engine_actions(world):
    r = process(world["conn"], world["cid"], prop(ooc={"command": "save", "text": "save game"}))
    assert r == {"type": "ooc", "ooc": {"command": "save", "text": "save game"}, "results": []}
    assert world["conn"].execute("SELECT COUNT(*) FROM save_games").fetchone()[0] == 0


def test_turn_counter_increments(world):
    assert run(world, act("wait"))["turn"] == 1
    assert run(world, act("wait"))["turn"] == 2


# --- clarify -----------------------------------------------------------------------

def test_unresolved_references_clarify_without_state_change(world):
    r = run(world, act("attack", target=None, items_used=["item_rusty_shortsword"], clarification_needed=True,
                       clarification_question="Which guard: the tall one or the dozing one?"),
            act("take", target="item_crowbar"), refs=["the guard"])
    [c] = r["results"]
    assert (c["outcome"], c["reason"], c["degree"], c["status"]) == ("clarify", "clarify", None, "clarify")
    assert c["state_changes"] == [] and c["consequences"] == []
    assert r["clarification_question"] == "Which guard: the tall one or the dozing one?"
    assert r["skipped"] == [{"action_type": "take", "intent_summary": "Do it.", "reason": "prior_clarify"}]
    assert not inv.has_item(world["conn"], world["cid"], "item_crowbar")


def test_unresolved_references_generic_question(world):
    r = run(world, refs=["the door"])
    assert r["results"][0]["outcome"] == "clarify" and r["clarification_question"] == 'Which do you mean: "the door"?'


def test_clarification_needed_mid_turn(world):
    r = run(world, act("take", target="item_crowbar"),
            act("attack", clarification_needed=True, clarification_question="Which guard?"),
            act("wait"))
    assert [x["outcome"] for x in r["results"]] == ["success", "clarify"]
    assert r["clarification_question"] == "Which guard?" and r["skipped"][0]["reason"] == "prior_clarify"
    assert inv.has_item(world["conn"], world["cid"], "item_crowbar")  # earlier action did resolve


# --- trivial actions / no_check ------------------------------------------------------

@pytest.mark.parametrize("a", [act("inventory"), act("wait"), act("examine", target="item_hanging_lamp"),
                               act("talk", target="npc_tall_guard")])
def test_no_check_actions(world, a):
    [r] = run(world, a)["results"]
    assert (r["status"], r["outcome"], r["reason"], r["degree"]) == ("resolved", "success", "no_check", None)
    assert not (r["auto_success"] or r["impossible"] or r["last_ditch"])


def test_examine_gives_description_even_out_of_reach(world):
    [r] = run(world, act("examine", target="item_shelf_antidote"))["results"]
    assert r["status"] == "resolved" and r["target"]["key"] == "item_shelf_antidote"
    [r] = run(world, act("examine", target="loc_cellar"))["results"]
    assert r["target"]["description"] == "iron-banded door (locked)"


def test_examine_with_skill_rolls(world):
    [r] = run(world, act("examine", skill="investigation", target="item_hanging_lamp", est_difficulty="medium"),
              rng=Script(16))["results"]
    assert r["reason"] == "rolled" and r["outcome"] == "success" and r["degree"] == "narrow"  # 16 vs DC 15
    [r] = run(world, act("examine", skill="investigation", target="item_hanging_lamp", est_difficulty="hard"))["results"]
    assert r["reason"] == "impossible" and r["degree"] is None  # INT +0 needs a 20: no roll


# --- DCs -----------------------------------------------------------------------------

@pytest.mark.parametrize("est,dc", list(actions.DIFFICULTY_DC.items()))
def test_est_difficulty_maps_to_dc(world, est, dc):
    t = actions.Turn(world["conn"], world["cid"], Script(), 1)
    assert t.dc_for(act("skill_check", skill="athletics", est_difficulty=est), None) == dc
    assert actions.DIFFICULTY_DC == {"trivial": 5, "easy": 10, "medium": 15, "hard": 20, "very_hard": 25,
                                     "near_impossible": 30}


def test_exit_unlock_dc_overrides_hint(world, caplog):
    with caplog.at_level("DEBUG", logger="rpg.debug"):
        [r] = run(world, act("skill_check", skill="sleight_of_hand", target="loc_cellar", est_difficulty="trivial"),
                  rng=Script(10))["results"]
    assert "'dc': 18" in caplog.text  # not 5
    assert r["outcome"] == "failure" and r["reason"] == "rolled"  # 10+2 < 18


# --- move / exits ----------------------------------------------------------------------

def test_move_through_open_exit(world):
    [r] = run(world, act("move", target="loc_back_hall"))["results"]
    assert (r["outcome"], r["reason"], r["degree"]) == ("success", "no_check", None)
    assert r["state_changes"] == [{"type": "moved_to_location", "key": "loc_back_hall", "name": "Back Hall"},
                                  {"type": "flag_set", "key": "visited:loc_back_hall", "value": True}]
    assert char(world)["location_id"] == world["hall"]
    [r] = run(world, act("move", target="loc_storeroom"))["results"]
    [r] = run(world, act("move", target="loc_back_hall"))["results"]
    assert [c["type"] for c in r["state_changes"]] == ["moved_to_location"]  # already visited


@pytest.mark.parametrize("target,code,detail", [
    ("loc_cellar", "impossible_action", "exit_locked"),
    ("loc_far_tower", "target_not_present", "no_exit"),
    ("exit:down", "target_not_present", "unknown_key"),  # v0.2 style is gone
    ("loc_storeroom", "impossible_action", "already_here"),
    ("npc_tall_guard", "impossible_action", "not_an_exit"),
])
def test_move_rejections(world, target, code, detail):
    [r] = run(world, act("move", target=target))["results"]
    assert (r["status"], r["outcome"], r["reason"], r.get("detail"), r["degree"]) == \
        ("rejected", "failure", code, detail, None)
    assert char(world)["location_id"] == world["store"]


def test_unlock_then_move(world):
    r = run(world, act("skill_check", skill="sleight_of_hand", target="loc_cellar"),
            act("move", target="loc_cellar"), rng=Script(17))
    unlock, move = r["results"]
    assert unlock["outcome"] == "success" and unlock["state_changes"] == [
        {"type": "exit_state", "key": "loc_cellar", "direction": "down", "from": "locked", "to": "open"}]
    assert move["outcome"] == "success" and char(world)["location_id"] == world["cellar"]
    back = world["conn"].execute("SELECT locked FROM location_exits WHERE from_location_id = ?",
                                 (world["cellar"],)).fetchone()
    assert back["locked"] == 0  # both sides unlocked


def test_weak_character_door_is_impossible_unless_last_ditch(world):
    [r] = run(world, act("skill_check", skill="athletics", target="loc_cellar"))["results"]
    assert r["impossible"] and r["reason"] == "impossible" and r["degree"] is None  # STR 8: needs 19
    [r] = run(world, act("skill_check", skill="athletics", target="loc_cellar", last_ditch=True),
              rng=Script(14))["results"]  # 14 - 1 + 5 = 18
    assert r["outcome"] == "success" and r["last_ditch"] and r["reason"] == "last_ditch"
    assert r["consequences"][0]["name"] == "strained_str"
    assert r["state_changes"][0]["type"] == "exit_state"
    names = [x["name"] for x in world["conn"].execute("SELECT name FROM character_conditions")]
    assert names == ["strained_str"]


def test_failed_last_ditch_injures_and_persists(world):
    r = run(world, act("skill_check", skill="athletics", target="loc_cellar", last_ditch=True), rng=Script(2))
    [res] = r["results"]
    assert res["outcome"] == "failure" and res["consequences"][0]["name"] == "injured_str"
    assert r["player_status"]["conditions"] == ["injured_str"]


# --- chains --------------------------------------------------------------------------------

def test_failed_check_skips_dependent_but_not_independent(world):
    r = run(world, act("skill_check", skill="sleight_of_hand", target="loc_cellar"),
            act("examine", target="item_hanging_lamp"),
            act("move", target="loc_cellar"), rng=Script(3))
    assert [x["action_type"] for x in r["results"]] == ["skill_check", "examine"]
    assert r["results"][0]["outcome"] == "failure"
    assert r["skipped"] == [{"action_type": "move", "intent_summary": "Do it.", "reason": "prior_failure"}]


def test_failed_move_skips_everything_after(world):
    r = run(world, act("move", target="loc_back_hall", skill="acrobatics", est_difficulty="easy"),
            act("wait"), rng=Script(2))
    assert len(r["results"]) == 1 and r["results"][0]["outcome"] == "failure"
    assert r["skipped"][0]["action_type"] == "wait" and char(world)["location_id"] == world["store"]


def test_rejection_affects_only_that_action(world):
    """Interpreter example 11: real action + claimed possession."""
    conn = world["conn"]
    conn.execute("UPDATE characters SET current_hp = 4 WHERE id = ?", (world["cid"],))
    r = run(world, act("use_item", target="self", items_used=["item_healing_draught"], est_difficulty="trivial"),
            act("use_item", target="npc_tall_guard", items_used=["item_dragon_wand"]),
            act("examine", target="npc_tall_guard"), rng=Script(3, 4))  # 2d4+2 = 9
    drink, wand, look = r["results"]
    assert drink["outcome"] == "success" and drink["reason"] == "no_check"
    assert {"type": "item_removed", "key": "item_healing_draught", "name": "Minor Healing Draught"} in drink["state_changes"]
    assert [c["type"] for c in drink["consequences"]] == ["healed", "item_consumed"]
    assert drink["consequences"][0]["hp_band"] == "unhurt" and char(world)["current_hp"] == 10
    assert (wand["status"], wand["reason"], wand["degree"]) == ("rejected", "item_not_owned", None)
    assert wand["target"] == {"key": "npc_tall_guard", "name": "Tall Guard"}
    assert look["status"] == "resolved"
    assert "skipped" not in r


# --- validation codes ----------------------------------------------------------------------

@pytest.mark.parametrize("a,code", [
    (act("attack", target="npc_dozing_guard", items_used=["item_greatsword"]), "item_not_owned"),
    (act("attack", target="npc_dozing_guard", items_used=["item_long_blade"]), "item_not_owned"),  # exists elsewhere
    (act("use_item", items_used=["item_legendary_sword"], target="item_sealed_crate"), "item_not_owned"),
    (act("talk", target="npc_mara"), "target_not_present"),  # in another room
    (act("attack", target="npc_dragon"), "target_not_present"),
    (act("examine", target="item_long_blade"), "target_not_present"),
    (act("take", target="item_hanging_lamp"), "item_unusable"),
    (act("take", target="item_rusty_shortsword"), "item_unusable"),
    (act("take", target="item_shelf_antidote"), "out_of_reach"),
    (act("take", target="item_long_blade"), "target_not_present"),
    (act("use_item", items_used=["item_shelf_antidote"]), "out_of_reach"),
    (act("attack", target="npc_archer", items_used=["item_rusty_shortsword"]), "out_of_reach"),
    (act("attack", target="npc_tall_guard", items_used=["item_hanging_lamp"]), "item_not_owned"),  # not held
    (act("attack", target="self"), "impossible_action"),
    (act("talk", target="item_hanging_lamp"), "impossible_action"),
    (act("other", intent="Cast fireball at the guard.", est_difficulty="near_impossible",
         target="npc_tall_guard"), "impossible_action"),
    (act("drop", items_used=["item_crowbar"]), "item_not_owned"),
    (act("equip", items_used=["item_dragon_wand"]), "item_not_owned"),
    (act("unequip", items_used=["item_buckler"]), "item_unusable"),
])
def test_validation_codes(world, a, code):
    before = world["conn"].total_changes
    [r] = run(world, a)["results"]
    assert (r["status"], r["outcome"], r["reason"], r["degree"]) == ("rejected", "failure", code, None)
    assert r["state_changes"] == [] and r["consequences"] == []
    assert code in actions.VALIDATION_CODES
    # nothing changed: the turn counter was bumped and handed back (no turn spent)
    assert world["conn"].total_changes - before <= 2
    assert world["conn"].execute("SELECT turn FROM save_games").fetchone()[0] == 0


def test_other_with_near_impossible_but_skill_still_rolls(world):
    [r] = run(world, act("other", skill="athletics", est_difficulty="near_impossible"))["results"]
    assert r["reason"] == "impossible"  # stat-based filter, not a validation rejection


# --- items ------------------------------------------------------------------------------------

def test_take_drop_equip_unequip(world):
    conn, cid = world["conn"], world["cid"]
    [r] = run(world, act("take", target="item_crowbar"))["results"]
    assert r["state_changes"] == [{"type": "item_added", "key": "item_crowbar", "name": "Crowbar"}]
    assert inv.has_item(conn, cid, "item_crowbar")
    [r] = run(world, act("equip", items_used=["item_crowbar"]))["results"]
    assert (r["reason"], r.get("detail")) == ("item_unusable", "not_equippable")
    [r] = run(world, act("drop", items_used=["item_crowbar"]))["results"]
    assert r["state_changes"] == [{"type": "item_removed", "key": "item_crowbar", "name": "Crowbar",
                                   "to": "loc_storeroom"}]
    assert inv.get_item(conn, "item_crowbar")["location_id"] == world["store"]
    run(world, act("equip", items_used=["item_buckler"]))
    [r] = run(world, act("equip", target="item_heavy_blade"))["results"]
    types = [(c["type"], c["key"]) for c in r["state_changes"]]
    assert set(types[:2]) == {("item_unequipped", "item_rusty_shortsword"), ("item_unequipped", "item_buckler")}
    assert r["state_changes"][-1] == {"type": "item_equipped", "key": "item_heavy_blade",
                                      "name": "Heavy Two-Handed Blade", "slot": "two_hands"}
    [r] = run(world, act("unequip", items_used=["item_heavy_blade"]))["results"]
    assert r["state_changes"] == [{"type": "item_unequipped", "key": "item_heavy_blade",
                                   "name": "Heavy Two-Handed Blade"}]


def test_use_fixture_sets_flag(world):
    [r] = run(world, act("use_item", target="item_lever"))["results"]
    assert r["outcome"] == "success" and r["state_changes"] == [{"type": "flag_set", "key": "gate_opened", "value": True}]
    assert db.get_flag(world["conn"], "gate_opened") is True


def test_bandage_needs_check_and_has_uses(world):
    world["conn"].execute("UPDATE characters SET current_hp = 5 WHERE id = ?", (world["cid"],))
    [r] = run(world, act("use_item", target="self", items_used=["item_bandage_roll"]), rng=Script(12, 3))["results"]
    assert r["reason"] == "rolled" and r["outcome"] == "success"
    assert [c["type"] for c in r["consequences"]] == ["healed", "item_used"] and r["state_changes"] == []
    assert char(world)["current_hp"] == 8 and inv.get_item(world["conn"], "item_bandage_roll")["uses_left"] == 2
    [r] = run(world, act("use_item", target="self", items_used=["item_bandage_roll"]), rng=Script(4))["results"]
    assert r["outcome"] == "failure" and inv.get_item(world["conn"], "item_bandage_roll")["uses_left"] == 2


def test_antidote_cures_condition(world):
    conn, cid = world["conn"], world["cid"]
    conn.execute("UPDATE items SET out_of_reach = 0 WHERE key = 'item_shelf_antidote'")
    db.add_condition(conn, cid, rules.Condition("poisoned", {"all_checks": -2}))
    r = run(world, act("take", target="item_shelf_antidote"),
            act("use_item", target="self", items_used=["item_shelf_antidote"]))
    use = r["results"][1]
    assert {"type": "condition_removed", "target": "self", "name": "poisoned"} in use["consequences"]
    assert r["player_status"]["conditions"] == []


def test_tool_with_skill_on_exit_is_a_check(world):
    r = run(world, act("take", target="item_crowbar"),
            act("use_item", skill="athletics", target="loc_cellar", items_used=["item_crowbar"], last_ditch=True),
            rng=Script(15, 3))  # crowbar: advantage on athletics -> keep 15
    pry = r["results"][1]
    assert pry["reason"] == "last_ditch" and pry["outcome"] == "success"
    assert pry["state_changes"][0]["type"] == "exit_state"


# --- attacks -------------------------------------------------------------------------------------

def test_attack_finesse_uses_better_ability_and_proficiency(world, caplog):
    with caplog.at_level("DEBUG", logger="rpg.debug"):
        r = run(world, act("attack", skill="str", target="npc_dozing_guard", items_used=["item_rusty_shortsword"]),
                rng=Script(4, 1, 1))  # 4 + DEX 2 + PB 2 = 8 vs DC 8; damage 1 + 2 = 3; guard's counterattack misses
    [a] = r["results"]
    assert a["ability"] == "dex" and a["outcome"] == "success" and a["degree"] == "narrow"
    assert "'modifier': 4" in caplog.text and "'damage': 3" in caplog.text
    assert a["consequences"] == [{"type": "damage_dealt", "target": "npc_dozing_guard", "hp_band": "badly_wounded"},
                                 {"type": "npc_disposition_changed", "key": "npc_dozing_guard", "to": "hostile"}]
    assert not HIDDEN & set(walk_keys(r))


def test_attack_kills_and_then_target_defeated(world):
    [a] = run(world, act("attack", target="npc_dozing_guard", items_used=["item_rusty_shortsword"]),
              rng=Script(15, 6))["results"]  # 15 + 4 vs DC 8: margin 11
    assert a["degree"] == "overwhelming" and a["consequences"][0]["hp_band"] == "down"
    assert {"type": "npc_defeated", "key": "npc_dozing_guard", "name": "Dozing Guard"} in a["state_changes"]
    assert db.get_flag(world["conn"], "defeated:npc_dozing_guard") is True
    [a] = run(world, act("attack", target="npc_dozing_guard"))["results"]
    assert (a["reason"], a["detail"]) == ("impossible_action", "target_defeated")
    [a] = run(world, act("talk", target="npc_dozing_guard"))["results"]
    assert a["reason"] == "impossible_action"


def test_attack_miss_angers_target(world):
    [a] = run(world, act("attack", target="npc_tall_guard", items_used=["item_rusty_shortsword"]),
              rng=Script(2, 1))["results"]  # miss; the angered guard's counterattack misses
    assert a["outcome"] == "failure" and a["consequences"] == [
        {"type": "npc_disposition_changed", "key": "npc_tall_guard", "to": "hostile"}]
    [r] = run(world, act("rest"))["results"]
    assert (r["reason"], r["detail"]) == ("impossible_action", "enemies_present")


def test_attack_unproficient_martial_uses_str(world, caplog):
    with caplog.at_level("DEBUG", logger="rpg.debug"):
        [a] = run(world, act("attack", target="npc_tall_guard", items_used=["item_heavy_blade"]),
                  rng=Script(10, 1))["results"]
    assert a["ability"] == "str" and "'modifier': -1" in caplog.text and a["outcome"] == "failure"


def test_unarmed_attack_str_proficient(world, caplog):
    with caplog.at_level("DEBUG", logger="rpg.debug"):
        [a] = run(world, act("attack", target="npc_dozing_guard"), rng=Script(7, 1))["results"]
    assert a["ability"] == "str" and "'modifier': 1" in caplog.text and a["outcome"] == "success"  # -1 + 2


def test_ranged_attack_reaches_out_of_reach_target(world):
    db.add_item(world["conn"], "item_short_bow", character_id=world["cid"], key="item_short_bow")
    [a] = run(world, act("attack", target="npc_archer", items_used=["item_short_bow"]), rng=Script(18, 5))["results"]
    assert a["status"] == "resolved" and a["ability"] == "dex" and a["outcome"] == "success"


def test_improvised_attack_on_fixture(world):
    """Interpreter example 1: throw a boot at the hanging lamp."""
    [a] = run(world, act("attack", skill="dex", target="item_hanging_lamp", items_used=["item_travel_boots"],
                         est_difficulty="easy"), rng=Script(9))["results"]  # 9 + DEX 2 (no PB) = 11 vs 10
    assert a["ability"] == "dex" and a["outcome"] == "success"
    assert a["state_changes"] == [{"type": "entity_state", "key": "item_hanging_lamp", "field": "state",
                                   "from": None, "to": "damaged"}]


# --- rest / talk / status --------------------------------------------------------------------------

def test_rest_heals_half_and_clears_temporary_conditions(world):
    conn, cid = world["conn"], world["cid"]
    conn.execute("UPDATE characters SET current_hp = 3 WHERE id = ?", (cid,))
    db.add_condition(conn, cid, rules.Condition("winded", {"all_checks": -1}))
    db.add_condition(conn, cid, rules.Condition("injured_str", {"abilities": {"str": -2}}, lasting=True))
    r = run(world, act("rest", intent="Take a short rest."))
    [x] = r["results"]
    assert x["consequences"] == [{"type": "healed", "target": "self", "hp_band": "wounded"},
                                 {"type": "condition_removed", "target": "self", "name": "winded"}]
    assert char(world)["current_hp"] == 7 and r["player_status"]["conditions"] == ["injured_str"]


def test_talk_with_social_skill_rolls(world):
    [r] = run(world, act("talk", skill="deception", target="npc_tall_guard"), rng=Script(9))["results"]
    assert r["reason"] == "rolled" and r["outcome"] == "failure" and r["degree"] == "clear"  # 9 vs 15


def test_defeat_when_down(world):
    world["conn"].execute("UPDATE characters SET current_hp = 0 WHERE id = ?", (world["cid"],))
    r = run(world, act("wait"))
    assert r["player_status"]["hp_band"] == "down" and r["defeat"]["type"] == "death"
    assert r["results"] == [] and r["turn"] is None  # the dead take no actions


def test_result_envelope_and_no_numbers_anywhere(world):
    r = run(world, act("skill_check", skill="athletics", target="loc_cellar", last_ditch=True),
            act("attack", target="npc_dozing_guard", items_used=["item_rusty_shortsword"]),
            act("take", target="item_crowbar"), rng=Script(1, 15, 2))
    assert set(r) >= {"turn", "results", "player_status", "dialogue", "defeat", "clarification_question"}
    assert not HIDDEN & set(walk_keys(r))
    for x in r["results"]:
        assert set(x) >= {"action_type", "intent_summary", "target", "items_used", "status", "outcome", "degree",
                          "reason", "auto_success", "impossible", "last_ditch", "consequences", "state_changes"}
    json.dumps(r)

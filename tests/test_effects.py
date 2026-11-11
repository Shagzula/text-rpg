"""NPC counterattacks, death, item effects (tonic, flash powder, light, rations, tools), timers."""
import json
import pytest

from rpg import db, effects, rules
from rpg import inventory as inv
from test_actions import HIDDEN, Script, act, run, walk_keys, world  # noqa: F401  (fixture)


def hp(w):
    return w["conn"].execute("SELECT current_hp FROM characters WHERE id = ?", (w["cid"],)).fetchone()[0]


def set_hp(w, v):
    w["conn"].execute("UPDATE characters SET current_hp = ? WHERE id = ?", (v, w["cid"]))


def hostile(w, key="npc_tall_guard"):
    w["conn"].execute("UPDATE npcs SET disposition = -80 WHERE key = ?", (key,))


def give(w, template, **kw):
    return db.add_item(w["conn"], template, character_id=w["cid"], **kw)


# --- NPC phase -----------------------------------------------------------------------------------

def test_hostile_npc_counterattacks_and_hits(world, caplog):
    hostile(world)
    with caplog.at_level("DEBUG", logger="rpg.debug"):
        r = run(world, act("wait"), rng=Script(10, 3))  # 10 + 2 = 12 vs defense 12 (10 + DEX 2); 1d4 -> 3
    assert r["npc_actions"] == [{"npc": "npc_tall_guard", "name": "Tall Guard", "action": "attack",
                                 "outcome": "success", "degree": "narrow", "hp_band": "wounded"}]
    assert hp(world) == 7 and r["player_status"]["hp_band"] == "wounded" and r["defeat"] is None
    assert not HIDDEN & set(walk_keys(r))
    assert "'defense': 12" in caplog.text and "'damage': 3" in caplog.text


def test_counterattack_miss_leaves_hp(world):
    hostile(world)
    r = run(world, act("wait"), rng=Script(9))
    assert r["npc_actions"][0]["outcome"] == "failure" and r["npc_actions"][0]["hp_band"] == "unhurt"
    assert hp(world) == 10


def test_only_hostile_alive_reachable_npcs_here_attack(world):
    conn = world["conn"]
    hostile(world, "npc_archer")      # out of reach
    hostile(world, "npc_mara")        # elsewhere
    hostile(world, "npc_dozing_guard")
    conn.execute("UPDATE npcs SET alive = 0 WHERE key = 'npc_dozing_guard'")
    r = run(world, act("wait"))       # no rolls scripted: nobody may attack
    assert r["npc_actions"] == []


def test_npc_attack_uses_its_row_stats(world):
    hostile(world)
    world["conn"].execute("UPDATE npcs SET attack_bonus = 6, damage = '2d6' WHERE key = 'npc_tall_guard'")
    run(world, act("wait"), rng=Script(6, 1, 1))  # 6 + 6 = 12 hits; 2d6 = 2
    assert hp(world) == 8


def test_attacking_neutral_npc_provokes_counterattack_same_turn(world):
    r = run(world, act("attack", target="npc_dozing_guard"), rng=Script(1, 20, 2))  # miss; it hits for 2
    assert r["results"][0]["outcome"] == "failure"
    assert r["npc_actions"][0]["npc"] == "npc_dozing_guard" and r["npc_actions"][0]["outcome"] == "success"
    assert hp(world) == 8


def npc_loc(w, key):
    return w["conn"].execute("SELECT location_id FROM npcs WHERE key = ?", (key,)).fetchone()[0]


def test_fleeing_hostile_follows_once_without_attacking(world):
    hostile(world)
    r = run(world, act("move", target="loc_back_hall"))  # Script(): any attack roll would fail the test
    assert r["npc_actions"] == [{"npc": "npc_tall_guard", "name": "Tall Guard", "action": "follow",
                                 "outcome": None, "degree": None, "hp_band": "unhurt"}]
    assert npc_loc(world, "npc_tall_guard") == world["hall"]
    assert db.get_flag(world["conn"], "pursued:npc_tall_guard") is True
    r = run(world, act("wait"), rng=Script(1))  # next turn it fights in the new room
    assert [a["action"] for a in r["npc_actions"]] == ["attack"]
    r = run(world, act("move", target="loc_storeroom"))  # it only pursues once, then stays put
    assert r["npc_actions"] == [] and npc_loc(world, "npc_tall_guard") == world["hall"]


def test_unreachable_or_incapacitated_hostiles_do_not_follow(world):
    conn = world["conn"]
    hostile(world)
    hostile(world, "npc_archer")  # out of reach on the ledge
    nid = conn.execute("SELECT id FROM npcs WHERE key = 'npc_tall_guard'").fetchone()[0]
    conn.execute("INSERT INTO npc_conditions (npc_id, name, duration_turns) VALUES (?, 'blinded', 3)", (nid,))
    r = run(world, act("move", target="loc_back_hall"))
    assert r["npc_actions"] == [] and npc_loc(world, "npc_tall_guard") == world["store"]


def test_pursuit_works_in_the_dark(world):
    conn = world["conn"]
    hostile(world)
    conn.execute("UPDATE locations SET dark = 1 WHERE key = 'loc_cellar'")
    conn.execute("UPDATE location_exits SET locked = 0")
    r = run(world, act("move", target="loc_cellar"))
    assert [a["action"] for a in r["npc_actions"]] == ["follow"] and npc_loc(world, "npc_tall_guard") == world["cellar"]
    r = run(world, act("wait"), rng=Script(1))
    assert [a["action"] for a in r["npc_actions"]] == ["attack"]


def test_no_rest_with_a_hostile_in_an_adjacent_room(world):
    conn = world["conn"]
    hostile(world)
    conn.execute("UPDATE characters SET location_id = ? WHERE id = ?", (world["hall"], world["cid"]))
    r = run(world, act("rest"))
    assert (r["results"][0]["reason"], r["results"][0]["detail"]) == ("impossible_action", "hostile_nearby")
    assert r["time_passed"] is False and r["npc_actions"] == []  # not in the room: not a wasted round
    conn.execute("UPDATE characters SET location_id = ? WHERE id = ?", (world["far"], world["cid"]))
    assert run(world, act("rest"))["results"][0]["status"] == "resolved"  # no connected room holds a hostile


def test_rest_allowed_when_the_neighbour_is_not_hostile(world):
    world["conn"].execute("UPDATE characters SET location_id = ? WHERE id = ?", (world["hall"], world["cid"]))
    assert run(world, act("rest"))["results"][0]["status"] == "resolved"


def test_rejected_or_clarify_only_turn_passes_no_time(world):
    r = run(world, act("other", intent="Dance."))  # no hostile here: a rejection is free
    assert r["results"][0]["detail"] == "unrecognized" and r["npc_actions"] == [] and r["expired"] == []
    assert r["time_passed"] is False


def test_rejected_turn_in_combat_is_a_wasted_round(world):
    hostile(world)
    r = run(world, act("rest"), rng=Script(1))  # blocked: enemies present -> the guard still swings
    assert r["results"][0]["detail"] == "enemies_present"
    assert r["time_passed"] is True and r["wasted_round"] is True and r["npc_actions"][0]["action"] == "attack"


def test_malformed_and_ooc_in_combat_never_cost_time(world):
    from rpg.actions import process_action_proposal as process
    hostile(world)
    conn, cid = world["conn"], world["cid"]
    assert process(conn, cid, "not json", rng=Script())["error"] == "invalid_proposal"
    assert process(conn, cid, {"actions": [], "ooc": {"command": "stats", "text": "/stats"},
                               "unresolved_references": []}, rng=Script())["type"] == "ooc"
    assert run(world, act("wait"), rng=Script(1))["turn"] == 1  # nothing above spent a turn


def test_clarify_in_combat_stays_free(world):
    hostile(world)
    a = act("attack", clarification_needed=True, clarification_question="Which one?")
    r = run(world, a)
    assert r["time_passed"] is False and "wasted_round" not in r


def test_player_defense_from_armor_and_shield(world):
    conn, cid = world["conn"], world["cid"]
    assert effects.player_defense(conn, cid) == 12
    give(world, "item_leather_armor")
    inv.equip(conn, cid, "item_leather_armor")
    assert effects.player_defense(conn, cid) == 13        # 11 + DEX 2 (no cap)
    inv.equip(conn, cid, "item_buckler")
    assert effects.player_defense(conn, cid) == 14        # + buckler 1


# --- death ---------------------------------------------------------------------------------------

def test_death_at_zero_hp_stops_further_attacks(world):
    hostile(world)
    hostile(world, "npc_dozing_guard")
    set_hp(world, 2)
    r = run(world, act("wait"), rng=Script(15, 4))  # first guard kills; the second never rolls
    assert [x["npc"] for x in r["npc_actions"]] == ["npc_tall_guard"]
    assert r["npc_actions"][0]["hp_band"] == "down"
    assert r["defeat"] == {"type": "death", "description": actions_death()}
    again = run(world, act("wait"))
    assert again["results"] == [] and again["defeat"]["type"] == "death" and again["npc_actions"] == []


def actions_death():
    from rpg.actions import DEATH_DESCRIPTION
    return DEATH_DESCRIPTION


# --- flash powder --------------------------------------------------------------------------------

def test_flash_powder_blinds_target_and_cancels_its_attack(world):
    hostile(world)
    give(world, "item_flash_powder")
    r = run(world, act("use_item", target="npc_tall_guard", items_used=["item_flash_powder"]), rng=Script(5))
    cons = r["results"][0]["consequences"]
    assert {"type": "npc_condition", "key": "npc_tall_guard", "npc_name": "Tall Guard", "name": "blinded",
            "outcome": "applied"} in cons
    assert any(c["type"] == "item_consumed" for c in cons)
    assert not inv.has_item(world["conn"], world["cid"], "item_flash_powder")
    assert r["npc_actions"] == [{"npc": "npc_tall_guard", "name": "Tall Guard", "action": "none",
                                 "reason": "blinded", "outcome": None, "degree": None, "hp_band": "unhurt"}]
    assert r["expired"] == []  # 1 round: lasts until the NPC's next action (BUG-R01)
    r = run(world, act("wait"), rng=Script(1))  # expires before the NPC phase, so it attacks again
    assert {"type": "npc_condition_expired", "key": "npc_tall_guard", "npc_name": "Tall Guard",
            "name": "blinded"} in r["expired"]
    assert r["npc_actions"][0]["action"] == "attack"


def test_flash_powder_resisted(world):
    hostile(world)
    give(world, "item_flash_powder")
    r = run(world, act("use_item", target="npc_tall_guard", items_used=["item_flash_powder"]), rng=Script(15, 1))
    assert r["results"][0]["consequences"][0]["outcome"] == "resisted"
    assert r["npc_actions"][0]["action"] == "attack"


def test_flash_powder_without_target_bursts_over_everyone_here(world):
    give(world, "item_flash_powder")
    r = run(world, act("use_item", items_used=["item_flash_powder"]), rng=Script(5, 5))
    keys = [c["key"] for c in r["results"][0]["consequences"] if c["type"] == "npc_condition"]
    assert keys == ["npc_tall_guard", "npc_dozing_guard"]  # the out-of-reach archer is spared


def test_attack_on_blinded_npc_has_advantage(world):
    give(world, "item_flash_powder")
    r = run(world, act("use_item", target="npc_tall_guard", items_used=["item_flash_powder"]),
            act("attack", target="npc_tall_guard", items_used=["item_rusty_shortsword"]),
            rng=Script(5, 2, 18, 1))  # save fails; attack 2/18 keep 18; damage 1
    assert r["results"][1]["outcome"] == "success"
    assert r["npc_actions"][0]["reason"] == "blinded"  # angered but blinded


# --- bracing tonic -------------------------------------------------------------------------------

def test_tonic_grants_advantage_once_then_is_spent(world):
    give(world, "item_bracing_tonic")
    r = run(world, act("use_item", items_used=["item_bracing_tonic"]))
    assert r["results"][0]["consequences"][0]["name"] == "braced"
    assert "braced" in r["player_status"]["conditions"]
    r = run(world, act("skill_check", skill="athletics", est_difficulty="easy"), rng=Script(3, 12))
    res = r["results"][0]
    assert res["outcome"] == "success"  # kept the 12 (12 - 1 >= 10)
    assert {"type": "condition_removed", "target": "self", "name": "braced", "reason": "spent"} in res["consequences"]
    assert "braced" not in r["player_status"]["conditions"]


def test_tonic_disadvantage_on_wis_and_expires_with_time(world):
    give(world, "item_bracing_tonic")
    run(world, act("use_item", items_used=["item_bracing_tonic"]))
    r = run(world, act("skill_check", skill="insight", est_difficulty="easy"), rng=Script(15, 2))
    assert r["results"][0]["outcome"] == "failure"  # kept the 2
    expired_on = None
    for i in range(2, 12):  # drinking was turn 1, insight turn 2
        r = run(world, act("wait"))
        if r["expired"]:
            expired_on = r["turn"]
            assert r["expired"] == [{"type": "condition_expired", "target": "self", "name": "braced"}]
            break
    assert expired_on == 10  # "10 minutes" = 10 turns


# --- light and darkness --------------------------------------------------------------------------

@pytest.fixture
def dark_cellar(world):
    conn = world["conn"]
    conn.execute("UPDATE locations SET dark = 1 WHERE key = 'loc_cellar'")
    conn.execute("UPDATE characters SET location_id = ? WHERE id = ?", (world["cellar"], world["cid"]))
    db.add_item(conn, "item_coin", location_id=world["cellar"], key="item_floor_coin")
    return world


def test_examine_in_the_dark_is_blocked(dark_cellar):
    r = run(dark_cellar, act("examine", target="item_floor_coin"))
    assert (r["results"][0]["reason"], r["results"][0]["detail"]) == ("impossible_action", "too_dark")
    r = run(dark_cellar, act("examine", target="loc_cellar"))  # looking around shows the dark (BUG-R02)
    assert r["results"][0]["status"] == "resolved"
    assert r["results"][0]["target"]["description"] == "It is pitch dark; you can see nothing."
    r = run(dark_cellar, act("examine", target="item_rusty_shortsword"))  # by touch: fine
    assert r["results"][0]["status"] == "resolved"


def test_search_in_the_dark_has_disadvantage(dark_cellar):
    r = run(dark_cellar, act("skill_check", skill="perception", est_difficulty="easy"), rng=Script(15, 4))
    res = r["results"][0]
    assert res["outcome"] == "failure"  # kept the 4
    assert {"type": "darkness", "description": "hard to see in the dark"} in res["consequences"]


def test_lit_light_source_dispels_darkness_and_burns_out(dark_cellar):
    w = dark_cellar
    give(w, "item_light_source")
    r = run(w, act("use_item", items_used=["item_light_source"]))
    assert r["results"][0]["state_changes"] == [{"type": "entity_state", "key": "item_light_source",
                                                "name": "Torch", "field": "state", "from": "unlit", "to": "lit"}]
    assert effects.location_is_lit(w["conn"], w["cellar"], w["cid"])
    assert run(w, act("examine", target="loc_cellar"))["results"][0]["status"] == "resolved"
    assert inv.get_item(w["conn"], "item_light_source")["timer"] == 58  # 1 hour = 60 turns, 2 ticked
    w["conn"].execute("UPDATE items SET timer = 1 WHERE key = 'item_light_source'")
    r = run(w, act("wait"))
    assert r["expired"] == [{"type": "light_burnt_out", "key": "item_light_source", "name": "Torch"}]
    assert inv.get_item(w["conn"], "item_light_source") is None
    assert not effects.location_is_lit(w["conn"], w["cellar"], w["cid"])


def test_light_toggles_off_and_must_be_held(dark_cellar):
    w = dark_cellar
    give(w, "item_light_source")
    run(w, act("use_item", items_used=["item_light_source"]))
    r = run(w, act("use_item", items_used=["item_light_source"]))
    assert r["results"][0]["state_changes"][0]["to"] == "unlit"
    db.add_item(w["conn"], "item_light_source", location_id=w["cellar"], key="item_wall_torch")
    r = run(w, act("use_item", items_used=["item_wall_torch"]))
    assert (r["results"][0]["reason"], r["results"][0]["detail"]) == ("item_not_owned", "light_not_held")


def test_lit_light_lying_in_room_lights_it(dark_cellar):
    w = dark_cellar
    db.add_item(w["conn"], "item_light_source", location_id=w["cellar"], key="item_wall_torch")
    w["conn"].execute("UPDATE items SET state = 'lit' WHERE key = 'item_wall_torch'")
    assert effects.location_is_lit(w["conn"], w["cellar"], w["cid"])


# --- rations / rest ------------------------------------------------------------------------------

def test_rest_with_ration_heals_fully_and_eats_it(world):
    give(world, "item_rations")
    set_hp(world, 3)
    r = run(world, act("rest"))
    cons = r["results"][0]["consequences"]
    assert hp(world) == 10 and any(c["type"] == "item_consumed" and c["key"] == "item_rations" for c in cons)
    assert not inv.has_item(world["conn"], world["cid"], "item_rations")


def test_rest_without_ration_heals_half(world):
    set_hp(world, 3)
    run(world, act("rest"))
    assert hp(world) == 7  # 3 + ceil(7 / 2)


# --- tools ---------------------------------------------------------------------------------------

def test_pick_lock_needs_lockpicks(world):
    inv.remove_item(world["conn"], "item_lockpicks")
    r = run(world, act("skill_check", skill="sleight_of_hand", target="loc_cellar"))
    assert (r["results"][0]["reason"], r["results"][0]["detail"]) == ("impossible_action", "needs_tool")


def test_held_lockpicks_count_without_items_used(world):
    r = run(world, act("skill_check", skill="sleight_of_hand", target="loc_cellar"), rng=Script(15))
    res = r["results"][0]
    assert res["outcome"] == "success" and res["items_used"] == ["item_lockpicks"]
    assert res["state_changes"][0]["type"] == "exit_state"


def test_crowbar_caps_force_open_dc_and_grants_advantage(world):
    conn = world["conn"]
    conn.execute("UPDATE location_exits SET unlock_dc = 28 WHERE direction = 'down'")
    conn.execute("UPDATE characters SET str_score = 16 WHERE id = ?", (world["cid"],))
    r = run(world, act("skill_check", skill="athletics", target="loc_cellar"))
    assert r["results"][0]["impossible"]  # 28 - 3 = 25 needed: no roll
    inv.transfer(conn, "item_crowbar", to_character=world["cid"])
    r = run(world, act("skill_check", skill="athletics", target="loc_cellar"), rng=Script(4, 17))
    res = r["results"][0]
    assert res["outcome"] == "success" and res["state_changes"][0]["type"] == "exit_state"  # 17 + 3 vs 20
    assert {"type": "tool_used", "key": "item_crowbar", "name": "Crowbar"} in res["consequences"]


def test_tool_skill_bonus_needs_relevance(world):
    inv.transfer(world["conn"], "item_crowbar", to_character=world["cid"])
    r = run(world, act("skill_check", skill="athletics", est_difficulty="easy"), rng=Script(9))  # held, unrelated
    assert not any(c["type"] == "tool_used" for c in r["results"][0]["consequences"])
    r = run(world, act("skill_check", skill="athletics", est_difficulty="easy", items_used=["item_crowbar"]),
            rng=Script(2, 12))
    assert r["results"][0]["outcome"] == "success"


def test_lockpicks_open_a_locked_item(world):
    conn = world["conn"]
    db.add_item(conn, "item_coin", location_id=world["store"], key="item_strongbox")
    conn.execute("UPDATE items SET state = 'locked' WHERE key = 'item_strongbox'")
    r = run(world, act("skill_check", skill="sleight_of_hand", target="item_strongbox"), rng=Script(15))
    assert r["results"][0]["state_changes"] == [{"type": "entity_state", "key": "item_strongbox",
                                                "field": "state", "from": "locked", "to": "open"}]


def test_flash_powder_angers_a_neutral_npc(world):
    give(world, "item_flash_powder")
    r = run(world, act("use_item", target="npc_dozing_guard", items_used=["item_flash_powder"]), rng=Script(5))
    assert {"type": "npc_disposition_changed", "key": "npc_dozing_guard", "to": "hostile"} in r["results"][0]["consequences"]
    assert r["npc_actions"][0]["reason"] == "blinded"


# --- Story Designer decisions (condition_effect, improvised picks, digit-free names) -------------

def test_flash_powder_reads_condition_effect_from_content(world):
    conn = world["conn"]
    db.add_item_template(conn, {"id": "item_dazzle_powder", "name": "Dazzle Powder", "category": "consumable",
                                "stats": {"effect": "condition", "condition": "dazzled", "duration": "1 round",
                                          "condition_effect": {"no_attack": True}, "uses": 1}})
    hostile(world)
    give(world, "item_dazzle_powder")
    r = run(world, act("use_item", target="npc_tall_guard", items_used=["item_dazzle_powder"]))  # no save
    assert r["npc_actions"][0] == {"npc": "npc_tall_guard", "name": "Tall Guard", "action": "none",
                                   "reason": "dazzled", "outcome": None, "degree": None, "hp_band": "unhurt"}


def test_flash_powder_condition_effect_can_leave_attacks_on(world):
    conn = world["conn"]
    tid = conn.execute("SELECT id, stats FROM item_templates WHERE key = 'item_flash_powder'").fetchone()
    stats = json.loads(tid["stats"])
    stats["condition_effect"] = {"no_attack": False}
    conn.execute("UPDATE item_templates SET stats = ? WHERE id = ?", (json.dumps(stats), tid["id"]))
    hostile(world)
    give(world, "item_flash_powder")
    r = run(world, act("use_item", target="npc_tall_guard", items_used=["item_flash_powder"]), rng=Script(5, 1))
    assert r["npc_actions"][0]["action"] == "attack"


def test_tonic_reads_condition_effect_from_content(world):
    conn = world["conn"]
    db.add_item_template(conn, {"id": "item_iron_tonic", "name": "Iron Tonic", "category": "consumable",
                                "stats": {"effect": "buff", "duration": "3 minutes", "uses": 1,
                                          "condition_name": "ironskin",
                                          "condition_effect": {"advantage": ["con"]}}})
    give(world, "item_iron_tonic")
    r = run(world, act("use_item", items_used=["item_iron_tonic"]))
    assert "ironskin" in r["player_status"]["conditions"]
    r = run(world, act("skill_check", skill="con", est_difficulty="easy"), rng=Script(2, 18))
    assert r["results"][0]["outcome"] == "success"


def test_improvised_pick_allows_lock_at_very_hard(world, caplog):
    conn = world["conn"]
    db.add_item_template(conn, {"id": "item_knife", "name": "Knife", "category": "weapon", "slot": "main_hand",
                                "stats": {"damage": "1d4"}, "tags": ["improvised_pick"]})
    inv.remove_item(conn, "item_lockpicks")
    assert run(world, act("skill_check", skill="sleight_of_hand", target="loc_cellar"))["results"][0]["detail"] \
        == "needs_tool"
    give(world, "item_knife")
    with caplog.at_level("DEBUG", logger="rpg.debug"):
        r = run(world, act("skill_check", skill="sleight_of_hand", target="loc_cellar"), rng=Script(18))
    res = r["results"][0]
    assert "'dc': 25" in caplog.text and res["items_used"] == ["item_knife"]
    assert res["outcome"] == "failure"  # 18 + 4 = 22 < 25


def test_real_lockpicks_keep_the_authored_dc(world, caplog):
    with caplog.at_level("DEBUG", logger="rpg.debug"):
        run(world, act("skill_check", skill="sleight_of_hand", target="loc_cellar"), rng=Script(15))
    assert "'dc': 18" in caplog.text


@pytest.mark.parametrize("name", ["Coil of Rope (50 ft)", "Bundle of Arrows (20)", "Rations x3"])
def test_item_names_with_digits_are_rejected(world, name):
    with pytest.raises(ValueError, match="digits"):
        db.add_item_template(world["conn"], {"id": "item_bad", "name": name, "category": "misc"})
    with pytest.raises(ValueError, match="digits"):
        db.add_item(world["conn"], "item_coin", character_id=world["cid"], display_name=name)


def test_demo_rope_is_digit_free():
    from rpg.world import TEMPLATES
    rope = next(t for t in TEMPLATES if t["id"] == "item_rope")
    assert rope["name"] == "Hempen Rope" and rope["stats"]["length_ft"] == 50
    assert not any(ch.isdigit() for t in TEMPLATES for ch in t["name"])

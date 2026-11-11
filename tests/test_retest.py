"""Regression tests for the retest report playtest/2026-11-10-retest-da16509.md (BUG-R01..R08),
driven through the mock interpreter on the demo slice, as the playtester played."""
import random

import pytest

from rpg import actions, db, llm as llm_mod
from rpg.context import build_context
from rpg.world import seed_demo_slice
from test_actions import Script
from test_cli import play


@pytest.fixture
def demo(tmp_path):
    conn = db.get_connection(tmp_path / "r.db")
    db.create_schema(conn)
    cid = seed_demo_slice(conn)
    yield conn, cid
    conn.close()


def go(demo, where):
    conn, cid = demo
    conn.execute("UPDATE characters SET location_id = (SELECT id FROM locations WHERE key = ?) WHERE id = ?",
                 (where, cid))


def say(demo, text, *rolls):
    conn, cid = demo
    llm = llm_mod.MockLLM()
    ctx = build_context(conn, cid)
    p = llm.interpret(text, ctx)
    r = actions.process_action_proposal(conn, cid, p, rng=Script(*rolls))
    return r, llm_mod.narrate_safely(llm, r, build_context(conn, cid))


def blinded(demo):
    return demo[0].execute("SELECT COUNT(*) FROM npc_conditions WHERE name = 'blinded'").fetchone()[0]


# R01 (high) --------------------------------------------------------------------------------------

def test_r01_one_round_blind_lasts_into_the_next_attack(demo, caplog):
    go(demo, "loc_storeroom")
    r, _ = say(demo, "throw flash powder at guard", 5)  # guard's CON save: 5 + 1 < 15
    assert r["npc_actions"][0]["action"] == "none" and r["npc_actions"][0]["reason"] == "blinded"
    assert r["expired"] == [] and blinded(demo) == 1   # not expired on the throw turn
    with caplog.at_level("DEBUG", logger="rpg.debug"):
        r, _ = say(demo, "attack guard", 3, 15, 4, 1)  # advantage: two d20s, keeps 15; dmg 4; guard misses (1)
    assert "'rolls': [3, 15]" in caplog.text and "'advantage': 1" in caplog.text
    assert r["results"][0]["outcome"] == "success"
    assert {"type": "npc_condition_expired", "key": "npc_dozing_guard", "npc_name": "Dozing Guard",
            "name": "blinded"} in r["expired"]
    assert r["npc_actions"][0]["action"] == "attack"   # blind over: he swings this turn
    assert blinded(demo) == 0


# R02 (medium) ------------------------------------------------------------------------------------

def test_r02_walking_into_the_dark_says_so_and_lists_exits(demo):
    conn, cid = demo
    go(demo, "loc_storeroom")
    conn.execute("UPDATE location_exits SET locked = 0")
    r, text = say(demo, "down")
    moved = r["results"][0]["state_changes"][0]
    assert moved["type"] == "moved_to_location" and moved["dark"] is True
    assert "pitch dark" in text and "Ways out: up" in text


def test_r02_look_in_the_dark_renders_the_dark_scene(demo):
    go(demo, "loc_cellar")
    r, text = say(demo, "look")
    assert r["results"][0]["status"] == "resolved" and r["time_passed"] is True
    assert "pitch dark" in text and "up" in text and "too dark" not in text


def test_r02_lit_arrival_is_unchanged(demo):
    r, text = say(demo, "go east")
    assert r["results"][0]["state_changes"][0]["dark"] is False and "pitch dark" not in text


# R03 (medium) ------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["pull rusty lever", "pull lever"])
def test_r03_lever_by_touch_in_the_dark(demo, text):
    conn, cid = demo
    go(demo, "loc_cellar")
    ctx = build_context(conn, cid)
    assert ctx["visible_items"] == [] and [i["key"] for i in ctx["felt_items"]] == ["item_rusty_lever"]
    r, narration = say(demo, text)
    res = r["results"][0]
    assert res["status"] == "resolved" and res["items_used"] == ["item_rusty_lever"]
    assert db.get_flag(conn, "cellar_gate_opened") is True
    assert "clunks" in narration


def test_r03_examine_lever_in_dark_is_by_touch_not_the_sword(demo):
    go(demo, "loc_cellar")
    r, text = say(demo, "examine rusty lever")  # v0.6: a felt fixture is known by touch (R2-05)
    assert r["results"][0]["target"]["key"] == "item_rusty_lever"
    assert "by touch" in text and "Shortsword" not in text


def test_r03_felt_items_only_fixtures_and_only_in_the_dark(demo):
    conn, cid = demo
    go(demo, "loc_cellar")
    assert "item_rope" not in {i["key"] for i in build_context(conn, cid)["felt_items"]}
    conn.execute("UPDATE items SET state = 'lit' WHERE key = 'item_torch'")
    assert build_context(conn, cid)["felt_items"] == []


# R04 (medium) ------------------------------------------------------------------------------------

def test_r04_bare_talk_picks_the_only_npc(demo):
    r, text = say(demo, "talk")  # Mara is in the square
    assert r["results"][0]["status"] == "resolved" and r["results"][0]["target"]["key"] == "npc_mara"
    assert "no one here" not in text


def test_r04_bare_talk_engine_side_with_null_target(demo):
    conn, cid = demo
    a = {"action_type": "talk", "intent_summary": "Talk.", "approach": "small_talk"}
    r = actions.process_action_proposal(conn, cid, {"actions": [a], "ooc": None, "unresolved_references": []},
                                        rng=Script())
    assert r["results"][0]["target"]["key"] == "npc_mara"


def test_r04_bare_talk_with_several_asks_which(demo):
    conn, cid = demo
    conn.execute("UPDATE npcs SET location_id = (SELECT id FROM locations WHERE key='loc_town_square')")
    r, text = say(demo, "talk")
    assert r["results"][0]["status"] == "clarify" and r["time_passed"] is False
    assert r["clarification_question"] == "Who do you want to talk to: Mara or Dozing Guard?"


def test_r04_hostile_npc_present_still_talkable(demo):
    conn, cid = demo
    go(demo, "loc_storeroom")
    conn.execute("UPDATE npcs SET disposition = -100 WHERE key = 'npc_dozing_guard'")
    r, text = say(demo, "talk", 1)
    assert r["results"][0]["target"]["key"] == "npc_dozing_guard" and "no one here" not in text


def test_r04_nobody_here_keeps_the_rejection(demo):
    go(demo, "loc_cellar")
    r, text = say(demo, "talk")
    assert r["results"][0]["detail"] == "no_target" and r["time_passed"] is False


# R05 (low) ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["use crowbar", "use lockpick set"])
def test_r05_tool_with_no_target_is_rejected_and_free(demo, text):
    conn, cid = demo
    conn.execute("UPDATE items SET character_id = ?, location_id = NULL WHERE key = 'item_crowbar'", (cid,))
    r, narration = say(demo, text)
    res = r["results"][0]
    assert (res["status"], res["detail"]) == ("rejected", "needs_target") and r["time_passed"] is False
    assert "manage it" not in narration


def test_r05_weapon_on_self_does_nothing(demo):
    conn, cid = demo
    a = {"action_type": "use_item", "intent_summary": "Use the sword.", "target": "self",
         "items_used": ["item_rusty_shortsword"]}
    r = actions.process_action_proposal(conn, cid, {"actions": [a], "ooc": None, "unresolved_references": []},
                                        rng=Script())
    assert r["results"][0]["status"] == "rejected" and r["time_passed"] is False


# R06 (low) ---------------------------------------------------------------------------------------

def test_r06_scene_consumable_is_not_from_your_pack(demo):
    r, text = say(demo, "drink tonic")  # the tonic lies in the square
    removed = [c for c in r["results"][0]["state_changes"] if c["type"] == "item_removed"]
    assert removed and removed[0]["from"] == "scene"
    assert "your pack" not in text


def test_r06_held_consumable_still_from_pack(demo):
    conn, cid = demo
    conn.execute("UPDATE characters SET current_hp = 3")
    r, text = say(demo, "drink healing draught", 4, 4, 4, 4)
    assert "gone from your pack" in text


# R07 (low) ---------------------------------------------------------------------------------------

def test_r07_continuing_a_dead_save_has_no_greeting(tmp_path):
    g, _ = play(tmp_path, "/quit", new=True)
    with db.connect(g.path) as conn:
        conn.execute("UPDATE characters SET current_hp = 0")
    g, out = play(tmp_path, "q")
    text = "\n".join(out)
    assert "What do you do?" not in text and "Your story ends here. Start a [N]ew game, or [Q]uit?" in text


# R08 (low) ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["search", "search the room", "rummage around"])
def test_r08_search_in_the_dark_is_a_check_at_disadvantage(demo, text, caplog):
    go(demo, "loc_cellar")
    with caplog.at_level("DEBUG", logger="rpg.debug"):
        r, narration = say(demo, text, 15, 4)
    res = r["results"][0]
    assert res["status"] == "resolved" and res["skill"] == "investigation"
    assert "'advantage': -1" in caplog.text
    assert {"type": "darkness", "description": "hard to see in the dark"} in res["consequences"]


# ---- retest of 9127044 (playtest/2026-11-10-retest-9127044.md) ------------------------------------

def test_r2_01_talk_to_absent_npc_is_rejected_not_retargeted(demo):
    go(demo, "loc_storeroom")
    r, text = say(demo, "talk to mara")
    res = r["results"][0]
    assert (res["status"], res["reason"], res["detail"]) == ("rejected", "target_not_present", "elsewhere")
    assert res["target"] == {"key": "npc_mara", "name": "Mara"} and "talk" not in res
    assert r["time_passed"] is False and r["turn"] == 0
    assert "Mara isn't here." in text and "Dozing Guard" not in text


def test_r2_01_ask_absent_npc_parses_as_talk(demo):
    go(demo, "loc_storeroom")
    p = llm_mod.MockLLM().interpret("ask mara about the cellar", build_context(*demo))
    a = p["actions"][0]
    assert (a["action_type"], a["target"], a["topic"]) == ("talk", "npc_mara", None)


def test_r2_07_absent_npc_line_is_people_shaped(demo):
    go(demo, "loc_storeroom")
    _, text = say(demo, "ask mara about the cellar")
    assert "Mara isn't here." in text and "that isn't here" not in text
    _, text = say(demo, "talk to the blacksmith")
    assert "There's nobody like that here." in text


@pytest.mark.parametrize("text", ["ask mara about the lever in the cellar", "ask mara about the hidden lever",
                                  "ask mara about mara_past", "ask mara about her dealings with the merchant",
                                  "ask mara about fencing stolen goods", "ask mara about cellar_lever",
                                  "lie to mara about being the merchant's partner"])
def test_r2_06_no_loose_topic_match_and_unknown(demo, text):
    conn, cid = demo
    p = llm_mod.MockLLM().interpret(text, build_context(conn, cid))
    assert p["actions"][0]["topic"] is None
    r, narration = say(demo, text, 2, 2)
    t = r["results"][0]["talk"]
    assert t["requested_topic_status"] == "unknown" and t["allowed_topics"] == []
    for secret in ("fence stolen goods", "old lever in the cellar wall", "sleeps like a stone"):
        assert secret not in narration


@pytest.mark.parametrize("text,topic", [("ask mara about the cellar", "the_cellar"),
                                        ("ask mara about the cellar door", "the_cellar"),
                                        ("ask mara about her stall", "mara_self"),
                                        ("ask mara about market news", "market_news"),
                                        ("ask mara about the guard", "guard_sleeps")])
def test_r2_06_keys_labels_and_aliases_still_match(demo, text, topic):
    p = llm_mod.MockLLM().interpret(text, build_context(*demo))
    assert p["actions"][0]["topic"] == topic


def test_r2_06_secret_aliases_hidden_below_band(demo):
    ctx = build_context(*demo)
    mara = next(n for n in ctx["npcs_present"] if n["key"] == "npc_mara")
    assert "the hidden lever" not in str(mara)  # cellar_lever (allied) and its aliases stay out of context


# R2-02 / R2-03 / R2-04: trades -------------------------------------------------------------------

def gold(demo):
    return demo[0].execute("SELECT gold FROM characters").fetchone()[0]


@pytest.mark.parametrize("text,detail", [("sell shortsword to mara", "no_such_trade"),
                                         ("buy a dragon from mara", "no_such_trade"),
                                         ("sell rope to mara", "trade_item_missing")])
def test_r2_02_unmatched_buy_or_sell_is_rejected_never_offers_rations(demo, text, detail):
    r, narration = say(demo, text)
    res = r["results"][0]
    assert res["status"] == "rejected" and res["detail"] == detail and r["time_passed"] is False
    assert "rations" not in narration and "can't cover" not in narration
    assert gold(demo) == 60


def test_r2_02_one_off_trade_done_is_named(demo):
    conn, cid = demo
    db.add_item(conn, "item_rope", character_id=cid)
    r, _ = say(demo, "sell rope to mara")
    assert r["results"][0]["talk"]["trade"]["status"] == "completed" and gold(demo) == 68
    db.add_item(conn, "item_rope", character_id=cid)
    r, narration = say(demo, "sell rope to mara")
    assert r["results"][0]["detail"] == "trade_done" and "already done" in narration and gold(demo) == 68


def test_r2_03_missing_item_line_is_not_about_coins(demo):
    _, narration = say(demo, "sell rope to mara")
    assert "You don't have what that deal needs." in narration and "cover" not in narration


@pytest.mark.parametrize("text", ["buy -1 torch from mara", "buy 0 torches from mara",
                                  "buy 999 torches from mara", "buy 11 torches from mara"])
def test_r2_04_bad_quantities_are_rejected(demo, text):
    conn, cid = demo
    torches = len([1 for i in build_context(conn, cid)["player_inventory"] if i["name"] == "Torch"])
    r, narration = say(demo, text)
    res = r["results"][0]
    assert (res["status"], res["detail"]) == ("rejected", "bad_quantity") and r["time_passed"] is False
    assert gold(demo) == 60
    assert len([1 for i in build_context(conn, cid)["player_inventory"] if i["name"] == "Torch"]) == torches


def test_r2_04_valid_quantity_repeats_a_repeatable_trade(demo):
    r, _ = say(demo, "buy two torches from mara")
    t = r["results"][0]["talk"]["trade"]
    assert t["status"] == "completed" and t["quantity"] == 2 and t["wants"] == [{"gold": 10}]
    assert gold(demo) == 50


def test_r2_04_quantity_on_a_one_off_is_rejected(demo):
    conn, cid = demo
    db.add_item(conn, "item_rope", character_id=cid, quantity=3)
    r, _ = say(demo, "sell three ropes to mara")
    assert r["results"][0]["detail"] == "bad_quantity"


def test_r2_04_validator_rejects_non_integer_quantity():
    a = {"action_type": "talk", "target": "npc_mara", "intent_summary": "Buy.", "approach": "trade",
         "topic": "trade_buy_torch", "quantity": "lots"}
    _, errors = actions.validate_proposal({"actions": [a], "ooc": None, "unresolved_references": []})
    assert errors and errors[0]["path"] == "actions[0].quantity"


def test_r2_02_bare_trade_lists_options(demo):
    r, narration = say(demo, "trade with mara")
    t = r["results"][0]["talk"]["trade"]
    assert t["key"] is None and len(t["options"]) == 4 and gold(demo) == 60


# R2-05: searching and feeling in the dark ----------------------------------------------------------

def test_r2_05_successful_dark_search_finds_fixtures_and_items(demo):
    conn, cid = demo
    go(demo, "loc_cellar")
    r, text = say(demo, "search", 20, 18)  # disadvantage keeps 18: 18 + 3 >= 15
    res = r["results"][0]
    assert res["outcome"] == "success"
    assert {f["key"] for f in res["felt"]} == {"item_rusty_lever", "item_rope"}
    assert "By touch you find" in text and "Rusty lever" in text and "Hempen Rope" in text
    felt = {i["key"] for i in build_context(conn, cid)["felt_items"]}
    assert felt == {"item_rusty_lever", "item_rope"}  # remembered


def test_r2_05_failed_dark_search_finds_nothing_new(demo):
    conn, cid = demo
    go(demo, "loc_cellar")
    r, text = say(demo, "search", 2, 2)
    assert r["results"][0]["outcome"] == "failure" and "felt" not in r["results"][0]
    assert {i["key"] for i in build_context(conn, cid)["felt_items"]} == {"item_rusty_lever"}


def test_r2_05_feel_around_is_the_touch_path(demo):
    go(demo, "loc_cellar")
    r, text = say(demo, "feel around")  # no dice scripted: no roll
    res = r["results"][0]
    assert res["status"] == "resolved" and res["skill"] is None
    assert [f["key"] for f in res["felt"]] == ["item_rusty_lever"] and "By touch you find the Rusty lever" in text


@pytest.mark.parametrize("text", ["feel for the lever", "feel lever", "feel the rusty lever"])
def test_r2_05_feel_for_the_lever_by_touch(demo, text):
    go(demo, "loc_cellar")
    p = llm_mod.MockLLM().interpret(text, build_context(*demo))
    assert p["actions"][0]["intent_summary"] in ("Feel for the lever.", "Feel for the rusty lever.")
    r, narration = say(demo, text)
    res = r["results"][0]
    assert res["status"] == "resolved" and res["target"]["key"] == "item_rusty_lever" and res["skill"] is None
    assert "the for" not in narration and "by touch" in narration


def test_r2_05_rope_only_by_touch_after_a_search(demo):
    go(demo, "loc_cellar")
    r, _ = say(demo, "examine rope")
    assert r["results"][0]["status"] == "rejected"
    say(demo, "search", 20, 18)
    r, _ = say(demo, "examine rope")
    assert r["results"][0]["status"] == "resolved"


# --- Lead decisions v0.6 (5:52 PM): wasted rounds, rest near hostiles, pursuit ----------------------

def hostile_guard(demo):
    demo[0].execute("UPDATE npcs SET disposition = -80 WHERE key = 'npc_dozing_guard'")


def test_lead_flee_rest_and_pursuit_on_the_demo_slice(demo):
    conn = demo[0]
    go(demo, "loc_storeroom")
    hostile_guard(demo)
    r, out = say(demo, "go west")
    assert [a["action"] for a in r["npc_actions"]] == ["follow"] and "comes after you" in out
    r, out = say(demo, "rest", 1)  # it's right here now: blocked, and in combat that's a wasted round
    assert r["results"][0]["detail"] == "enemies_present" and r["wasted_round"] and "attacks" in out
    r, out = say(demo, "go east", 1)  # flee again: no second pursuit
    assert r["npc_actions"] == [] and "comes after you" not in out
    r, out = say(demo, "rest")  # but it's next door
    assert r["results"][0]["detail"] == "hostile_nearby" and r["time_passed"] is False
    assert "next door" in out
    guard_loc = conn.execute("SELECT l.key FROM npcs n JOIN locations l ON l.id = n.location_id "
                             "WHERE n.key = 'npc_dozing_guard'").fetchone()[0]
    assert guard_loc == "loc_town_square"


def test_lead_pursuit_into_the_dark_cellar(demo):
    conn = demo[0]
    go(demo, "loc_storeroom")
    hostile_guard(demo)
    conn.execute("UPDATE location_exits SET locked = 0")
    r, out = say(demo, "go down")
    assert [a["action"] for a in r["npc_actions"]] == ["follow"] and "comes after you" in out
    r, _ = say(demo, "wait", 1)
    assert [a["action"] for a in r["npc_actions"]] == ["attack"]


def test_lead_guard_stats_unchanged(demo):
    g = demo[0].execute("SELECT defense_dc, max_hp, attack_bonus, damage, save_bonus FROM npcs "
                        "WHERE key = 'npc_dozing_guard'").fetchone()
    assert tuple(g) == (11, 12, 3, "1d6", 1)

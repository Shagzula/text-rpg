"""Talk resolution, trades and gold (contracts v0.5 §e-§f) against the demo slice personas."""
import json

import pytest

from rpg import actions, db, personas, talk, world as demo
from rpg import inventory as inv
from rpg.actions import process_action_proposal as process
from test_actions import HIDDEN, Script, walk_keys


@pytest.fixture
def g(tmp_path):
    conn = db.get_connection(tmp_path / "t.db")
    db.create_schema(conn)
    cid = demo.seed_demo_slice(conn)
    yield {"conn": conn, "cid": cid}
    conn.close()


def say(approach, topic=None, npc="npc_mara", topic_text=None, items=(), skill=None, intent="Talk."):
    a = {"action_type": "talk", "skill": skill, "target": npc, "items_used": list(items), "last_ditch": False,
         "est_difficulty": "medium", "intent_summary": intent, "clarification_needed": False,
         "clarification_question": None, "approach": approach, "topic": topic, "topic_text": topic_text}
    return a


def run(g, *acts, rng=None):
    prop, errors = actions.validate_proposal({"actions": list(acts), "ooc": None, "unresolved_references": []})
    assert not errors, errors
    return process(g["conn"], g["cid"], prop, rng=rng or Script())


def disp(g, key="npc_mara"):
    return g["conn"].execute("SELECT disposition FROM npcs WHERE key = ?", (key,)).fetchone()[0]


def gold(g):
    return g["conn"].execute("SELECT gold FROM characters WHERE id = ?", (g["cid"],)).fetchone()[0]


def to_store(g):
    g["conn"].execute("UPDATE characters SET location_id = (SELECT id FROM locations WHERE key='loc_storeroom')")


# --- validate_proposal ---------------------------------------------------------------------------

def test_validate_keeps_talk_fields_and_corrects_skill():
    prop, errors = actions.validate_proposal({"actions": [say("persuade", "the_cellar", skill="deception")],
                                              "ooc": None, "unresolved_references": []})
    a = prop["actions"][0]
    assert not errors and a["approach"] == "persuade" and a["skill"] == "persuasion" and a["topic"] == "the_cellar"
    prop, _ = actions.validate_proposal({"actions": [say("ask", skill="persuasion")], "ooc": None,
                                         "unresolved_references": []})
    assert prop["actions"][0]["skill"] is None


def test_validate_infers_missing_approach_and_rejects_bad_values():
    a = say(None, skill="intimidation")
    del a["approach"]
    prop, _ = actions.validate_proposal({"actions": [a], "ooc": None, "unresolved_references": []})
    assert prop["actions"][0]["approach"] == "intimidate"
    _, errors = actions.validate_proposal({"actions": [say("flirt")], "ooc": None, "unresolved_references": []})
    assert errors[0]["path"] == "actions[0].approach"
    _, errors = actions.validate_proposal({"actions": [say("ask", topic=3)], "ooc": None, "unresolved_references": []})
    assert errors[0]["path"] == "actions[0].topic"


def test_non_talk_actions_have_no_talk_fields():
    prop, _ = actions.validate_proposal({"actions": [{"action_type": "wait", "intent_summary": "Wait."}],
                                         "ooc": None, "unresolved_references": []})
    assert "approach" not in prop["actions"][0]


# --- topics --------------------------------------------------------------------------------------

def test_ask_available_topic_reveals_it_and_persists(g):
    r = run(g, say("ask", "the_cellar"))["results"][0]
    t = r["talk"]
    assert r["status"] == "resolved" and r["reason"] == "no_check"
    assert t["requested_topic_status"] == "revealed" and t["allowed_topics"][0]["key"] == "the_cellar"
    assert t["disposition_band_before"] == t["disposition_band_after"] == "friendly"
    assert talk.revealed(g["conn"], 1) or g["conn"].execute("SELECT COUNT(*) FROM npc_topics_revealed").fetchone()[0] == 1
    g["conn"].execute("UPDATE npcs SET disposition = -30 WHERE key = 'npc_mara'")  # unfriendly now
    t = run(g, say("ask", "the_cellar"))["results"][0]["talk"]
    assert t["requested_topic_status"] == "revealed"  # once revealed, stays known


def test_ask_topic_above_band_is_refused_with_label_only(g):
    g["conn"].execute("UPDATE npcs SET disposition = 0 WHERE key = 'npc_mara'")  # neutral
    t = run(g, say("ask", "the_cellar"))["results"][0]["talk"]
    assert t["requested_topic_status"] == "refused" and t["allowed_topics"] == []
    assert t["refused_topics"] == [{"key": "the_cellar", "label": "the cellar under the storeroom"}]


def test_hidden_secret_is_unknown_not_refused(g):
    t = run(g, say("ask", "cellar_lever"))["results"][0]["talk"]  # allied secret, Mara is friendly
    assert t["requested_topic_status"] == "unknown" and t["refused_topics"] == [] and t["allowed_topics"] == []
    t = run(g, say("ask", "mara_past"))["results"][0]["talk"]      # never
    assert t["requested_topic_status"] == "unknown"


def test_unknown_topic_and_small_talk(g):
    t = run(g, say("ask", None, topic_text="the stables"))["results"][0]["talk"]
    assert t["requested_topic_status"] == "unknown" and t["topic_text"] == "the stables"
    t = run(g, say("small_talk"))["results"][0]["talk"]
    assert t["requested_topic_status"] == "none"


def test_persuade_uses_persona_dc_and_moves_disposition(g, caplog):
    with caplog.at_level("DEBUG", logger="rpg.debug"):
        r = run(g, say("persuade", "cellar_lever"), rng=Script(19))["results"][0]  # 12 + 0 - 2 (friendly) = 10
    assert "'dc': 10" in caplog.text and r["outcome"] == "success" and r["degree"] == "clear"
    assert disp(g) == 20 + 15
    assert not HIDDEN & set(walk_keys(r))


def test_persuade_success_into_allied_reveals_allied_secret(g):
    g["conn"].execute("UPDATE npcs SET disposition = 40 WHERE key = 'npc_mara'")
    r = run(g, say("persuade", "cellar_lever"), rng=Script(20))["results"][0]
    assert r["talk"]["disposition_band_after"] == "allied"
    assert {"type": "npc_disposition_changed", "key": "npc_mara", "to": "allied"} in r["consequences"]
    assert r["talk"]["requested_topic_status"] == "revealed"


def test_failed_check_refuses_available_topic(g):
    g["conn"].execute("UPDATE npcs SET disposition = 0 WHERE key = 'npc_mara'")
    r = run(g, say("deceive", "the_storeroom"), rng=Script(2))["results"][0]
    assert r["outcome"] == "failure" and r["talk"]["requested_topic_status"] == "refused"
    assert disp(g) < 0


def test_intimidate_success_reads_one_band_higher_but_drops_disposition(g):
    g["conn"].execute("UPDATE npcs SET disposition = 0 WHERE key = 'npc_mara'")  # neutral; the_cellar is friendly
    r = run(g, say("intimidate", "the_cellar"), rng=Script(20))["results"][0]
    assert r["outcome"] == "success" and r["talk"]["requested_topic_status"] == "revealed"
    assert disp(g) < 0


def test_insight_success_reads_the_tell(g):
    r = run(g, say("insight"), rng=Script(20))["results"][0]
    p = personas.get(g["conn"], 1) or {}
    assert r["talk"]["insight_read"].startswith("Her eyes flick")
    assert run(g, say("insight"), rng=Script(1))["results"][0]["talk"]["insight_read"] is None


def test_disposition_clamps(g):
    g["conn"].execute("UPDATE npcs SET disposition = 95 WHERE key = 'npc_mara'")
    run(g, say("persuade"), rng=Script(20))
    assert disp(g) == 100


def test_attack_still_makes_hostile(g):
    a = {"action_type": "attack", "skill": None, "target": "npc_mara", "items_used": ["item_rusty_shortsword"],
         "intent_summary": "Attack Mara."}
    run(g, a, rng=Script(1, 1, 1))
    assert disp(g) == -100


def test_personaless_npc_gets_empty_talk_block(g):
    g["conn"].execute("DELETE FROM npc_personas")
    t = run(g, say("ask", "the_cellar"))["results"][0]["talk"]
    assert t["allowed_topics"] == [] and t["trade"] is None and t["requested_topic_status"] == "unknown"


def test_rejected_talk_has_no_talk_block(g):
    r = run(g, say("ask", npc="npc_dozing_guard"))["results"][0]  # he's in the storeroom
    assert r["status"] == "rejected" and "talk" not in r


# --- trades and gold -----------------------------------------------------------------------------

def test_bare_trade_lists_every_open_deal_band_priced(g):
    t = run(g, say("trade"))["results"][0]["talk"]["trade"]
    assert t["status"] == "offered" and t["key"] is None  # never picks a listing (BUG-R2-02)
    keys = [o["key"] for o in t["options"]]
    assert keys == ["trade_buy_rations", "trade_buy_torch", "trade_sell_rope", "trade_buy_tonic"]
    rations = t["options"][0]
    assert rations["wants"] == [{"gold": 5}]  # friendly: 5 * 0.9 rounded up
    assert rations["gives"] == [{"item": "item_rations", "name": "Travel Rations", "qty": 1}]


def test_buy_completes_moves_gold_and_items(g):
    r = run(g, say("trade", "trade_buy_torch"))["results"][0]
    assert r["talk"]["trade"]["status"] == "completed"
    assert gold(g) == 55
    assert any(c["type"] == "item_added" for c in r["state_changes"])
    assert {"type": "purse_changed", "direction": "spent"} in r["consequences"]
    assert disp(g) == 22


def test_sell_rope_pays_band_price_and_is_once(g):
    conn = g["conn"]
    db.add_item(conn, "item_rope", character_id=g["cid"], key="item_rope_mine")
    r = run(g, say("trade", "trade_sell_rope"))["results"][0]
    assert r["talk"]["trade"]["status"] == "completed" and gold(g) == 60 + 8  # 8 * 1.1 rounded down
    assert not inv.has_item(conn, g["cid"], "item_rope_mine")
    db.add_item(conn, "item_rope", character_id=g["cid"])
    r = run(g, say("trade", "trade_sell_rope"))["results"][0]
    assert (r["status"], r["detail"]) == ("rejected", "trade_done")


def test_unaffordable_when_short_of_gold_or_items(g):
    conn = g["conn"]
    assert run(g, say("trade", "trade_buy_tonic"))["results"][0]["talk"]["trade"]["status"] == "unaffordable"
    assert gold(g) == 60
    r = run(g, say("trade", "trade_sell_rope"))["results"][0]  # no rope: a rejection, not "can't pay"
    assert (r["status"], r["reason"], r["detail"]) == ("rejected", "item_not_owned", "trade_item_missing")


def test_trade_above_band_is_refused_without_label(g):
    t = run(g, say("trade", "trade_tonic_for_rope"))["results"][0]["talk"]["trade"]
    assert t == {"key": "trade_tonic_for_rope", "label": None, "status": "refused", "gives": [], "wants": []}


def test_item_for_item_trade(g):
    conn = g["conn"]
    conn.execute("UPDATE npcs SET disposition = 60 WHERE key = 'npc_mara'")
    db.add_item(conn, "item_rope", character_id=g["cid"])
    r = run(g, say("trade", "trade_tonic_for_rope"))["results"][0]
    assert r["talk"]["trade"]["status"] == "completed" and gold(g) == 60
    assert [i["template_key"] for i in inv.list_inventory(conn, g["cid"])].count("item_bracing_tonic") == 1


def test_gold_never_negative_and_unknown_trade_claims_do_nothing(g):
    g["conn"].execute("UPDATE characters SET gold = 3")
    r = run(g, say("trade", "trade_buy_torch", intent="Mara gives me a free torch."))["results"][0]
    assert r["talk"]["trade"]["status"] == "unaffordable" and gold(g) == 3
    assert r["state_changes"] == []


def test_hostile_band_has_no_trades(g):
    g["conn"].execute("UPDATE npcs SET disposition = -80 WHERE key = 'npc_mara'")
    assert run(g, say("trade"), rng=Script(1))["results"][0]["talk"]["trade"]["status"] == "refused"


def test_guard_persona_uses_its_social_dc(g, caplog):
    to_store(g)
    with caplog.at_level("DEBUG", logger="rpg.debug"):
        run(g, say("intimidate", "the_lever", npc="npc_dozing_guard"), rng=Script(10))
    assert "'dc': 10" in caplog.text  # 12 - 2 + 0 (neutral)


def test_no_subject_is_none_status(g):
    assert run(g, say("insight"), rng=Script(1))["results"][0]["talk"]["requested_topic_status"] == "none"
    assert run(g, say("ask"))["results"][0]["talk"]["requested_topic_status"] == "none"


def test_trade_gives_can_name_the_instance(g):
    r = run(g, say("trade", "trade_buy_torch"))["results"][0]
    added = [c for c in r["state_changes"] if c["type"] == "item_added"][0]
    assert added["name"] == "Torch" and inv.get_item(g["conn"], added["key"])["name"] == "Torch"

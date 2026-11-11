"""The Story Designer's v0.6 example persona (content/npcs/npc_example_gatekeeper.yaml), installed into the
demo slice and driven through the mock interpreter: the behaviour the content README says it verified."""
from pathlib import Path

import pytest

from rpg import actions, db, llm as llm_mod, personas, talk, yamlish
from rpg.context import build_context
from rpg.world import seed_demo_slice
from test_actions import Script

CONTENT = Path(__file__).resolve().parents[1] / "content"


@pytest.fixture
def gk(tmp_path):
    conn = db.get_connection(tmp_path / "gk.db")
    db.create_schema(conn)
    cid = seed_demo_slice(conn)
    have = {r[0] for r in conn.execute("SELECT key FROM item_templates")}
    for t in yamlish.load(CONTENT / "items_tier1.yaml"):
        if t["id"] not in have:
            db.add_item_template(conn, t)
    conn.execute("INSERT INTO npcs (key, name, location_id) SELECT 'npc_example_gatekeeper', 'Gatekeeper', "
                 "location_id FROM characters WHERE id = ?", (cid,))
    keys = [r[0] for r in conn.execute("SELECT key FROM item_templates")]
    personas.install(conn, personas.load_file(CONTENT / "npcs" / "npc_example_gatekeeper.yaml", keys))
    yield conn, cid
    conn.close()


def say(gk, text):
    conn, cid = gk
    m = llm_mod.MockLLM()
    p = m.interpret(text, build_context(conn, cid))
    return p, actions.process_action_proposal(conn, cid, p, rng=Script())


def gold(gk):
    return gk[0].execute("SELECT gold FROM characters WHERE id = ?", (gk[1],)).fetchone()[0]


def test_gatekeeper_installs_with_combat_block_and_aliases(gk):
    row = gk[0].execute("SELECT defense_dc, max_hp, attack_bonus, damage, disposition FROM npcs "
                        "WHERE key = 'npc_example_gatekeeper'").fetchone()
    assert tuple(row) == (12, 8, 3, "1d6", -5)
    npc = next(n for n in build_context(*gk)["npcs_present"] if n["key"] == "npc_example_gatekeeper")
    trades = {t["key"]: t for t in npc["trades"]}
    assert "rations" in trades["trade_buy_rations"]["aliases"]
    assert all("side_entrance" != t["key"] for t in npc["topics"])  # allied secret hidden at neutral


def test_gatekeeper_bare_trade_lists_options(gk):
    _, r = say(gk, "trade with the gatekeeper")
    tr = r["results"][0]["talk"]["trade"]
    assert tr["status"] == "offered" and tr["key"] is None
    assert {o["key"] for o in tr["options"]} == {"trade_whistle_for_rations", "trade_buy_rations", "trade_sell_rope"}


def test_gatekeeper_three_rations_cost_eighteen(gk):
    before = gold(gk)
    p, r = say(gk, "buy three rations from the gatekeeper")
    assert p["actions"][0]["topic"] == "trade_buy_rations" and p["actions"][0]["quantity"] == 3
    tr = r["results"][0]["talk"]["trade"]
    assert tr["status"] == "completed" and tr["quantity"] == 3 and before - gold(gk) == 18


def test_gatekeeper_rejections(gk):
    _, r = say(gk, "sell rope to the gatekeeper")  # no rope held
    assert (r["results"][0]["reason"], r["results"][0]["detail"]) == ("item_not_owned", "trade_item_missing")
    _, r = say(gk, "buy a horse from the gatekeeper")
    assert r["results"][0]["detail"] == "no_such_trade"
    a = {"action_type": "talk", "skill": None, "target": "npc_example_gatekeeper", "items_used": [],
         "last_ditch": False, "est_difficulty": "medium", "intent_summary": "Buy rations.",
         "clarification_needed": False, "clarification_question": None, "approach": "trade",
         "topic": "trade_buy_rations", "topic_text": None, "quantity": 11}
    prop, errors = actions.validate_proposal({"actions": [a], "ooc": None, "unresolved_references": []})
    assert not errors
    r = actions.process_action_proposal(gk[0], gk[1], prop, rng=Script())
    assert r["results"][0]["detail"] == "bad_quantity" and talk.MAX_TRADE_QTY == 10


def test_gatekeeper_topic_alias(gk):
    p, r = say(gk, "ask the gatekeeper about closing time")
    assert p["actions"][0]["topic"] == "gate_rules"
    assert r["results"][0]["talk"]["requested_topic_status"] == "revealed"


def test_gatekeeper_values_listed_gifts(gk):
    conn, cid = gk
    db.add_item(conn, "item_light_source", character_id=cid, key="item_spare_torch")
    _, r = say(gk, "give the spare torch to the gatekeeper")
    assert r["results"][0]["talk"]["gift"]["status"] == "accepted"
    assert conn.execute("SELECT disposition FROM npcs WHERE key = 'npc_example_gatekeeper'").fetchone()[0] == 5

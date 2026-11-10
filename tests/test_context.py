import json

import pytest

from rpg import context, db, rules
from helpers import build_world


@pytest.fixture
def world(tmp_path):
    conn = db.get_connection(tmp_path / "w.db")
    db.create_schema(conn)
    w = build_world(conn)
    yield conn, w
    conn.close()


def test_build_context_shape(world):
    conn, w = world
    ctx = context.build_context(conn, w["cid"])
    assert set(ctx) == {"location", "npcs_present", "visible_items", "player_inventory", "player_sheet",
                        "active_conditions", "hp_band", "recent_narrations"}
    loc = ctx["location"]
    assert (loc["key"], loc["name"]) == ("loc_storeroom", "Storeroom")
    assert loc["description"] == "A cramped room of shelves and crates."
    exits = {e["to"]: e for e in loc["exits"]}
    assert exits["loc_cellar"]["state"] == "locked" and exits["loc_cellar"]["direction"] == "down"
    assert exits["loc_back_hall"]["state"] == "open"
    npcs = {n["key"]: n for n in ctx["npcs_present"]}
    assert set(npcs) == {"npc_tall_guard", "npc_dozing_guard", "npc_archer"}  # Mara is elsewhere
    assert npcs["npc_tall_guard"]["disposition"] == "unfriendly" and npcs["npc_tall_guard"]["hp_band"] == "unhurt"
    items = {i["key"]: i for i in ctx["visible_items"]}
    assert items["item_hanging_lamp"]["fixture"] and "fixture" in items["item_hanging_lamp"]["tags"]
    assert not items["item_crowbar"]["fixture"] and items["item_shelf_antidote"]["out_of_reach"]
    inv = {i["key"]: i for i in ctx["player_inventory"]}
    assert inv["item_rusty_shortsword"]["equipped"] and inv["item_rusty_shortsword"]["name"] == "Rusty Shortsword"
    assert inv["item_coins"]["quantity"] == 5
    assert ctx["hp_band"] == "unhurt" and ctx["recent_narrations"] == []
    json.dumps(ctx)  # serializable


def test_player_sheet(world):
    conn, w = world
    db.add_condition(conn, w["cid"], rules.Condition("injured_str", {"abilities": {"str": -2}}, lasting=True,
                                                     description="Hurt yourself"))
    db.set_skill(conn, w["cid"], "stealth", "expertise")
    ctx = context.build_context(conn, w["cid"])
    sheet = ctx["player_sheet"]
    assert sheet["abilities"]["str"] == {"score": 8, "mod": -1}
    assert sheet["abilities"]["dex"] == {"score": 14, "mod": 2}
    assert sheet["skills"] == {"perception": "proficient", "sleight_of_hand": "proficient", "stealth": "expertise"}
    assert sheet["proficiency_bonus"] == 2 and sheet["weapon_proficiencies"] == ["simple"]
    assert sheet["conditions"] == [{"name": "injured_str", "description": "Hurt yourself", "lasting": True}]
    assert ctx["active_conditions"] == sheet["conditions"]


def test_hp_band_and_defeated_npc(world):
    conn, w = world
    conn.execute("UPDATE characters SET current_hp = 3 WHERE id = ?", (w["cid"],))
    conn.execute("UPDATE npcs SET alive = 0, current_hp = 0 WHERE key = 'npc_dozing_guard'")
    ctx = context.build_context(conn, w["cid"])
    assert ctx["hp_band"] == "badly_wounded"
    dozing = [n for n in ctx["npcs_present"] if n["key"] == "npc_dozing_guard"][0]
    assert dozing["status"] == "defeated" and dozing["hp_band"] == "down"


def test_recent_narrations_last_three_oldest_first(world):
    conn, w = world
    for t in range(1, 6):
        context.log_narration(conn, w["cid"], t, f"narration {t}")
    assert context.build_context(conn, w["cid"])["recent_narrations"] == ["narration 3", "narration 4", "narration 5"]


def test_context_without_location(tmp_path):
    conn = db.get_connection(tmp_path / "x.db")
    db.create_schema(conn)
    cid = db.create_character(conn, "Lost")
    ctx = context.build_context(conn, cid)
    assert ctx["location"] is None and ctx["npcs_present"] == [] and ctx["visible_items"] == []

"""Shared test world: a storeroom with a locked cellar door, two guards, fixtures, gear."""
from rpg import db

TEMPLATES = [
    {"id": "item_short_blade", "name": "Short Blade", "category": "weapon", "slot": "main_hand",
     "weight": 2, "value": 10, "stats": {"subcategory": "simple_melee", "damage": "1d6",
                                         "damage_type": "piercing", "proficiency": "simple",
                                         "properties": ["finesse", "light"]}, "tags": ["blade"]},
    {"id": "item_long_blade", "name": "Long Blade", "category": "weapon", "slot": "main_hand",
     "stats": {"damage": "1d8", "proficiency": "martial", "properties": ["versatile"]}},
    {"id": "item_heavy_blade", "name": "Heavy Two-Handed Blade", "category": "weapon", "slot": "two_hands",
     "stats": {"damage": "2d6", "proficiency": "martial", "properties": ["heavy", "two_handed"]}},
    {"id": "item_short_bow", "name": "Short Bow", "category": "weapon", "slot": "two_hands",
     "stats": {"damage": "1d6", "proficiency": "simple", "properties": ["ammunition", "two_handed"],
               "range": [80, 320]}},
    {"id": "item_buckler", "name": "Buckler", "category": "shield", "slot": "off_hand", "stats": {"ac_bonus": 1}},
    {"id": "item_leather_armor", "name": "Leather Armor", "category": "armor", "slot": "body",
     "stats": {"armor_type": "light", "base_ac": 11}},
    {"id": "item_travel_boots", "name": "Travel Boots", "category": "armor", "slot": "feet", "stats": {}},
    {"id": "item_healing_draught", "name": "Minor Healing Draught", "category": "consumable", "slot": "belt",
     "stats": {"effect": "heal", "amount": "2d4+2", "uses": 1}},
    {"id": "item_bandage_roll", "name": "Bandage Roll", "category": "consumable", "slot": "belt",
     "stats": {"effect": "heal", "amount": "1d4", "uses": 3,
               "check": {"skill": "medicine", "est_difficulty": "easy"}}},
    {"id": "item_antidote", "name": "Antidote Vial", "category": "consumable", "slot": "belt",
     "stats": {"effect": "cure_condition", "condition": "poisoned", "uses": 1}},
    {"id": "item_crowbar", "name": "Crowbar", "category": "tool", "slot": "none",
     "stats": {"enables": ["force_open"]}},
    {"id": "item_coin", "name": "Coin", "category": "misc", "slot": "none", "stats": {}},
    {"id": "item_hanging_lamp", "name": "Hanging lamp", "category": "utility", "slot": "none",
     "stats": {}, "tags": ["fixture"]},
    {"id": "item_lever", "name": "Rusty lever", "category": "utility", "slot": "none",
     "stats": {"use_sets_flag": "gate_opened"}, "tags": ["fixture"]},
]


def build_world(conn, *, str_score=8, dex=14, level=1, hp=10, skills=None):
    for t in TEMPLATES:
        db.add_item_template(conn, t)
    store = conn.execute("INSERT INTO locations (key, name, description) VALUES "
                         "('loc_storeroom', 'Storeroom', 'A cramped room of shelves and crates.')").lastrowid
    hall = conn.execute("INSERT INTO locations (key, name, description) VALUES "
                        "('loc_back_hall', 'Back Hall', 'A narrow hall.')").lastrowid
    cellar = conn.execute("INSERT INTO locations (key, name, description) VALUES "
                          "('loc_cellar', 'Cellar', 'Damp and dark.')").lastrowid
    far = conn.execute("INSERT INTO locations (key, name) VALUES ('loc_far_tower', 'Far Tower')").lastrowid
    ex = "INSERT INTO location_exits (from_location_id, to_location_id, direction, description, locked, unlock_dc) VALUES (?, ?, ?, ?, ?, ?)"
    conn.execute(ex, (store, hall, "west", "open doorway", 0, None))
    conn.execute(ex, (hall, store, "east", "open doorway", 0, None))
    conn.execute(ex, (store, cellar, "down", "iron-banded door", 1, 18))
    conn.execute(ex, (cellar, store, "up", "iron-banded door", 1, 18))
    npc = "INSERT INTO npcs (key, name, location_id, traits, disposition, defense_dc, max_hp, out_of_reach) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    conn.execute(npc, ("npc_tall_guard", "Tall Guard", store, '["alert"]', -20, 13, 11, 0))
    conn.execute(npc, ("npc_dozing_guard", "Dozing Guard", store, '["drowsy"]', 0, 8, 4, 0))
    conn.execute(npc, ("npc_archer", "Archer on the ledge", store, '["watchful"]', 0, 12, 6, 1))
    conn.execute(npc, ("npc_mara", "Mara", hall, '["gruff"]', 30, 10, 6, 0))
    cid = db.create_character(conn, "Hero", key="pc_hero", level=level, scores={"str": str_score, "dex": dex},
                              max_hp=hp, location_id=store,
                              skills=skills or {"sleight_of_hand": "proficient", "perception": "proficient"})
    db.add_item(conn, "item_short_blade", character_id=cid, equipped=True, display_name="Rusty Shortsword",
                key="item_rusty_shortsword")
    db.add_item(conn, "item_healing_draught", character_id=cid)
    db.add_item(conn, "item_bandage_roll", character_id=cid)
    db.add_item(conn, "item_travel_boots", character_id=cid, equipped=True)
    db.add_item(conn, "item_buckler", character_id=cid)
    db.add_item(conn, "item_heavy_blade", character_id=cid)
    db.add_item(conn, "item_coin", character_id=cid, quantity=5, key="item_coins")
    db.add_item(conn, "item_hanging_lamp", location_id=store)
    db.add_item(conn, "item_lever", location_id=store)
    db.add_item(conn, "item_crowbar", location_id=store)
    db.add_item(conn, "item_antidote", location_id=store, key="item_shelf_antidote")
    conn.execute("UPDATE items SET out_of_reach = 1 WHERE key = 'item_shelf_antidote'")
    db.add_item(conn, "item_long_blade", location_id=far)
    return {"cid": cid, "store": store, "hall": hall, "cellar": cellar, "far": far}

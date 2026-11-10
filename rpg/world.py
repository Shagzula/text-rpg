"""Built-in demo slice: a hub square, a storeroom with a dozing guard, and a locked cellar.
Setting-neutral placeholder content until Dan picks a setting."""
from __future__ import annotations

from . import db, personas

PLAYER_KEY = "pc_player"
# Guard numbers (playtest: combat was trivial at 4 HP / DC 8 and no counterattacks).
DEMO_GOLD = 60  # content README: a 60-unit purse (coins, same unit as item_templates.value)
GUARD = {"defense_dc": 11, "max_hp": 12, "attack_bonus": 3, "damage": "1d6", "save_bonus": 1}

TEMPLATES = [
    {"id": "item_short_blade", "name": "Short Blade", "category": "weapon", "slot": "main_hand", "value": 100,
     "weight": 2, "stats": {"subcategory": "martial_melee", "damage": "1d6", "damage_type": "piercing",
                            "proficiency": "simple", "properties": ["finesse", "light"]},
     "tags": ["blade"], "notes": "A plain, serviceable blade."},
    {"id": "item_travel_boots", "name": "Travel Boots", "category": "armor", "slot": "feet", "value": 20,
     "weight": 2, "stats": {"subcategory": "feet", "armor_type": "accessory"}, "notes": "Scuffed but sturdy."},
    {"id": "item_healing_draught", "name": "Minor Healing Draught", "category": "consumable", "slot": "belt",
     "value": 500, "weight": 0.5, "stats": {"subcategory": "healing", "effect": "heal", "amount": "2d4+2", "uses": 1},
     "notes": "A small vial of red liquid."},
    # Keys and stats below mirror content/items_tier1.yaml.
    {"id": "item_lockpicks", "name": "Lockpick Set", "category": "tool", "slot": "belt", "value": 250,
     "weight": 1, "stats": {"subcategory": "thieves_tools", "enables": ["pick_lock", "disarm_trap"],
                            "skill_bonus": {"skill": "sleight_of_hand", "grants": "required_for_check"},
                            "uses": None}, "tags": ["tool", "illicit"], "notes": "Thin picks in a roll."},
    {"id": "item_crowbar", "name": "Crowbar", "category": "tool", "slot": "belt", "value": 20, "weight": 5,
     "stats": {"subcategory": "lever", "enables": ["force_open"],
               "skill_bonus": {"skill": "athletics", "grants": "advantage"}, "uses": None},
     "tags": ["tool", "noisy", "improvised_weapon"], "notes": "Heavy iron, bent at one end."},
    {"id": "item_rope", "name": "Hempen Rope", "category": "utility", "slot": "none", "value": 10,
     "weight": 10, "stats": {"subcategory": "climbing", "length_ft": 50,
                             "effect": "enables climbing/descending; advantage on athletics climb checks when anchored"},
     "notes": "Fifty feet of hemp."},
    {"id": "item_light_source", "name": "Light Source", "category": "utility", "slot": "off_hand", "value": 5,
     "weight": 1, "stats": {"subcategory": "light", "effect": "light", "bright_radius_ft": 20, "dim_radius_ft": 20,
                            "duration": "1 hour", "uses": 1, "improvised_damage": "1 fire"},
     "tags": ["utility", "light", "fire"], "notes": "A pitch-soaked torch."},
    {"id": "item_rations", "name": "Travel Rations", "category": "consumable", "slot": "none", "value": 5,
     "weight": 2, "stats": {"subcategory": "food", "effect": "sustenance", "uses": 1, "days": 1}, "tags": ["food"],
     "notes": "Hard bread and dried meat."},
    {"id": "item_bracing_tonic", "name": "Bracing Tonic", "category": "consumable", "slot": "belt", "value": 75,
     "weight": 0.5, "stats": {"subcategory": "buff", "effect": "buff", "buff": "advantage on next STR or CON check",
                              "duration": "10 minutes or 1 check", "uses": 1,
                              "side_effect": "disadvantage on WIS checks for duration"},
     "tags": ["drinkable"], "notes": "A bitter green draught."},
    {"id": "item_flash_powder", "name": "Flash Powder Packet", "category": "consumable", "slot": "belt", "value": 50,
     "weight": 0.5, "stats": {"subcategory": "thrown_utility", "effect": "condition", "condition": "blinded",
                              "duration": "1 round", "area": "10 ft",
                              "save": {"ability": "con", "est_difficulty": "medium"}, "uses": 1},
     "tags": ["thrown", "escape"], "notes": "A twist of paper that bursts into blinding light."},
    {"id": "item_hanging_lamp", "name": "Hanging lamp", "category": "utility", "slot": "none",
     "stats": {}, "tags": ["fixture"], "notes": "An oil lamp swinging from a hook, lit."},
    {"id": "item_rusty_lever", "name": "Rusty lever", "category": "utility", "slot": "none",
     "stats": {"use_sets_flag": "cellar_gate_opened"}, "tags": ["fixture"],
     "notes": "An old iron lever set into the wall."},
]


def find_player(conn):
    row = conn.execute("SELECT id FROM characters WHERE is_player = 1 ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def seed_demo_slice(conn) -> int:
    """Create the demo slice if the DB has no player. Returns the player's id."""
    existing = find_player(conn)
    if existing is not None:
        return existing
    for t in TEMPLATES:
        if not conn.execute("SELECT 1 FROM item_templates WHERE key = ?", (t["id"],)).fetchone():
            db.add_item_template(conn, t)
    loc = "INSERT INTO locations (key, name, description, area_tier) VALUES (?, ?, ?, 1)"
    square = conn.execute(loc, ("loc_town_square", "Town Square",
                                "A muddy square ringed by shuttered shops. A storeroom door stands ajar to the east.")).lastrowid
    store = conn.execute(loc, ("loc_storeroom", "Storeroom",
                               "A cramped room of shelves and crates. An iron-banded door leads down.")).lastrowid
    cellar = conn.execute(loc, ("loc_cellar", "Cellar",
                                "Damp stone walls and the smell of old wine. Something glints in the corner.")).lastrowid
    conn.execute("UPDATE locations SET dark = 1 WHERE id = ?", (cellar,))  # bring a light
    ex = ("INSERT INTO location_exits (from_location_id, to_location_id, direction, description, locked, unlock_dc)"
          " VALUES (?, ?, ?, ?, ?, ?)")
    conn.execute(ex, (square, store, "east", "storeroom door", 0, None))
    conn.execute(ex, (store, square, "west", "door to the square", 0, None))
    conn.execute(ex, (store, cellar, "down", "iron-banded door", 1, 15))
    conn.execute(ex, (cellar, store, "up", "iron-banded door", 1, 15))
    npc = ("INSERT INTO npcs (key, name, description, location_id, traits, disposition, defense_dc, max_hp, is_major,"
           " attack_bonus, damage, save_bonus) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
    conn.execute(npc, ("npc_mara", "Mara", "A gruff stallholder packing up for the night.", square,
                       '["gruff", "observant"]', 20, 10, 6, 1, 2, "1d4", 0))
    # A real fight: hits the premade (defense 12) on 9+ for 1d6; ~3 trades to drop him.
    conn.execute(npc, ("npc_dozing_guard", "Dozing Guard", "A guard asleep on a stool, keys at his belt.", store,
                       '["drowsy"]', 0, GUARD["defense_dc"], GUARD["max_hp"], 0, GUARD["attack_bonus"],
                       GUARD["damage"], GUARD["save_bonus"]))
    # Placeholder premade (STR 8 / INT 16), per content drafts.
    cid = db.create_character(conn, "You", key=PLAYER_KEY, level=1,
                              scores={"str": 8, "dex": 14, "con": 12, "int": 16, "wis": 12, "cha": 10},
                              max_hp=10, location_id=square,
                              skills={"arcana": "proficient", "investigation": "proficient",
                                      "sleight_of_hand": "proficient", "perception": "proficient"})
    db.add_item(conn, "item_short_blade", character_id=cid, equipped=True, key="item_rusty_shortsword",
                display_name="Rusty Shortsword")
    db.add_item(conn, "item_travel_boots", character_id=cid, equipped=True)
    db.add_item(conn, "item_healing_draught", character_id=cid)
    db.add_item(conn, "item_lockpicks", character_id=cid)
    db.add_item(conn, "item_light_source", character_id=cid, key="item_torch", display_name="Torch")
    db.add_item(conn, "item_rations", character_id=cid)
    db.add_item(conn, "item_crowbar", location_id=square)
    db.add_item(conn, "item_bracing_tonic", location_id=square)
    db.add_item(conn, "item_hanging_lamp", location_id=store)
    db.add_item(conn, "item_flash_powder", location_id=store)
    db.add_item(conn, "item_rusty_lever", location_id=cellar)
    db.add_item(conn, "item_rope", location_id=cellar)
    conn.execute("UPDATE characters SET gold = ? WHERE id = ?", (DEMO_GOLD, cid))
    personas.install_dir(conn)  # rpg/data/npcs/*.yaml -> npc_personas (+ combat, disposition)
    conn.execute("INSERT INTO save_games (name, character_id, turn) VALUES ('autosave', ?, 0)", (cid,))
    return cid

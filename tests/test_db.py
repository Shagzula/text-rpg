import json
import sqlite3

import pytest

from rpg import db, rules

EXPECTED_TABLES = {"schema_meta", "factions", "locations", "location_exits", "characters",
                   "character_skills", "character_conditions", "item_templates", "items",
                   "npcs", "relationships", "world_flags", "save_games", "narration_log"}


@pytest.fixture
def path(tmp_path):
    return db.init_db(tmp_path / "save.db")


def test_schema_created_with_version(path):
    with db.connect(path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert EXPECTED_TABLES <= tables
        assert conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "4"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    db.init_db(path)  # idempotent


SHORT_BLADE = {  # content shape from items_tier1.yaml
    "id": "item_short_blade", "name": "Short Blade", "category": "weapon",
    "subcategory": "simple_melee", "power_tier": 1, "slot": "main_hand", "value": 10, "weight": 2,
    "stats": {"damage": "1d6", "damage_type": "piercing", "properties": ["finesse", "light"]},
    "tags": ["blade", "concealable"], "notes": "Default finesse sidearm.",
}


def test_insert_and_read_world(path):
    with db.connect(path) as conn:
        fid = conn.execute("INSERT INTO factions (key, name, traits) VALUES ('fac_watch', 'Watch', '[\"lawful\"]')").lastrowid
        town = conn.execute("INSERT INTO locations (key, name, area_tier, faction_id) VALUES ('loc_town', 'Town', 1, ?)", (fid,)).lastrowid
        cave = conn.execute("INSERT INTO locations (key, name, area_tier) VALUES ('loc_cave', 'Cave', 1)").lastrowid
        conn.execute("INSERT INTO location_exits (from_location_id, to_location_id, direction, locked, unlock_dc)"
                     " VALUES (?, ?, 'east', 1, 14)", (town, cave))
        conn.execute("INSERT INTO npcs (key, name, location_id, faction_id, traits, disposition) VALUES ('npc_mara', 'Mara', ?, ?, ?, 20)",
                     (town, fid, json.dumps(["gruff"])))
        db.add_item_template(conn, SHORT_BLADE)
        cid = db.create_character(conn, "Hero", level=2, scores={"str": 8, "dex": 15}, max_hp=12,
                                  location_id=town, skills={"stealth": "expertise", "perception": "proficient"})
        db.add_item(conn, "item_short_blade", character_id=cid, equipped=True, display_name="Rusty blade")
        db.add_item(conn, "item_short_blade", location_id=cave, key="item_cave_blade")
        conn.execute("INSERT INTO world_flags (key, value) VALUES ('quest_started', 'true')")
        conn.execute("INSERT INTO save_games (name, character_id) VALUES ('slot1', ?)", (cid,))

    with db.connect(path) as conn:
        c = db.load_character(conn, cid)
        assert c.scores["dex"] == 15 and c.level == 2
        assert c.skills == {"stealth": "expertise", "perception": "proficient"}
        assert rules.skill_modifier(c, "stealth") == 2 + 4
        inv = conn.execute("SELECT i.key, i.display_name, i.equipped, t.key AS tkey, t.slot, t.weight, t.value, t.stats, t.description"
                           " FROM inventory i JOIN item_templates t ON t.id = i.template_id WHERE character_id = ?", (cid,)).fetchall()
        assert len(inv) == 1
        inv = inv[0]
        assert inv["display_name"] == "Rusty blade" and inv["equipped"] == 1 and inv["key"].startswith("item_")
        assert (inv["tkey"], inv["slot"], inv["weight"], inv["value"]) == ("item_short_blade", "main_hand", 2, 10)
        stats = json.loads(inv["stats"])
        assert stats["damage"] == "1d6" and stats["subcategory"] == "simple_melee"
        assert inv["description"] == "Default finesse sidearm."
        on_floor = conn.execute("SELECT key FROM items WHERE location_id = ?", (cave,)).fetchall()
        assert [r["key"] for r in on_floor] == ["item_cave_blade"]
        ex = conn.execute("SELECT * FROM location_exits WHERE from_location_id = ?", (town,)).fetchone()
        assert ex["to_location_id"] == cave and ex["unlock_dc"] == 14
        assert json.loads(conn.execute("SELECT value FROM world_flags WHERE key='quest_started'").fetchone()[0]) is True


def test_foreign_keys_enforced(path):
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(path) as conn:
            conn.execute("INSERT INTO items (key, character_id, template_id) VALUES ('x', 999, 999)")


@pytest.mark.parametrize("sql", [
    "INSERT INTO factions (key, name) VALUES ('dup', 'A'), ('dup', 'B')",  # key unique
    "INSERT INTO item_templates (key, name, category) VALUES ('t', 'T', 'spaceship')",  # category enum
    "INSERT INTO item_templates (key, name, category, slot) VALUES ('t', 'T', 'shield', 'tail')",  # slot enum
])
def test_content_constraints(path, sql):
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(path) as conn:
            conn.execute(sql)


def test_item_needs_exactly_one_owner(path):
    with db.connect(path) as conn:
        db.add_item_template(conn, SHORT_BLADE)
        loc = conn.execute("INSERT INTO locations (key, name) VALUES ('l', 'L')").lastrowid
        cid = db.create_character(conn, "Hero")
    for kw in ({}, {"character_id": cid, "location_id": loc}):
        with pytest.raises(sqlite3.IntegrityError):
            with db.connect(path) as conn:
                db.add_item(conn, "item_short_blade", **kw)


def test_check_constraints(path):
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(path) as conn:
            db.create_character(conn, "Bad", scores={"str": 40})


def test_conditions_roundtrip_and_affect_checks(path):
    with db.connect(path) as conn:
        cid = db.create_character(conn, "Hero", scores={"str": 12})
        db.add_condition(conn, cid, rules.Condition("injured_str", {"abilities": {"str": -2}}, lasting=True))
    with db.connect(path) as conn:
        c = db.load_character(conn, cid)
        assert c.conditions[0].lasting and rules.skill_modifier(c, "athletics") == 1 - 2


def test_starter_format_db_rejected(tmp_path):
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE characters (id INTEGER PRIMARY KEY, name TEXT)")
    conn.close()
    with pytest.raises(RuntimeError):
        db.init_db(p)


def test_db_path_env_var(tmp_path, monkeypatch):
    target = tmp_path / "sub" / "env.db"
    monkeypatch.setenv("RPG_DB_PATH", str(target))
    assert db.default_db_path() == target
    assert db.init_db() == target and target.exists()


def test_default_db_path_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("RPG_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    p = db.default_db_path()
    assert p.is_absolute() and p == db.PROJECT_ROOT / "saves" / "rpg_state.db"


def test_instance_keys_follow_template_slug(path):
    with db.connect(path) as conn:
        db.add_item_template(conn, SHORT_BLADE)
        cid = db.create_character(conn, "Hero", key="pc_hero")
        keys = [conn.execute("SELECT key FROM items WHERE id = ?",
                             (db.add_item(conn, "item_short_blade", character_id=cid),)).fetchone()[0]
                for _ in range(3)]
        assert keys == ["item_short_blade", "item_short_blade_2", "item_short_blade_3"]
        assert db.character_id_by_key(conn, "pc_hero") == cid
        assert db.character_id_by_key(conn, "nobody") is None


def test_subcategory_accepted_inside_or_outside_stats(path):
    with db.connect(path) as conn:
        db.add_item_template(conn, {"id": "item_a", "name": "A", "category": "tool",
                                    "stats": {"subcategory": "lever"}})
        db.add_item_template(conn, {"id": "item_b", "name": "B", "category": "tool", "subcategory": "lever"})
        for k in ("item_a", "item_b"):
            stats = json.loads(conn.execute("SELECT stats FROM item_templates WHERE key=?", (k,)).fetchone()[0])
            assert stats == {"subcategory": "lever"}


V2_SNIPPET = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO schema_meta VALUES ('schema_version', '2');
CREATE TABLE locations (id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL, description TEXT,
    area_tier INTEGER NOT NULL DEFAULT 1, parent_id INTEGER, faction_id INTEGER);
INSERT INTO locations (key, name) VALUES ('loc_old', 'Old');
CREATE TABLE items (id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, template_id INTEGER NOT NULL,
    character_id INTEGER, location_id INTEGER, display_name TEXT, quantity INTEGER NOT NULL DEFAULT 1,
    equipped INTEGER NOT NULL DEFAULT 0, uses_left INTEGER, state TEXT, out_of_reach INTEGER NOT NULL DEFAULT 0,
    notes TEXT, acquired_at TEXT);
CREATE TABLE npcs (id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, name TEXT NOT NULL, description TEXT,
    location_id INTEGER, faction_id INTEGER, traits TEXT NOT NULL DEFAULT '[]', disposition INTEGER NOT NULL DEFAULT 0,
    defense_dc INTEGER NOT NULL DEFAULT 10, max_hp INTEGER NOT NULL DEFAULT 4, current_hp INTEGER,
    out_of_reach INTEGER NOT NULL DEFAULT 0, is_major INTEGER NOT NULL DEFAULT 0, alive INTEGER NOT NULL DEFAULT 1,
    notes TEXT);
INSERT INTO npcs (key, name) VALUES ('npc_old', 'Old Guard');
CREATE TABLE characters (id INTEGER PRIMARY KEY, key TEXT UNIQUE, name TEXT NOT NULL);
INSERT INTO characters (name) VALUES ('Old Hero');
"""


def test_v2_save_migrates_to_current(tmp_path):
    p = tmp_path / "v2.db"
    c = sqlite3.connect(p)
    c.executescript(V2_SNIPPET)
    c.close()
    db.init_db(p)
    with db.connect(p) as conn:
        assert conn.execute("SELECT value FROM schema_meta").fetchone()[0] == "4"
        assert conn.execute("SELECT dark FROM locations").fetchone()[0] == 0
        n = conn.execute("SELECT attack_bonus, damage, save_bonus FROM npcs").fetchone()
        assert tuple(n) == (2, "1d4", 0)
        assert "timer" in {r[1] for r in conn.execute("PRAGMA table_info(items)")}
        assert conn.execute("SELECT COUNT(*) FROM npc_conditions").fetchone()[0] == 0
        assert conn.execute("SELECT gold FROM characters").fetchone()[0] == 0
        for t in ("npc_personas", "npc_topics_revealed", "npc_dialogue_log"):
            assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0

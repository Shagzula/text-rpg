import json
import sqlite3

import pytest

from rpg import db, rules

EXPECTED_TABLES = {"schema_meta", "factions", "locations", "location_exits", "characters",
                   "character_skills", "character_conditions", "item_templates", "items",
                   "npcs", "relationships", "world_flags", "save_games"}


@pytest.fixture
def path(tmp_path):
    return db.init_db(tmp_path / "save.db")


def test_schema_created_with_version(path):
    with db.connect(path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert EXPECTED_TABLES <= tables
        assert conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0] == "1"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    db.init_db(path)  # idempotent


SHORT_BLADE = {  # content shape from items_tier1.yaml
    "id": "wpn_short_blade", "name": "Short Blade", "category": "weapon",
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
        db.add_item(conn, "wpn_short_blade", character_id=cid, equipped=True, display_name="Rusty blade")
        db.add_item(conn, "wpn_short_blade", location_id=cave, key="item_cave_blade")
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
        assert (inv["tkey"], inv["slot"], inv["weight"], inv["value"]) == ("wpn_short_blade", "main_hand", 2, 10)
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
                db.add_item(conn, "wpn_short_blade", **kw)


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

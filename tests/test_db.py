import json
import sqlite3

import pytest

from rpg import db, rules

EXPECTED_TABLES = {"schema_meta", "factions", "locations", "location_exits", "characters",
                   "character_skills", "character_conditions", "item_templates", "inventory",
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


def test_insert_and_read_world(path):
    with db.connect(path) as conn:
        fid = conn.execute("INSERT INTO factions (name, traits) VALUES ('Watch', '[\"lawful\"]')").lastrowid
        town = conn.execute("INSERT INTO locations (name, area_tier, faction_id) VALUES ('Town', 1, ?)", (fid,)).lastrowid
        cave = conn.execute("INSERT INTO locations (name, area_tier) VALUES ('Cave', 1)").lastrowid
        conn.execute("INSERT INTO location_exits (from_location_id, to_location_id, direction, locked, unlock_dc)"
                     " VALUES (?, ?, 'east', 1, 14)", (town, cave))
        conn.execute("INSERT INTO npcs (name, location_id, faction_id, traits, disposition) VALUES ('Mara', ?, ?, ?, 20)",
                     (town, fid, json.dumps(["gruff"])))
        conn.execute("INSERT INTO item_templates (id, name, category, power_tier, stats, tags) VALUES "
                     "('shortsword', 'Shortsword', 'weapon', 1, ?, '[\"finesse\"]')",
                     (json.dumps({"damage_dice": "1d6", "damage_type": "piercing"}),))
        cid = db.create_character(conn, "Hero", level=2, scores={"str": 8, "dex": 15}, max_hp=12,
                                  location_id=town, skills={"stealth": "expertise", "perception": "proficient"})
        conn.execute("INSERT INTO inventory (character_id, template_id, display_name, quantity, equipped)"
                     " VALUES (?, 'shortsword', 'Rusty blade', 1, 1)", (cid,))
        conn.execute("INSERT INTO world_flags (key, value) VALUES ('quest_started', 'true')")
        conn.execute("INSERT INTO save_games (name, character_id) VALUES ('slot1', ?)", (cid,))

    with db.connect(path) as conn:
        c = db.load_character(conn, cid)
        assert c.scores["dex"] == 15 and c.level == 2
        assert c.skills == {"stealth": "expertise", "perception": "proficient"}
        assert rules.skill_modifier(c, "stealth") == 2 + 4
        inv = conn.execute("SELECT i.display_name, t.stats FROM inventory i JOIN item_templates t "
                           "ON t.id = i.template_id WHERE character_id = ?", (cid,)).fetchone()
        assert inv["display_name"] == "Rusty blade" and json.loads(inv["stats"])["damage_dice"] == "1d6"
        ex = conn.execute("SELECT * FROM location_exits WHERE from_location_id = ?", (town,)).fetchone()
        assert ex["to_location_id"] == cave and ex["unlock_dc"] == 14
        assert json.loads(conn.execute("SELECT value FROM world_flags WHERE key='quest_started'").fetchone()[0]) is True


def test_foreign_keys_enforced(path):
    with pytest.raises(sqlite3.IntegrityError):
        with db.connect(path) as conn:
            conn.execute("INSERT INTO inventory (character_id, template_id) VALUES (999, 'nope')")


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

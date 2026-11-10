"""SQLite persistence: schema creation, connections, and the thin layer that
loads characters for rpg.rules and persists check consequences."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Optional, Union

from .rules import ABILITIES, Character, CheckResult, Condition, normalize_skill

SCHEMA_VERSION = 4

# Columns added after v2; older saves get them via ALTER TABLE on open.
MIGRATION_COLUMNS = [
    ("locations", "dark", "INTEGER NOT NULL DEFAULT 0"),
    ("items", "timer", "INTEGER"),
    ("npcs", "attack_bonus", "INTEGER NOT NULL DEFAULT 2"),
    ("npcs", "damage", "TEXT NOT NULL DEFAULT '1d4'"),
    ("npcs", "save_bonus", "INTEGER NOT NULL DEFAULT 0"),
    ("characters", "gold", "INTEGER NOT NULL DEFAULT 0 CHECK (gold >= 0)"),  # v4
]
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PathLike = Union[str, os.PathLike]


def default_db_path() -> Path:
    """$RPG_DB_PATH if set, else <project>/saves/rpg_state.db (independent of cwd)."""
    env = os.environ.get("RPG_DB_PATH")
    return Path(env).expanduser() if env else PROJECT_ROOT / "saves" / "rpg_state.db"


def resolve_path(path: Optional[PathLike] = None) -> Path:
    return Path(path).expanduser() if path is not None else default_db_path()


def get_connection(path: Optional[PathLike] = None) -> sqlite3.Connection:
    p = resolve_path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connect(path: Optional[PathLike] = None) -> Iterator[sqlite3.Connection]:
    """Connection that commits on success, rolls back on error, and always closes."""
    conn = get_connection(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_schema(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "characters" in tables and "schema_meta" not in tables:
        raise RuntimeError(
            "Database predates schema versioning (starter rpg_engine.py format); "
            "start a new save file or migrate it."
        )
    conn.executescript(SCHEMA_PATH.read_text())
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    elif int(row[0]) in (2, 3):
        _migrate_v2(conn)
    elif int(row[0]) != SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported schema_version {row[0]} (expected {SCHEMA_VERSION})")
    conn.commit()


def _migrate_v2(conn) -> None:
    """v2/v3 -> current: new tables come from the schema script; add any missing columns.
    (npcs.disposition has been an INTEGER -100..100 since v1: nothing to convert.)"""
    for table, col, ddl in MIGRATION_COLUMNS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and col not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION),))


def init_db(path: Optional[PathLike] = None) -> Path:
    """Create (or verify) the schema at path / $RPG_DB_PATH / default. Returns the path."""
    p = resolve_path(path)
    with connect(p) as conn:
        create_schema(conn)
    return p


# ---------------------------------------------------------------------------
# Characters & conditions
# ---------------------------------------------------------------------------

def create_character(conn, name: str, *, level: int = 1, scores: Optional[Dict[str, int]] = None,
                     skills: Optional[Dict[str, str]] = None, max_hp: int = 10,
                     location_id: Optional[int] = None, is_player: bool = True,
                     key: Optional[str] = None, weapon_proficiencies=("simple",)) -> int:
    scores = {**{a: 10 for a in ABILITIES}, **(scores or {})}
    cur = conn.execute(
        f"""INSERT INTO characters (key, name, is_player, level, {', '.join(a + '_score' for a in ABILITIES)},
            max_hp, current_hp, location_id, weapon_proficiencies)
            VALUES (?, ?, ?, ?, {', '.join('?' * len(ABILITIES))}, ?, ?, ?, ?)""",
        (key, name, int(is_player), level, *[scores[a] for a in ABILITIES], max_hp, max_hp, location_id,
         json.dumps(list(weapon_proficiencies))),
    )
    cid = cur.lastrowid
    for skill, prof in (skills or {}).items():
        set_skill(conn, cid, skill, prof)
    return cid


def character_id_by_key(conn, key: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM characters WHERE key = ?", (key,)).fetchone()
    return row["id"] if row else None


def set_skill(conn, character_id: int, skill: str, proficiency: str) -> None:
    """proficiency: 'proficient' | 'expertise' (expertise implies proficient)."""
    if proficiency not in ("proficient", "expertise"):
        raise ValueError(proficiency)
    conn.execute(
        "INSERT OR REPLACE INTO character_skills (character_id, skill, proficient, expertise) VALUES (?, ?, 1, ?)",
        (character_id, normalize_skill(skill), int(proficiency == "expertise")),
    )


def load_conditions(conn, character_id: int):
    rows = conn.execute("SELECT * FROM character_conditions WHERE character_id = ? ORDER BY id",
                        (character_id,)).fetchall()
    return [Condition(name=r["name"], effect=json.loads(r["effect"]), lasting=bool(r["lasting"]),
                      duration_turns=r["duration_turns"], source=r["source"],
                      description=r["description"], severity=r["severity"]) for r in rows]


def load_character(conn, character_id: int) -> Character:
    r = conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()
    if r is None:
        raise ValueError(f"Character {character_id} not found")
    skills = {}
    for s in conn.execute("SELECT skill, proficient, expertise FROM character_skills WHERE character_id = ?",
                          (character_id,)):
        if s["expertise"]:
            skills[s["skill"]] = "expertise"
        elif s["proficient"]:
            skills[s["skill"]] = "proficient"
    return Character(id=r["id"], name=r["name"], level=r["level"],
                     scores={a: r[a + "_score"] for a in ABILITIES}, skills=skills,
                     conditions=load_conditions(conn, character_id),
                     weapon_proficiencies=json.loads(r["weapon_proficiencies"]))


def set_flag(conn, key: str, value) -> None:
    conn.execute("""INSERT INTO world_flags (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
                 (key, json.dumps(value)))


def get_flag(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM world_flags WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row["value"])


def _stronger(a, b):
    """Merge two condition effects without stacking: per key the larger magnitude wins."""
    if isinstance(a, dict) and isinstance(b, dict):
        return {k: _stronger(a.get(k), b.get(k)) if k in a and k in b else (a[k] if k in a else b[k])
                for k in {**a, **b}}
    if isinstance(a, list) and isinstance(b, list):
        return a + [x for x in b if x not in a]
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a if abs(a) >= abs(b) else b
    return b if a is None else a


def add_condition(conn, character_id: int, cond: Condition) -> int:
    """Add a condition. Re-applying one the character already has (same name) does not
    stack (BUG-07): the existing row is kept and refreshed -- stronger effect per key,
    higher severity, lasting if either is, the longer duration (NULL = indefinite)."""
    old = conn.execute("SELECT * FROM character_conditions WHERE character_id = ? AND name = ? ORDER BY id LIMIT 1",
                       (character_id, cond.name)).fetchone()
    if old is not None:
        sev = max(int(old["severity"] or 0), int(cond.severity or 0))
        dur = None if old["duration_turns"] is None or cond.duration_turns is None \
            else max(old["duration_turns"], cond.duration_turns)
        conn.execute("UPDATE character_conditions SET effect = ?, severity = ?, lasting = ?, duration_turns = ?, "
                     "description = COALESCE(?, description) WHERE id = ?",
                     (json.dumps(_stronger(json.loads(old["effect"] or "{}"), cond.effect)), sev,
                      int(bool(old["lasting"]) or cond.lasting), dur, cond.description, old["id"]))
        return old["id"]
    cur = conn.execute(
        """INSERT INTO character_conditions
           (character_id, name, description, severity, effect, lasting, duration_turns, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (character_id, cond.name, cond.description, cond.severity, json.dumps(cond.effect),
         int(cond.lasting), cond.duration_turns, cond.source),
    )
    return cur.lastrowid


def apply_check_consequences(conn, character_id: int, result: CheckResult) -> CheckResult:
    """Persist consequences from rules.resolve_skill_check; records DB ids on them."""
    for c in result.consequences:
        if c.get("type") == "condition":
            fields = {k: v for k, v in c.items() if k not in ("type", "id")}
            c["id"] = add_condition(conn, character_id, Condition(**fields))
    return result


# ---------------------------------------------------------------------------
# Item templates & item instances (minimal; inventory helpers come in task 2)
# ---------------------------------------------------------------------------

def add_item_template(conn, tpl: Dict) -> int:
    """Insert a template from the content shape (items_tier1.yaml / contracts.md (c)).
    Content `id` -> `key`, `notes` -> `description`, `subcategory` -> stats.subcategory."""
    validate_item_name(tpl["name"])
    stats = dict(tpl.get("stats") or {})
    if tpl.get("subcategory") is not None:
        stats["subcategory"] = tpl["subcategory"]
    cur = conn.execute(
        """INSERT INTO item_templates (key, name, category, power_tier, slot, weight, value,
           stats, tags, description) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (tpl.get("key") or tpl["id"], tpl["name"], tpl["category"], tpl.get("power_tier", 1),
         tpl.get("slot") or "none", tpl.get("weight", 0), tpl.get("value", 0), json.dumps(stats),
         json.dumps(tpl.get("tags") or []), tpl.get("description") or tpl.get("notes")),
    )
    return cur.lastrowid


def validate_item_name(name: str) -> None:
    """Item names carry no digits (quantities live in stats, e.g. length_ft); narration
    must stay number-free."""
    if any(ch.isdigit() for ch in str(name)):
        raise ValueError(f"Item name {name!r} contains digits; move quantities into stats")


def next_item_key(conn, base: str) -> str:
    """'item_short_blade' if free, else 'item_short_blade_2', '_3', ..."""
    prefix = base + "_"
    taken = {r[0] for r in conn.execute("SELECT key FROM items WHERE key = ? OR substr(key, 1, ?) = ?",
                                        (base, len(prefix), prefix))}
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def add_item(conn, template_key: str, *, character_id: Optional[int] = None,
             location_id: Optional[int] = None, quantity: int = 1, equipped: bool = False,
             display_name: Optional[str] = None, key: Optional[str] = None) -> int:
    """Create an item instance owned by a character or lying in a location."""
    row = conn.execute("SELECT id FROM item_templates WHERE key = ?", (template_key,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown item template {template_key!r}")
    if display_name is not None:
        validate_item_name(display_name)
    if key is None:
        key = next_item_key(conn, template_key)
    cur = conn.execute(
        """INSERT INTO items (key, template_id, character_id, location_id, display_name, quantity, equipped)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (key, row["id"], character_id, location_id, display_name, quantity, int(equipped)),
    )
    return cur.lastrowid


# -- dice state (BUG-04): the RNG lives in the save so slots and resumes are deterministic --
RNG_FLAG = "_rng_state"


def save_rng_state(conn, rng) -> None:
    version, internal, gauss = rng.getstate()
    set_flag(conn, RNG_FLAG, [version, list(internal), gauss])


def load_rng_state(conn, rng) -> bool:
    """Restore the saved dice stream into `rng`. False if the save has none."""
    state = get_flag(conn, RNG_FLAG)
    if not state:
        return False
    rng.setstate((state[0], tuple(state[1]), state[2]))
    return True

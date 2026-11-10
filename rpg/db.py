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

SCHEMA_VERSION = 1
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
    elif int(row[0]) != SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported schema_version {row[0]} (expected {SCHEMA_VERSION})")
    conn.commit()


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
                     location_id: Optional[int] = None, is_player: bool = True) -> int:
    scores = {**{a: 10 for a in ABILITIES}, **(scores or {})}
    cur = conn.execute(
        f"""INSERT INTO characters (name, is_player, level, {', '.join(a + '_score' for a in ABILITIES)},
            max_hp, current_hp, location_id) VALUES (?, ?, ?, {', '.join('?' * len(ABILITIES))}, ?, ?, ?)""",
        (name, int(is_player), level, *[scores[a] for a in ABILITIES], max_hp, max_hp, location_id),
    )
    cid = cur.lastrowid
    for skill, prof in (skills or {}).items():
        set_skill(conn, cid, skill, prof)
    return cid


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
                     conditions=load_conditions(conn, character_id))


def add_condition(conn, character_id: int, cond: Condition) -> int:
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


def add_item(conn, template_key: str, *, character_id: Optional[int] = None,
             location_id: Optional[int] = None, quantity: int = 1, equipped: bool = False,
             display_name: Optional[str] = None, key: Optional[str] = None) -> int:
    """Create an item instance owned by a character or lying in a location."""
    row = conn.execute("SELECT id FROM item_templates WHERE key = ?", (template_key,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown item template {template_key!r}")
    if key is None:
        n = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM items").fetchone()[0]
        key = f"item_{n:04d}"
    cur = conn.execute(
        """INSERT INTO items (key, template_id, character_id, location_id, display_name, quantity, equipped)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (key, row["id"], character_id, location_id, display_name, quantity, int(equipped)),
    )
    return cur.lastrowid

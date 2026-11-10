"""
rpg_engine.py
Minimal hybrid RPG engine: SQLite owns state and resolution,
LLM is expected to propose actions and narrate outcomes.
"""

import json
import random
import sys
from pathlib import Path
from typing import Optional, Dict, Any, Callable

from rpg import db, rules
from rpg.db import get_connection, init_db, connect, default_db_path  # re-exported
from rpg.rules import ability_modifier, proficiency_bonus, SKILL_ABILITY  # re-exported

# Save file location: pass db_path explicitly, or set $RPG_DB_PATH.
# Default is <project>/saves/rpg_state.db regardless of the current directory.
DB_PATH: Path = default_db_path()

# ---------------------------------------------------------------------------
# Schema lives in rpg/schema.sql (created by rpg.db.init_db).
# Ability / skill math lives in rpg/rules.py (pure, unit-tested).
# ---------------------------------------------------------------------------

def get_skill_modifier(conn, character_id: int, skill: str, ability: Optional[str] = None) -> int:
    """Return total modifier for a skill check (ability + proficiency/expertise + conditions)."""
    return rules.skill_modifier(db.load_character(conn, character_id), skill, ability)

# ---------------------------------------------------------------------------
# Skill-check resolver (DB-applying wrapper around rules.resolve_skill_check)
# ---------------------------------------------------------------------------

def resolve_skill_check(
    character_id: int,
    skill: str,
    ability: Optional[str] = None,
    proposed_dc: int = 12,
    is_last_ditch: bool = False,
    *,
    roller: Optional[Callable[[], int]] = None,
    rng: Optional[random.Random] = None,
    db_path=None,
) -> Dict[str, Any]:
    """
    Resolve a skill check per DESIGN.md (see rpg/rules.py for exact thresholds):
    - Proficient + 10 + modifier >= DC (DC <= 15) = auto-success, no roll
    - Needs more than an 18 on the d20 = impossible, no roll (unless last-ditch)
    - Last-ditch: +5, can succeed, ALWAYS applies a lasting condition (persisted)
    - No natural-20 rule
    """
    with connect(db_path if db_path is not None else DB_PATH) as conn:
        char = db.load_character(conn, character_id)
        result = rules.resolve_skill_check(
            char, skill, proposed_dc, last_ditch=is_last_ditch, ability=ability,
            roller=roller, rng=rng,
        )
        db.apply_check_consequences(conn, character_id, result)
    return result.to_dict()

# ---------------------------------------------------------------------------
# Action processing (contracts.md v0.3) lives in rpg/actions.py
# ---------------------------------------------------------------------------

from rpg.actions import process_action_proposal  # noqa: E402  (conn, character_id, proposal, rng=None)
from rpg.context import build_context  # noqa: E402

# ---------------------------------------------------------------------------
# Demo / quick test
# ---------------------------------------------------------------------------

DEMO_TEMPLATES = [
    {"id": "item_short_blade", "name": "Short Blade", "category": "weapon", "slot": "main_hand",
     "stats": {"subcategory": "simple_melee", "damage": "1d6", "damage_type": "piercing",
               "proficiency": "simple", "properties": ["finesse", "light"]}},
    {"id": "item_hanging_lamp", "name": "Hanging lamp", "category": "utility", "tags": ["fixture"]},
]


def _seed_demo(conn) -> int:
    """Create the demo scene once (looked up by key) and return the rogue's id."""
    cid = db.character_id_by_key(conn, "pc_test_rogue")
    if cid is not None:
        return cid
    for t in DEMO_TEMPLATES:
        if not conn.execute("SELECT 1 FROM item_templates WHERE key = ?", (t["id"],)).fetchone():
            db.add_item_template(conn, t)
    store = conn.execute("INSERT INTO locations (key, name, description) VALUES "
                         "('loc_demo_storeroom', 'Storeroom', 'A cramped room of shelves and crates.')").lastrowid
    yard = conn.execute("INSERT INTO locations (key, name, description) VALUES "
                        "('loc_demo_yard', 'Yard', 'A muddy yard.')").lastrowid
    cellar = conn.execute("INSERT INTO locations (key, name, description) VALUES "
                          "('loc_demo_cellar', 'Cellar', 'Damp and dark.')").lastrowid
    ex = ("INSERT INTO location_exits (from_location_id, to_location_id, direction, description, locked, unlock_dc)"
          " VALUES (?, ?, ?, ?, ?, ?)")
    conn.execute(ex, (store, yard, "north", "low wall", 0, None))
    conn.execute(ex, (yard, store, "south", "low wall", 0, None))
    conn.execute(ex, (store, cellar, "down", "iron-banded door", 1, 21))
    conn.execute("INSERT INTO npcs (key, name, location_id, traits, defense_dc, max_hp) VALUES "
                 "('npc_demo_guard', 'Dozing Guard', ?, '[\"drowsy\"]', 8, 4)", (store,))
    # Rogue: STR 10, DEX 16, level 3 (PB +2)
    cid = db.create_character(conn, "Test Rogue", key="pc_test_rogue", level=3, scores={"str": 10, "dex": 16},
                              max_hp=24, location_id=store,
                              skills={"acrobatics": "proficient", "athletics": "proficient"})
    db.add_item(conn, "item_short_blade", character_id=cid, equipped=True, key="item_rusty_shortsword",
                display_name="Rusty Shortsword")
    db.add_item(conn, "item_hanging_lamp", location_id=store, key="item_demo_lamp")
    return cid


def _action(action_type, intent, **kw):
    a = {"action_type": action_type, "skill": None, "target": None, "items_used": [], "last_ditch": False,
         "est_difficulty": "medium", "intent_summary": intent, "clarification_needed": False,
         "clarification_question": None}
    a.update(kw)
    return {"actions": [a], "ooc": None, "unresolved_references": []}


def demo(db_path=None, seed: Optional[int] = None):
    """Seed a tiny scene (once per save file) and run a few v0.3 proposals.
    With no db_path the demo uses a throwaway temp file, so the real save is untouched."""
    import tempfile
    tmp = None
    if db_path is None:
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "demo.db"
    path = init_db(db_path)
    rng = random.Random(seed)
    with connect(path) as conn:
        char_id = _seed_demo(conn)
    print(f"Save file: {path}{' (throwaway)' if tmp else ''}")
    print(f"Character id={char_id} (pc_test_rogue)")

    turns = [
        ("Easy climb (auto-success)", _action("move", "Climb over the low wall into the yard.",
                                              skill="athletics", est_difficulty="easy", target="loc_demo_yard")),
        ("Back again", _action("move", "Climb back over the wall.", target="loc_demo_storeroom")),
        ("Impossible door", _action("skill_check", "Kick the iron-banded door open.", skill="athletics",
                                    target="loc_demo_cellar", est_difficulty="hard")),
        ("Last-ditch door", _action("skill_check", "Throw everything at the door, whatever it costs.",
                                    skill="athletics", target="loc_demo_cellar", last_ditch=True)),
        ("Claimed item", _action("attack", "Cleave the guard with a greatsword.", skill="str",
                                 target="npc_demo_guard", items_used=["item_greatsword"])),
    ]
    for title, proposal in turns:
        with connect(path) as conn:
            print(f"\n--- {title} ---")
            print(json.dumps(process_action_proposal(conn, char_id, proposal, rng=rng), indent=2))
    with connect(path) as conn:
        print("\n--- Context block ---")
        print(json.dumps(build_context(conn, char_id), indent=2))
    if tmp:
        tmp.cleanup()


if __name__ == "__main__":
    # Usage: python rpg_engine.py [db_path] [seed]   (no db_path = throwaway demo DB)
    demo(sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None,
         int(sys.argv[2]) if len(sys.argv) > 2 else None)

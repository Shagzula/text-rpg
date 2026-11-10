"""
rpg_engine.py
Minimal hybrid RPG engine: SQLite owns state and resolution,
LLM is expected to propose actions and narrate outcomes.
"""

import sqlite3
import json
import random
from pathlib import Path
from typing import Optional, Dict, Any, List

DB_PATH = Path("rpg_state.db")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS characters (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    level           INTEGER DEFAULT 1,
    str_score       INTEGER DEFAULT 10,
    dex_score       INTEGER DEFAULT 10,
    con_score       INTEGER DEFAULT 10,
    int_score       INTEGER DEFAULT 10,
    wis_score       INTEGER DEFAULT 10,
    cha_score       INTEGER DEFAULT 10,
    current_hp      INTEGER,
    max_hp          INTEGER,
    location_id     INTEGER,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS character_skills (
    character_id    INTEGER,
    skill           TEXT NOT NULL,
    proficient      INTEGER DEFAULT 0,
    expertise       INTEGER DEFAULT 0,
    PRIMARY KEY (character_id, skill)
);

CREATE TABLE IF NOT EXISTS character_conditions (
    id              INTEGER PRIMARY KEY,
    character_id    INTEGER,
    name            TEXT NOT NULL,
    description     TEXT,
    severity        INTEGER DEFAULT 1,
    expires_on      TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS item_templates (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    power_tier      INTEGER DEFAULT 1,
    damage_dice     TEXT,
    damage_type     TEXT,
    properties      TEXT,
    description     TEXT
);

CREATE TABLE IF NOT EXISTS inventory (
    id              INTEGER PRIMARY KEY,
    character_id    INTEGER,
    template_id     INTEGER,
    display_name    TEXT,
    quantity        INTEGER DEFAULT 1,
    equipped        INTEGER DEFAULT 0,
    notes           TEXT,
    acquired_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS locations (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    area_tier       INTEGER DEFAULT 1,
    parent_id       INTEGER
);

CREATE TABLE IF NOT EXISTS npcs (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    location_id     INTEGER,
    faction_id      INTEGER,
    traits          TEXT,
    is_major        INTEGER DEFAULT 0,
    alive           INTEGER DEFAULT 1,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS factions (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT
);

CREATE TABLE IF NOT EXISTS relationships (
    id              INTEGER PRIMARY KEY,
    character_id    INTEGER,
    target_type     TEXT NOT NULL,
    target_id       INTEGER NOT NULL,
    value           INTEGER DEFAULT 0,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS world_flags (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Create tables if they do not exist."""
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.commit()

# ---------------------------------------------------------------------------
# Ability / skill math (D&D 5e style)
# ---------------------------------------------------------------------------

ABILITY_MAP = {
    "str": "str_score",
    "dex": "dex_score",
    "con": "con_score",
    "int": "int_score",
    "wis": "wis_score",
    "cha": "cha_score",
}

def ability_modifier(score: int) -> int:
    return (score - 10) // 2

def proficiency_bonus(level: int) -> int:
    return 2 + (level - 1) // 4

def get_skill_modifier(conn: sqlite3.Connection, character_id: int, skill: str, ability: str) -> int:
    """Return total modifier for a skill check."""
    char = conn.execute(
        "SELECT level, str_score, dex_score, con_score, int_score, wis_score, cha_score FROM characters WHERE id = ?",
        (character_id,)
    ).fetchone()
    if not char:
        raise ValueError(f"Character {character_id} not found")

    ability_score = char[ABILITY_MAP[ability]]
    mod = ability_modifier(ability_score)
    pb = proficiency_bonus(char["level"])

    skill_row = conn.execute(
        "SELECT proficient, expertise FROM character_skills WHERE character_id = ? AND skill = ?",
        (character_id, skill)
    ).fetchone()

    if skill_row:
        if skill_row["expertise"]:
            mod += pb * 2
        elif skill_row["proficient"]:
            mod += pb

    return mod

# ---------------------------------------------------------------------------
# Skill-check resolver
# ---------------------------------------------------------------------------

def resolve_skill_check(
    character_id: int,
    skill: str,
    ability: str,
    proposed_dc: int,
    is_last_ditch: bool = False,
    auto_success_threshold: int = 8,
) -> Dict[str, Any]:
    """
    Resolve a skill check according to the rules:
    - Impossible actions fail unless last-ditch
    - Proficient + high enough modifier = auto-success
    - Otherwise roll
    - Last-ditch success can apply injury
    """
    with get_connection() as conn:
        modifier = get_skill_modifier(conn, character_id, skill, ability)
        char = conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()

        # Simple impossibility heuristic: if modifier is very low relative to DC
        # and not last-ditch, fail hard.
        if not is_last_ditch and (modifier + 5) < proposed_dc:
            return {
                "success": False,
                "auto_success": False,
                "impossible": True,
                "roll": None,
                "modifier": modifier,
                "total": None,
                "dc": proposed_dc,
                "injury_applied": None,
                "reason": "Action is beyond the character's capabilities"
            }

        # Auto-success for proficient characters on easy tasks
        skill_row = conn.execute(
            "SELECT proficient FROM character_skills WHERE character_id = ? AND skill = ?",
            (character_id, skill)
        ).fetchone()
        is_proficient = skill_row and skill_row["proficient"]

        if is_proficient and modifier >= auto_success_threshold and proposed_dc <= 12:
            return {
                "success": True,
                "auto_success": True,
                "impossible": False,
                "roll": None,
                "modifier": modifier,
                "total": modifier + 10,  # representative
                "dc": proposed_dc,
                "injury_applied": None,
                "reason": "Auto-success (proficient and capable)"
            }

        # Normal or last-ditch roll
        roll = random.randint(1, 20)
        total = roll + modifier
        success = total >= proposed_dc

        injury = None
        if is_last_ditch and success:
            # Example injury on last-ditch success
            injury = {
                "name": "strained_muscle",
                "description": "Pushed past limits and suffered a strain",
                "severity": 1
            }
            conn.execute(
                "INSERT INTO character_conditions (character_id, name, description, severity) VALUES (?, ?, ?, ?)",
                (character_id, injury["name"], injury["description"], injury["severity"])
            )
            conn.commit()

        return {
            "success": success,
            "auto_success": False,
            "impossible": False,
            "roll": roll,
            "modifier": modifier,
            "total": total,
            "dc": proposed_dc,
            "injury_applied": injury,
            "reason": "Rolled" if not is_last_ditch else "Last-ditch effort"
        }

# ---------------------------------------------------------------------------
# Example action processing
# ---------------------------------------------------------------------------

def process_action_proposal(character_id: int, proposal: Dict[str, Any]) -> Dict[str, Any]:
    """
    Take an LLM action proposal and return an engine result
    suitable for feeding back into the narration prompt.
    """
    action_type = proposal.get("action_type")

    if action_type == "skill_check":
        result = resolve_skill_check(
            character_id=character_id,
            skill=proposal.get("skill", "athletics"),
            ability=proposal.get("ability", "str"),
            proposed_dc=proposal.get("proposed_dc", 12),
            is_last_ditch=proposal.get("is_last_ditch", False),
        )
        result["narrative_constraints"] = {
            "must_mention": [proposal.get("success_outcome") if result["success"] else proposal.get("failure_outcome")],
            "must_not_mention": ["dice", "roll", "modifier", "DC", "skill check"]
        }
        return result

    # Placeholder for other action types
    return {
        "success": False,
        "reason": f"Action type '{action_type}' not yet implemented",
        "narrative_constraints": {"must_mention": [], "must_not_mention": []}
    }

# ---------------------------------------------------------------------------
# Demo / quick test
# ---------------------------------------------------------------------------

def demo():
    """Create a sample character and run a couple of checks."""
    init_db()
    with get_connection() as conn:
        # Create a sample rogue
        conn.execute(
            """INSERT INTO characters (name, level, str_score, dex_score, current_hp, max_hp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("Test Rogue", 3, 10, 16, 24, 24)
        )
        char_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO character_skills (character_id, skill, proficient) VALUES (?, ?, ?)",
            (char_id, "acrobatics", 1)
        )
        conn.execute(
            "INSERT INTO character_skills (character_id, skill, proficient) VALUES (?, ?, ?)",
            (char_id, "athletics", 1)
        )
        conn.commit()

    print(f"Created character id={char_id}")

    # Easy climb (should auto-succeed)
    proposal_easy = {
        "action_type": "skill_check",
        "skill": "athletics",
        "ability": "str",
        "proposed_dc": 10,
        "is_last_ditch": False,
        "success_outcome": "You climb the short wall easily",
        "failure_outcome": "You slip"
    }
    print("\n--- Easy climb ---")
    print(json.dumps(process_action_proposal(char_id, proposal_easy), indent=2))

    # Hard door kick by low-STR character (should be impossible)
    proposal_impossible = {
        "action_type": "skill_check",
        "skill": "athletics",
        "ability": "str",
        "proposed_dc": 20,
        "is_last_ditch": False,
        "success_outcome": "You smash the door",
        "failure_outcome": "The door does not budge"
    }
    print("\n--- Impossible door ---")
    print(json.dumps(process_action_proposal(char_id, proposal_impossible), indent=2))

    # Last-ditch version
    proposal_lastditch = {
        "action_type": "skill_check",
        "skill": "athletics",
        "ability": "str",
        "proposed_dc": 18,
        "is_last_ditch": True,
        "success_outcome": "You force the door open",
        "failure_outcome": "You hurt yourself and fail"
    }
    print("\n--- Last-ditch door ---")
    print(json.dumps(process_action_proposal(char_id, proposal_lastditch), indent=2))


if __name__ == "__main__":
    demo()

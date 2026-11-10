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
# Example action processing
# ---------------------------------------------------------------------------

def process_action_proposal(character_id: int, proposal: Dict[str, Any], *, db_path=None,
                            roller: Optional[Callable[[], int]] = None) -> Dict[str, Any]:
    """
    Take an LLM action proposal and return an engine result
    suitable for feeding back into the narration prompt.
    """
    action_type = proposal.get("action_type")

    if action_type == "skill_check":
        result = resolve_skill_check(
            character_id=character_id,
            skill=proposal.get("skill", "athletics"),
            ability=proposal.get("ability"),
            proposed_dc=proposal.get("proposed_dc", 12),
            is_last_ditch=proposal.get("is_last_ditch", False),
            db_path=db_path,
            roller=roller,
        )
        outcome_text = proposal.get("success_outcome") if result["success"] else proposal.get("failure_outcome")
        must_mention = [t for t in [outcome_text] if t]
        # Narration hints must match engine state: mention an injury only if one was applied.
        if result["injury_applied"]:
            must_mention.append(result["injury_applied"]["description"])
        result["narrative_constraints"] = {
            "must_mention": must_mention,
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

def demo(db_path=None, seed: Optional[int] = None):
    """Create a sample character and run a couple of checks."""
    path = init_db(db_path if db_path is not None else DB_PATH)
    rng = random.Random(seed)
    roller = lambda: rng.randint(1, 20)
    with connect(path) as conn:
        # Create a sample rogue (STR 10, DEX 16, level 3: PB +2)
        # Reuse the demo rogue across runs (looked up by key).
        char_id = db.character_id_by_key(conn, "pc_test_rogue") or db.create_character(
            conn, "Test Rogue", key="pc_test_rogue", level=3, scores={"str": 10, "dex": 16},
            max_hp=24, skills={"acrobatics": "proficient", "athletics": "proficient"},
        )

    print(f"Save file: {path}")
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
    print(json.dumps(process_action_proposal(char_id, proposal_easy, db_path=path, roller=roller), indent=2))

    # Hard door kick by low-STR character (should be impossible)
    proposal_impossible = {
        "action_type": "skill_check",
        "skill": "athletics",
        "ability": "str",
        "proposed_dc": 21,  # +2 athletics would need a 19+: filtered as impossible
        "is_last_ditch": False,
        "success_outcome": "You smash the door",
        "failure_outcome": "The door does not budge"
    }
    print("\n--- Impossible door ---")
    print(json.dumps(process_action_proposal(char_id, proposal_impossible, db_path=path, roller=roller), indent=2))

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
    print(json.dumps(process_action_proposal(char_id, proposal_lastditch, db_path=path, roller=roller), indent=2))


if __name__ == "__main__":
    # Usage: python rpg_engine.py [db_path] [seed]   (or set RPG_DB_PATH)
    demo(sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None,
         int(sys.argv[2]) if len(sys.argv) > 2 else None)

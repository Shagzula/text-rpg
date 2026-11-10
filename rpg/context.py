"""Prompt context block shared by the interpreter and narrator prompts
(content/schemas_proposed.md section 0), plus the narration log."""
from __future__ import annotations

import json
from typing import Dict, List

from . import db, effects, personas, rules, talk
from .inventory import list_inventory, list_location_items

RECENT_NARRATIONS = 3


def log_narration(conn, character_id: int, turn: int, text: str) -> int:
    return conn.execute("INSERT INTO narration_log (character_id, turn, text) VALUES (?, ?, ?)",
                        (character_id, turn, text)).lastrowid


def recent_narrations(conn, character_id: int, n: int = RECENT_NARRATIONS) -> List[str]:
    rows = conn.execute("SELECT text FROM narration_log WHERE character_id = ? ORDER BY id DESC LIMIT ?",
                        (character_id, n)).fetchall()
    return [r["text"] for r in reversed(rows)]  # oldest first


def short_description(text, limit: int = 160) -> str:
    if not text:
        return ""
    first = text.strip().split(". ")[0].rstrip(".") + "."
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def npc_hp(row) -> int:
    return row["max_hp"] if row["current_hp"] is None else row["current_hp"]


def location_exits(conn, location_id: int) -> List[Dict]:
    rows = conn.execute(
        """SELECT e.*, l.key AS to_key, l.name AS to_name FROM location_exits e
           JOIN locations l ON l.id = e.to_location_id WHERE e.from_location_id = ? ORDER BY e.direction""",
        (location_id,)).fetchall()
    return [{"direction": r["direction"], "to": r["to_key"], "to_name": r["to_name"],
             "description": r["description"], "state": "locked" if r["locked"] else "open",
             "lockable": r["unlock_dc"] is not None} for r in rows]


def player_sheet(conn, character_id: int) -> Dict:
    char = db.load_character(conn, character_id)
    row = conn.execute("SELECT weapon_proficiencies, gold FROM characters WHERE id = ?", (character_id,)).fetchone()
    return {
        "name": char.name,
        "level": char.level,
        "proficiency_bonus": rules.proficiency_bonus(char.level),
        "abilities": {a: {"score": s, "mod": rules.ability_modifier(s)} for a, s in char.scores.items()},
        "skills": dict(sorted(char.skills.items())),
        "weapon_proficiencies": json.loads(row["weapon_proficiencies"]),
        "conditions": _conditions(conn, character_id),
        "features": [],  # no spells/features in the slice
        "gold": row["gold"],  # coins, same unit as item_templates.value
    }


DARK_DESCRIPTION = "It is pitch dark; you can see nothing."


def _conditions(conn, character_id) -> List[Dict]:
    """name/description/lasting, plus `timed` (it will wear off by itself; turns left are
    engine-only)."""
    return [{"name": r["name"], "description": r["description"], "lasting": bool(r["lasting"]),
             "timed": r["duration_turns"] is not None}
            for r in conn.execute("SELECT * FROM character_conditions WHERE character_id = ? ORDER BY id",
                                  (character_id,))]


def npc_talk_context(conn, n) -> Dict:
    """topics (public + secrets at/below the band), open trades at the band, last lines said.
    Persona-less or defeated NPCs: all empty. Secret text and insight_tell never appear."""
    p = personas.get(conn, n["id"]) if n["alive"] else None
    if not p:
        return {"topics": [], "trades": [], "recent_dialogue": talk.recent_dialogue(conn, n["id"])}
    band = rules.disposition_band(n["disposition"])
    return {"topics": personas.visible_topics(p, band),
            "trades": [{"key": k, "label": p["trades"][k]["label"]} for k in talk.open_trades(conn, n["key"], p, band)],
            "recent_dialogue": talk.recent_dialogue(conn, n["id"])}


def build_context(conn, character_id: int) -> Dict:
    ch = conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()
    if ch is None:
        raise ValueError(f"Character {character_id} not found")
    loc = conn.execute("SELECT * FROM locations WHERE id = ?", (ch["location_id"],)).fetchone()
    location = None
    npcs, items = [], []
    if loc is not None:
        lit = effects.location_is_lit(conn, loc["id"], character_id)
        location = {"key": loc["key"], "name": loc["name"],
                    "description": short_description(loc["description"]) if lit else DARK_DESCRIPTION,
                    "dark": bool(loc["dark"]), "lit": lit,
                    "exits": location_exits(conn, loc["id"])}
        for n in conn.execute("SELECT * FROM npcs WHERE location_id = ? ORDER BY id", (loc["id"],)):
            npcs.append({"key": n["key"], "name": n["name"],
                         "disposition": rules.disposition_band(n["disposition"]),
                         "traits": json.loads(n["traits"]),
                         "status": "alive" if n["alive"] else "defeated",
                         "hp_band": rules.hp_band(npc_hp(n), n["max_hp"]),
                         "out_of_reach": bool(n["out_of_reach"]),
                         "conditions": effects.npc_conditions(conn, n["id"]),
                         "visible": lit, **npc_talk_context(conn, n)})
        items = [] if not lit else [{"key": i["key"], "name": i["name"], "tags": i["tags"], "fixture": i["fixture"],
                  "state": i["state"], "out_of_reach": i["out_of_reach"]}
                 for i in list_location_items(conn, loc["id"])]
    sheet = player_sheet(conn, character_id)
    return {
        "location": location,
        "npcs_present": npcs,
        "visible_items": items,
        "player_inventory": [{"key": i["key"], "name": i["name"], "equipped": i["equipped"],
                              "slot": i["slot"], "quantity": i["quantity"], "state": i["state"]}
                             for i in list_inventory(conn, character_id)],
        "player_sheet": sheet,
        "active_conditions": sheet["conditions"],
        "hp_band": rules.hp_band(ch["current_hp"], ch["max_hp"]),
        "recent_narrations": recent_narrations(conn, character_id),
    }

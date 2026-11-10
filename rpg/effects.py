"""World effects that are not a single player action: light and darkness, tools,
timed conditions, item-driven conditions (buffs, NPC conditions) and the NPC phase
(hostile NPCs counterattack after the player's actions).

Everything here writes through `conn` (the caller commits) and keeps numbers out of
its return values; rolls and damage go to the `rpg.debug` logger.
"""
from __future__ import annotations

import json
import random
from typing import Dict, List, Optional

from . import inventory as inv
from . import rules

DARK_SKILLS = ("perception", "investigation")  # sight-based: disadvantage in the dark
LIGHT_STATE = "lit"
NPC_SAVE_DC = {"trivial": 5, "easy": 10, "medium": 15, "hard": 20, "very_hard": 25, "near_impossible": 30}
# NPC conditions that stop the NPC attacking (and give the player advantage on attacks vs it).
INCAPACITATING = ("blinded", "stunned", "incapacitated", "paralyzed", "unconscious")

# Structured effects for buff items whose YAML only carries descriptive text.
# A template's stats.condition_effect (same shape) takes precedence.
BUFF_EFFECTS = {
    "item_bracing_tonic": {
        "name": "braced",
        "effect": {"advantage": ["str", "con"], "disadvantage": ["wis"], "expires_after_check": ["str", "con"]},
    },
}


# ---------------------------------------------------------------------------
# Light and darkness
# ---------------------------------------------------------------------------

def is_light_source(item: Dict) -> bool:
    return item["stats"].get("effect") == "light"


def location_is_dark(conn, location_id) -> bool:
    row = conn.execute("SELECT dark FROM locations WHERE id = ?", (location_id,)).fetchone()
    return bool(row and row["dark"])


def location_is_lit(conn, location_id, character_id) -> bool:
    """A location is lit if it is not dark, or a lit light source is held by the player or
    lies in the location."""
    if not location_is_dark(conn, location_id):
        return True
    row = conn.execute("SELECT 1 FROM items WHERE state = ? AND (character_id = ? OR location_id = ?) LIMIT 1",
                       (LIGHT_STATE, character_id, location_id)).fetchone()
    return row is not None


def light(conn, item: Dict) -> Dict:
    """Toggle a light source lit/unlit. Returns an entity_state change. Lighting a fresh
    source starts its timer from stats.duration (turns); relighting keeps the remainder."""
    old = item.get("state")
    if old == LIGHT_STATE:
        conn.execute("UPDATE items SET state = NULL WHERE key = ?", (item["key"],))
        return {"type": "entity_state", "key": item["key"], "name": item["name"], "field": "state",
                "from": LIGHT_STATE, "to": "unlit"}
    timer = item.get("timer")
    if timer is None:
        timer = rules.duration_turns(item["stats"].get("duration"))
    conn.execute("UPDATE items SET state = ?, timer = ? WHERE key = ?", (LIGHT_STATE, timer, item["key"]))
    return {"type": "entity_state", "key": item["key"], "name": item["name"], "field": "state",
            "from": old or "unlit", "to": LIGHT_STATE}


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def _is_tool(item: Dict) -> bool:
    s = item["stats"]
    return bool(s.get("enables") or s.get("skill_bonus"))


def available_tools(conn, character_id, items_used: List[str]) -> List[Dict]:
    """Tools the actor can bring to bear: everything held, plus any listed in items_used
    (which the caller has already validated as held or within reach)."""
    tools = [i for i in inv.list_inventory(conn, character_id) if _is_tool(i)]
    held = {i["key"] for i in tools}
    for k in items_used:
        if k not in held:
            item = inv.get_item(conn, k)
            if item is not None and _is_tool(item):
                tools.append(item)
    return tools


def tool_enabling(tools: List[Dict], method: str) -> Optional[Dict]:
    return next((t for t in tools if method in (t["stats"].get("enables") or [])), None)


def tool_advantage(tools: List[Dict], skill: Optional[str], method: Optional[str],
                   items_used: List[str]) -> List[Dict]:
    """Tools granting advantage on this check: skill_bonus.skill matches and grants
    'advantage', and the tool is relevant (listed in items_used, or the check is one of
    the methods it enables)."""
    out = []
    for t in tools:
        sb = t["stats"].get("skill_bonus") or {}
        if sb.get("skill") != skill or sb.get("grants") != "advantage":
            continue
        if t["key"] in items_used or (method and method in (t["stats"].get("enables") or [])):
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

def buff_spec(item: Dict) -> Dict:
    s = item["stats"]
    if isinstance(s.get("condition_effect"), dict):
        return {"name": s.get("condition_name") or item["template_key"].replace("item_", ""),
                "effect": s["condition_effect"]}
    return BUFF_EFFECTS.get(item["template_key"]) or {"name": item["template_key"].replace("item_", ""),
                                                       "effect": {}}


def apply_buff(conn, character_id, item: Dict) -> Dict:
    spec = buff_spec(item)
    s = item["stats"]
    desc = "; ".join(x for x in (s.get("buff"), s.get("side_effect")) if x) or None
    turns = rules.duration_turns(s.get("duration"))
    conn.execute("DELETE FROM character_conditions WHERE character_id = ? AND name = ?", (character_id, spec["name"]))
    conn.execute("INSERT INTO character_conditions (character_id, name, description, severity, effect, lasting, "
                 "duration_turns, source) VALUES (?, ?, ?, 'minor', ?, 0, ?, ?)",
                 (character_id, spec["name"], desc, json.dumps(spec["effect"]), turns, item["key"]))
    return {"type": "condition", "target": "self", "name": spec["name"], "description": desc,
            "lasting": False, "severity": "minor"}


def expire_after_check(conn, character_id, skill: Optional[str], ability: str) -> List[Dict]:
    """Remove conditions whose effect.expires_after_check lists this check's ability/skill
    (e.g. the bracing tonic is spent by one STR or CON check)."""
    out = []
    for r in conn.execute("SELECT id, name, effect FROM character_conditions WHERE character_id = ?",
                          (character_id,)).fetchall():
        spent = (json.loads(r["effect"] or "{}")).get("expires_after_check") or []
        if ability in spent or (skill and skill in spent):
            conn.execute("DELETE FROM character_conditions WHERE id = ?", (r["id"],))
            out.append({"type": "condition_removed", "target": "self", "name": r["name"], "reason": "spent"})
    return out


def npc_conditions(conn, npc_id) -> List[str]:
    return [r["name"] for r in conn.execute("SELECT name FROM npc_conditions WHERE npc_id = ? ORDER BY id", (npc_id,))]


def npc_incapacitated(conn, npc_id) -> Optional[str]:
    return next((n for n in npc_conditions(conn, npc_id) if n in INCAPACITATING), None)


def npc_save_condition(conn, npc: Dict, item: Dict, rng: random.Random, turn: int) -> Dict:
    """The NPC saves against an item's condition (stats.save {ability, est_difficulty});
    on a failure the condition (stats.condition) lands for stats.duration turns (default 1)."""
    s = item["stats"]
    name = s.get("condition") or "affected"
    save = s.get("save") or {}
    dc = NPC_SAVE_DC.get(save.get("est_difficulty"), NPC_SAVE_DC["medium"])
    if save:
        roll = rng.randint(1, 20)
        total = roll + int(npc.get("save_bonus") or 0)
        resisted = total >= dc
        rules.debug_log.debug("npc_save %s", {"turn": turn, "npc": npc["key"], "ability": save.get("ability"),
                                              "dc": dc, "roll": roll, "total": total, "resisted": resisted})
    else:
        resisted = False
    if not resisted:
        turns = rules.duration_turns(s.get("duration")) or 1
        conn.execute("DELETE FROM npc_conditions WHERE npc_id = ? AND name = ?", (npc["id"], name))
        conn.execute("INSERT INTO npc_conditions (npc_id, name, effect, duration_turns, source) VALUES (?, ?, ?, ?, ?)",
                     (npc["id"], name, json.dumps({"no_attack": name in INCAPACITATING}), turns, item["key"]))
    return {"type": "npc_condition", "key": npc["key"], "npc_name": npc["name"], "name": name,
            "outcome": "resisted" if resisted else "applied"}


# ---------------------------------------------------------------------------
# End of turn: ticking timers
# ---------------------------------------------------------------------------

def tick(conn, character_id) -> List[Dict]:
    """Advance one turn: timed player conditions, NPC conditions and lit light sources
    count down; at 0 they expire. Returns `expired` entries (no numbers)."""
    out: List[Dict] = []
    conn.execute("UPDATE character_conditions SET duration_turns = duration_turns - 1 "
                 "WHERE character_id = ? AND duration_turns IS NOT NULL", (character_id,))
    for r in conn.execute("SELECT id, name FROM character_conditions WHERE character_id = ? AND duration_turns <= 0",
                          (character_id,)).fetchall():
        conn.execute("DELETE FROM character_conditions WHERE id = ?", (r["id"],))
        out.append({"type": "condition_expired", "target": "self", "name": r["name"]})
    conn.execute("UPDATE npc_conditions SET duration_turns = duration_turns - 1 WHERE duration_turns IS NOT NULL")
    for r in conn.execute("SELECT c.id, c.name, n.key, n.name AS npc_name FROM npc_conditions c "
                          "JOIN npcs n ON n.id = c.npc_id WHERE c.duration_turns <= 0").fetchall():
        conn.execute("DELETE FROM npc_conditions WHERE id = ?", (r["id"],))
        out.append({"type": "npc_condition_expired", "key": r["key"], "npc_name": r["npc_name"], "name": r["name"]})
    conn.execute("UPDATE items SET timer = timer - 1 WHERE state = ? AND timer IS NOT NULL", (LIGHT_STATE,))
    for r in conn.execute("SELECT key FROM items WHERE state = ? AND timer <= 0", (LIGHT_STATE,)).fetchall():
        item = inv.get_item(conn, r["key"])
        conn.execute("DELETE FROM items WHERE key = ?", (r["key"],))
        out.append({"type": "light_burnt_out", "key": r["key"], "name": item["name"]})
    return out


# ---------------------------------------------------------------------------
# NPC phase
# ---------------------------------------------------------------------------

def player_defense(conn, character_id) -> int:
    ch = conn.execute("SELECT dex_score FROM characters WHERE id = ?", (character_id,)).fetchone()
    return rules.armor_defense(ch["dex_score"], inv.equipped_items(conn, character_id))


def npc_phase(conn, character_id, rng: random.Random, turn: int) -> List[Dict]:
    """Every hostile, alive, reachable NPC in the player's location attacks once:
    d20 + attack_bonus vs the player's defense; a hit deals its damage dice (min 1).
    Incapacitated (e.g. blinded) NPCs lose their attack. Stops once the player is down."""
    out: List[Dict] = []
    ch = conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()
    if ch["current_hp"] <= 0:
        return out
    defense = player_defense(conn, character_id)
    hp = ch["current_hp"]
    for n in conn.execute("SELECT * FROM npcs WHERE location_id = ? AND alive = 1 AND out_of_reach = 0 ORDER BY id",
                          (ch["location_id"],)).fetchall():
        if rules.disposition_band(n["disposition"]) != "hostile":
            continue
        entry = {"npc": n["key"], "name": n["name"]}
        blocked = npc_incapacitated(conn, n["id"])
        if blocked:
            out.append({**entry, "action": "none", "reason": blocked, "outcome": None, "degree": None,
                        "hp_band": rules.hp_band(hp, ch["max_hp"])})
            continue
        roll = rng.randint(1, 20)
        total = roll + n["attack_bonus"]
        hit = total >= defense
        margin = total - defense
        dmg = max(1, rules.roll_dice(n["damage"], rng)) if hit else 0
        hp -= dmg
        rules.debug_log.debug("npc_attack %s", {"turn": turn, "npc": n["key"], "roll": roll, "total": total,
                                                "defense": defense, "damage": dmg, "player_hp": hp})
        out.append({**entry, "action": "attack", "outcome": "success" if hit else "failure",
                    "degree": rules.degree_band(margin),
                    "hp_band": rules.hp_band(hp, ch["max_hp"])})
        if hp <= 0:
            break
    if hp != ch["current_hp"]:
        conn.execute("UPDATE characters SET current_hp = ? WHERE id = ?", (hp, character_id))
    return out

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

from . import db
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
# Equipment perks (v0.7): head / hands / feet house rules
# ---------------------------------------------------------------------------

# The YAML describes these perks only in `use_note`; a template's own stats.perk (same shape) wins.
EQUIP_PERKS = {
    # "negates one 'stunned/dazed by blow to head' consequence per long rest"
    "item_leather_cap": {"negate_conditions": ["stunned", "dazed"], "per": "rest"},
    # "advantage on checks to handle hot/sharp/rough objects (climbing rope, thorns)"
    "item_work_gloves": {"advantage_tags": ["climbing", "sharp", "hot", "rough"], "advantage_methods": ["climb"]},
    # item_travel_boots: no perk in the YAML ("mostly flavor"; improvised throw only)
}
PERK_USED_FLAG = "perk_used:"


def perk_of(item: Dict) -> Dict:
    p = item["stats"].get("perk")
    return dict(p) if isinstance(p, dict) else dict(EQUIP_PERKS.get(item["template_key"]) or {})


def equipped_perks(conn, character_id) -> List[Dict]:
    """[(item, perk)] for equipped items with a perk."""
    return [(i, perk_of(i)) for i in inv.list_inventory(conn, character_id) if i["equipped"] and perk_of(i)]


def perk_advantage(conn, character_id, method: Optional[str], items_used: List[str]) -> List[Dict]:
    """Equipped items whose perk gives advantage on this check: the check is one of its methods
    (e.g. climbing) or an item used carries one of its tags (the rope is `climbing`)."""
    used_tags = set()
    for k in items_used:
        it = inv.get_item(conn, k)
        if it:
            used_tags |= set(it["tags"])
    out = []
    for item, perk in equipped_perks(conn, character_id):
        if (method and method in (perk.get("advantage_methods") or [])) or \
                used_tags & set(perk.get("advantage_tags") or []):
            out.append(item)
    return out


def perk_negates(conn, character_id, condition_name: str) -> Optional[Dict]:
    """An equipped item whose perk negates this condition and hasn't been spent since the last rest.
    Spends it (world flag perk_used:<item>) and returns the item, else None."""
    for item, perk in equipped_perks(conn, character_id):
        if condition_name in (perk.get("negate_conditions") or []):
            flag = PERK_USED_FLAG + item["key"]
            if not db.get_flag(conn, flag):
                db.set_flag(conn, flag, True)
                return item
    return None


def apply_check_consequences(conn, character_id, result):
    """db.apply_check_consequences with perks: a negated condition is kept in the result with
    `negated_by: {key, name}` and is not stored."""
    for c in result.consequences:
        if c.get("type") == "condition":
            fields = {k: v for k, v in c.items() if k not in ("type", "id", "negated_by")}
            out = add_player_condition(conn, character_id, rules.Condition(**fields))
            c.update(out)
    return result


def reset_rest_perks(conn) -> None:
    conn.execute("DELETE FROM world_flags WHERE key LIKE ?", (PERK_USED_FLAG + "%",))


def add_player_condition(conn, character_id, cond) -> Dict:
    """Every player condition goes through here: a perk may negate it. Returns
    {"id": row id} or {"negated_by": {key, name}}."""
    item = perk_negates(conn, character_id, cond.name)
    if item is not None:
        return {"negated_by": {"key": item["key"], "name": item["name"]}}
    return {"id": db.add_condition(conn, character_id, cond)}


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
    perk = perk_negates(conn, character_id, spec["name"])
    if perk is not None:  # v0.7: e.g. a leather cap shrugs off a daze
        return {"type": "condition", "target": "self", "name": spec["name"], "description": desc, "lasting": False,
                "severity": 0, "negated_by": {"key": perk["key"], "name": perk["name"]}}
    turns = rules.duration_turns(s.get("duration"))
    conn.execute("DELETE FROM character_conditions WHERE character_id = ? AND name = ?", (character_id, spec["name"]))
    conn.execute("INSERT INTO character_conditions (character_id, name, description, severity, effect, lasting, "
                 "duration_turns, source) VALUES (?, ?, ?, 0, ?, 0, ?, ?)",
                 (character_id, spec["name"], desc, json.dumps(spec["effect"]), turns, item["key"]))
    return {"type": "condition", "target": "self", "name": spec["name"], "description": desc,
            "lasting": False, "severity": 0}


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


INCAPACITATION_FLAGS = ("no_attack", "prevents_actions", "incapacitated")


def effect_incapacitates(name: str, eff: Optional[Dict]) -> bool:
    """Driven by the condition's effect spec: any of INCAPACITATION_FLAGS true. The
    hard-coded INCAPACITATING name list is only a fallback for conditions with no spec."""
    if eff:
        return any(bool(eff.get(f)) for f in INCAPACITATION_FLAGS)
    return name in INCAPACITATING


def npc_incapacitated(conn, npc_id) -> Optional[str]:
    """Name of a condition that stops this NPC acting this turn, else None."""
    for r in conn.execute("SELECT name, effect FROM npc_conditions WHERE npc_id = ? ORDER BY id", (npc_id,)):
        if effect_incapacitates(r["name"], json.loads(r["effect"] or "{}")):
            return r["name"]
    return None


def npc_condition_effect(item: Dict, name: str) -> Dict:
    """stats.condition_effect from content if present, else the built-in default."""
    eff = item["stats"].get("condition_effect")
    if isinstance(eff, dict):
        return dict(eff)
    return {}  # no spec: npc_incapacitated falls back to the INCAPACITATING name list


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
                     (npc["id"], name, json.dumps(npc_condition_effect(item, name)), turns, item["key"]))
    return {"type": "npc_condition", "key": npc["key"], "npc_name": npc["name"], "name": name,
            "outcome": "resisted" if resisted else "applied"}


# ---------------------------------------------------------------------------
# End of turn: ticking timers
# ---------------------------------------------------------------------------

def npc_condition_mark(conn) -> int:
    """Highest npc_conditions id so far; conditions with a higher id were applied this turn."""
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM npc_conditions").fetchone()[0]


def tick_npc_conditions(conn, applied_before: int) -> List[Dict]:
    """BUG-R01: NPC conditions count the NPC's actions. They tick after the player's actions and
    *before* the NPC phase, skipping ones applied this turn. A "1 round" blind thrown on turn T
    stops the NPC's attack on T, still gives advantage to the player's attack on T+1, and expires
    before the NPC acts on T+1."""
    out: List[Dict] = []
    conn.execute("UPDATE npc_conditions SET duration_turns = duration_turns - 1 "
                 "WHERE duration_turns IS NOT NULL AND id <= ?", (applied_before,))
    for r in conn.execute("SELECT c.id, c.name, n.key, n.name AS npc_name FROM npc_conditions c "
                          "JOIN npcs n ON n.id = c.npc_id WHERE c.duration_turns <= 0").fetchall():
        conn.execute("DELETE FROM npc_conditions WHERE id = ?", (r["id"],))
        out.append({"type": "npc_condition_expired", "key": r["key"], "npc_name": r["npc_name"], "name": r["name"]})
    return out


def tick(conn, character_id) -> List[Dict]:
    """End of turn: timed player conditions and lit light sources count down; at 0 they
    expire. (NPC conditions tick earlier, see tick_npc_conditions.) Returns `expired` entries."""
    out: List[Dict] = []
    conn.execute("UPDATE character_conditions SET duration_turns = duration_turns - 1 "
                 "WHERE character_id = ? AND duration_turns IS NOT NULL", (character_id,))
    for r in conn.execute("SELECT id, name FROM character_conditions WHERE character_id = ? AND duration_turns <= 0",
                          (character_id,)).fetchall():
        conn.execute("DELETE FROM character_conditions WHERE id = ?", (r["id"],))
        out.append({"type": "condition_expired", "target": "self", "name": r["name"]})
    conn.execute("UPDATE items SET timer = timer - 1 WHERE state = ? AND timer IS NOT NULL", (LIGHT_STATE,))
    for r in conn.execute("SELECT key FROM items WHERE state = ? AND timer <= 0", (LIGHT_STATE,)).fetchall():
        item = inv.get_item(conn, r["key"])
        conn.execute("DELETE FROM items WHERE key = ?", (r["key"],))
        out.append({"type": "light_burnt_out", "key": r["key"], "name": item["name"]})
    return out


# ---------------------------------------------------------------------------
# NPC phase
# ---------------------------------------------------------------------------

def hostiles_here(conn, character_id) -> List[str]:
    """Keys of living hostile NPCs in the player's location (combat, for wasted rounds)."""
    return [n["key"] for n in conn.execute(
        "SELECT n.key, n.disposition FROM npcs n JOIN characters c ON c.location_id = n.location_id "
        "WHERE c.id = ? AND n.alive = 1 ORDER BY n.id", (character_id,))
        if rules.disposition_band(n["disposition"]) == "hostile"]


def player_defense(conn, character_id) -> int:
    ch = conn.execute("SELECT dex_score FROM characters WHERE id = ?", (character_id,)).fetchone()
    return rules.armor_defense(ch["dex_score"], inv.equipped_items(conn, character_id))


PURSUED_FLAG = "pursued:"


def pursuit(conn, character_id, from_location_id) -> List[Dict]:
    """v0.6 (Lead): when the player leaves a room with a living, hostile, reachable NPC that can
    act, it follows once into the player's new room (flag pursued:<npc>) and then stays there.
    Following is that NPC's action for the turn. Works in the dark (sight isn't needed)."""
    ch = conn.execute("SELECT * FROM characters WHERE id = ?", (character_id,)).fetchone()
    if ch["current_hp"] <= 0 or from_location_id is None or ch["location_id"] == from_location_id:
        return []
    out = []
    for n in conn.execute("SELECT * FROM npcs WHERE location_id = ? AND alive = 1 AND out_of_reach = 0 ORDER BY id",
                          (from_location_id,)).fetchall():
        if rules.disposition_band(n["disposition"]) != "hostile" or npc_incapacitated(conn, n["id"]):
            continue
        if db.get_flag(conn, PURSUED_FLAG + n["key"]):
            continue
        conn.execute("UPDATE npcs SET location_id = ? WHERE id = ?", (ch["location_id"], n["id"]))
        db.set_flag(conn, PURSUED_FLAG + n["key"], True)
        out.append({"npc": n["key"], "name": n["name"], "action": "follow", "outcome": None, "degree": None,
                    "hp_band": rules.hp_band(ch["current_hp"], ch["max_hp"])})
    return out


def hostile_nearby(conn, location_id) -> Optional[str]:
    """'enemies_present' (same room) or 'hostile_nearby' (a room one exit away) if a living
    hostile NPC is there, else None. Used to block resting."""
    def hostile_in(lid):
        return any(rules.disposition_band(n["disposition"]) == "hostile" for n in conn.execute(
            "SELECT disposition FROM npcs WHERE location_id = ? AND alive = 1", (lid,)))
    if hostile_in(location_id):
        return "enemies_present"
    for e in conn.execute("SELECT DISTINCT to_location_id FROM location_exits WHERE from_location_id = ?",
                          (location_id,)).fetchall():
        if hostile_in(e[0]):
            return "hostile_nearby"
    return None


DISTRACTED = "distracted"
WHISTLED_FLAG = "noise_distracted:"


def make_noise(conn, character_id, item: Dict) -> List[Dict]:
    """v0.7 whistle (YAML: "loud noise audible ~600 ft; can distract"; tag `noisy`). Consequences:
    `noise_made {key, name, heard_by: [npc keys]}` for every living NPC here or one exit away, then
    `npc_condition {..., name: "distracted", outcome: "applied"}` for each living, hostile, non-incapacitated
    NPC here that hasn't been distracted by a noise before (once per NPC per game: they won't fall for it
    twice). Distracted (1 turn, effect {attack_disadvantage: true}): its next attack rolls at disadvantage."""
    loc = conn.execute("SELECT location_id FROM characters WHERE id = ?", (character_id,)).fetchone()[0]
    rooms = [loc] + [r[0] for r in conn.execute(
        "SELECT DISTINCT to_location_id FROM location_exits WHERE from_location_id = ?", (loc,))]
    heard = []
    for lid in rooms:
        heard += [r["key"] for r in conn.execute("SELECT key FROM npcs WHERE location_id = ? AND alive = 1 "
                                                  "ORDER BY id", (lid,))]
    out = [{"type": "noise_made", "key": item["key"], "name": item["name"], "heard_by": heard}]
    for n in conn.execute("SELECT * FROM npcs WHERE location_id = ? AND alive = 1 ORDER BY id", (loc,)).fetchall():
        if rules.disposition_band(n["disposition"]) != "hostile" or npc_incapacitated(conn, n["id"]):
            continue
        if db.get_flag(conn, WHISTLED_FLAG + n["key"]):
            continue
        db.set_flag(conn, WHISTLED_FLAG + n["key"], True)
        conn.execute("DELETE FROM npc_conditions WHERE npc_id = ? AND name = ?", (n["id"], DISTRACTED))
        conn.execute("INSERT INTO npc_conditions (npc_id, name, effect, duration_turns, source) VALUES (?, ?, ?, 1, ?)",
                     (n["id"], DISTRACTED, json.dumps({"attack_disadvantage": True}), item["key"]))
        out.append({"type": "npc_condition", "key": n["key"], "npc_name": n["name"], "name": DISTRACTED,
                    "outcome": "applied"})
    return out


def npc_attack_disadvantage(conn, npc_id) -> bool:
    return any(json.loads(r[0] or "{}").get("attack_disadvantage")
               for r in conn.execute("SELECT effect FROM npc_conditions WHERE npc_id = ?", (npc_id,)))


def npc_phase(conn, character_id, rng: random.Random, turn: int, skip=()) -> List[Dict]:
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
        if rules.disposition_band(n["disposition"]) != "hostile" or n["key"] in skip:
            continue
        entry = {"npc": n["key"], "name": n["name"]}
        blocked = npc_incapacitated(conn, n["id"])
        if blocked:
            out.append({**entry, "action": "none", "reason": blocked, "outcome": None, "degree": None,
                        "hp_band": rules.hp_band(hp, ch["max_hp"])})
            continue
        roll = rng.randint(1, 20)
        if npc_attack_disadvantage(conn, n["id"]):  # v0.7: e.g. distracted by a whistle
            roll = min(roll, rng.randint(1, 20))
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

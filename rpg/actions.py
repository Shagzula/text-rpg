"""Turn resolution: action proposal JSON (docs/contracts.md (a)) -> engine result
for the narrator (docs/contracts.md (b)).

process_action_proposal(conn, character_id, proposal, rng=None) validates the
proposal, resolves up to 3 actions in order, writes state changes through `conn`
(the caller commits), and returns the narrator-safe result. Check numbers go
only to the `rpg.debug` logger.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import db, effects, personas, rules, talk, unseen
from . import inventory as inv
from . import context
from .context import npc_hp
from .inventory import ItemError

ACTION_TYPES = ("move", "examine", "talk", "attack", "use_item", "skill_check", "take", "drop",
                "equip", "unequip", "inventory", "rest", "wait", "other")
DIFFICULTY_DC = {"trivial": 5, "easy": 10, "medium": 15, "hard": 20, "very_hard": 25, "near_impossible": 30}
DEFAULT_DIFFICULTY = "medium"
OOC_COMMANDS = ("save", "load", "quit", "help", "stats", "undo", "other")
MAX_ACTIONS = 3
VALIDATION_CODES = ("item_not_owned", "target_not_present", "item_unusable", "out_of_reach", "impossible_action")
IMPROVISED_DAMAGE = "1d4"
UNARMED_DAMAGE = "1"
DIRECTIONS = ("north", "south", "east", "west", "up", "down", "in", "out",
              "northeast", "northwest", "southeast", "southwest")
DIRECTION_ALIASES = {"n": "north", "s": "south", "e": "east", "w": "west", "u": "up", "d": "down",
                     "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
                     "upstairs": "up", "downstairs": "down", "inside": "in", "outside": "out",
                     **{d: d for d in ("north", "south", "east", "west", "up", "down", "in", "out",
                                       "northeast", "northwest", "southeast", "southwest")}}
BACK = "back"  # the exit leading to where you came from
# Ways to get through something locked (an exit, or an item whose state is 'locked').
LOCK_METHODS = {"sleight_of_hand": "pick_lock", "dex": "pick_lock", "athletics": "force_open", "str": "force_open"}
TOOL_REQUIRED_METHODS = ("pick_lock",)
IMPROVISED_PICK_TAG = "improvised_pick"  # e.g. a knife: picks a lock at very_hard (25)
TOOL_METHOD_SKILLS = (("sleight_of_hand", "pick_lock"), ("athletics", "force_open"))  # impossible without a tool that enables it
TOOL_DC_CAP = {"force_open": DIFFICULTY_DC["hard"]}  # a crowbar turns impossible into hard
TALK_APPROACHES = ("ask", "persuade", "deceive", "intimidate", "insight", "small_talk", "trade", "farewell", "gift")
APPROACH_SKILL, SKILL_APPROACH = talk.APPROACH_SKILL, talk.SKILL_APPROACH
TOUCH_DESCRIPTION = "You find it by touch in the dark: the {name}."
DEATH_DESCRIPTION = "Your wounds overcome you, and you fall."


# ---------------------------------------------------------------------------
# Proposal validation
# ---------------------------------------------------------------------------

def validate_proposal(proposal) -> Tuple[Optional[Dict], List[Dict]]:
    """Return (normalized proposal, errors). Errors are for re-prompting the interpreter."""
    errors: List[Dict] = []

    def err(path, msg):
        errors.append({"path": path, "message": msg})

    if isinstance(proposal, (str, bytes)):
        try:
            proposal = json.loads(proposal)
        except json.JSONDecodeError as e:
            err("$", f"not valid JSON: {e.msg}")
            return None, errors
    if not isinstance(proposal, dict):
        err("$", "must be a JSON object")
        return None, errors

    actions = proposal.get("actions")
    if not isinstance(actions, list):
        err("actions", "required array (0-3 action objects)")
        actions = []
    elif len(actions) > MAX_ACTIONS:
        err("actions", f"at most {MAX_ACTIONS} actions; drop the rest and note them in the last intent_summary")

    ooc = proposal.get("ooc")
    if ooc is not None:
        if not isinstance(ooc, dict) or ooc.get("command") not in OOC_COMMANDS:
            err("ooc", f"null or {{\"command\": one of {list(OOC_COMMANDS)}, \"text\": str}}")
        elif actions:
            err("actions", "must be [] when ooc is set")

    refs = proposal.get("unresolved_references", [])
    if not isinstance(refs, list) or not all(isinstance(r, str) for r in refs):
        err("unresolved_references", "array of strings")
        refs = []

    if ooc is None and not actions and not refs and not errors:
        err("actions", "no actions, no ooc command and no unresolved_references")

    norm_actions = []
    for i, a in enumerate(actions[:MAX_ACTIONS]):
        p = f"actions[{i}]"
        if not isinstance(a, dict):
            err(p, "must be an object")
            continue
        at = a.get("action_type")
        if at not in ACTION_TYPES:
            err(f"{p}.action_type", f"one of {list(ACTION_TYPES)}")
        skill = a.get("skill")
        if skill is not None and not _valid_skill(skill):
            err(f"{p}.skill", "one of the 18 5e skills, an ability (str/dex/con/int/wis/cha), or null")
        if at == "skill_check" and skill is None:
            err(f"{p}.skill", "skill_check needs a skill or ability")
        target = a.get("target")
        if target is not None and not isinstance(target, str):
            err(f"{p}.target", "context key string, \"self\", or null")
        items = a.get("items_used", [])
        if not isinstance(items, list) or not all(isinstance(k, str) for k in items):
            err(f"{p}.items_used", "array of item key strings")
            items = []
        ld = a.get("last_ditch", False)
        if not isinstance(ld, bool):
            err(f"{p}.last_ditch", "boolean")
        est = a.get("est_difficulty") or DEFAULT_DIFFICULTY
        if est not in DIFFICULTY_DC:
            err(f"{p}.est_difficulty", f"one of {list(DIFFICULTY_DC)}")
        summary = a.get("intent_summary")
        if not isinstance(summary, str) or not summary.strip():
            err(f"{p}.intent_summary", "non-empty string")
        cn = a.get("clarification_needed", False)
        if not isinstance(cn, bool):
            err(f"{p}.clarification_needed", "boolean")
        cq = a.get("clarification_question")
        if cq is not None and not isinstance(cq, str):
            err(f"{p}.clarification_question", "string or null")
        # All 13 schema fields are present on every action; talk fields are null on non-talk.
        norm = {"action_type": at, "skill": skill, "target": target, "items_used": list(items),
                "last_ditch": ld is True, "est_difficulty": est, "intent_summary": summary,
                "clarification_needed": cn is True, "clarification_question": cq,
                "approach": None, "topic": None, "topic_text": None, "quantity": None}
        if at == "talk":
            approach = a.get("approach")
            if approach is None:  # deviation: inferred instead of re-prompting (schemas §e says required)
                approach = SKILL_APPROACH.get(skill, "ask")
            if approach not in TALK_APPROACHES:
                err(f"{p}.approach", f"one of {list(TALK_APPROACHES)}")
            else:
                norm["skill"] = APPROACH_SKILL[approach]  # pairing enforced by correction, not re-prompt
            for f in ("topic", "topic_text"):
                if a.get(f) is not None and not isinstance(a.get(f), str):
                    err(f"{p}.{f}", "string or null")
            q = a.get("quantity")
            if q is not None and (isinstance(q, bool) or not isinstance(q, int)):
                err(f"{p}.quantity", "integer or null (the engine rejects values outside 1-10)")
            norm.update({"approach": approach, "topic": a.get("topic"), "topic_text": a.get("topic_text"),
                         "quantity": q})
        norm_actions.append(norm)
    if errors:
        return None, errors
    return {"actions": norm_actions, "ooc": ooc, "unresolved_references": refs}, []


def _valid_skill(skill) -> bool:
    if not isinstance(skill, str):
        return False
    try:
        rules.split_check(skill)
        return True
    except ValueError:
        return False


def invalid_proposal(errors: List[Dict]) -> Dict:
    lines = "; ".join(f"{e['path']}: {e['message']}" for e in errors)
    return {"error": "invalid_proposal", "errors": errors,
            "reprompt": f"Your previous output did not match the action-proposal schema ({lines}). "
                        "Return only one corrected JSON object."}


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

@dataclass
class Target:
    kind: str  # self | npc | item | exit | location
    key: str
    name: str
    row: Dict = field(default_factory=dict)

    def ref(self) -> Dict:
        out = {"key": self.key, "name": self.name}
        if self.kind == "npc" and self.row.get("article") is not None:
            out["article"] = self.row["article"]
        return out


class Reject(Exception):
    def __init__(self, code: str, detail: str = ""):
        assert code in VALIDATION_CODES, code
        super().__init__(code)
        self.code, self.detail = code, detail


# ---------------------------------------------------------------------------
# Turn
# ---------------------------------------------------------------------------

class Turn:
    def __init__(self, conn, character_id: int, rng: random.Random, turn: int):
        self.conn, self.cid, self.rng, self.turn = conn, character_id, rng, turn
        self.pending: List[Dict] = []  # extra consequences from the last check (tools, spent buffs)

    # -- state --------------------------------------------------------------
    @property
    def char_row(self):
        return self.conn.execute("SELECT * FROM characters WHERE id = ?", (self.cid,)).fetchone()

    @property
    def loc_id(self):
        return self.char_row["location_id"]

    def roller(self):
        return self.rng.randint(1, 20)

    # -- lookups ------------------------------------------------------------
    def resolve_target(self, key: Optional[str]) -> Optional[Target]:
        if key is None:
            return None
        if key == "self":
            return Target("self", "self", "you")
        c = self.conn
        n = c.execute("SELECT * FROM npcs WHERE key = ?", (key,)).fetchone()
        if n is not None:
            if n["location_id"] != self.loc_id:
                raise Reject("target_not_present", "elsewhere")
            row = dict(n)
            art = personas.article_for(self.conn, n["id"])
            if art is not None:
                row["article"] = art
            return Target("npc", key, n["name"], row)
        item = inv.get_item(c, key)
        if item is not None:
            if item["character_id"] != self.cid and item["location_id"] != self.loc_id:
                raise Reject("target_not_present", "elsewhere")
            return Target("item", key, item["name"], item)
        loc = c.execute("SELECT * FROM locations WHERE key = ?", (key,)).fetchone()
        if loc is not None:
            if loc["id"] == self.loc_id:
                return Target("location", key, loc["name"], dict(loc))
            ex = c.execute("SELECT * FROM location_exits WHERE from_location_id = ? AND to_location_id = ?",
                           (self.loc_id, loc["id"])).fetchone()
            if ex is None:
                raise Reject("target_not_present", "no_exit")
            return Target("exit", key, ex["description"] or loc["name"],
                          {**dict(ex), "to_name": loc["name"], "to_description": loc["description"]})
        exit_target = self._direction_target(key)
        if exit_target is not None:
            return exit_target
        raise Reject("target_not_present", "unknown_key")

    def _direction_target(self, word: str) -> Optional[Target]:
        """'up', 'u', 'north', ... or 'back' -> the matching exit from here (contracts (a):
        a move target may be a direction instead of a destination key)."""
        word = word.strip().lower()
        c = self.conn
        if word == BACK:
            prev = db.get_flag(c, f"last_location:{self.cid}")
            if not prev:
                raise Reject("target_not_present", "no_way_back")
            row = c.execute("SELECT id FROM locations WHERE key = ?", (prev,)).fetchone()
            ex = row and c.execute("SELECT * FROM location_exits WHERE from_location_id = ? AND to_location_id = ?",
                                   (self.loc_id, row["id"])).fetchone()
        elif word in DIRECTION_ALIASES:
            ex = c.execute("SELECT * FROM location_exits WHERE from_location_id = ? AND direction = ?",
                           (self.loc_id, DIRECTION_ALIASES[word])).fetchone()
        else:
            return None
        if not ex:
            raise Reject("target_not_present", "no_exit")
        dest = c.execute("SELECT * FROM locations WHERE id = ?", (ex["to_location_id"],)).fetchone()
        return Target("exit", dest["key"], ex["description"] or dest["name"],
                      {**dict(ex), "to_name": dest["name"], "to_description": dest["description"]})

    def item(self, key: str, *, allow_scene=True, reach=True) -> Dict:
        """An item the actor holds or (allow_scene) one visible here; else item_not_owned."""
        item = inv.get_item(self.conn, key)
        if item is None:
            raise Reject("item_not_owned", "no_such_item")
        if item["character_id"] == self.cid:
            return item
        if allow_scene and item["location_id"] == self.loc_id:
            if reach and item["out_of_reach"]:
                raise Reject("out_of_reach")
            return item
        raise Reject("item_not_owned")

    # -- results ------------------------------------------------------------
    def result(self, a, *, status="resolved", outcome="success", reason="no_check", target=None,
               check: Optional[rules.CheckResult] = None, consequences=(), state_changes=(), detail=None):
        r = {"action_type": a["action_type"], "intent_summary": a["intent_summary"],
             "target": target.ref() if isinstance(target, Target) else target,
             "items_used": list(a["items_used"]), "status": status}
        consequences = list(consequences) + self.pending
        self.pending = []
        if check is not None:
            r.update(check.to_narration_dict(list(state_changes)))
            r["consequences"] += list(consequences)
        else:
            r.update({"skill": None, "ability": None, "outcome": outcome, "degree": None, "reason": reason,
                      "auto_success": False, "impossible": False, "last_ditch": False,
                      "consequences": list(consequences), "state_changes": list(state_changes)})
        if detail:
            r["detail"] = detail
        return r

    def reject(self, a, code, detail="", target=None):
        return self.result(a, status="rejected", outcome="failure", reason=code, target=target,
                           detail=detail or None)

    def clarify(self, a):
        r = self.result(a, status="clarify", outcome="clarify", reason="clarify",
                        target=None) if a else {
            "action_type": None, "intent_summary": None, "target": None, "items_used": [], "status": "clarify",
            "skill": None, "ability": None, "outcome": "clarify", "degree": None, "reason": "clarify",
            "auto_success": False, "impossible": False, "last_ditch": False, "consequences": [],
            "state_changes": []}
        return r

    @property
    def lit(self) -> bool:
        return effects.location_is_lit(self.conn, self.loc_id, self.cid)

    def check(self, a, skill: str, dc: int, *, method: Optional[str] = None) -> rules.CheckResult:
        """Resolve a check with situational advantage: relevant tools (skill_bonus grants
        advantage) and darkness (sight skills at disadvantage). Conditions are applied by
        the rules layer. One-check buffs for this ability are spent afterwards."""
        char = db.load_character(self.conn, self.cid)
        sk, ab = rules.split_check(skill)
        tools = effects.tool_advantage(effects.available_tools(self.conn, self.cid, a["items_used"]),
                                       sk, method, a["items_used"])
        perks = effects.perk_advantage(self.conn, self.cid, method, a["items_used"])  # v0.7: e.g. gloves
        dark = sk in effects.DARK_SKILLS and not self.lit
        res = rules.resolve_skill_check(char, skill, dc, last_ditch=a["last_ditch"], roller=self.roller,
                                        advantage=bool(tools or perks), disadvantage=dark)
        effects.apply_check_consequences(self.conn, self.cid, res)
        res.log_debug(turn=self.turn, action=a["action_type"], intent=a["intent_summary"], method=method,
                      tools=[t["key"] for t in tools], dark=dark)
        self.pending += [{"type": "tool_used", "key": t["key"], "name": t["name"]} for t in tools]
        self.pending += [{"type": "perk_used", "key": t["key"], "name": t["name"]} for t in perks]
        if dark:
            self.pending.append({"type": "darkness", "description": "hard to see in the dark"})
        if res.roll is not None or res.auto_success:
            self.pending += effects.expire_after_check(self.conn, self.cid, sk, ab)
        return res

    def dc_for(self, a, target: Optional[Target]) -> int:
        if target is not None and target.kind == "exit" and target.row.get("locked") and target.row.get("unlock_dc"):
            return target.row["unlock_dc"]  # authored DC overrides the hint
        if target is not None and target.kind == "item" and target.row["stats"].get("dc"):
            return target.row["stats"]["dc"]
        return DIFFICULTY_DC[a["est_difficulty"]]

    # -- dispatcher ---------------------------------------------------------
    def resolve(self, a) -> Dict:
        t, target_error = None, None
        try:
            if a["action_type"] == "other" and a["skill"] is None:
                # Nothing the engine can model: never a free success (BUG-02).
                raise Reject("impossible_action",
                             "lacks_ability" if a["est_difficulty"] == "near_impossible" else "unrecognized")
            handler = getattr(self, "do_" + a["action_type"])
            if a["action_type"] in ("take", "drop", "equip", "unequip"):
                return handler(a)  # item actions: the item is the target
            try:
                t = self.resolve_target(a["target"])
            except Reject as r:
                target_error = r
            for k in a["items_used"]:  # claimed-but-nonexistent items win over target errors
                if inv.get_item(self.conn, k) is None:
                    raise Reject("item_not_owned", "no_such_item")
            if target_error:
                raise target_error
            return handler(a, t)
        except Reject as r:
            return self.reject(a, r.code, r.detail, target=t if t else self._loose_target(a["target"]))
        except ItemError as e:
            return self.reject(a, e.code, e.detail, target=t)

    def _loose_target(self, key):
        if not key:
            return None
        n = self.conn.execute("SELECT id, name FROM npcs WHERE key = ?", (key,)).fetchone()
        if n is None:
            return {"key": key, "name": None}  # a named person elsewhere (BUG-R2-07)
        out = {"key": key, "name": n["name"]}
        art = personas.article_for(self.conn, n["id"])
        if art is not None:
            out["article"] = art
        return out

    # -- handlers -----------------------------------------------------------
    def do_wait(self, a, t):
        return self.result(a, target=t)

    def do_inventory(self, a, t):
        return self.result(a, target=t)

    def do_examine(self, a, t):
        for k in a["items_used"]:
            self.item(k, reach=False)
        held = t is not None and (t.kind == "self" or (t.kind == "item" and t.row["character_id"] == self.cid))
        here = t is None or (t.kind == "location" and t.row["id"] == self.loc_id)
        lit = self.lit
        felt = {i["key"] for i in context.felt_items(self.conn, self.loc_id, self.cid)} if not lit else set()
        by_touch = not lit and t is not None and t.kind == "item" and t.key in felt and not held
        if not a["skill"] and not held and not here and not by_touch and not lit:
            raise Reject("impossible_action", "too_dark")  # searching (a skill) is allowed, at disadvantage
        target = t.ref() if t else None
        if t is not None:
            target["description"] = self._describe(t)
            if here and not lit:  # BUG-R02: looking around in the dark shows the darkness
                target["description"] = context.DARK_DESCRIPTION
            if by_touch:  # BUG-R2-05: a felt thing is known by touch, not by sight
                target["description"] = TOUCH_DESCRIPTION.format(name=t.name)
        check = self.check(a, a["skill"], self.dc_for(a, t)) if a["skill"] else None
        r = self.result(a, target=target, check=check)
        if here and not lit and (check is None or check.success):
            if check is not None:  # a successful search finds the loose things too, and remembers them
                loc_key = self.conn.execute("SELECT key FROM locations WHERE id = ?", (self.loc_id,)).fetchone()[0]
                db.set_flag(self.conn, f"{context.SEARCHED_DARK_FLAG}{loc_key}", True)
            r["felt"] = [{"key": i["key"], "name": i["name"]}
                         for i in context.felt_items(self.conn, self.loc_id, self.cid)]
        return r

    def _describe(self, t: Target) -> Optional[str]:
        if t.kind == "item":
            d = t.row.get("description") or ""
            return f"{d} ({t.row['state']})".strip() if t.row.get("state") else (d or None)
        if t.kind == "npc":
            return t.row.get("description")
        if t.kind == "exit":
            return f"{t.row.get('description') or t.row['to_name']} ({'locked' if t.row['locked'] else 'open'})"
        if t.kind == "location":
            return t.row.get("description")
        return None

    def do_move(self, a, t):
        if t is None:
            raise Reject("target_not_present", "no_destination")
        if t.kind == "location":
            raise Reject("impossible_action", "already_here")
        if t.kind != "exit":
            raise Reject("impossible_action", "not_an_exit")
        if t.row["locked"]:
            raise Reject("impossible_action", "exit_locked")
        check = None
        if a["skill"]:  # e.g. climbing down: a real check gates the move
            # v0.7: an athletics move is a climb (rope / gloves give advantage)
            method = "climb" if rules.split_check(a["skill"])[0] == "athletics" else None
            check = self.check(a, a["skill"], self.dc_for(a, t), method=method)
            if not check.success:
                return self.result(a, target=t, check=check)
        dest = t.row["to_location_id"]
        here = self.conn.execute("SELECT key FROM locations WHERE id = ?", (self.loc_id,)).fetchone()["key"]
        db.set_flag(self.conn, f"last_location:{self.cid}", here)
        self.conn.execute("UPDATE characters SET location_id = ? WHERE id = ?", (dest, self.cid))
        changes = [{"type": "moved_to_location", "key": t.key, "name": t.row["to_name"],
                    "dark": not self.lit}]  # BUG-R02: arriving somewhere pitch dark
        flag = f"visited:{t.key}"
        if not db.get_flag(self.conn, flag):
            db.set_flag(self.conn, flag, True)
            changes.append({"type": "flag_set", "key": flag, "value": True})
        return self.result(a, target=t, check=check, state_changes=changes)

    def _npcs_here(self):
        return self.conn.execute("SELECT * FROM npcs WHERE location_id = ? AND alive = 1 ORDER BY id",
                                 (self.loc_id,)).fetchall()

    def unseen_name_question(self, verb: str = "attack") -> str:
        """The player named someone they can only hear, and more than one unseen NPC
        is here: never confirm the name; ask by sound."""
        names = [f"the one making {unseen.sound(self.conn, n)}" for n in self._npcs_here()]
        if len(names) == 1:
            return f"You can't tell who that is in the dark. Do you mean {names[0]}?"
        if len(names) > 1:
            return "Who do you want to " + verb + ": " + ", ".join(names[:-1]) + " or " + names[-1] + "?"
        return "You can't tell who that is in the dark."

    def default_npc_target(self, a, verb: str = "talk to") -> Tuple[Dict, Optional[str]]:
        """Bare 'talk' / 'attack' (BUG-R04, BUG-R4-01): the only living NPC here, visible or only heard;
        a question if there are several (unseen ones by their sound, never by name); else unchanged."""
        npcs = self._npcs_here()
        if len(npcs) == 1:
            return {**a, "target": npcs[0]["key"]}, None
        if len(npcs) > 1:
            names = [n["name"] if self.lit else f"the one making {unseen.sound(self.conn, n)}" for n in npcs]
            return a, f"Who do you want to {verb}: " + ", ".join(names[:-1]) + " or " + names[-1] + "?"
        return a, None

    def default_talk_target(self, a) -> Tuple[Dict, Optional[str]]:
        return self.default_npc_target(a, "talk to")

    def do_talk(self, a, t):
        if t is None:
            raise Reject("target_not_present", "no_target")  # BUG-10
        if t.kind != "npc":
            raise Reject("impossible_action", "not_a_creature")
        if not t.row["alive"]:
            raise Reject("impossible_action", "target_defeated")
        try:
            return talk.resolve(self, a, t)
        except talk.TalkReject as e:
            raise Reject(e.code, e.detail)

    def do_skill_check(self, a, t):
        for k in a["items_used"]:
            self.item(k)
        locked = t is not None and ((t.kind == "exit" and t.row["locked"]) or
                                    (t.kind == "item" and t.row.get("state") == "locked"))
        if not locked and t is not None and t.kind == "exit" and a["skill"] in LOCK_METHODS:
            # picking/forcing an open way: nothing to roll for, no last-ditch cost (BUG-07/12)
            raise Reject("impossible_action", "already_open")
        method = LOCK_METHODS.get(a["skill"]) if locked else None
        dc = self.dc_for(a, t)
        if method:
            tool = effects.tool_enabling(effects.available_tools(self.conn, self.cid, a["items_used"]), method)
            if tool is None and method == "pick_lock":
                tool = next((i for i in inv.list_inventory(self.conn, self.cid)
                             if IMPROVISED_PICK_TAG in (i["tags"] or [])), None)
                if tool is not None:  # a dagger tip in the lock: possible, but very hard
                    dc = max(dc, DIFFICULTY_DC["very_hard"])
            if tool is None and method in TOOL_REQUIRED_METHODS:
                raise Reject("impossible_action", "needs_tool")
            if tool is not None and method in TOOL_DC_CAP:
                dc = min(dc, TOOL_DC_CAP[method])
            if tool is not None and tool["key"] not in a["items_used"]:
                a = {**a, "items_used": a["items_used"] + [tool["key"]]}  # the held tool is the one used
        check = self.check(a, a["skill"], dc, method=method)
        changes = []
        if check.success and method:
            changes += self._unlock(t) if t.kind == "exit" else self._open_item(t)
        return self.result(a, target=t, check=check, state_changes=changes)

    def _open_item(self, t: Target) -> List[Dict]:
        self.conn.execute("UPDATE items SET state = 'open' WHERE key = ?", (t.key,))
        return [{"type": "entity_state", "key": t.key, "field": "state", "from": "locked", "to": "open"}]

    def do_other(self, a, t):
        if a["skill"]:
            return self.do_skill_check(a, t)
        for k in a["items_used"]:
            self.item(k)
        return self.result(a, target=t)

    def _unlock(self, t: Target) -> List[Dict]:
        c = self.conn
        c.execute("UPDATE location_exits SET locked = 0 WHERE from_location_id = ? AND to_location_id = ?",
                  (self.loc_id, t.row["to_location_id"]))
        c.execute("UPDATE location_exits SET locked = 0 WHERE from_location_id = ? AND to_location_id = ?",
                  (t.row["to_location_id"], self.loc_id))  # the other side too
        return [{"type": "exit_state", "key": t.key, "direction": t.row["direction"], "from": "locked", "to": "open"}]

    def do_rest(self, a, t):
        """Rest heals half the missing HP (rounded up) and clears non-lasting conditions.
        Eating a ration while resting (one is consumed if held) heals to full instead."""
        danger = effects.hostile_nearby(self.conn, self.loc_id)  # v0.6: no resting next to a hostile either
        if danger:
            raise Reject("impossible_action", danger)
        effects.reset_rest_perks(self.conn)  # v0.7: once-per-rest perks (leather cap) are ready again
        ch = self.char_row
        cons = []
        ration = next((i for i in inv.list_inventory(self.conn, self.cid)
                       if i["stats"].get("effect") == "sustenance"), None)
        if ration is not None:
            cons += self._consume(ration)
        missing = ch["max_hp"] - ch["current_hp"]
        if missing > 0:
            new = ch["max_hp"] if ration is not None else ch["current_hp"] + math.ceil(missing / 2)
            self.conn.execute("UPDATE characters SET current_hp = ? WHERE id = ?", (new, self.cid))
            cons.append({"type": "healed", "target": "self", "hp_band": rules.hp_band(new, ch["max_hp"])})
        for r in self.conn.execute("SELECT id, name FROM character_conditions WHERE character_id = ? AND lasting = 0",
                                   (self.cid,)).fetchall():
            self.conn.execute("DELETE FROM character_conditions WHERE id = ?", (r["id"],))
            cons.append({"type": "condition_removed", "target": "self", "name": r["name"]})
        return self.result(a, target=t, consequences=cons)

    # item handling -----------------------------------------------------------
    def _item_keys(self, a) -> List[str]:
        keys = list(a["items_used"])
        if not keys and a["target"] and a["target"] != "self":
            keys = [a["target"]]
        if not keys:
            raise Reject("item_not_owned", "no_item")
        return keys

    def do_take(self, a):
        keys = self._item_keys(a)
        items = []
        for k in keys:
            item = inv.get_item(self.conn, k)
            if item is None or (item["location_id"] != self.loc_id and item["character_id"] != self.cid):
                raise Reject("target_not_present")
            if item["character_id"] == self.cid:
                raise Reject("item_unusable", "already_held")
            if item["fixture"]:
                raise Reject("item_unusable", "fixture")
            if item["out_of_reach"]:
                raise Reject("out_of_reach")
            items.append(item)
        changes = []
        for item in items:
            inv.transfer(self.conn, item["key"], to_character=self.cid)
            changes.append({"type": "item_added", "key": item["key"], "name": item["name"]})
        return self.result(a, target=self._item_target(items), state_changes=changes)

    def _item_target(self, items):
        return {"key": items[0]["key"], "name": items[0]["name"]} if items else None

    def _held(self, keys) -> List[Dict]:
        items = []
        for k in keys:
            item = inv.get_item(self.conn, k)
            if item is None or item["character_id"] != self.cid:
                raise Reject("item_not_owned")
            items.append(item)
        return items

    def do_drop(self, a):
        items = self._held(self._item_keys(a))
        loc_key = self.conn.execute("SELECT key FROM locations WHERE id = ?", (self.loc_id,)).fetchone()
        changes = []
        for item in items:
            inv.transfer(self.conn, item["key"], to_location=self.loc_id)
            changes.append({"type": "item_removed", "key": item["key"], "name": item["name"],
                            "to": loc_key["key"] if loc_key else None})
        return self.result(a, target=self._item_target(items), state_changes=changes)

    def do_equip(self, a):
        items = self._held(self._item_keys(a))
        changes = []
        for item in items:
            for off in inv.equip(self.conn, self.cid, item["key"]):
                o = inv.get_item(self.conn, off)
                changes.append({"type": "item_unequipped", "key": off, "name": o["name"]})
            changes.append({"type": "item_equipped", "key": item["key"], "name": item["name"], "slot": item["slot"]})
        return self.result(a, target=self._item_target(items), state_changes=changes)

    def do_unequip(self, a):
        items = self._held(self._item_keys(a))
        changes = []
        for item in items:
            inv.unequip(self.conn, self.cid, item["key"])
            changes.append({"type": "item_unequipped", "key": item["key"], "name": item["name"]})
        return self.result(a, target=self._item_target(items), state_changes=changes)

    def do_use_item(self, a, t):
        keys = list(a["items_used"])
        if not keys and t is not None and t.kind == "item":
            keys = [t.key]
        if not keys:
            raise Reject("item_not_owned", "no_item")
        item = self.item(keys[0])
        for k in keys[1:]:
            self.item(k)
        stats = item["stats"]
        effect = stats.get("effect")
        # A lock tool on a way through: the tool decides the skill (lockpicks -> sleight of
        # hand, crowbar -> athletics), whatever the interpreter guessed (BUG-03).
        enables = stats.get("enables") or []
        if t is not None and t.kind in ("exit", "item") and t.key != item["key"]:
            skill = next((sk for sk, m in TOOL_METHOD_SKILLS if m in enables), None)
            if skill:
                return self.do_skill_check({**a, "skill": skill}, t)
        # A tool on something (crowbar on a door) with a skill is a skill check.
        if a["skill"] and effect not in ("heal", "cure_condition", "condition") and not (t and t.key == item["key"]):
            return self.do_skill_check(a, t)
        if effect == "light":
            if item["character_id"] != self.cid:
                raise Reject("item_not_owned", "light_not_held")
            change = effects.light(self.conn, item)
            return self.result(a, target={"key": item["key"], "name": item["name"]}, state_changes=[change])
        if effect == "condition":  # thrown at NPCs: flash powder
            return self._use_condition_item(a, t, item)
        if effects.noise_spec(item):  # v0.7/v0.7.1: stats.noise, else subcategory signal (the whistle)
            if item["character_id"] != self.cid:
                raise Reject("item_not_owned", "not_held")
            return self.result(a, target={"key": item["key"], "name": item["name"]},
                               consequences=effects.make_noise(self.conn, self.cid, item))
        check = None
        if isinstance(stats.get("check"), dict):  # e.g. bandages need a medicine check
            spec = stats["check"]
            check = self.check(a, spec.get("skill") or a["skill"] or "medicine",
                               DIFFICULTY_DC.get(spec.get("est_difficulty"), DIFFICULTY_DC["easy"]))
            if not check.success:
                return self.result(a, target=t, check=check)
        if (check is None and not effect and not stats.get("use_sets_flag")
                and (t is None or t.kind == "self" or t.key == item["key"])):
            # BUG-R05: a tool or weapon used on nothing does nothing; no free success, no time
            raise Reject("impossible_action", "needs_target" if enables or item["category"] in ("tool", "weapon")
                         else "no_effect")
        cons, changes = [], []
        if effect == "heal" and self.char_row["current_hp"] >= self.char_row["max_hp"]:
            raise Reject("item_unusable", "already_full")  # don't waste it (BUG-08)
        if effect == "heal":
            ch = self.char_row
            new = min(ch["max_hp"], ch["current_hp"] + max(0, rules.roll_dice(stats.get("amount", "1"), self.rng)))
            self.conn.execute("UPDATE characters SET current_hp = ? WHERE id = ?", (new, self.cid))
            cons.append({"type": "healed", "target": "self", "hp_band": rules.hp_band(new, ch["max_hp"])})
        elif effect == "buff":
            cons.append(effects.apply_buff(self.conn, self.cid, item))
        elif effect == "cure_condition":
            name = stats.get("condition")
            rows = self.conn.execute("SELECT id, name FROM character_conditions WHERE character_id = ? AND name = ?",
                                     (self.cid, name)).fetchall()
            for r in rows:
                self.conn.execute("DELETE FROM character_conditions WHERE id = ?", (r["id"],))
                cons.append({"type": "condition_removed", "target": "self", "name": r["name"]})
        if stats.get("use_sets_flag"):
            db.set_flag(self.conn, stats["use_sets_flag"], True)
            changes.append({"type": "flag_set", "key": stats["use_sets_flag"], "value": True})
        if stats.get("uses") is not None:
            for c in self._consume(item):
                (changes if c["type"] == "item_removed" else cons).append(c)
        return self.result(a, target=t or {"key": item["key"], "name": item["name"]}, check=check,
                           consequences=cons, state_changes=changes)

    def _consume(self, item) -> List[Dict]:
        out = []
        if inv.consume_use(self.conn, item["key"]):
            out.append({"type": "item_consumed", "key": item["key"], "name": item["name"],
                        "description": f"{item['name']} used up"})
            if inv.get_item(self.conn, item["key"]) is None:
                gone = {"type": "item_removed", "key": item["key"], "name": item["name"]}
                if item["character_id"] != self.cid:
                    gone["from"] = "scene"  # BUG-R06: used where it lay, not from the pack
                out.append(gone)
        else:
            out.append({"type": "item_used", "key": item["key"], "name": item["name"],
                        "description": f"{item['name']} has uses left"})
        return out

    def _use_condition_item(self, a, t, item):
        """An item that inflicts a condition on NPCs (stats.condition, stats.save). Aimed at
        one NPC, or with no NPC target, at every living NPC here (an area burst). Each NPC
        saves; the item is consumed either way."""
        if t is not None and t.kind == "npc":
            if not t.row["alive"]:
                raise Reject("impossible_action", "target_defeated")
            npcs = [t.row]
        elif t is None or t.kind in ("self", "location") or t.key == item["key"]:
            npcs = [dict(r) for r in self.conn.execute(
                "SELECT * FROM npcs WHERE location_id = ? AND alive = 1 AND out_of_reach = 0", (self.loc_id,))]
        else:
            raise Reject("impossible_action", "not_a_creature")
        cons = []
        for n in npcs:
            cons.append(effects.npc_save_condition(self.conn, n, item, self.rng, self.turn))
            if rules.disposition_band(n["disposition"]) != "hostile":  # it's an attack, near enough
                self.conn.execute("UPDATE npcs SET disposition = -100 WHERE id = ?", (n["id"],))
                cons.append({"type": "npc_disposition_changed", "key": n["key"], "to": "hostile"})
        changes = []
        for c in self._consume(item):
            (changes if c["type"] == "item_removed" else cons).append(c)
        return self.result(a, target=t if t is not None and t.kind == "npc" else {"key": item["key"], "name": item["name"]},
                           consequences=cons, state_changes=changes)

    # combat ------------------------------------------------------------------
    def do_attack(self, a, t):
        if t is None:
            raise Reject("target_not_present", "no_target")
        if t.kind == "self":
            raise Reject("impossible_action", "self")
        if t.kind in ("exit", "location"):
            raise Reject("impossible_action", "not_attackable")
        if t.kind == "npc" and not t.row["alive"]:
            raise Reject("impossible_action", "target_defeated")
        weapon, improvised = None, False
        if a["items_used"]:
            weapon = inv.get_item(self.conn, a["items_used"][0])
            if weapon["character_id"] != self.cid:
                # you attack with what you hold; pick it up first (BUG-11)
                raise Reject("item_not_owned", "not_held")
            improvised = weapon["category"] != "weapon"
        stats = weapon["stats"] if weapon else {}
        props = stats.get("properties") or []
        ranged = bool(stats.get("range")) or "thrown" in props
        if (t.row.get("out_of_reach")) and not ranged:
            raise Reject("out_of_reach")
        char = db.load_character(self.conn, self.cid)
        ability, proficient = self._attack_ability(a, char, weapon, improvised, props)
        dc = t.row["defense_dc"] if t.kind == "npc" else self.dc_for(a, t)
        helpless = t.kind == "npc" and effects.npc_incapacitated(self.conn, t.row["id"])
        res = rules.resolve_attack(char, ability, dc, proficient=proficient, last_ditch=a["last_ditch"],
                                   roller=self.roller, advantage=bool(helpless))
        effects.apply_check_consequences(self.conn, self.cid, res)
        res.log_debug(turn=self.turn, action="attack", target=t.key, target_condition=helpless or None)
        cons, changes = [], []
        if res.roll is not None:
            self.pending += effects.expire_after_check(self.conn, self.cid, None, ability)
        if t.kind == "npc":
            if res.success:
                if weapon is None:
                    dice = UNARMED_DAMAGE
                elif improvised:
                    dice = stats.get("improvised_damage") or IMPROVISED_DAMAGE
                else:
                    dice = stats.get("damage") or IMPROVISED_DAMAGE
                dmg = max(1, rules.roll_dice(dice, self.rng) + rules.ability_modifier(char.scores[ability]))
                hp = npc_hp(t.row) - dmg
                rules.debug_log.debug("damage %s", {"turn": self.turn, "target": t.key, "dice": dice, "damage": dmg})
                self.conn.execute("UPDATE npcs SET current_hp = ? WHERE id = ?", (max(hp, 0), t.row["id"]))
                cons.append({"type": "damage_dealt", "target": t.key, "hp_band": rules.hp_band(hp, t.row["max_hp"])})
                if hp <= 0:
                    self.conn.execute("UPDATE npcs SET alive = 0 WHERE id = ?", (t.row["id"],))
                    defeated = {"type": "npc_defeated", "key": t.key, "name": t.name}
                    if t.row.get("article") is not None:
                        defeated["article"] = t.row["article"]
                    changes.append(defeated)
                    db.set_flag(self.conn, f"defeated:{t.key}", True)
                    changes.append({"type": "flag_set", "key": f"defeated:{t.key}", "value": True})
            if not changes and rules.disposition_band(t.row["disposition"]) != "hostile":
                self.conn.execute("UPDATE npcs SET disposition = -100 WHERE id = ?", (t.row["id"],))
                cons.append({"type": "npc_disposition_changed", "key": t.key, "to": "hostile"})
        elif res.success:  # an object
            old = t.row.get("state")
            self.conn.execute("UPDATE items SET state = 'damaged' WHERE key = ?", (t.key,))
            changes.append({"type": "entity_state", "key": t.key, "field": "state", "from": old, "to": "damaged"})
        return self.result(a, target=t, check=res, consequences=cons, state_changes=changes)

    @staticmethod
    def _attack_ability(a, char, weapon, improvised, props) -> Tuple[str, bool]:
        if weapon is None:
            return "str", True  # unarmed strike
        if improvised:
            hint = a["skill"] if a["skill"] in ("str", "dex") else "str"
            return hint, False
        stats = weapon["stats"]
        if "finesse" in props:
            ability = max(("str", "dex"), key=lambda ab: rules.skill_modifier(char, ab))
        elif "ammunition" in props or (stats.get("range") and "thrown" not in props):
            ability = "dex"
        else:
            ability = "str"
        wp = char.weapon_proficiencies
        proficient = stats.get("proficiency") in wp or weapon["template_key"] in wp
        return ability, proficient


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _next_turn(conn, character_id: int) -> int:
    row = conn.execute("SELECT id, turn FROM save_games WHERE character_id = ? ORDER BY id LIMIT 1",
                       (character_id,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO save_games (name, character_id, turn) VALUES ('autosave', ?, 1)", (character_id,))
        return 1
    conn.execute("UPDATE save_games SET turn = turn + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
    return row["turn"] + 1


def _undo_turn(conn, character_id: int) -> None:
    """Nothing resolved (all rejected / clarify): the turn is not spent."""
    conn.execute("UPDATE save_games SET turn = turn - 1 WHERE id = (SELECT id FROM save_games "
                 "WHERE character_id = ? ORDER BY id LIMIT 1)", (character_id,))


def _refs(a) -> set:
    return ({a["target"]} | set(a["items_used"])) - {None, "self"}


def _depends_on(a, failed: List[Dict]) -> bool:
    """A later action is invalidated by an earlier failed check if the failed action was a
    move, or the two share a target/item."""
    for f in failed:
        if f["action_type"] == "move" or (_refs(a) & _refs(f)):
            return True
    return False


def player_status(conn, character_id: int) -> Dict:
    ch = conn.execute("SELECT current_hp, max_hp FROM characters WHERE id = ?", (character_id,)).fetchone()
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM character_conditions WHERE character_id = ? ORDER BY id", (character_id,))]
    return {"hp_band": rules.hp_band(ch["current_hp"], ch["max_hp"]), "conditions": names}


FREE_REJECTIONS = frozenset({"unrecognized"})  # never cost a round, even in combat
# v0.10: the second consecutive same-reference clarify in combat costs the round;
# any 5 consecutive free (no-time) turns in combat also cost, then keep costing
# until a resolved action resets the streak. Lit and dark both count.
FREE_STREAK_CAP = 5
PENDING_FLAG = "pending_clarify:"
STREAK_FLAG = "combat_free_streak:"
LAST_CLARIFY_FLAG = "last_free_clarify:"


def _sig_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ",".join(_sig_text(v) for v in value)
    return " ".join(str(value).lower().replace("_", " ").split())


AFFIRM_REPLIES = frozenset({"yes", "y", "yeah", "yep", "yup", "ok", "okay", "sure", "do it",
                            "it", "them", "him", "her", "that"})


def _candidate_keys(candidates) -> str:
    keys = sorted({(c.get("key") if isinstance(c, dict) else str(c))
                   for c in (candidates or []) if c})
    return ",".join(keys)


def _clarify_signature(kind: str, action: dict, raw_target=None, candidates=None) -> str:
    """Same-reference identity is the action type plus the resolved candidate set,
    not the player's wording ('the guard' vs 'the dozing guard')."""
    cand = _candidate_keys(candidates)
    if cand:
        return "|".join(("ref", str(action.get("action_type") or ""), cand))
    return "|".join((
        kind,
        str(action.get("action_type") or ""),
        _sig_text(raw_target if raw_target is not None else action.get("target")),
        _sig_text(action.get("clarification_question") or action.get("intent_summary")),
    ))


def _pending_candidates(conn, character_id: int, hidden: dict) -> list:
    living_hidden = [{"key": info["ref"], "sound": info["sound"]}
                     for info in hidden.values() if info.get("alive")]
    if living_hidden:
        return living_hidden
    loc = conn.execute("SELECT location_id FROM characters WHERE id = ?", (character_id,)).fetchone()
    if not loc or loc[0] is None:
        return []
    return [{"key": n["key"]} for n in conn.execute(
        "SELECT key FROM npcs WHERE location_id = ? AND alive = 1 ORDER BY id", (loc[0],))]


def _store_pending(conn, character_id: int, action: dict, question: str, hidden: dict,
                   signature: str, reasks: int = 0) -> None:
    db.set_flag(conn, PENDING_FLAG + str(character_id), {
        "action_type": action.get("action_type"),
        "skill": action.get("skill"),
        "items_used": list(action.get("items_used") or []),
        "intent_summary": action.get("intent_summary"),
        "last_ditch": bool(action.get("last_ditch")),
        "question": question,
        "signature": signature,
        "candidates": _pending_candidates(conn, character_id, hidden),
        "reasks": int(reasks or 0),
    })


def _affirmation_reask(prop: dict, pending) -> bool:
    """True when the player answered a multi-candidate pending clarify with yes/ok/etc."""
    if not pending or len(pending.get("candidates") or []) < 2:
        return False
    refs = [_sig_text(r) for r in (prop.get("unresolved_references") or [])]
    return bool(refs) and all(r in AFFIRM_REPLIES for r in refs)


def _clear_flag(conn, key: str) -> None:
    if db.get_flag(conn, key) is not None:
        db.set_flag(conn, key, None)


def _clear_pending(conn, character_id: int) -> None:
    _clear_flag(conn, PENDING_FLAG + str(character_id))


def _reset_stall(conn, character_id: int) -> None:
    cid = str(character_id)
    streak = db.get_flag(conn, STREAK_FLAG + cid)
    if streak:  # leave a never-written flag alone so rejected turns don't touch the DB
        db.set_flag(conn, STREAK_FLAG + cid, 0)
    _clear_flag(conn, LAST_CLARIFY_FLAG + cid)
    _clear_pending(conn, character_id)


def _maybe_cap_free_turn(conn, character_id: int, in_combat, question, signature: str):
    """If this combat turn passed no time, apply the stall cap. Returns True if the
    turn should become a wasted round (foe acts, question still shown)."""
    if not in_combat:
        if question:
            # still remember the pending clarify outside combat so 'yes' / sound replies work
            return False
        _clear_pending(conn, character_id)
        return False
    cid = str(character_id)
    streak = int(db.get_flag(conn, STREAK_FLAG + cid) or 0) + 1
    last = db.get_flag(conn, LAST_CLARIFY_FLAG + cid)
    db.set_flag(conn, STREAK_FLAG + cid, streak)
    if signature:
        db.set_flag(conn, LAST_CLARIFY_FLAG + cid, signature)
    same = bool(question) and bool(signature) and signature == last
    return same or streak >= FREE_STREAK_CAP


def process_action_proposal(conn, character_id: int, proposal, rng: Optional[random.Random] = None) -> Dict[str, Any]:
    """Validate and resolve one interpreter proposal. Returns one of:
    - {"error": "invalid_proposal", "errors": [...], "reprompt": str}  (nothing changed)
    - {"type": "ooc", "ooc": {...}, "results": []}                       (nothing changed)
    - the engine result for the narrator (contracts.md (b))
    """
    prop, errors = validate_proposal(proposal)
    if errors:
        return invalid_proposal(errors)
    if prop["ooc"] is not None:
        return {"type": "ooc", "ooc": prop["ooc"], "results": []}
    if conn.execute("SELECT 1 FROM characters WHERE id = ?", (character_id,)).fetchone() is None:
        raise ValueError(f"Character {character_id} not found")

    rng = rng or random.Random()
    if conn.execute("SELECT current_hp FROM characters WHERE id = ?", (character_id,)).fetchone()[0] <= 0:
        return {"turn": None, "results": [], "player_status": player_status(conn, character_id), "dialogue": [],
                "npc_actions": [], "expired": [], "defeat": death(), "clarification_question": None,
                "time_passed": False}
    turn = Turn(conn, character_id, rng, _next_turn(conn, character_id))
    hidden = unseen.unseen_npcs(conn, character_id)  # v0.7.1: unseen at the start of the turn...
    named_in_dark = set()
    raw_targets = [a.get("target") for a in prop["actions"]]
    living = unseen.living_unseen(conn, character_id)
    for i, a in enumerate(prop["actions"]):
        # v0.10.1: a remembered name or real/invented npc_ key in the dark, with exactly
        # one unseen NPC, resolves to that NPC's opaque ref (name never confirmed).
        # With several, ask by sound and do not resolve.
        if unseen.named_or_slugged_in_dark(conn, character_id, a.get("target")):
            if len(living) == 1:
                only = next(iter(living.values()))
                a["target"] = only["ref"]
                a["intent_summary"] = unseen.sound_intent(
                    a.get("action_type"), only.get("sound"), a.get("items_used"))
            elif len(living) > 1:
                named_in_dark.add(i)
        a["target"] = unseen.resolve(conn, a["target"])  # opaque refs target the NPC behind them
    clarify_action = None
    clarify_signature = None
    npc_mark = effects.npc_condition_mark(conn)
    in_combat = effects.hostiles_here(conn, character_id)  # at the start of the turn
    start_loc = conn.execute("SELECT location_id FROM characters WHERE id = ?", (character_id,)).fetchone()[0]
    actions = prop["actions"]
    results: List[Dict] = []
    skipped: List[Dict] = []
    question = None
    candidates = _pending_candidates(conn, character_id, hidden)
    prior_pending = db.get_flag(conn, PENDING_FLAG + str(character_id))

    if prop["unresolved_references"]:
        flagged = next((a for a in actions if a["clarification_needed"]), actions[0] if actions else None)
        question = (flagged or {}).get("clarification_question") or _generic_question(prop["unresolved_references"])
        if _affirmation_reask(prop, prior_pending) and prior_pending.get("question"):
            question = prior_pending["question"]  # re-ask the same sound question
        results.append(turn.clarify(flagged))
        skipped = [_skip(a, "prior_clarify") for a in actions if a is not flagged]
        clarify_action = flagged or {"action_type": None}
        clarify_signature = _clarify_signature("unresolved", clarify_action,
                                               prop["unresolved_references"], candidates)
    else:
        failed: List[Dict] = []
        for i, a in enumerate(actions):
            if question is not None:
                skipped.append(_skip(a, "prior_clarify"))
                continue
            if _depends_on(a, failed):
                skipped.append(_skip(a, "prior_failure"))
                continue
            if i in named_in_dark:
                verb = "talk to" if a["action_type"] == "talk" else "attack"
                question = turn.unseen_name_question(verb)
                results.append(turn.clarify(a))
                clarify_action, clarify_signature = a, _clarify_signature("named", a, raw_targets[i], candidates)
                continue
            if a["clarification_needed"]:
                question = a["clarification_question"] or _generic_question([a["intent_summary"]])
                results.append(turn.clarify(a))
                clarify_action, clarify_signature = a, _clarify_signature("needed", a, raw_targets[i], candidates)
                continue
            if a["action_type"] in ("talk", "attack") and a["target"] is None:
                a, ask = turn.default_npc_target(a, "talk to" if a["action_type"] == "talk" else "attack")
                if ask:
                    question = ask
                    results.append(turn.clarify(a))
                    clarify_action, clarify_signature = a, _clarify_signature("who", a, raw_targets[i], candidates)
                    continue
            r = turn.resolve(a)
            results.append(r)
            if r["status"] == "resolved" and r["outcome"] == "failure":
                failed.append(a)

    npc_actions: List[Dict] = []
    expired: List[Dict] = []
    # Time passes (NPCs act, timers tick) only if some action actually resolved; turns that
    # were all rejected or clarification requests cost nothing.
    resolved = any(r["status"] == "resolved" for r in results)
    time_passed = resolved
    # v0.6 (Lead decision): in combat, a rejected action is a wasted round; hostiles still act.
    # v0.6.1 (Lead): only a parsed-but-rejected action wastes the round; input the engine couldn't map at
    # all (`unrecognized`: typos, nonsense) is free even in combat, like malformed input.
    wasted_round = not time_passed and bool(in_combat) and any(
        r["status"] == "rejected" and r.get("detail") not in FREE_REJECTIONS for r in results)
    is_reask = bool(question) and _affirmation_reask(prop, prior_pending)
    reasks = (int(prior_pending.get("reasks") or 0) + 1) if is_reask else 0
    if question and clarify_action is not None:
        _store_pending(conn, character_id, clarify_action, question, hidden,
                       clarify_signature or "", reasks=reasks)
    if resolved or (wasted_round and not question):
        # a real resolved action (or a parsed rejection that already costs the round) resets the stall
        _reset_stall(conn, character_id)
    elif is_reask:
        # First 'yes' with several candidates re-asks for free and does not consume a stall
        # slot (streak unchanged). A second consecutive yes on the same pending clarify
        # costs the round, so it cannot be used to stall forever.
        if in_combat and reasks >= 2:
            wasted_round = True
    elif not time_passed and not wasted_round:
        # typo / clarify / other free input: maybe cap the stall in combat
        if _maybe_cap_free_turn(conn, character_id, in_combat, question, clarify_signature or "free"):
            wasted_round = True
    time_passed = time_passed or wasted_round
    if time_passed:
        expired = effects.tick_npc_conditions(conn, npc_mark)
        npc_actions = effects.pursuit(conn, character_id, start_loc)
        player_mark = effects.player_condition_mark(conn)
        npc_actions += effects.npc_phase(conn, character_id, rng, turn.turn, skip={f["npc"] for f in npc_actions})
        expired += effects.tick(conn, character_id, player_mark)
    else:
        _undo_turn(conn, character_id)
        turn.turn -= 1
    status = player_status(conn, character_id)
    defeat = death() if status["hp_band"] == "down" else None
    out = {"turn": turn.turn, "results": results, "player_status": status, "dialogue": [],
           "npc_actions": npc_actions, "expired": expired, "defeat": defeat, "clarification_question": question,
           "time_passed": time_passed}
    if skipped:
        out["skipped"] = skipped
    if wasted_round:
        out["wasted_round"] = True
    hidden.update(unseen.unseen_npcs(conn, character_id))  # ...or at its end: never named
    return unseen.mask(out, hidden)


def death() -> Dict:
    return {"type": "death", "description": DEATH_DESCRIPTION}


def _skip(a, reason):
    return {"action_type": a["action_type"], "intent_summary": a["intent_summary"], "reason": reason}


def _generic_question(refs: List[str]) -> str:
    return "Which do you mean: " + ", ".join(f'"{r}"' for r in refs) + "?"

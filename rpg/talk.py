"""Talk resolution (schemas §e-§f, accepted in contracts v0.5) and the voice step (§g).

The engine decides everything: the social DC, the disposition change, which topics may be
revealed, and whether a trade happens. The voice LLM only phrases what `allowed_topics`
already permits, and its lines are never parsed for claims."""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Dict, List, Optional

from . import db, personas, rules, unseen
from . import inventory as inv

log = logging.getLogger("rpg.debug")

APPROACH_SKILL = {"persuade": "persuasion", "deceive": "deception", "intimidate": "intimidation",
                  "insight": "insight", "ask": None, "small_talk": None, "trade": None, "farewell": None,
                  "gift": None}
SKILL_APPROACH = {v: k for k, v in APPROACH_SKILL.items() if v}
NO_SUBJECT = ("small_talk", "farewell")
# disposition deltas (npc_persona_schema.md): approach -> success/failure -> degree
DELTAS = {
    "persuade": {True: {"narrow": 10, "clear": 15, "overwhelming": 25}, False: {"narrow": 0, "clear": -5, "overwhelming": -10}},
    "deceive": {True: {"narrow": 10, "clear": 15, "overwhelming": 20}, False: {"narrow": -10, "clear": -20, "overwhelming": -30}},
    "intimidate": {True: {"narrow": -10, "clear": -10, "overwhelming": -15}, False: {"narrow": -10, "clear": -15, "overwhelming": -25}},
}
TRADE_DELTA = 2
# v0.6.1 gifts (Lead): the only player-driven way past friendly. A persona's `gifts: {template: delta}` sets
# what that NPC values; any other item worth >= GIFT_VALUE_THRESHOLD coins is worth GIFT_DELTA. Items the
# NPC itself trades away are never valued, and each template moves an NPC only once.
GIFT_VALUE_THRESHOLD = 50
GIFT_DELTA = 10
GIFT_FLAG = "gifted:"
MAX_TRADE_QTY = 10  # a trade may be repeated up to this many times in one action (repeatable trades only)


RETRY_TURNS = 10        # a blocked social retry opens up again after this many turns
SOCIAL_CAP = 49         # small gains (social checks, trades) can't lift disposition into allied (>= 50)
SOCIAL_TRY_FLAG = "social_try:"
SOCIAL_LAST_FLAG = "social_last:"  # last subject per NPC + approach (paraphrases fold into it, BUG-R3-02)
DETERMINERS = ("the ", "a ", "an ", "my ", "his ", "her ", "their ", "your ", "its ", "some ")


class TalkReject(Exception):
    """Raised before any state change; actions.do_talk turns it into a validation rejection."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail
RECENT_DIALOGUE = 5
MAX_LINES, MAX_WORDS = 3, 40
LINT_NGRAM = 6


def _clamp(v: int) -> int:
    return max(-100, min(100, v))


def normalize(a: Dict) -> Dict:
    """approach/topic/topic_text with the skill corrected to the approach pairing."""
    approach = a.get("approach") or SKILL_APPROACH.get(a.get("skill"), "ask")
    if approach not in APPROACH_SKILL:
        approach = "ask"
    return {**a, "approach": approach, "skill": APPROACH_SKILL[approach],
            "topic": a.get("topic"), "topic_text": a.get("topic_text")}


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def revealed(conn, npc_id) -> List[str]:
    return [r[0] for r in conn.execute("SELECT topic_key FROM npc_topics_revealed WHERE npc_id = ? ORDER BY id",
                                       (npc_id,))]


def _reveal(conn, npc_id, key, turn):
    conn.execute("INSERT OR IGNORE INTO npc_topics_revealed (npc_id, topic_key, turn) VALUES (?, ?, ?)",
                 (npc_id, key, turn))


def _trade_flag(npc_key, trade_key):
    return f"trade_done:{npc_key}:{trade_key}"


def open_trades(conn, npc_key: str, p: Dict, band: str) -> List[str]:
    """Trades available at the band, minus completed non-repeatable ones."""
    return [k for k in personas.available_trades(p, band)
            if p["trades"][k].get("repeatable") or not db.get_flag(conn, _trade_flag(npc_key, k))]


def done_trades(conn, npc_key: str, p: Dict, band: str) -> List[str]:
    """One-off trades available at the band that were already completed (listed with done: true)."""
    return [k for k in personas.available_trades(p, band)
            if not p["trades"][k].get("repeatable") and db.get_flag(conn, _trade_flag(npc_key, k))]


def recent_dialogue(conn, npc_id, n=RECENT_DIALOGUE) -> List[str]:
    rows = conn.execute("SELECT line FROM npc_dialogue_log WHERE npc_id = ? ORDER BY id DESC LIMIT ?",
                        (npc_id, n)).fetchall()
    return [r[0] for r in reversed(rows)]


def price(entry: Dict, band: str, side: str) -> Dict:
    """Band-priced coin entry. side 'wants' = the player pays (rounded up), 'gives' = the
    player receives (rounded down)."""
    g = entry["gold"]
    if side == "wants":
        return {"gold": max(1, math.ceil(g * personas.BUY_PRICE_MULT[band]))}
    return {"gold": max(1, math.floor(g * personas.SELL_PRICE_MULT[band]))}


def trade_context(conn, p: Dict, key: str, band: str) -> Dict:
    """Prompt-facing trade entry: key, label, optional aliases, band-priced gives/wants."""
    v = trade_view(conn, p, key, band, "offered")
    e = {"key": key, "label": v["label"], "gives": v["gives"], "wants": v["wants"]}
    if p["trades"][key].get("aliases"):
        e["aliases"] = list(p["trades"][key]["aliases"])
    return e


def trade_view(conn, p: Dict, key: str, band: str, status: str) -> Dict:
    tr = p["trades"][key]

    def side(entries, which):
        out = []
        for e in entries or []:
            if "gold" in e:
                out.append(price(e, band, which))
            else:
                t = conn.execute("SELECT name FROM item_templates WHERE key = ?", (e["item"],)).fetchone()
                out.append({"item": e["item"], "name": e.get("name") or (t["name"] if t else e["item"]),
                            "qty": e.get("qty", 1)})
        return out
    return {"key": key, "label": tr["label"], "status": status,
            "gives": side(tr.get("gives"), "gives"), "wants": side(tr.get("wants"), "wants")}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve(turn, a: Dict, t) -> Dict:
    """Resolve a talk action against NPC target `t` (already validated alive and present)."""
    conn = turn.conn
    a = normalize(a)
    npc = t.row
    p = personas.get(conn, npc["id"])
    approach, skill = a["approach"], a["skill"]
    disp = npc["disposition"]
    before = rules.disposition_band(disp)
    if approach == "gift":
        return _gift(turn, a, t, p)
    is_trade = approach == "trade" or bool(p and a["topic"] in (p.get("trades") or {}))
    if is_trade:
        _precheck_trade(turn, a, t, p, before)  # BUG-R2-02/03/04: rejections cost nothing
    check = None
    try_key = None
    known_now = all_revealed(conn)
    if skill:
        # 5e no-retry rule (v0.6/v0.6.1): the same approach on the same NPC and subject can't be rolled
        # again until something changes (see _new_leverage). The subject is a real topic/trade key of
        # this NPC; anything else (a paraphrase, free text) is the same subject as the last attempt.
        last_key = f"{SOCIAL_LAST_FLAG}{t.key}:{approach}"
        subject = a["topic"] if _is_subject(p, a["topic"]) else (db.get_flag(conn, last_key) or "")
        try_key = f"{SOCIAL_TRY_FLAG}{t.key}:{approach}:{subject}"
        prev = db.get_flag(conn, try_key)
        cited = (cited_topics(conn, a["topic_text"]) - {f"{t.key}:{subject}"}) if _is_subject(p, a["topic"]) \
            else set()
        if prev and not _new_leverage(prev, turn.turn, disp, a["items_used"], cited):
            raise TalkReject("impossible_action", "no_new_leverage")
        dc = personas.social_dc(p, skill, before) if p else turn.dc_for(a, t)
        check = turn.check(a, skill, dc)
    success = check.success if check else True
    degree = (check.degree or "clear") if check else None
    delta = DELTAS.get(approach, {}).get(success, {}).get(degree, 0) if check else 0

    consequences, changes = [], []
    talk = {"npc": t.key, "approach": approach, "topic": a["topic"], "topic_text": a["topic_text"],
            "disposition_band_before": before, "disposition_band_after": before,
            "requested_topic_status": "none" if approach in NO_SUBJECT or not (a["topic"] or a["topic_text"])
            else "unknown",
            "allowed_topics": [], "refused_topics": [], "trade": None, "insight_read": None}

    def set_disp(d):
        nonlocal disp
        new = _clamp(disp + d)
        if d > 0:  # v0.6/v0.6.1: every small gain (talk, trades) tops out at friendly; allied = gifts, quests
            new = max(disp, min(new, SOCIAL_CAP))
        if new != disp:
            conn.execute("UPDATE npcs SET disposition = ? WHERE id = ?", (new, npc["id"]))
            disp = new

    set_disp(delta)
    if try_key:
        db.set_flag(conn, try_key, {"turn": turn.turn, "disposition": disp, "items": sorted(a["items_used"]),
                                    "revealed": sorted(known_now)})
        db.set_flag(conn, last_key, subject)
    after = rules.disposition_band(disp)
    # cowed: topics read one band above the band *before* the drop (else intimidation could never help)
    topic_band = personas.shift_band(before, 1) if approach == "intimidate" and success and check else after

    if p:
        topic = a["topic"]
        known = revealed(conn, npc["id"])
        if topic and topic in (p.get("trades") or {}):
            talk["trade"] = _trade(turn, a, t, p, topic, after, changes, consequences, a.get("quantity") or 1)
            talk["requested_topic_status"] = "none"
            if talk["trade"]["status"] == "completed":
                set_disp(TRADE_DELTA)
        elif approach == "trade":
            talk["trade"] = _offer(turn, a, t, p, after)
        elif topic and personas.topic(p, topic) and approach not in NO_SUBJECT:
            if not personas.topic_visible(p, topic, topic_band) and topic not in known:
                talk["requested_topic_status"] = "unknown"  # a hidden secret: don't confirm it exists
            elif topic in known or (success and topic in personas.available_topics(p, topic_band)):
                talk["requested_topic_status"] = "revealed"
                talk["allowed_topics"] = [{"key": topic, "text": personas.topic(p, topic)["text"]}]
                _reveal(conn, npc["id"], topic, turn.turn)
            else:
                talk["requested_topic_status"] = "refused"
                talk["refused_topics"] = [{"key": topic, "label": personas.topic(p, topic)["label"]}]
        if approach == "insight" and success and check and p.get("insight_tell"):
            talk["insight_read"] = p["insight_tell"]

    after = rules.disposition_band(disp)
    talk["disposition_band_after"] = after
    if after != before:
        consequences.append({"type": "npc_disposition_changed", "key": t.key, "to": after})
    log.debug("talk turn=%s npc=%s approach=%s disposition %s -> %s", turn.turn, t.key, approach, npc["disposition"],
              disp)
    r = turn.result(a, target=t, check=check, consequences=consequences, state_changes=changes)
    r["talk"] = talk
    return r


def _gift(turn, a, t, p) -> Dict:
    """approach "gift": hand over the items in `items_used` (held). Valued ones are accepted and removed,
    moving disposition by their worth with **no** friendly cap (clamped to 100); the rest are declined
    and kept. Declines: `own_stock` (the NPC trades that item away), `already_given` (that template
    already moved this NPC), `not_valued` (not in the persona's gifts and worth < the threshold)."""
    conn, cid = turn.conn, turn.cid
    npc = t.row
    if not a["items_used"]:
        raise TalkReject("item_not_owned", "no_item")
    held = {i["key"]: i for i in inv.list_inventory(conn, cid)}
    if any(k not in held for k in a["items_used"]):
        raise TalkReject("item_not_owned", "not_held")
    valued = (p or {}).get("gifts") or {}
    own_stock = {e["item"] for tr in ((p or {}).get("trades") or {}).values()
                 for e in tr.get("gives") or [] if "item" in e}
    disp = npc["disposition"]
    before = rules.disposition_band(disp)
    entries, changes, delta = [], [], 0
    for k in dict.fromkeys(a["items_used"]):
        i = held[k]
        tk = i["template_key"]
        flag = f"{GIFT_FLAG}{t.key}:{tk}"
        reason, d = None, 0
        if tk in own_stock:
            reason = "own_stock"
        elif db.get_flag(conn, flag):
            reason = "already_given"
        elif tk in valued:
            d = valued[tk]
        elif (i.get("value") or 0) >= GIFT_VALUE_THRESHOLD:
            d = GIFT_DELTA
        else:
            reason = "not_valued"
        entry = {"key": k, "name": i["name"], "accepted": reason is None}
        if reason:
            entry["reason"] = reason
        else:
            inv.remove_item(conn, k, 1)
            changes.append({"type": "item_removed", "key": k, "name": i["name"], "given_to": t.key})
            db.set_flag(conn, flag, True)
            delta += d
        entries.append(entry)
    new = _clamp(disp + delta)  # gifts are not capped at friendly
    if new != disp:
        conn.execute("UPDATE npcs SET disposition = ? WHERE id = ?", (new, npc["id"]))
    after = rules.disposition_band(new)
    consequences = [{"type": "npc_disposition_changed", "key": t.key, "to": after}] if after != before else []
    log.debug("gift turn=%s npc=%s disposition %s -> %s", turn.turn, t.key, disp, new)
    r = turn.result(a, target=t, check=None, consequences=consequences, state_changes=changes)
    r["talk"] = {"npc": t.key, "approach": "gift", "topic": None, "topic_text": a["topic_text"],
                 "disposition_band_before": before, "disposition_band_after": after,
                 "requested_topic_status": "none", "allowed_topics": [], "refused_topics": [], "trade": None,
                 "insight_read": None,
                 "gift": {"status": "accepted" if any(e["accepted"] for e in entries) else "declined",
                          "items": entries}}
    return r


def story_disposition(conn, npc_key: str, delta: int) -> int:
    """Engine/quest events (not the player's words): change an NPC's disposition with no friendly cap."""
    row = conn.execute("SELECT id, disposition FROM npcs WHERE key = ?", (npc_key,)).fetchone()
    new = _clamp(row["disposition"] + delta)
    conn.execute("UPDATE npcs SET disposition = ? WHERE id = ?", (new, row["id"]))
    return new


def _new_leverage(prev: Dict, turn_no: int, disposition: int, items_used, cited=()) -> bool:
    """A retry is allowed after RETRY_TURNS turns, if the NPC's disposition changed, if `items_used`
    has an item not used before, or (v0.6.1) if the proposal cites (via `topic_text`, next to the
    subject `topic`) a topic revealed to the player since the last attempt."""
    return (turn_no - prev.get("turn", 0) >= RETRY_TURNS or disposition != prev.get("disposition")
            or bool(set(items_used) - set(prev.get("items") or []))
            or bool(set(cited) - set(prev.get("revealed") or [])))


def _is_subject(p, key) -> bool:
    return bool(key) and bool(p) and (personas.topic(p, key) is not None or key in (p.get("trades") or {}))


def _norm(text) -> str:
    s = " ".join(re.sub(r"[^\w' ]+", " ", str(text or "").lower()).split())
    for d in DETERMINERS:
        if s.startswith(d):
            s = s[len(d):]
    return s


def all_revealed(conn) -> set:
    """Every topic revealed to the player so far, as "<npc_key>:<topic_key>"."""
    return {f"{r[0]}:{r[1]}" for r in conn.execute(
        "SELECT n.key, r.topic_key FROM npc_topics_revealed r JOIN npcs n ON n.id = r.npc_id")}


def cited_topics(conn, text) -> set:
    """Revealed topics (any NPC) that `text` names exactly: the key, the key with spaces, an alias or a
    label segment (split on "/"), ignoring case, punctuation and a leading determiner."""
    want = _norm(text)
    if not want:
        return set()
    out = set()
    for r in conn.execute("SELECT n.key, n.id, r.topic_key FROM npc_topics_revealed r JOIN npcs n ON n.id = r.npc_id"):
        p = personas.get(conn, r["id"])
        tp = personas.topic(p, r["topic_key"]) if p else None
        if not tp:
            continue
        names = [r["topic_key"], r["topic_key"].replace("_", " ")] + list(tp.get("aliases") or []) + \
            [x for x in str(tp.get("label") or "").split("/")]
        if want in {_norm(x) for x in names if x}:
            out.add(f"{r['key']}:{r['topic_key']}")
    return out


def _precheck_trade(turn, a, t, p, band) -> None:
    """Trade requests the engine can reject outright (no time, no dice): a bad quantity, a deal
    the NPC doesn't have (never fall back to another listing), a one-off already done, more
    than one of a one-off, or wanted items the player doesn't hold."""
    q = a.get("quantity")
    if q is not None and (isinstance(q, bool) or not isinstance(q, int) or not 1 <= q <= MAX_TRADE_QTY):
        raise TalkReject("impossible_action", "bad_quantity")
    topic = a["topic"]
    trades = (p or {}).get("trades") or {}
    if topic is None:
        if a["topic_text"]:
            raise TalkReject("impossible_action", "no_such_trade")
        if q not in (None, 1):
            raise TalkReject("impossible_action", "bad_quantity")
        return  # a bare "trade": list what's on offer
    if topic not in trades:
        raise TalkReject("impossible_action", "no_such_trade")
    tr = trades[topic]
    if not tr.get("repeatable") and db.get_flag(turn.conn, _trade_flag(t.key, topic)):
        raise TalkReject("impossible_action", "trade_done")
    if (q or 1) > 1 and not tr.get("repeatable"):
        raise TalkReject("impossible_action", "bad_quantity")
    if topic not in open_trades(turn.conn, t.key, p, band):
        return  # exists but not at this band: a resolved refusal
    for e in tr.get("wants") or []:
        if "item" in e and sum(i["quantity"] for i in _held_of(turn.conn, turn.cid, e["item"])) < \
                e.get("qty", 1) * (q or 1):
            raise TalkReject("item_not_owned", "trade_item_missing")


def _offer(turn, a, t, p, band) -> Optional[Dict]:
    """A bare 'trade': status offered with every open deal in `options` (no deal is picked)."""
    conn = turn.conn
    avail = open_trades(conn, t.key, p, band)
    if not avail:
        return {"key": None, "label": None, "status": "refused", "gives": [], "wants": [], "options": []}
    return {"key": None, "label": None, "status": "offered", "gives": [], "wants": [],
            "options": [trade_context(conn, p, k, band) for k in avail]}


def _held_of(conn, cid, template_key) -> List[Dict]:
    return [i for i in inv.list_inventory(conn, cid) if i["template_key"] == template_key]


def _trade(turn, a, t, p, key, band, changes, consequences, qty: int = 1) -> Dict:
    conn, cid = turn.conn, turn.cid
    if key not in open_trades(conn, t.key, p, band):
        if _label_visible(p, key, band):
            return trade_view(conn, p, key, band, "refused")
        return {"key": key, "label": None, "status": "refused", "gives": [], "wants": []}
    view = trade_view(conn, p, key, band, "unaffordable")
    for e in view["wants"] + view["gives"]:  # repeated deals (repeatable trades only, prechecked)
        if "gold" in e:
            e["gold"] *= qty
        else:
            e["qty"] *= qty
    if qty > 1:
        view["quantity"] = qty
    gold = conn.execute("SELECT gold FROM characters WHERE id = ?", (cid,)).fetchone()[0]
    pay = sum(e["gold"] for e in view["wants"] if "gold" in e)
    if pay > gold:
        return view
    for e in view["wants"]:
        if "item" in e and sum(i["quantity"] for i in _held_of(conn, cid, e["item"])) < e["qty"]:
            return view
    # all checks passed: execute (engine-owned; nothing the LLM said matters)
    for e in view["wants"]:
        if "item" not in e:
            continue
        need = e["qty"]
        for i in _held_of(conn, cid, e["item"]):
            take = min(need, i["quantity"])
            inv.remove_item(conn, i["key"], take)
            changes.append({"type": "item_removed", "key": i["key"], "name": i["name"]})
            need -= take
            if not need:
                break
    receive = sum(e["gold"] for e in view["gives"] if "gold" in e)
    if pay or receive:
        conn.execute("UPDATE characters SET gold = gold - ? + ? WHERE id = ?", (pay, receive, cid))
        consequences.append({"type": "purse_changed", "direction": "spent" if pay > receive else "received"})
    for e in view["gives"]:
        if "item" in e:
            k = inv.next_item_key(conn, e["item"])
            tname = conn.execute("SELECT name FROM item_templates WHERE key = ?", (e["item"],)).fetchone()[0]
            inv.add_item(conn, e["item"], character_id=cid, quantity=e["qty"], key=k,
                         display_name=e["name"] if e["name"] != tname else None)
            changes.append({"type": "item_added", "key": k, "name": e["name"]})
    if not p["trades"][key].get("repeatable"):
        db.set_flag(conn, _trade_flag(t.key, key), True)
    view["status"] = "completed"
    return view


def _label_visible(p, key, band) -> bool:
    """A trade's label may be echoed only if the trade is listed at some band <= the current one
    (or done already); a higher-band trade is reported refused without its label."""
    return key in personas.available_trades(p, band)


# ---------------------------------------------------------------------------
# Voice step (§g)
# ---------------------------------------------------------------------------

def _clean_lines(out) -> Optional[List[str]]:
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except json.JSONDecodeError:
            return None
    if not isinstance(out, dict) or not isinstance(out.get("lines"), list):
        return None
    lines = []
    for ln in out["lines"]:
        if not isinstance(ln, str):
            return None
        ln = ln.strip().strip('"').strip()
        if ln:
            words = ln.split()
            lines.append(" ".join(words[:MAX_WORDS]))
    return lines[:MAX_LINES] or None


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z']+", text.lower())


def leaks(line: str, secrets: List[str], n: int = LINT_NGRAM) -> bool:
    w = _words(line)
    grams = {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}
    for s in secrets:
        sw = _words(s)
        if grams & {tuple(sw[i:i + n]) for i in range(len(sw) - n + 1)}:
            return True
    return False


def voice_input_talk(r: Dict) -> Dict:
    """The talk block as the voice prompt sees it: no insight_read (that's the narrator's)."""
    talk = {k: v for k, v in r["talk"].items() if k not in ("insight_read", "npc")}
    talk.update({"outcome": r["outcome"], "degree": r["degree"]})
    return talk


def voice(conn, llm, result: Dict, player_text: str, context: Optional[Dict] = None) -> Dict:
    """Fill result['dialogue'] for each resolved talk with a persona NPC. Mutates and returns it."""
    dialogue = result.setdefault("dialogue", [])
    for r in result.get("results", []):
        if r.get("action_type") != "talk" or r.get("status") != "resolved" or "talk" not in r:
            continue
        shown_key = r["talk"]["npc"]
        dark = shown_key != unseen.resolve(conn, shown_key)  # v0.7.1: masked as an opaque ref
        npc = conn.execute("SELECT * FROM npcs WHERE key = ?", (unseen.resolve(conn, shown_key),)).fetchone()
        p = npc and personas.get(conn, npc["id"])
        if not p or not hasattr(llm, "voice_npc"):
            continue
        talk = voice_input_talk(r)
        payload = {"player_text": player_text, "talk": talk, "recent_dialogue": recent_dialogue(conn, npc["id"])}
        lines = None
        for attempt in range(2):  # one retry
            try:
                lines = _clean_lines(llm.voice_npc(personas.persona_view(p), talk["allowed_topics"], payload,
                                                   context or {}))
            except Exception as e:  # provider stubs raise; a failed voice is never fatal
                log.debug("voice_npc failed (attempt %s): %s", attempt + 1, e)
                lines = None
            if lines:
                break
        if not lines:
            continue  # dialogue stays []: the narrator falls back to indirect speech
        hidden = [s for s in personas.secret_texts(p)
                  if not any(s == at["text"] for at in talk["allowed_topics"])]
        hidden += [p["insight_tell"]] if p.get("insight_tell") else []
        for ln in lines:
            if leaks(ln, hidden):
                log.debug("voice line dropped by secret lint: %r", ln)
                continue
            if dark:
                dialogue.append({"speaker_key": shown_key, "speaker_name": unseen.VOICE_NAME,
                                 "line": ln.replace(npc["name"], "I"), "source": "npc_llm"})
            else:
                dialogue.append({"speaker_key": npc["key"], "speaker_name": npc["name"], "line": ln,
                                 "source": "npc_llm"})
            conn.execute("INSERT INTO npc_dialogue_log (npc_id, turn, line) VALUES (?, ?, ?)",
                         (npc["id"], result.get("turn"), ln))
    return result

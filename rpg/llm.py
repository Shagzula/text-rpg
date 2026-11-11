"""Pluggable LLM layer: interpreter (player text -> action proposal) and narrator
(engine result -> prose). Stdlib only.

RPG_LLM selects the provider: 'mock' (default, deterministic keyword rules),
'openai', or 'anthropic'. Real providers read their API key from the environment
only (OPENAI_API_KEY / ANTHROPIC_API_KEY); their network calls are not
implemented yet.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Dict, List, Optional, Protocol, Tuple, runtime_checkable

CLOSING_LINE = "What do you do?"
FALLBACK_NARRATION = "The moment passes in a blur, and the details are hard to make out."


class LLMConfigError(RuntimeError):
    """Provider misconfiguration (unknown provider, missing API key)."""


@runtime_checkable
class LLMClient(Protocol):
    def interpret(self, player_text: str, context: Dict, reprompt: Optional[str] = None) -> Dict:
        """Player text + context block -> action proposal dict (contracts.md (a))."""

    def narrate(self, result: Dict, context: Dict) -> str:
        """Engine result (contracts.md (b)) + context block -> second-person prose."""

    def voice_npc(self, npc_persona_view: Dict, allowed_topics: List[Dict], result: Dict, context: Dict) -> Dict:
        """Talk turns only, after the engine resolved: {"lines": [1-3 strings]} in the NPC's
        voice. Sees the public persona and allowed_topics, never secrets (contracts v0.5 §g).
        `result` = {player_text, talk, recent_dialogue}."""


# ---------------------------------------------------------------------------
# Narration post-processing
# ---------------------------------------------------------------------------

def is_malformed(text) -> bool:
    if not isinstance(text, str) or not text.strip():
        return True
    t = text.strip()
    return t[0] in "{[<" or "```" in t or len(t) < 3


def ensure_closing_line(text: str) -> str:
    t = text.rstrip()
    if t.endswith(CLOSING_LINE):
        return t
    return f"{t}\n\n{CLOSING_LINE}"


def strip_closing_line(text: str) -> str:
    t = text.rstrip()
    while t.endswith(CLOSING_LINE):
        t = t[: -len(CLOSING_LINE)].rstrip()
    return t


def finalize_narration(text, regenerate: Optional[Callable[[], str]] = None, *, defeat: bool = False) -> str:
    """Regenerate once if empty/malformed (JSON, markup, fences), then make sure the
    narration ends with the closing line -- unless the turn ended in defeat, where the
    closing line is removed (the CLI follows with the load/new/quit offer)."""
    if is_malformed(text) and regenerate is not None:
        text = regenerate()
    if is_malformed(text):
        text = FALLBACK_NARRATION
    if defeat:
        return strip_closing_line(text.strip()) or FALLBACK_NARRATION
    return ensure_closing_line(text.strip())


def narrate_safely(llm: "LLMClient", result: Dict, context: Dict) -> str:
    def regen():
        try:
            return llm.narrate(result, context)
        except Exception:  # noqa: BLE001 - a broken narrator must not kill the turn
            return ""
    return finalize_narration(regen(), regenerate=regen, defeat=bool(result.get("defeat")))


# ---------------------------------------------------------------------------
# Mock LLM
# ---------------------------------------------------------------------------

LAST_DITCH_PHRASES = ("try hard", "trying hard", "desperately", "with everything i have", "with all my strength",
                      "whatever it takes", "all out")
STOPWORDS = {"the", "a", "an", "my", "at", "to", "into", "on", "up", "with", "of", "some", "this", "that", "its", "it"}
DIRECTIONS = ("north", "south", "east", "west", "up", "down", "in", "out", "northeast", "northwest",
              "southeast", "southwest")
DIRECTION_WORDS = {"n": "north", "s": "south", "e": "east", "w": "west", "u": "up", "d": "down",
                   "ne": "northeast", "nw": "northwest", "se": "southeast", "sw": "southwest",
                   "upstairs": "up", "downstairs": "down", "back": "back", **{d: d for d in DIRECTIONS}}

VERBS: List[Tuple[Tuple[str, ...], str]] = [
    (("i", "inv", "inventory"), "inventory"),
    (("rest", "sleep"), "rest"),
    (("wait", "z"), "wait"),
    (("look", "l", "examine", "x", "inspect", "search", "feel", "grope", "rummage"), "examine"),
    (("go", "move", "walk", "enter", "climb", "head", "run"), "move"),
    (("take", "get", "grab", "pick up", "pickup"), "take"),
    (("drop", "leave"), "drop"),
    (("equip", "wield", "wear", "ready", "draw"), "equip"),
    (("unequip", "remove", "sheathe", "unwield"), "unequip"),
    (("attack", "hit", "stab", "strike", "kill", "punch", "slash", "fight"), "attack"),
    (("use", "drink", "pull", "push", "apply", "light", "eat", "throw", "toss", "blow"), "use_item"),
    (("pick", "unlock", "lockpick"), "pick"),
    (("open", "force", "kick", "break", "smash", "bash", "shove"), "force"),
    (("talk", "speak", "ask", "greet", "chat", "say", "hello", "hi", "persuade", "convince", "plead", "beg",
      "lie", "deceive", "bluff", "trick", "threaten", "intimidate", "menace", "read", "study", "sense",
      "buy", "purchase", "sell", "trade", "barter", "haggle", "bye", "goodbye", "farewell", "give", "gift",
      "hand"), "talk"),
]
TALK_APPROACH_WORDS = {
    "persuade": "persuade", "convince": "persuade", "plead": "persuade", "beg": "persuade",
    "lie": "deceive", "deceive": "deceive", "bluff": "deceive", "trick": "deceive",
    "threaten": "intimidate", "intimidate": "intimidate", "menace": "intimidate",
    "read": "insight", "study": "insight", "sense": "insight",
    "buy": "trade", "purchase": "trade", "sell": "trade", "trade": "trade", "barter": "trade", "haggle": "trade",
    "bye": "farewell", "goodbye": "farewell", "farewell": "farewell",
    "give": "gift", "gift": "gift", "hand": "gift",
    "greet": "small_talk", "hello": "small_talk", "hi": "small_talk", "chat": "small_talk",
}
TALK_SKILL = {"persuade": "persuasion", "deceive": "deception", "intimidate": "intimidation", "insight": "insight"}
TALK_SUMMARY = {"ask": "Ask the {npc}{about}.", "persuade": "Try to persuade the {npc}{about}.",
                "deceive": "Try to deceive the {npc}{about}.", "intimidate": "Try to intimidate the {npc}{about}.",
                "insight": "Study the {npc} for what they are hiding{about}.", "small_talk": "Chat with the {npc}.",
                "trade": "Trade with the {npc}{about}.", "farewell": "Say goodbye to the {npc}.",
                "gift": "Give the {npc}{about}."}
# v0.6.1 leverage in social talk: "..., now that I know about X" cites a topic (topic_text next to the topic);
# "... with / offering / showing her the X" presents a held item (items_used).
CITE_RE = re.compile(r"\s*,?\s*\b(?:now that i know about|since i know about|now i know about|citing|mentioning|"
                     r"pointing out|because of what i learned about|given what i know about)\s+(.+)$")
OFFER_RE = re.compile(r"\s+(?:with|offering|showing (?:her|him|them) |showing|holding out)\s+(.+)$")
SUBJECT_SPLITS = (" about ", " regarding ", " for ", " into ", " that ", " to tell me about ", " to tell me ")
SEARCH_WORDS = ("search", "rummage")  # an investigation check
TOUCH_WORDS = ("feel", "grope")        # the touch path: no roll
ROOM_WORDS = ("", "room", "area", "around", "here", "the room", "the area", "around the room", "around room",
              "place", "around here", "in the dark", "the dark", "dark")
KEY_PREFIXES = {"item", "npc", "loc", "pc"}  # slug prefixes are not words to match on (BUG-14)
OOC_WORDS = {"quit": "quit", "exit": "quit", "save": "save", "load": "load", "help": "help",
             "stats": "stats", "undo": "undo"}


def _slug(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS]
    return "_".join(words) or "thing"


def _strip_articles(text: str) -> str:
    words = text.split()
    while words and words[0] in ("to", "at", "the", "a", "an", "my", "up", "through", "into", "over"):
        words = words[1:]
    return " ".join(words)


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS}


class MockLLM:
    """Deterministic keyword interpreter and templated narrator for offline play and tests."""

    name = "mock"

    # -- interpreter ----------------------------------------------------------
    def interpret(self, player_text: str, context: Dict, reprompt: Optional[str] = None) -> Dict:
        # drop emoji and other symbols (BUG-13); keep letters, digits and light punctuation
        text = " ".join(re.sub(r"[^\w\s'.,!?;-]", " ", player_text).strip().lower().split())
        last_ditch = False
        for p in LAST_DITCH_PHRASES:
            if p in text:
                last_ditch = True
                text = " ".join(text.replace(p, " ").split())
        while text.split(" ", 1)[0] in ("to", "and", "i", "then") and " " in text:
            text = text.split(" ", 1)[1]
        if text in OOC_WORDS:
            return {"actions": [], "ooc": {"command": OOC_WORDS[text], "text": player_text}, "unresolved_references": []}
        verb, rest = self._split_verb(text)
        if verb is None and text in DIRECTION_WORDS:
            verb, rest = "move", text
        direction = None
        if verb == "move":
            raw = text.split()[1:] if text not in DIRECTION_WORDS else [text]
            direction = next((DIRECTION_WORDS[w] for w in raw if w in DIRECTION_WORDS and w not in ("in", "out")), None)
            if direction is None and raw and raw[0] in ("in", "out") and len(raw) == 1:
                direction = raw[0]
        action = {"action_type": "other", "skill": None, "target": None, "items_used": [], "last_ditch": last_ditch,
                  "est_difficulty": "medium", "intent_summary": f"Try to {text[:80].rstrip('.')}.",
                  "clarification_needed": False, "clarification_question": None}
        refs: List[str] = []
        if verb in ("inventory", "rest", "wait"):
            action.update(action_type=verb, est_difficulty="trivial",
                          intent_summary={"inventory": "Check what you are carrying.", "rest": "Take a short rest.",
                                          "wait": "Wait and watch."}[verb])
        elif verb == "examine":
            action.update(action_type="examine", est_difficulty="trivial")
            if text.split()[0] in SEARCH_WORDS:  # BUG-R08: searching is an investigation check
                action.update(skill="investigation", est_difficulty="medium")
            touch = text.split()[0] in TOUCH_WORDS
            rest = re.sub(r"^(for|at|along|over)\s+", "", rest)
            rest = _strip_articles(rest)  # 'feel for the lever' -> 'lever' (BUG-R2-05)
            if rest in ROOM_WORDS:
                rest = ""
            if rest:
                key, amb = self._match(rest, context, ("npc", "item", "inv", "exit", "loc"), "item")
                self._apply_target(action, key, amb, rest, refs)
                action["intent_summary"] = f"Feel for the {rest}." if touch else f"Examine the {rest}."
            else:
                action["target"] = (context.get("location") or {}).get("key")
                action["intent_summary"] = ("Search the area." if action["skill"] else
                                            "Feel around." if touch else "Look around.")
        elif verb == "move":
            exits = (context.get("location") or {}).get("exits", [])
            if direction:
                # a direction: the matching exit's destination if listed, else let the engine say "no way"
                match = next((e["to"] for e in exits if e["direction"] == direction), None)
                action["target"] = match or direction
                summary = "Go back." if direction == "back" else f"Go {direction}."
            else:
                key, amb = self._match(rest, context, ("exit",), "loc")
                self._apply_target(action, key, amb, rest, refs)
                summary = f"Go to the {rest}."
            action.update(action_type="move", est_difficulty="trivial", intent_summary=summary)
        elif verb in ("take", "drop", "equip", "unequip"):
            pools = {"take": ("item",), "drop": ("inv",), "equip": ("inv",), "unequip": ("inv",)}[verb]
            key, amb = self._match(rest, context, pools, "item")
            if amb:
                self._apply_target(action, None, amb, rest, refs)
            else:
                action["items_used"] = [key]
            action.update(action_type=verb, est_difficulty="trivial", intent_summary=f"{verb.capitalize()} the {rest}.")
        elif verb == "attack":
            target_txt, _, weapon_txt = rest.partition(" with ")
            key, amb = self._match(target_txt, context, ("npc", "item"), "npc")
            self._apply_target(action, key, amb, target_txt, refs)
            if weapon_txt:
                wkey, _ = self._match(weapon_txt, context, ("inv", "item"), "item")
                action["items_used"] = [wkey]
            else:
                action["items_used"] = [i["key"] for i in context.get("player_inventory", [])
                                        if i.get("equipped") and i.get("slot") in ("main_hand", "two_hands")][:1]
            action.update(action_type="attack", skill="str", intent_summary=f"Attack the {rest}.")
        elif verb == "use_item":
            item_txt, _, on_txt = rest.partition(" on ")
            if not on_txt and " at " in rest:
                item_txt = rest.partition(" at ")[0]
            key, amb = self._match(item_txt, context, ("inv", "item"), "item")
            if amb:
                self._apply_target(action, None, amb, item_txt, refs)
            else:
                action["items_used"] = [key]
            on_txt = on_txt or (rest.partition(" at ")[2])  # "throw powder at guard"
            if on_txt:
                tkey, tamb = self._door_target(on_txt, context, ("exit", "npc", "item"))
                self._apply_target(action, tkey, tamb, on_txt, refs)
                if any(e["to"] == tkey for e in (context.get("location") or {}).get("exits", [])):
                    # lockpicks pick, anything else pries (the engine decides from the tool's stats)
                    action["skill"] = "sleight_of_hand" if "lock" in item_txt else "athletics"
            elif key in {i["key"] for i in context.get("visible_items", []) + context.get("felt_items", [])}:
                action["target"] = key  # a fixture or scene object
            else:
                action["target"] = "self"
            action.update(action_type="use_item", est_difficulty="trivial", intent_summary=f"Use the {rest}.")
        elif verb in ("pick", "force"):
            for junk in ("the lock on ", "lock on ", "the lock of ", "lock of ", "open "):
                if rest.startswith(junk):
                    rest = _strip_articles(rest[len(junk):])
            key, amb = self._door_target(rest or "door", context, ("exit", "item"))
            exits = (context.get("location") or {}).get("exits", [])
            if not rest or rest in ("lock", "door", "the lock", "the door"):
                rest = next((e.get("description") or e["to_name"] for e in exits if e["to"] == key), rest)
            self._apply_target(action, key, amb, rest or "door", refs)
            skill = "sleight_of_hand" if verb == "pick" else "athletics"
            action.update(action_type="skill_check", skill=skill,
                          intent_summary=f"{'Pick the lock on' if verb == 'pick' else 'Force open'} the {rest or 'door'}.")
        elif verb == "talk":
            self._talk(text, rest, context, action, refs)
        if last_ditch:
            action["intent_summary"] = action["intent_summary"].rstrip(".") + ", desperately, whatever it costs."
        return {"actions": [action], "ooc": None, "unresolved_references": refs}

    def _talk(self, text: str, rest: str, context: Dict, action: Dict, refs: List[str]) -> None:
        """talk/ask/persuade/lie/threaten/read/buy/sell/bye ... -> approach, topic, topic_text."""
        first = text.split()[0]
        approach = TALK_APPROACH_WORDS.get(first, "ask")
        npcs = [n for n in context.get("npcs_present", []) if n.get("status") == "alive"]
        rest = re.sub(r"^(to|with|at)\s+", "", rest)
        npc_txt, subject = rest, ""
        if approach == "gift":
            # "give the rations to mara" / "give mara the rations" / "hand mara the rations as a gift"
            rest = re.sub(r"\s+as an? (gift|present|token)$", "", rest)
            if " to " in f" {rest} ":
                subject, _, npc_txt = rest.rpartition(" to ")
            else:
                npc_txt, subject = rest, ""
                for n in context.get("npcs_present", []):
                    for nm in sorted({n["name"].lower(), n["name"].lower().split()[-1]}, key=len, reverse=True):
                        for pre in ("the ", ""):
                            if rest.startswith(pre + nm + " "):
                                npc_txt, subject = pre + nm, rest[len(pre + nm):].strip()
                                break
                        if subject:
                            break
                    if subject:
                        break
        elif approach == "trade" and first in ("buy", "purchase", "sell"):
            subject, _, npc_txt = rest.partition(" from ") if " from " in rest else rest.partition(" to ")
        else:
            for sep in SUBJECT_SPLITS:
                if sep in f" {rest} ":
                    npc_txt, _, subject = f" {rest} ".partition(sep)
                    npc_txt, subject = npc_txt.strip(), subject.strip()
                    break
        # A named person who isn't here stays a best-guess npc_ slug, so the engine rejects it
        # (target_not_present) instead of the talk landing on whoever is present (BUG-R2-01).
        key, amb = (self._match(npc_txt, context, ("npc",), "npc") if npc_txt else (None, []))
        if not npc_txt and len(npcs) == 1:
            key = npcs[0]["key"]
        self._apply_target(action, key, amb, npc_txt or "them", refs)
        npc = next((n for n in context.get("npcs_present", []) if n["key"] == action["target"]), None)
        quantity = None
        if approach == "trade" and subject:
            quantity, subject = _split_quantity(subject)
        cite, offered = None, []
        if approach == "gift":
            ikey, iamb = self._match(subject, context, ("inv",), "item") if subject else (None, [])
            if iamb:
                refs.append(subject)
            action.update(action_type="talk", skill=None, est_difficulty="medium", approach="gift", topic=None,
                          topic_text=None, items_used=[ikey] if ikey else [])
            nm = npc["name"] if npc else (npc_txt or "them")
            action["intent_summary"] = (f"Give the {subject} to " + (nm if " " not in nm else f"the {nm}") + ".")
            return
        if approach in ("persuade", "deceive", "intimidate", "insight") and subject:
            m = CITE_RE.search(subject)
            if m:
                cite, subject = m.group(1).strip(" .!?"), subject[:m.start()].strip()
            m = OFFER_RE.search(subject)
            if m:
                ikey, iamb = self._match(m.group(1), context, ("inv",), "item")
                if ikey and not iamb and any(i["key"] == ikey for i in context.get("player_inventory", [])):
                    offered, subject = [ikey], subject[:m.start()].strip()
        topic, topic_text = None, (_strip_articles(subject) or None) if subject else None
        if npc and subject:
            # BUG-R2-06: only exact key / label / alias (or traded item name) matches, no loose overlap
            if approach == "trade":
                pool = npc.get("trades", [])
                if first == "sell":
                    pool = [t for t in pool if any("gold" in e for e in t.get("gives", []))]
                elif first in ("buy", "purchase"):
                    pool = [t for t in pool if any("item" in e for e in t.get("gives", []))]
            else:
                pool = npc.get("topics", [])
            hit = _topic_match(subject, pool, sell=first == "sell")
            if hit:
                topic, topic_text = hit, None
        if approach in ("small_talk", "farewell"):
            topic, topic_text = None, None
        if cite and topic:  # the subject is a real topic: the cited phrase rides along as leverage
            topic_text = cite
        if offered:
            action["items_used"] = offered
        name = npc["name"] if npc else (npc_txt or "them")
        about = f" about {subject if cite else (topic_text or subject)}" if (topic_text or subject) and \
            approach != "trade" else \
            (f" for {subject}" if subject else "")
        if quantity is not None:
            action["quantity"] = quantity
        action.update(action_type="talk", skill=TALK_SKILL.get(approach), est_difficulty="medium",
                      approach=approach, topic=topic, topic_text=topic_text,
                      intent_summary=TALK_SUMMARY[approach].replace("the {npc}", "{npc}" if " " not in name
                                                                    else "the {npc}").format(npc=name, about=about[:60]))

    def _door_target(self, phrase: str, context: Dict, pools):
        """Match a door-ish phrase; "the door"/"the lock" prefers the one locked exit, then
        the one lockable exit (BUG-06/12)."""
        key, amb = self._match(phrase, context, pools, "loc")
        exits = (context.get("location") or {}).get("exits", [])
        for pick in ({e["to"] for e in exits if e["state"] == "locked"},
                     {e["to"] for e in exits if e.get("lockable")}):
            if amb and len(set(amb) & pick) == 1:
                return (set(amb) & pick).pop(), []
            known = {k for k, _ in self._candidates(context, pools)}
            if not amb and key not in known and len(pick) == 1 and _words(phrase) <= {"lock", "door", "it"}:
                return next(iter(pick)), []
        return key, amb

    @staticmethod
    def _split_verb(text: str):
        for words, verb in VERBS:
            for w in sorted(words, key=len, reverse=True):
                if text == w or text.startswith(w + " "):
                    rest = text[len(w):].strip()
                    return verb, _strip_articles(rest)
        return None, text

    @staticmethod
    def _candidates(context: Dict, pools) -> List[Tuple[str, str]]:
        out = []
        loc = context.get("location") or {}
        if "npc" in pools:
            out += [(n["key"], n["name"]) for n in context.get("npcs_present", [])]
        if "item" in pools:
            out += [(i["key"], i["name"]) for i in context.get("visible_items", []) + context.get("felt_items", [])]
        if "inv" in pools:
            out += [(i["key"], i["name"]) for i in context.get("player_inventory", [])]
        if "exit" in pools:
            out += [(e["to"], f"{e['direction']} {e['to_name']} {e.get('description') or ''}") for e in loc.get("exits", [])]
        if "loc" in pools and loc:
            out.append((loc["key"], loc["name"]))
        return out

    def _match(self, phrase: str, context: Dict, pools, prefix: str):
        """Return (key, ambiguous_candidates). Unknown things get a best-guess slug."""
        want = _words(phrase)
        if not want:
            return None, []
        scored = []
        for key, name in self._candidates(context, pools):
            score = len(want & (_words(name) | (_words(key.replace("_", " ")) - KEY_PREFIXES)))
            if score:
                scored.append((score, key))
        if not scored:
            return f"{prefix}_{_slug(phrase)}", []
        best = max(s for s, _ in scored)
        top = sorted({k for s, k in scored if s == best})
        if len(top) > 1:
            return None, top
        return top[0], []

    @staticmethod
    def _apply_target(action, key, ambiguous, phrase, refs):
        if ambiguous:
            refs.append(phrase)
            action["clarification_needed"] = True
            noun = (re.findall(r"[a-z0-9]+", phrase.lower()) or ["one"])[-1]
            action["clarification_question"] = f"Which {noun} do you mean?"
        else:
            action["target"] = key

    # -- NPC voice (deterministic) ----------------------------------------------
    BAND_OPENER = {"hostile": "Get away from me.", "unfriendly": "Make it quick.", "neutral": "",
                   "friendly": "", "allied": ""}
    TRADE_LINE = {"offered": "I can do {label}, if you can pay for it.", "completed": "Done. Pleasure doing business.",
                  "refused": "Not trading that with you.", "unaffordable": "You can't cover that. Come back when you can."}

    def voice_npc(self, npc_persona_view: Dict, allowed_topics: List[Dict], result: Dict, context: Dict) -> Dict:
        talk = result["talk"]
        phrases = npc_persona_view["voice"]["sample_phrases"] or ["Hm."]
        band = talk.get("disposition_band_after") or "neutral"
        lines = [self.BAND_OPENER.get(band, "")]
        status, approach = talk.get("requested_topic_status"), talk.get("approach")
        if allowed_topics:
            lines += [t["text"] for t in allowed_topics]
        elif talk.get("trade"):
            tr = talk["trade"]
            if tr.get("options"):
                lines.append("I've got " + _join([o["label"] for o in tr["options"]]) + ". What'll it be?")
            else:
                lines.append(self.TRADE_LINE[tr["status"]].format(label=tr.get("label") or "that"))
        elif talk.get("gift"):
            lines.append("For me? That's... kind of you." if talk["gift"]["status"] == "accepted"
                         else "Keep it. I've no use for that.")
        elif approach == "farewell":
            lines.append("Off with you, then.")
        elif approach == "small_talk":
            lines.append(phrases[0])
        elif status == "refused":
            lines.append("That's not something I'll talk about.")
        elif status == "unknown":
            lines.append("Can't help you there.")
        elif talk.get("outcome") == "failure":
            lines.append("I don't think so.")
        else:
            lines.append(phrases[-1])
        return {"lines": [l for l in lines if l][:3]}

    # -- narrator -------------------------------------------------------------
    def narrate(self, result: Dict, context: Dict) -> str:
        if result.get("opening"):
            return self._describe_scene(context)
        lines = []
        if result.get("clarification_question"):
            lines.append(result["clarification_question"])
        self._dialogue = list(result.get("dialogue") or [])
        for r in result.get("results", []):
            if r["outcome"] == "clarify":
                continue
            lines.append(self._narrate_one(r, context))
        if result.get("wasted_round"):
            lines.append("You lose precious moments, and your foe doesn't wait.")
        lines += [self._npc_action_text(a) for a in result.get("npc_actions", [])]
        lines += [self._expired_text(e) for e in result.get("expired", [])]
        if result.get("defeat"):
            lines.append(result["defeat"].get("description") or "Everything goes dark.")
            return "\n".join(l for l in lines if l)  # no closing line: the run is over
        return "\n".join(l for l in lines if l) + f"\n\n{CLOSING_LINE}"

    HURT_TEXT = {"unhurt": "", "scratched": "It stings.", "wounded": "You're bleeding now.",
                 "badly_wounded": "You're badly hurt.", "near_death": "You can barely stay on your feet.",
                 "down": "You crumple to the ground."}

    def _npc_action_text(self, a: Dict) -> str:
        name = a.get("name") or "foe"
        if a.get("action") == "follow":
            return f"The {name} comes after you!"
        if a.get("action") == "none":
            return f"The {name} flails about, {a.get('reason') or 'helpless'}, and can't find you."
        if a.get("outcome") == "success":
            how = {"narrow": " a glancing blow", "clear": "", "overwhelming": " a brutal blow"}.get(a.get("degree"), "")
            lead = f"The {name} lands{how} on you." if how else f"The {name} hits you."
            daze = {"applied": " Your head rings; you're dazed.",
                    "negated": f" Your {(a.get('negated_by') or {}).get('name', 'gear')} takes the worst of it."
                    }.get(a.get("dazed"), "")
            return f"{lead} {self.HURT_TEXT.get(a.get('hp_band'), '')}".strip() + daze
        return f"The {name} attacks, but you avoid the blow."

    @staticmethod
    def _expired_text(e: Dict) -> str:
        t = e.get("type")
        if t == "condition_expired":
            return f"The feeling of being {e.get('name')} fades."
        if t == "npc_condition_expired":
            return f"The {e.get('npc_name')} is no longer {e.get('name')}."
        if t == "light_burnt_out":
            return f"Your {e.get('name')} gutters and goes out."
        return ""

    def _describe_scene(self, context: Dict, felt_line: bool = True) -> str:
        loc = context.get("location") or {}
        parts = [f"You are in the {loc.get('name', 'unknown place')}. {loc.get('description', '')}".strip()]
        alive = [n for n in context.get("npcs_present", []) if n.get("status") == "alive"]
        seen = [n["name"] for n in alive if n.get("visible", True)]
        heard = [n["name"] for n in alive if not n.get("visible", True)]
        if seen:
            parts.append("You see " + _join(seen) + ".")
        if heard:  # in the dark: you can hear them, not see them
            parts.append("Close by in the dark you hear " + _join(heard) + ".")
        items = [i["name"] for i in context.get("visible_items", [])]
        if items:
            parts.append("Nearby: " + _join(items) + ".")
        felt = [f"the {i['name']}" for i in context.get("felt_items", [])]
        if felt and felt_line:
            parts.append("By touch you know of " + _join(felt) + ".")
        exits = [f"{e['direction']} ({e.get('description') or e['to_name']}{', locked' if e['state'] == 'locked' else ''})"
                 for e in loc.get("exits", [])]
        if exits:
            parts.append("Ways out: " + _join(exits) + ".")
        return " ".join(parts) + f"\n\n{CLOSING_LINE}"

    REJECT_TEXT = {
        "item_not_owned": "You reach for it, but you don't have that.",
        "target_not_present": "You look around, but that isn't here.",
        "item_unusable": "That won't work the way you want.",
        "out_of_reach": "It's out of your reach.",
        "impossible_action": "That simply isn't something you can do.",
    }
    DETAIL_TEXT = {
        "exit_locked": "The way is locked tight.",
        "fixture": "It's fixed in place; it won't come loose.",
        "already_held": "You already have it.",
        "enemies_present": "Not with enemies this close.",
        "hostile_nearby": "Not with an enemy just next door; you'd never get a moment's peace.",
        "target_defeated": "There's no fight left in them.",
        "lacks_ability": "You have no such power.",
        "unrecognized": "Nothing happens. That isn't something you can do here.",
        "already_here": "You're already here.",
        "too_dark": "It's too dark to make anything out.",
        "needs_tool": "You'd need the right tool for that.",
        "no_exit": "There's no way to go in that direction.",
        "no_way_back": "There's nowhere to go back to.",
        "already_open": "It's already open.",
        "already_full": "You're already in full health; better to save it.",
        "no_target": "There's no one here to talk to.",
        "needs_target": "Use it on what? On its own, it does nothing.",
        "no_effect": "Nothing happens.",
        "no_item": "You'd need to name something you're carrying.",
        "no_new_leverage": "You've already tried that on them; without something new, it won't go differently.",
        "no_such_trade": "There's no such deal on offer here.",
        "trade_done": "That deal is already done; they won't make it again.",
        "trade_item_missing": "You don't have what that deal needs.",
        "bad_quantity": "That isn't a number of things anyone can trade.",
        "light_not_held": "You'd need to be holding it.",
    }
    DEGREE_WORD = {"narrow": "Just barely, ", "clear": "", "overwhelming": "Easily, ", None: ""}
    FAIL_WORD = {"narrow": "You come close, but ", "clear": "", "overwhelming": "It goes badly: ", None: ""}

    def _narrate_one(self, r: Dict, context: Dict) -> str:
        tname = (r.get("target") or {}).get("name") or "it"
        if r["status"] == "rejected" and r["reason"] == "target_not_present" and \
                str((r.get("target") or {}).get("key", "")).startswith("npc_") and r.get("detail") != "no_target":
            who = (r.get("target") or {}).get("name")
            return f"{who} isn't here." if who else "There's nobody like that here."
        if r["status"] == "rejected":
            return self.DETAIL_TEXT.get(r.get("detail"), self.REJECT_TEXT.get(r["reason"], "That doesn't work."))
        out = []
        at, ok = r["action_type"], r["outcome"] == "success"
        if r["reason"] in ("impossible", "impossible_even_last_ditch"):
            out.append("You throw yourself at it, but it is far beyond you." if r["last_ditch"]
                       else "You size it up and know at once it's beyond you.")
        elif at == "move" and ok:
            moved = [c for c in r["state_changes"] if c["type"] == "moved_to_location"]
            out.append(f"{self.DEGREE_WORD[r['degree']]}you make your way to the {moved[0]['name']}." if moved
                       else "You move on.")
            if moved and moved[0].get("dark"):  # BUG-R02: say it's dark, and how to get out
                out.append(self._describe_scene(context).replace(f"\n\n{CLOSING_LINE}", "")
                           .replace(f"You are in the {(context.get('location') or {}).get('name')}. ", ""))
        elif at == "examine" and ok:
            target = r.get("target") or {}
            if not target or target.get("key") == (context.get("location") or {}).get("key"):
                out.append(self._describe_scene(context, felt_line="felt" not in r)
                           .replace(f"\n\n{CLOSING_LINE}", ""))  # the result's own `felt` is said below
            else:
                desc = target.get("description")
                if desc and desc.startswith("You find it by touch"):
                    out.append(desc)
                else:
                    out.append(f"You study the {tname}." + (f" {desc}" if desc else " Nothing about it stands out."))
        elif at == "inventory":
            inv = [i["name"] + (" (equipped)" if i["equipped"] else "") for i in context.get("player_inventory", [])]
            out.append("You are carrying " + _join(inv) + "." if inv else "You are carrying nothing.")
        elif at == "rest":
            out.append("You rest for a while, catching your breath.")
        elif at == "wait":
            out.append("You wait. Time passes.")
        elif at == "attack":
            out.append(f"{self.DEGREE_WORD[r['degree']]}your attack lands on the {tname}." if ok
                       else f"{self.FAIL_WORD[r['degree']]}the {tname} avoids your attack.")
        elif at == "talk" and "talk" in r:
            out.append(self._talk_text(r, tname))
        elif ok and (r["state_changes"] or r["consequences"]) and at in ("take", "drop", "equip", "unequip", "use_item"):
            pass  # the changes below say what happened
        elif ok:
            out.append(f"{self.DEGREE_WORD[r['degree']]}you manage it: {r['intent_summary'][0].lower()}{r['intent_summary'][1:]}")
        else:
            out.append(f"{self.FAIL_WORD[r['degree']]}you fail to {r['intent_summary'][0].lower()}{r['intent_summary'][1:]}")
        if r.get("felt"):
            out.append("By touch you find " + _join([f"the {f['name']}" for f in r["felt"]]) + ".")
        elif "felt" in r:
            out.append("Your hands find nothing but stone and damp.")
        out += [self._change_text(c) for c in r["state_changes"]]
        out += [self._consequence_text(c, context) for c in r["consequences"]]
        text = " ".join(x for x in out if x)
        return text[0].upper() + text[1:] if text else text

    APPROACH_OPEN = {"ask": "You put your question to the {n}.", "persuade": "You make your case to the {n}.",
                     "deceive": "You spin the {n} a story.", "intimidate": "You lean on the {n}.",
                     "insight": "You watch the {n} closely.", "small_talk": "You pass the time with the {n}.",
                     "trade": "You talk trade with the {n}.", "farewell": "You take your leave of the {n}.",
                     "gift": "You hold out a gift to the {n}."}
    BAND_BODY = {"hostile": "They glare at you.", "unfriendly": "They eye you coldly.", "neutral": "",
                 "friendly": "They seem to warm to you.", "allied": "They lean in, on your side now."}

    def _talk_text(self, r: Dict, tname: str) -> str:
        t = r["talk"]
        the = "" if " " not in tname else "the "  # one-word names ("Mara") read without an article
        out = [self.APPROACH_OPEN.get(t["approach"], "You speak with the {n}.").replace("the {n}", the + "{n}")
               .format(n=tname)]
        if r["degree"] is not None or r["reason"] in ("auto_success",):
            x = (f"{self.DEGREE_WORD[r['degree']]}it lands." if r["outcome"] == "success"
                 else f"{self.FAIL_WORD[r['degree']]}it doesn't land.")
            out.append(x[0].upper() + x[1:])
        spoken = [d for d in getattr(self, "_dialogue", []) if d["speaker_key"] == t["npc"]]
        for d in spoken:  # verbatim, in order
            out.append(f'"{d["line"]}" {the}{d["speaker_name"]} says.')
        self._dialogue = [d for d in getattr(self, "_dialogue", []) if d["speaker_key"] != t["npc"]]
        if not spoken:
            for a in t["allowed_topics"]:  # voice failed: brief indirect speech, topic text only
                txt = a["text"].rstrip(".")
                out.append(f"{(the + tname)[0].upper()}{(the + tname)[1:]} tells you that {txt[0].lower()}{txt[1:]}.")
            if not t["allowed_topics"] and t["refused_topics"]:
                out.append(f"{(the + tname)[0].upper()}{(the + tname)[1:]} won't be drawn on that.")
            if t.get("trade") and not t["allowed_topics"]:
                out.append({"offered": "They tell you what they have on offer." if t["trade"].get("options") is not None
                            else "They name their terms.", "completed": "The deal is struck.",
                            "refused": "They shake their head.", "unaffordable": "You can't cover their price."}
                           [t["trade"]["status"]])
        if t.get("gift"):
            for e in t["gift"]["items"]:
                if not e["accepted"]:
                    out.append({"own_stock": f"They push the {e['name']} back; they've plenty of those.",
                                "already_given": f"They wave off the {e['name']}; you've given them one already.",
                                }.get(e.get("reason"), f"They glance at the {e['name']} and hand it back."))
        if t.get("insight_read"):
            out.append(t["insight_read"])
        if t["disposition_band_after"] != t["disposition_band_before"]:
            out.append(self.BAND_BODY.get(t["disposition_band_after"], ""))
        return " ".join(x for x in out if x)

    @staticmethod
    def _change_text(c: Dict) -> str:
        return {
            "exit_state": "The way opens.",
            "item_added": f"You take the {c.get('name')}.",
            "item_removed": (f"You hand over the {c.get('name')}." if c.get("given_to")
                             else f"You set down the {c.get('name')}." if c.get("to")
                             else f"Nothing is left of the {c.get('name')}." if c.get("from") == "scene"
                             else f"The {c.get('name')} is gone from your pack."),
            "item_equipped": f"You ready the {c.get('name')}.",
            "item_unequipped": f"You put away the {c.get('name')}.",
            "npc_defeated": f"The {c.get('name')} goes down and stays down.",
            "entity_state": {"lit": f"The {c.get('name') or 'light'} flares to life.",
                             "unlit": f"You put out the {c.get('name') or 'light'}.",
                             "open": "It comes open."}.get(c.get("to"), "It takes the blow."),
            "flag_set": "" if str(c.get("key", "")).startswith(("visited:", "defeated:")) else "Somewhere, something clunks into place.",
        }.get(c["type"], "")

    @staticmethod
    def _consequence_text(c: Dict, context: Dict) -> str:
        t = c["type"]
        if t == "condition" and c.get("negated_by"):  # v0.7 perk
            return f"Your {c['negated_by']['name']} takes the worst of it; you shake it off."
        if t == "condition" and c.get("severity") == 0 and not c.get("lasting"):  # a buff
            return f"You feel {c.get('name')}."
        if t == "condition":
            d = c.get("description") or "You are hurt"
            return ("You " + d[0].lower() + d[1:] if not d.lower().startswith("you") else d) + "."
        if t == "healed":
            return "Warmth spreads through you; you feel better."
        if t == "damage_dealt":
            return {"scratched": "It's only a scratch.", "wounded": "It's a real wound.",
                    "badly_wounded": "They stagger, badly hurt.", "near_death": "They can barely stand.",
                    "down": ""}.get(c.get("hp_band"), "")
        if t == "npc_disposition_changed" and c.get("to") == "hostile":
            return "They turn on you, hostile now."
        if t == "condition_removed":
            return "The tonic's edge is spent." if c.get("reason") == "spent" else "You feel the affliction lift."
        if t == "npc_condition":
            who = c.get("npc_name") or "target"
            return (f"The {who} reels, {c.get('name')}!" if c.get("outcome") == "applied"
                    else f"The {who} shields their eyes in time.")
        if t == "tool_used":
            return f"Your {c.get('name')} makes the work easier."
        if t == "perk_used":
            return f"Your {c.get('name')} give you a sure grip." if str(c.get("name", "")).endswith("s") \
                else f"Your {c.get('name')} helps."
        if t == "noise_made":
            return f"The {c.get('name')} shrills, loud enough to carry through the walls."
        if t == "darkness":
            return "It's hard to see in the dark."
        if t == "item_consumed":
            return "That was the last of it."
        if t == "purse_changed":
            return "Coins change hands." if c.get("direction") == "spent" else "Coins clink into your purse."
        return ""


NUMBER_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "zero": 0, "no": 0}
DETERMINERS = ("the", "a", "an", "some", "your", "his", "her", "their", "my", "this", "that", "any")


def _split_quantity(subject: str):
    """'-1 torch' -> (-1, 'torch'); 'two torches' -> (2, 'torches'); 'a torch' -> (None, 'a torch')."""
    m = re.match(r"^\s*(-?\d+)\s+(.*)$", subject)
    if m:
        return int(m.group(1)), m.group(2)
    w, _, rest = subject.partition(" ")
    if w in NUMBER_WORDS and w not in ("a", "an") and rest:
        return NUMBER_WORDS[w], rest
    return None, subject


def _norm_phrase(text: str) -> str:
    words = re.findall(r"[a-z0-9']+", text.lower().replace("_", " "))
    while words and words[0] in DETERMINERS:
        words = words[1:]
    if words and len(words[-1]) > 3 and words[-1].endswith("s") and not words[-1].endswith("ss"):
        w = words[-1]  # plural -> singular, both sides
        words[-1] = w[:-2] if w.endswith(("ches", "shes", "xes", "sses")) else w[:-1]
    return " ".join(words)


def _topic_match(subject: str, pool: List[Dict], sell: bool = False) -> Optional[str]:
    """Exact (normalized) match of the subject against a topic/trade key, a label segment
    ('a / b'), an alias, or for trades the name of the traded item (or its last word)."""
    want = _norm_phrase(subject)
    if not want:
        return None
    for t in pool:
        names = [t["key"]] + [seg for seg in t.get("label", "").split("/")] + list(t.get("aliases", []))
        for e in (t.get("wants", []) if sell else t.get("gives", [])):
            if e.get("name"):
                names += [e["name"], e["name"].split()[-1]]
        if want in {_norm_phrase(n) for n in names if n}:
            return t["key"]
    return None


def _join(items: List[str]) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# ---------------------------------------------------------------------------
# Real providers (stubs) and factory
# ---------------------------------------------------------------------------

class _KeyedProvider:
    name = "base"
    env_key = ""
    default_model = ""

    def __init__(self):
        key = os.environ.get(self.env_key)
        if not key:
            raise LLMConfigError(
                f"RPG_LLM={self.name} needs the {self.env_key} environment variable. "
                f"Set it (export {self.env_key}=...) or use RPG_LLM=mock.")
        self._api_key = key  # never logged or printed
        self.model = os.environ.get("RPG_LLM_MODEL", self.default_model)

    def __repr__(self):
        return f"<{type(self).__name__} model={self.model!r}>"

    def interpret(self, player_text: str, context: Dict, reprompt: Optional[str] = None) -> Dict:
        raise NotImplementedError(f"{self.name} interpreter calls are not implemented yet (use RPG_LLM=mock)")

    def narrate(self, result: Dict, context: Dict) -> str:
        raise NotImplementedError(f"{self.name} narrator calls are not implemented yet (use RPG_LLM=mock)")

    def voice_npc(self, npc_persona_view: Dict, allowed_topics: List[Dict], result: Dict, context: Dict) -> Dict:
        raise NotImplementedError(f"{self.name} voice calls are not implemented yet (use RPG_LLM=mock)")


class OpenAIClient(_KeyedProvider):
    name, env_key, default_model = "openai", "OPENAI_API_KEY", "gpt-4o-mini"


class AnthropicClient(_KeyedProvider):
    name, env_key, default_model = "anthropic", "ANTHROPIC_API_KEY", "claude-3-5-haiku-latest"


PROVIDERS = {"mock": MockLLM, "openai": OpenAIClient, "anthropic": AnthropicClient}


def get_llm(name: Optional[str] = None) -> LLMClient:
    """Provider from `name`, else $RPG_LLM, else 'mock'."""
    choice = (name or os.environ.get("RPG_LLM") or "mock").strip().lower()
    if choice not in PROVIDERS:
        raise LLMConfigError(f"Unknown RPG_LLM={choice!r}; choose one of {', '.join(PROVIDERS)}")
    return PROVIDERS[choice]()

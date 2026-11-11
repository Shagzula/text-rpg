"""v0.7.1: NPCs the player can't see (in an unlit dark room) are never named.

Context, engine results and narration carry only a sound (persona `dark_sound`, else a generic
one) and a stable opaque reference `npc_unseen_<id>` that the interpreter can target."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from . import effects, personas

PREFIX = "npc_unseen_"
UNSEEN_NAME = "unseen figure"
VOICE_NAME = "a voice in the dark"
GENERIC_SOUND = "something breathing nearby"
NAME_FIELDS = ("name", "npc_name", "speaker_name")


def ref(npc_id: int) -> str:
    return f"{PREFIX}{npc_id}"


def sound(conn, npc_row) -> str:
    p = personas.get(conn, npc_row["id"]) or {}
    return str(p.get("dark_sound") or GENERIC_SOUND)


def is_ref(key: Optional[str]) -> bool:
    return isinstance(key, str) and key.startswith(PREFIX) and key[len(PREFIX):].isdigit()


def resolve(conn, key: Optional[str]) -> Optional[str]:
    """Map an opaque ref back to the NPC key; any other key is returned unchanged."""
    if not is_ref(key):
        return key
    row = conn.execute("SELECT key FROM npcs WHERE id = ?", (int(key[len(PREFIX):]),)).fetchone()
    return row["key"] if row else key


def _name_words(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower().replace("_", " "))) - {"npc", "the", "a", "an"}


def named_in_dark(conn, character_id: int, raw_key: Optional[str]) -> bool:
    """True if `raw_key` is a real key or a name-like slug for a living unseen NPC here.

    Opaque `npc_unseen_<id>` refs do not count: those are the supported way to target
    someone you can only hear.
    """
    if not raw_key or is_ref(raw_key):
        return False
    hidden = living_unseen(conn, character_id)
    if not hidden:
        return False
    if raw_key in hidden:
        return True
    want = _name_words(raw_key)
    if not want:
        return False
    for key, info in hidden.items():
        if want & (_name_words(info.get("name") or "") | _name_words(key)):
            return True
    return False


def named_or_slugged_in_dark(conn, character_id: int, raw_key: Optional[str]) -> bool:
    """A remembered name, real npc_ key, or invented npc_ slug used in a dark room.

    v0.10.1: with exactly one living unseen NPC this should resolve to that NPC's
    opaque ref (never confirming the name). With several, the engine asks by sound.
    """
    if not raw_key or is_ref(raw_key):
        return False
    if not living_unseen(conn, character_id):
        return False
    if named_in_dark(conn, character_id, raw_key):
        return True
    return isinstance(raw_key, str) and raw_key.startswith("npc_")


def living_unseen(conn, character_id: int) -> Dict[str, Dict]:
    return {k: v for k, v in unseen_npcs(conn, character_id).items() if v.get("alive")}


def sound_intent(action_type: Optional[str], sound: str, items_used=None) -> str:
    """An intent_summary that describes the unseen NPC by sound, never by name."""
    thing = f"the unseen thing making {sound or GENERIC_SOUND}"
    if action_type == "talk":
        return f"Talk to {thing}."
    if action_type == "attack":
        return f"Attack {thing}."
    if items_used:
        return f"Use what you're holding on {thing}."
    return f"Act on {thing}."


def _aliases(conn, npc_row) -> list:
    """Persona display name (if different) and the key slug as words: names the LLM must not see."""
    out = []
    p = personas.get(conn, npc_row["id"]) or {}
    if p.get("name") and p["name"] != npc_row["name"]:
        out.append(p["name"])
    slug = npc_row["key"][4:].replace("_", " ") if str(npc_row["key"]).startswith("npc_") else ""
    if slug and slug.lower() != (npc_row["name"] or "").lower():
        out.append(slug)
    return out


def unseen_npcs(conn, character_id: int) -> Dict[str, Dict]:
    """{npc_key: {ref, name, sound, aliases, alive}} for NPCs in the player's room when it's dark and unlit."""
    loc = conn.execute("SELECT location_id FROM characters WHERE id = ?", (character_id,)).fetchone()
    if loc is None or effects.location_is_lit(conn, loc[0], character_id):
        return {}
    out = {}
    for n in conn.execute("SELECT * FROM npcs WHERE location_id = ?", (loc[0],)):
        out[n["key"]] = {"ref": ref(n["id"]), "name": n["name"], "sound": sound(conn, n),
                         "aliases": _aliases(conn, n), "alive": bool(n["alive"])}
    return out


def is_unseen(conn, character_id: int, npc_key: str) -> bool:
    return npc_key in unseen_npcs(conn, character_id)


PRESERVE_FIELDS = frozenset({"player_text"})  # the player may type a name; never rewrite it


class Maskable(dict):
    """A context/result dict that carries the unseen map without JSON-serializing it."""

    def __init__(self, data=None, hidden=None):
        super().__init__(data or {})
        if hidden is None:
            hidden = hidden_of(data)
        self._hidden = dict(hidden or {})

    def copy(self):
        return Maskable(dict(self), self._hidden)


def hidden_of(obj) -> Dict[str, Dict]:
    hidden = getattr(obj, "_hidden", None)
    return dict(hidden) if hidden else {}


def carry(obj: Any, hidden: Dict[str, Dict]) -> Any:
    """Shallow-wrap a dict so later for_llm calls still know who was unseen."""
    if not isinstance(obj, dict):
        return obj
    return Maskable(obj, hidden or hidden_of(obj))


def identities(hidden: Dict[str, Dict]) -> list:
    """Real keys, display names, and aliases that must not appear in an LLM payload."""
    out = []
    for key, info in (hidden or {}).items():
        out.append(key)
        if info.get("name"):
            out.append(info["name"])
        for a in info.get("aliases") or []:
            out.append(a)
        if isinstance(key, str) and key.startswith("npc_") and len(key) > 4:
            slug = key[4:].replace("_", " ")
            if slug:
                out.append(slug)
    seen, uniq = set(), []
    for s in out:
        if s and s.lower() not in seen:
            seen.add(s.lower())
            uniq.append(s)
    return uniq


def _word_count(label: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+", label or ""))


def _replacements(hidden: Dict[str, Dict]):
    """Keys are case-insensitive. Multi-word display names/aliases are case-insensitive
    whole-phrase matches. A single capitalized proper name (Will, Hope, Stone, Mara)
    is case-sensitive, so ordinary lowercase English is left alone. Single-word key
    slugs are not used as name replacements (they would over-mask 'will' / 'hope')."""
    keys, ci_names, cs_names = [], [], []
    for key, info in hidden.items():
        if key and info.get("ref"):
            keys.append((key, info["ref"]))
        labels = set()
        if info.get("name"):
            labels.add(info["name"])
        for a in info.get("aliases") or []:
            if a:
                labels.add(a)
        if isinstance(key, str) and key.startswith("npc_") and len(key) > 4:
            slug = key[4:].replace("_", " ")
            if slug and _word_count(slug) > 1:
                labels.add(slug)
        for lab in labels:
            if not lab:
                continue
            if _word_count(lab) > 1:
                ci_names.append((lab, UNSEEN_NAME))
            else:
                cs_names.append((lab, UNSEEN_NAME))
    keys.sort(key=lambda p: len(p[0]), reverse=True)
    ci_names.sort(key=lambda p: len(p[0]), reverse=True)
    cs_names.sort(key=lambda p: len(p[0]), reverse=True)
    return keys, ci_names, cs_names


def _sub_str(s: str, keys, ci_names, cs_names) -> str:
    for old, new in keys:
        s = re.sub(r"(?i)(?<![A-Za-z0-9_])" + re.escape(old) + r"(?![A-Za-z0-9_])", new, s)
    for old, new in ci_names:
        s = re.sub(r"(?i)(?<!\w)" + re.escape(old) + r"(?!\w)", new, s)
    for old, new in cs_names:
        s = re.sub(r"(?<!\w)" + re.escape(old) + r"(?!\w)", new, s)
    return s


def mask(obj: Any, hidden: Dict[str, Dict], *, preserve: Any = PRESERVE_FIELDS) -> Any:
    """Replace hidden NPC keys with their refs and their names with UNSEEN_NAME, anywhere in obj.

    Keys are rewritten even when they are a suffix of a compound flag (e.g.
    ``defeated:npc_dozing_guard`` -> ``defeated:npc_unseen_2``). Names and aliases are
    rewritten in any string, not only name fields. ``player_text`` is left intact.
    """
    if not hidden:
        return obj
    keys_map = {k: v["ref"] for k, v in hidden.items()}
    key_pairs, ci_names, cs_names = _replacements(hidden)
    preserve = set(preserve or ())

    def walk(o, field=None):
        if field in preserve:
            return o
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                nk = _sub_str(k, key_pairs, ci_names, cs_names) if isinstance(k, str) else k
                out[nk] = walk(v, k)
            raw_key = o.get("key")
            if raw_key in keys_map or (isinstance(out.get("key"), str) and out["key"].startswith(PREFIX)):
                out.pop("article", None)
            return out
        if isinstance(o, list):
            return [walk(v, field) for v in o]
        if isinstance(o, str):
            return _sub_str(o, key_pairs, ci_names, cs_names)
        return o
    return walk(obj)


def for_llm(obj: Any, hidden: Optional[Dict[str, Dict]] = None, *, preserve: Any = PRESERVE_FIELDS) -> Any:
    """The single masking step at the interpret / narrate / voice payload boundary.

    Real keys and names of NPCs unseen at that moment become their opaque
    ``npc_unseen_<id>`` / "unseen figure" form. The carried unseen map is kept on
    the returned dict (not serialized) so a later call can re-apply the same mask.
    Internal DB state is not touched.
    """
    hidden = hidden or hidden_of(obj) or {}
    masked = mask(obj, hidden, preserve=preserve)
    if isinstance(masked, dict):
        return carry(masked, hidden)
    return masked

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


def unseen_npcs(conn, character_id: int) -> Dict[str, Dict]:
    """{npc_key: {ref, name, sound}} for the NPCs in the player's room when it's dark and unlit."""
    loc = conn.execute("SELECT location_id FROM characters WHERE id = ?", (character_id,)).fetchone()
    if loc is None or effects.location_is_lit(conn, loc[0], character_id):
        return {}
    return {n["key"]: {"ref": ref(n["id"]), "name": n["name"], "sound": sound(conn, n), "alive": bool(n["alive"])}
            for n in conn.execute("SELECT * FROM npcs WHERE location_id = ?", (loc[0],))}


def is_unseen(conn, character_id: int, npc_key: str) -> bool:
    return npc_key in unseen_npcs(conn, character_id)


def mask(obj: Any, hidden: Dict[str, Dict]) -> Any:
    """Replace hidden NPC keys with their refs and their names with UNSEEN_NAME, anywhere in obj."""
    if not hidden:
        return obj
    keys = {k: v["ref"] for k, v in hidden.items()}
    names = {v["name"] for v in hidden.values()}

    def walk(o, field=None):
        if isinstance(o, dict):
            out = {k: walk(v, k) for k, v in o.items()}
            # article is a naming tell: never leave it on an unseen NPC entry
            raw_key = o.get("key")
            if raw_key in keys or (isinstance(out.get("key"), str) and out["key"].startswith(PREFIX)):
                out.pop("article", None)
            return out
        if isinstance(o, list):
            return [walk(v, field) for v in o]
        if isinstance(o, str):
            if o in keys:
                return keys[o]
            if field in NAME_FIELDS and o in names:
                return UNSEEN_NAME
        return o
    return walk(obj)

"""v0.7.1: NPCs the player can't see (in an unlit dark room) are never named.

Context, engine results and narration carry only a sound (persona `dark_sound`, else a generic
one) and a stable opaque reference `npc_unseen_<id>` that the interpreter can target."""
from __future__ import annotations

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


def resolve(conn, key: Optional[str]) -> Optional[str]:
    """Map an opaque ref back to the NPC key; any other key is returned unchanged."""
    if not isinstance(key, str) or not key.startswith(PREFIX) or not key[len(PREFIX):].isdigit():
        return key
    row = conn.execute("SELECT key FROM npcs WHERE id = ?", (int(key[len(PREFIX):]),)).fetchone()
    return row["key"] if row else key


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
            return {k: walk(v, k) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v, field) for v in o]
        if isinstance(o, str):
            if o in keys:
                return keys[o]
            if field in NAME_FIELDS and o in names:
                return UNSEEN_NAME
        return o
    return walk(obj)

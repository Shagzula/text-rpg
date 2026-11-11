"""Item instance helpers, all addressed by item `key`.

An item belongs to exactly one owner: a character (inventory) or a location
(visible in the scene). Templates tagged 'fixture' are scenery: they can be
examined, used and targeted but never moved (take/transfer raises item_unusable).
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from .db import add_item, next_item_key  # noqa: F401  (re-exported)

# Slots an item occupies when equipped; equipping frees every conflicting slot.
SLOT_CONFLICTS = {
    "main_hand": {"main_hand", "two_hands"},
    "off_hand": {"off_hand", "two_hands"},
    "two_hands": {"main_hand", "off_hand", "two_hands"},
    "body": {"body"}, "head": {"head"}, "hands": {"hands"}, "feet": {"feet"},
    "belt": set(),  # belt items (pouches, vials) don't conflict
}


class ItemError(Exception):
    """Raised with a contract validation code: item_not_owned, item_unusable, ..."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


_ITEM_SQL = """SELECT i.*, t.key AS template_key, t.name AS template_name, t.category, t.slot,
                      t.power_tier, t.weight, t.value, t.stats, t.tags, t.description
               FROM items i JOIN item_templates t ON t.id = i.template_id"""


# v0.7: structured defaults for items whose YAML effect is descriptive only (`use_note`). Keyed by the YAML's
# own structured fields; any key the template's stats already carry wins.
SUBCATEGORY_DEFAULTS = {
    # item_rope (stats.subcategory: climbing): "enables climbing/descending; advantage on athletics climb
    # checks when anchored" -> a climbing tool: advantage on athletics when climbing or when it's used.
    "climbing": {"enables": ["climb"], "skill_bonus": {"skill": "athletics", "grants": "advantage"}},
}


def _row_to_item(r) -> Dict:
    d = dict(r)
    d["stats"] = json.loads(d["stats"])
    d["stats"] = {**SUBCATEGORY_DEFAULTS.get(d["stats"].get("subcategory"), {}), **d["stats"]}
    d["tags"] = json.loads(d["tags"])
    d["name"] = d["display_name"] or d["template_name"]
    d["equipped"] = bool(d["equipped"])
    d["out_of_reach"] = bool(d["out_of_reach"])
    d["fixture"] = "fixture" in d["tags"]
    return d


def get_item(conn, key: str) -> Optional[Dict]:
    r = conn.execute(_ITEM_SQL + " WHERE i.key = ?", (key,)).fetchone()
    return _row_to_item(r) if r else None


def has_item(conn, character_id: int, key: str) -> bool:
    return conn.execute("SELECT 1 FROM items WHERE key = ? AND character_id = ?",
                        (key, character_id)).fetchone() is not None


def list_inventory(conn, character_id: int) -> List[Dict]:
    return [_row_to_item(r) for r in conn.execute(_ITEM_SQL + " WHERE i.character_id = ? ORDER BY i.id",
                                                  (character_id,))]


def list_location_items(conn, location_id: int) -> List[Dict]:
    return [_row_to_item(r) for r in conn.execute(_ITEM_SQL + " WHERE i.location_id = ? ORDER BY i.id",
                                                  (location_id,))]


def _require(conn, key: str) -> Dict:
    item = get_item(conn, key)
    if item is None:
        raise ItemError("item_not_owned", f"no item {key!r}")
    return item


def remove_item(conn, key: str, quantity: int = 1) -> int:
    """Remove `quantity` from a stack (deletes it at 0). Returns the quantity left."""
    item = _require(conn, key)
    left = item["quantity"] - quantity
    if left < 0:
        raise ItemError("item_unusable", f"only {item['quantity']} of {key}")
    if left == 0:
        conn.execute("DELETE FROM items WHERE id = ?", (item["id"],))
    else:
        conn.execute("UPDATE items SET quantity = ? WHERE id = ?", (left, item["id"]))
    return left


def transfer(conn, key: str, *, to_character: Optional[int] = None, to_location: Optional[int] = None,
             quantity: Optional[int] = None) -> str:
    """Move an item (or part of a stack) to a character or a location. Unequips it.
    Returns the key of the moved instance (a new `<key>_N` instance for a partial stack)."""
    if (to_character is None) == (to_location is None):
        raise ValueError("give exactly one of to_character / to_location")
    item = _require(conn, key)
    if item["fixture"]:
        raise ItemError("item_unusable", f"{key} is a fixture")
    qty = item["quantity"] if quantity is None else quantity
    if not 0 < qty <= item["quantity"]:
        raise ItemError("item_unusable", f"only {item['quantity']} of {key}")
    if qty == item["quantity"]:
        conn.execute("UPDATE items SET character_id = ?, location_id = ?, equipped = 0 WHERE id = ?",
                     (to_character, to_location, item["id"]))
        return key
    conn.execute("UPDATE items SET quantity = quantity - ? WHERE id = ?", (qty, item["id"]))
    new_key = next_item_key(conn, item["template_key"])
    conn.execute(
        """INSERT INTO items (key, template_id, character_id, location_id, display_name, quantity,
           uses_left, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_key, item["template_id"], to_character, to_location, item["display_name"], qty,
         item["uses_left"], item["state"]),
    )
    return new_key


def equipped_items(conn, character_id: int) -> List[Dict]:
    return [i for i in list_inventory(conn, character_id) if i["equipped"]]


def equip(conn, character_id: int, key: str, *, replace: bool = True) -> List[str]:
    """Equip a held item. Conflicting equipped items are unequipped (replace=True) or
    raise ItemError('item_unusable', 'slot_occupied'). Returns keys unequipped."""
    item = get_item(conn, key)
    if item is None or item["character_id"] != character_id:
        raise ItemError("item_not_owned", key)
    slot = item["slot"]
    if slot not in SLOT_CONFLICTS:
        raise ItemError("item_unusable", "not_equippable")
    if item["equipped"]:
        return []
    conflicts = [i for i in equipped_items(conn, character_id) if i["slot"] in SLOT_CONFLICTS[slot]]
    if conflicts and not replace:
        raise ItemError("item_unusable", "slot_occupied")
    for c in conflicts:
        conn.execute("UPDATE items SET equipped = 0 WHERE id = ?", (c["id"],))
    conn.execute("UPDATE items SET equipped = 1 WHERE id = ?", (item["id"],))
    return [c["key"] for c in conflicts]


def unequip(conn, character_id: int, key: str) -> None:
    item = get_item(conn, key)
    if item is None or item["character_id"] != character_id:
        raise ItemError("item_not_owned", key)
    if not item["equipped"]:
        raise ItemError("item_unusable", "not_equipped")
    conn.execute("UPDATE items SET equipped = 0 WHERE id = ?", (item["id"],))


def consume_use(conn, key: str) -> bool:
    """Spend one use (template stats.uses). Returns True if one unit was used up and removed.
    Durable items (uses null/absent) are never consumed."""
    item = _require(conn, key)
    uses = item["stats"].get("uses")
    if uses is None:
        return False
    left = (item["uses_left"] if item["uses_left"] is not None else uses) - 1
    if left > 0:
        conn.execute("UPDATE items SET uses_left = ? WHERE id = ?", (left, item["id"]))
        return False
    if item["quantity"] > 1:
        conn.execute("UPDATE items SET quantity = quantity - 1, uses_left = NULL WHERE id = ?", (item["id"],))
    else:
        conn.execute("DELETE FROM items WHERE id = ?", (item["id"],))
    return True

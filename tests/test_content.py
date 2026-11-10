"""Content loading: the stdlib YAML subset parser, and behaviour driven straight from the
Story Designer's YAML (content/items_tier1.yaml) rather than hard-coded names."""
import json
from pathlib import Path

import pytest

from rpg import db, effects, yamlish
from rpg import inventory as inv
from test_actions import Script, act, run, world  # noqa: F401  (fixture)

CONTENT = Path(__file__).resolve().parents[2] / "content"
ITEMS_YAML = CONTENT / "items_tier1.yaml"
GATEKEEPER_YAML = CONTENT / "npcs" / "npc_example_gatekeeper.yaml"
needs_items = pytest.mark.skipif(not ITEMS_YAML.exists(), reason="content/items_tier1.yaml not present")
needs_gatekeeper = pytest.mark.skipif(not GATEKEEPER_YAML.exists(), reason="gatekeeper example not present")


# --- yamlish --------------------------------------------------------------------------------------

def test_yamlish_block_maps_lists_and_scalars():
    doc = yamlish.loads("""
# comment
name: "Mara"   # trailing comment
n: 12
neg: -3
f: 0.5
yes: true
nothing: null
tags: [a, b, "c d"]
flow: {x: 1, y: [p, q]}
items:
  - id: one
    qty: 2
  - plain
nested:
  deep:
    key: 'single # not a comment'
""")
    assert doc == {"name": "Mara", "n": 12, "neg": -3, "f": 0.5, "yes": True, "nothing": None,
                   "tags": ["a", "b", "c d"], "flow": {"x": 1, "y": ["p", "q"]},
                   "items": [{"id": "one", "qty": 2}, "plain"],
                   "nested": {"deep": {"key": "single # not a comment"}}}


def test_yamlish_rejects_bad_indentation():
    with pytest.raises(yamlish.YamlError):
        yamlish.loads("a:\n  b: 1\n c: 2\n")


@needs_items
def test_items_tier1_loads_into_templates(world):
    items = yamlish.load(ITEMS_YAML)
    assert isinstance(items, list) and len(items) >= 30
    conn = world["conn"]
    existing = {r[0] for r in conn.execute("SELECT key FROM item_templates")}
    for tpl in items:
        if tpl["id"] not in existing:
            db.add_item_template(conn, tpl)  # digit-free names, content shape
    assert all(not any(ch.isdigit() for ch in t["name"]) for t in items)


@needs_gatekeeper
def test_gatekeeper_example_parses():
    p = yamlish.load(GATEKEEPER_YAML)
    assert p["key"] == "npc_example_gatekeeper" and "knowledge" in p and "reveal_by_band" in p


# --- behaviour from the YAML ----------------------------------------------------------------------

def _yaml_template(key):
    return next(t for t in yamlish.load(ITEMS_YAML) if t["id"] == key)


def _replace_template(conn, tpl):
    stats = dict(tpl.get("stats") or {})
    conn.execute("UPDATE item_templates SET stats = ?, tags = ? WHERE key = ?",
                 (json.dumps(stats), json.dumps(tpl.get("tags") or []), tpl["id"]))


@needs_items
def test_flash_powder_from_yaml_stops_the_npc_attack(world):
    conn = world["conn"]
    tpl = _yaml_template("item_flash_powder")
    assert tpl["stats"]["condition_effect"]  # the designer's spec drives it, not the name list
    _replace_template(conn, tpl)
    conn.execute("UPDATE npcs SET disposition = -80 WHERE key = 'npc_tall_guard'")
    db.add_item(conn, "item_flash_powder", character_id=world["cid"])
    r = run(world, act("use_item", target="npc_tall_guard", items_used=["item_flash_powder"]), rng=Script(5))
    assert r["npc_actions"][0]["action"] == "none" and r["npc_actions"][0]["reason"] == "blinded"
    assert r["expired"] == []  # "1 round" survives into the player's next action (BUG-R01)


@needs_items
def test_knife_from_yaml_is_an_improvised_pick_by_tag(world, caplog):
    conn = world["conn"]
    tpl = _yaml_template("item_knife")
    assert "improvised_pick" in tpl["tags"]
    if not conn.execute("SELECT 1 FROM item_templates WHERE key = 'item_knife'").fetchone():
        db.add_item_template(conn, tpl)
    inv.remove_item(conn, "item_lockpicks")
    db.add_item(conn, "item_knife", character_id=world["cid"])
    with caplog.at_level("DEBUG", logger="rpg.debug"):
        r = run(world, act("skill_check", skill="sleight_of_hand", target="loc_cellar"), rng=Script(18))
    assert r["results"][0]["status"] == "resolved" and "'dc': 25" in caplog.text


@pytest.mark.parametrize("eff,name,expected", [
    ({"no_attack": True}, "dazzled", True),
    ({"prevents_actions": True}, "dazzled", True),
    ({"incapacitated": True}, "dazzled", True),
    ({"no_attack": False}, "blinded", False),          # an explicit spec beats the name list
    ({"advantage": ["dex"]}, "stunned", False),        # a spec without a flag doesn't incapacitate
    ({}, "stunned", True),                             # no spec: fallback name list
    ({}, "dazzled", False),
])
def test_incapacitation_is_driven_by_the_effect_spec(eff, name, expected):
    assert effects.effect_incapacitates(name, eff) is expected


def test_prevents_actions_condition_stops_attack(world):
    conn = world["conn"]
    conn.execute("UPDATE npcs SET disposition = -80 WHERE key = 'npc_tall_guard'")
    nid = conn.execute("SELECT id FROM npcs WHERE key = 'npc_tall_guard'").fetchone()[0]
    conn.execute("INSERT INTO npc_conditions (npc_id, name, effect, duration_turns, source) VALUES "
                 "(?, 'entangled', ?, 2, 'test')", (nid, json.dumps({"prevents_actions": True})))
    r = run(world, act("wait"))
    assert r["npc_actions"][0]["action"] == "none" and r["npc_actions"][0]["reason"] == "entangled"

"""Persona loading/validation (content/npc_persona_schema.md) and the demo personas."""
import copy
import json

import pytest

from rpg import db, personas, world as demo, yamlish
from test_content import GATEKEEPER_YAML, ITEMS_YAML

MARA = personas.DEMO_PERSONA_DIR / "npc_mara.yaml"
GUARD = personas.DEMO_PERSONA_DIR / "npc_dozing_guard.yaml"


@pytest.fixture
def conn(tmp_path):
    c = db.get_connection(tmp_path / "d.db")
    db.create_schema(c)
    demo.seed_demo_slice(c)
    yield c
    c.close()


def tkeys(conn):
    return [r[0] for r in conn.execute("SELECT key FROM item_templates")]


@pytest.mark.parametrize("path", [MARA, GUARD])
def test_demo_personas_validate(conn, path):
    p = personas.load_file(path, tkeys(conn))
    texts = [t["text"] for t in {**p["knowledge"], **p.get("secrets", {})}.values()]
    assert not any(ch.isdigit() for t in texts + [p["insight_tell"]] for ch in t)  # narration stays digit-free


def test_designer_gatekeeper_example_validates():
    keys = [t["id"] for t in yamlish.load(ITEMS_YAML)]
    p = personas.load_file(GATEKEEPER_YAML, keys)
    assert p["combat"]["max_hp"] == 8


def _bad(conn, mutate, match):
    p = copy.deepcopy(yamlish.load(MARA))
    mutate(p)
    with pytest.raises(personas.PersonaError, match=match):
        personas.validate(p, tkeys(conn))


def test_rejects_secret_below_its_min_band(conn):
    _bad(conn, lambda p: p["reveal_by_band"]["neutral"]["topics"].append("cellar_lever"), "below its min_band")


def test_rejects_never_secret_in_reveal(conn):
    _bad(conn, lambda p: p["reveal_by_band"]["allied"]["topics"].append("mara_past"), "never")


def test_rejects_unknown_topic_and_trade_and_item(conn):
    _bad(conn, lambda p: p["reveal_by_band"]["neutral"]["topics"].append("nope"), "unknown topic")
    _bad(conn, lambda p: p["reveal_by_band"]["neutral"]["trades"].append("nope"), "unknown trade")
    _bad(conn, lambda p: p["trades"]["trade_buy_torch"]["gives"].__setitem__(0, {"item": "item_nope", "qty": 1}),
         "unknown item template")
    _bad(conn, lambda p: p["trades"]["trade_buy_torch"]["wants"].__setitem__(0, {"gold": 0}), "positive int")


def test_rejects_missing_band_and_bad_disposition(conn):
    _bad(conn, lambda p: p["reveal_by_band"].pop("allied"), "five bands")
    _bad(conn, lambda p: p.__setitem__("base_disposition", 150), "base_disposition")
    _bad(conn, lambda p: p.pop("voice"), "voice")


def test_rejects_bad_combat_block(conn):
    _bad(conn, lambda p: p["combat"].__setitem__("damage", "lots"), "dice")
    _bad(conn, lambda p: p["combat"].__setitem__("max_hp", 0), "max_hp")
    _bad(conn, lambda p: p["combat"].__setitem__("speed", 3), "combat accepts only")


def test_seed_installs_personas_gold_and_combat(conn):
    rows = {r["key"]: r for r in conn.execute("SELECT * FROM npcs")}
    for key in ("npc_mara", "npc_dozing_guard"):
        assert personas.get(conn, rows[key]["id"])["key"] == key
    g = rows["npc_dozing_guard"]
    assert (g["defense_dc"], g["max_hp"], g["attack_bonus"], g["damage"], g["save_bonus"]) == (11, 12, 3, "1d6", 1)
    assert rows["npc_mara"]["disposition"] == 20
    assert conn.execute("SELECT gold FROM characters").fetchone()[0] == demo.DEMO_GOLD == 60


def test_combat_block_overrides_defaults(conn):
    p = yamlish.load(GUARD)
    p["combat"] = {"max_hp": 30, "attack_bonus": 6, "damage": "2d6", "defense_dc": 15, "save_bonus": 4}
    conn.execute("UPDATE npcs SET current_hp = 3 WHERE key = 'npc_dozing_guard'")
    personas.install(conn, personas.validate(p, tkeys(conn)))
    g = conn.execute("SELECT * FROM npcs WHERE key = 'npc_dozing_guard'").fetchone()
    assert (g["defense_dc"], g["max_hp"], g["attack_bonus"], g["damage"], g["save_bonus"], g["current_hp"]) == \
        (15, 30, 6, "2d6", 4, None)


def test_persona_view_has_no_secrets(conn):
    p = personas.load_file(MARA, tkeys(conn))
    view = json.dumps(personas.persona_view(p))
    for s in personas.secret_texts(p) + [p["insight_tell"]] + [t["text"] for t in p["knowledge"].values()]:
        assert s not in view
    assert set(personas.persona_view(p)) == {"key", "name", "voice", "traits", "goals"}


def test_band_availability_is_cumulative(conn):
    p = personas.load_file(MARA, tkeys(conn))
    assert personas.available_topics(p, "unfriendly") == ["mara_self"]
    assert set(personas.available_topics(p, "friendly")) == {"mara_self", "the_storeroom", "market_news",
                                                             "the_cellar", "guard_sleeps"}
    assert "trade_tonic_for_rope" in personas.available_trades(p, "allied")
    vis = {t["key"] for t in personas.visible_topics(p, "friendly")}
    assert "guard_sleeps" in vis and "cellar_lever" not in vis and "mara_past" not in vis
    assert personas.social_dc(p, "deception", "hostile") == 12 + 3 + 5

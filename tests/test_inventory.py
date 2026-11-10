import pytest

from rpg import db
from rpg import inventory as inv
from rpg.inventory import ItemError
from helpers import build_world


@pytest.fixture
def world(tmp_path):
    conn = db.get_connection(tmp_path / "w.db")
    db.create_schema(conn)
    w = build_world(conn)
    yield conn, w
    conn.close()


def keys(items):
    return [i["key"] for i in items]


def test_list_and_has_item(world):
    conn, w = world
    held = inv.list_inventory(conn, w["cid"])
    assert "item_rusty_shortsword" in keys(held)
    blade = inv.get_item(conn, "item_rusty_shortsword")
    assert blade["name"] == "Rusty Shortsword" and blade["equipped"] and blade["stats"]["damage"] == "1d6"
    assert inv.has_item(conn, w["cid"], "item_healing_draught")
    assert not inv.has_item(conn, w["cid"], "item_hanging_lamp")
    assert not inv.has_item(conn, w["cid"], "item_dragon_wand")
    floor = inv.list_location_items(conn, w["store"])
    assert {"item_hanging_lamp", "item_lever", "item_crowbar"} <= set(keys(floor))
    assert inv.get_item(conn, "item_hanging_lamp")["fixture"]


def test_add_and_remove_stack(world):
    conn, w = world
    assert inv.remove_item(conn, "item_coins", 2) == 3
    assert inv.remove_item(conn, "item_coins", 3) == 0
    assert inv.get_item(conn, "item_coins") is None
    with pytest.raises(ItemError) as e:
        inv.remove_item(conn, "item_coins")
    assert e.value.code == "item_not_owned"
    with pytest.raises(ItemError):
        inv.remove_item(conn, "item_healing_draught", 5)


def test_transfer_between_character_and_location(world):
    conn, w = world
    assert inv.transfer(conn, "item_rusty_shortsword", to_location=w["store"]) == "item_rusty_shortsword"
    blade = inv.get_item(conn, "item_rusty_shortsword")
    assert blade["character_id"] is None and blade["location_id"] == w["store"] and not blade["equipped"]
    inv.transfer(conn, "item_rusty_shortsword", to_character=w["cid"])
    assert inv.has_item(conn, w["cid"], "item_rusty_shortsword")


def test_partial_stack_transfer_gets_new_slug_key(world):
    conn, w = world
    new = inv.transfer(conn, "item_coins", to_location=w["store"], quantity=2)
    assert new == "item_coin"  # first free key for template item_coin
    assert inv.get_item(conn, "item_coins")["quantity"] == 3
    assert inv.get_item(conn, new)["quantity"] == 2
    assert inv.transfer(conn, "item_coins", to_location=w["store"], quantity=1) == "item_coin_2"


def test_fixture_cannot_be_transferred(world):
    conn, w = world
    with pytest.raises(ItemError) as e:
        inv.transfer(conn, "item_hanging_lamp", to_character=w["cid"])
    assert e.value.code == "item_unusable"


def test_equip_slot_conflicts(world):
    conn, w = world
    cid = w["cid"]
    assert inv.equip(conn, cid, "item_buckler") == []  # off_hand free
    # two-handed blade displaces main hand AND off hand
    assert sorted(inv.equip(conn, cid, "item_heavy_blade")) == ["item_buckler", "item_rusty_shortsword"]
    assert set(keys(inv.equipped_items(conn, cid))) == {"item_travel_boots", "item_heavy_blade"}
    # one-hander displaces the two-hander
    assert inv.equip(conn, cid, "item_rusty_shortsword") == ["item_heavy_blade"]
    with pytest.raises(ItemError) as e:
        inv.equip(conn, cid, "item_heavy_blade", replace=False)
    assert (e.value.code, e.value.detail) == ("item_unusable", "slot_occupied")
    assert inv.equip(conn, cid, "item_rusty_shortsword") == []  # already equipped: no-op


def test_belt_items_do_not_conflict(world):
    conn, w = world
    inv.equip(conn, w["cid"], "item_healing_draught")
    inv.equip(conn, w["cid"], "item_bandage_roll")
    assert {"item_healing_draught", "item_bandage_roll"} <= set(keys(inv.equipped_items(conn, w["cid"])))


def test_equip_errors(world):
    conn, w = world
    with pytest.raises(ItemError) as e:
        inv.equip(conn, w["cid"], "item_crowbar")  # on the floor
    assert e.value.code == "item_not_owned"
    inv.transfer(conn, "item_crowbar", to_character=w["cid"])
    with pytest.raises(ItemError) as e:
        inv.equip(conn, w["cid"], "item_crowbar")  # slot none
    assert (e.value.code, e.value.detail) == ("item_unusable", "not_equippable")


def test_unequip(world):
    conn, w = world
    inv.unequip(conn, w["cid"], "item_travel_boots")
    assert not inv.get_item(conn, "item_travel_boots")["equipped"]
    with pytest.raises(ItemError) as e:
        inv.unequip(conn, w["cid"], "item_travel_boots")
    assert e.value.detail == "not_equipped"


def test_consume_use(world):
    conn, w = world
    assert not inv.consume_use(conn, "item_bandage_roll")  # 3 -> 2
    assert inv.get_item(conn, "item_bandage_roll")["uses_left"] == 2
    inv.consume_use(conn, "item_bandage_roll")
    assert inv.consume_use(conn, "item_bandage_roll")  # used up
    assert inv.get_item(conn, "item_bandage_roll") is None
    assert not inv.consume_use(conn, "item_rusty_shortsword")  # durable

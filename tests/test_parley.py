"""v0.11 PARLEY: talking to or gifting a hostile mid-combat is one contested social check."""
from rpg import db, llm as llm_mod, talk
from rpg import inventory as inv
from test_actions import Script
from test_talk import g, run, say, disp  # noqa: F401
from test_dark_npcs import leaks, put
from test_retest import demo, go, hostile_guard, say as play  # noqa: F401


def _to_store(g):
    g["conn"].execute(
        "UPDATE characters SET location_id = (SELECT id FROM locations WHERE key = 'loc_storeroom')")
    g["conn"].execute("UPDATE npcs SET disposition = -80, current_hp = max_hp, alive = 1 "
                      "WHERE key = 'npc_dozing_guard'")


def _rations(g):
    return next(i["key"] for i in inv.list_inventory(g["conn"], g["cid"]) if i["template_key"] == "item_rations")


def test_successful_parley_ends_combat_at_wary(g):
    _to_store(g)
    r = run(g, say("persuade", npc="npc_dozing_guard"), rng=Script(20))
    res = r["results"][0]
    assert res["status"] == "resolved" and res["outcome"] == "success"
    p = res["parley"]
    assert p["success"] is True and p["stance"] == "wary" and p["skill"] == "persuasion"
    assert p["gift"] is None and p["advantage"] is False
    assert disp(g, "npc_dozing_guard") == talk.WARY_DISPOSITION == -20
    assert talk.rules.disposition_band(talk.WARY_DISPOSITION) == "unfriendly"
    assert r["time_passed"] is True
    assert r.get("wasted_round") is not True
    assert not any(a.get("action") == "attack" for a in r["npc_actions"])
    out = llm_mod.MockLLM().narrate(r, {})
    assert "parley" in out.lower() and "wary" in out.lower()


def test_failed_parley_is_a_resolved_failure_and_the_foe_acts(g):
    _to_store(g)
    r = run(g, say("ask", npc="npc_dozing_guard"), rng=Script(1, 1))
    res = r["results"][0]
    assert res["status"] == "resolved" and res["outcome"] == "failure"
    assert res["parley"]["success"] is False and res["parley"]["stance"] is None
    assert disp(g, "npc_dozing_guard") == -80
    assert r["time_passed"] is True
    assert any(a.get("action") == "attack" for a in r["npc_actions"])


def test_gift_parley_has_advantage_consumes_the_gift_on_success_only(g):
    _to_store(g)
    rk = _rations(g)
    # Hostile DC 17, CHA 10: 1 fails, 20 succeeds; advantage keeps the 20.
    r = run(g, say("gift", npc="npc_dozing_guard", items=[rk]), rng=Script(1, 20))
    res = r["results"][0]
    assert res["parley"]["success"] is True and res["parley"]["advantage"] is True
    assert res["parley"]["gift"]["name"] == "Travel Rations"
    assert disp(g, "npc_dozing_guard") == talk.WARY_DISPOSITION
    assert rk not in {i["key"] for i in inv.list_inventory(g["conn"], g["cid"])}

    # A fresh hostile and a fresh ration: failure does not consume the gift.
    _to_store(g)
    db.add_item(g["conn"], "item_rations", character_id=g["cid"])
    rk = _rations(g)
    r = run(g, say("gift", npc="npc_dozing_guard", items=[rk]), rng=Script(1, 1, 1))
    assert r["results"][0]["parley"]["success"] is False
    assert rk in {i["key"] for i in inv.list_inventory(g["conn"], g["cid"])}
    assert disp(g, "npc_dozing_guard") == -80


def test_no_retry_without_new_leverage_but_a_gift_counts(g):
    _to_store(g)
    r = run(g, say("persuade", npc="npc_dozing_guard"), rng=Script(1, 1))
    assert r["results"][0]["parley"]["success"] is False
    r = run(g, say("persuade", npc="npc_dozing_guard"), rng=Script(1))
    assert r["results"][0]["status"] == "rejected" and r["results"][0]["detail"] == "no_new_leverage"
    assert r.get("wasted_round") is True  # in-combat rejection: hostiles still act
    rk = _rations(g)
    r = run(g, say("gift", npc="npc_dozing_guard", items=[rk]), rng=Script(1, 20))
    assert r["results"][0]["parley"]["success"] is True
    assert disp(g, "npc_dozing_guard") == talk.WARY_DISPOSITION


def test_disposition_does_not_rise_on_a_bystander_during_combat(g):
    _to_store(g)
    g["conn"].execute("UPDATE npcs SET location_id = (SELECT id FROM locations WHERE key = 'loc_storeroom'), "
                      "disposition = 0 WHERE key = 'npc_mara'")
    before = disp(g, "npc_mara")
    r = run(g, say("persuade", "the_cellar", npc="npc_mara"), rng=Script(20, 1))
    assert r["results"][0]["status"] == "resolved"
    assert "parley" not in r["results"][0]
    assert disp(g, "npc_mara") == before  # no rise while a hostile is here


def test_dark_parley_is_masked(demo):  # noqa: F811
    conn, cid = demo
    put(conn, "npc_dozing_guard", "loc_cellar")
    go(demo, "loc_cellar")
    hostile_guard(demo)
    r, out = play(demo, "talk to the breathing", 20)
    assert r["results"][0]["parley"]["success"] is True
    assert r["results"][0]["target"]["key"].startswith("npc_unseen_")
    assert leaks(r, conn, "npc_dozing_guard") is None
    assert leaks(out, conn, "npc_dozing_guard") is None
    assert "wary" in out.lower() and "parley" in out.lower()
    assert "Dozing Guard" not in out

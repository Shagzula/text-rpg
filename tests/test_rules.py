import random

import pytest

from rpg import rules
from rpg.rules import (Character, Condition, SKILL_ABILITY, ability_modifier,
                       proficiency_bonus, resolve_skill_check, skill_modifier)


def fixed(*rolls):
    it = iter(rolls)
    return lambda: next(it)


def never():
    def roller():
        raise AssertionError("should not roll")
    return roller


def make(level=1, skills=None, conditions=None, **scores):
    base = {a: 10 for a in rules.ABILITIES}
    base.update(scores)
    return Character(name="T", level=level, scores=base, skills=skills or {},
                     conditions=conditions or [])


@pytest.mark.parametrize("score,mod", [(1, -5), (3, -4), (8, -1), (9, -1), (10, 0),
                                       (11, 0), (12, 1), (15, 2), (20, 5), (30, 10)])
def test_ability_modifier(score, mod):
    assert ability_modifier(score) == mod


@pytest.mark.parametrize("level,pb", [(1, 2), (4, 2), (5, 3), (8, 3), (9, 4),
                                      (12, 4), (13, 5), (16, 5), (17, 6), (20, 6)])
def test_proficiency_bonus(level, pb):
    assert proficiency_bonus(level) == pb


def test_proficiency_bonus_rejects_bad_level():
    with pytest.raises(ValueError):
        proficiency_bonus(0)


def test_all_18_skills_mapped():
    assert len(SKILL_ABILITY) == 18
    assert set(SKILL_ABILITY.values()) <= set(rules.ABILITIES)
    assert SKILL_ABILITY["athletics"] == "str" and SKILL_ABILITY["stealth"] == "dex"


def test_skill_modifier_proficiency_and_expertise():
    c = make(level=5, dex=16, skills={"stealth": "expertise", "acrobatics": "proficient"})
    assert skill_modifier(c, "stealth") == 3 + 6
    assert skill_modifier(c, "acrobatics") == 3 + 3
    assert skill_modifier(c, "sleight of hand") == 3  # name normalized, unproficient
    assert skill_modifier(c, "athletics", ability="dex") == 3  # ability override


def test_condition_modifiers():
    c = make(str=14, conditions=[Condition("tired", {"all_checks": -1}),
                                 Condition("hurt arm", {"abilities": {"str": -2}}),
                                 Condition("muddy", {"skills": {"stealth": -3}})])
    assert skill_modifier(c, "athletics") == 2 - 1 - 2
    assert skill_modifier(c, "stealth") == 0 - 1 - 3


def test_auto_success_when_proficient_and_take_10_covers_dc():
    c = make(level=3, skills={"athletics": "proficient"})  # +2
    r = resolve_skill_check(c, "athletics", 12, roller=never())
    assert r.success and r.auto_success and r.roll is None and r.outcome == "success"


def test_no_auto_success_without_proficiency():
    c = make(str=14)  # +2 but not proficient
    r = resolve_skill_check(c, "athletics", 12, roller=fixed(5))
    assert not r.auto_success and r.roll == 5 and r.total == 7 and not r.success
    assert r.margin == -5


def test_no_auto_success_above_max_dc():
    c = make(level=17, str=20, skills={"athletics": "expertise"})  # +17
    r = resolve_skill_check(c, "athletics", rules.AUTO_SUCCESS_MAX_DC + 1, roller=fixed(1))
    assert not r.auto_success and r.roll == 1 and r.success  # 1 + 17 >= 16


def test_impossible_fails_without_roll():
    c = make(str=8)  # -1 ; DC 18 needs a 19
    r = resolve_skill_check(c, "athletics", 18, roller=never())
    assert r.impossible and not r.success and r.roll is None and r.consequences == []


def test_boundary_needed_18_still_rolls():
    c = make(str=8)  # -1 ; DC 17 needs an 18
    r = resolve_skill_check(c, "athletics", 17, roller=fixed(18))
    assert not r.impossible and r.success


def test_natural_20_does_not_override_dc(monkeypatch):
    monkeypatch.setattr(rules, "IMPOSSIBLE_NEEDED_ROLL", 99)  # disable filter
    c = make(str=6)  # -2
    r = resolve_skill_check(c, "athletics", 25, roller=fixed(20))
    assert r.roll == 20 and r.total == 18 and not r.success


def test_natural_1_does_not_auto_fail():
    c = make(str=18)  # +4, not proficient
    r = resolve_skill_check(c, "athletics", 5, roller=fixed(1))
    assert r.success


def test_last_ditch_success_applies_strain():
    c = make(str=8)  # -1, DC 18 is impossible normally
    r = resolve_skill_check(c, "athletics", 18, last_ditch=True, roller=fixed(15))
    assert r.last_ditch and r.success and r.bonus == rules.DESPERATION_BONUS
    assert r.total == 15 - 1 + 5
    [cost] = r.consequences
    assert cost["type"] == "condition" and cost["lasting"] and cost["name"] == "strained_str"
    assert cost["effect"] == {"abilities": {"str": -1}}


def test_last_ditch_failure_applies_injury():
    c = make(str=8)
    r = resolve_skill_check(c, "athletics", 18, last_ditch=True, roller=fixed(3))
    assert r.last_ditch and not r.success
    [cost] = r.consequences
    assert cost["name"] == "injured_str" and cost["lasting"] and cost["effect"]["abilities"]["str"] == -2
    assert r.to_dict()["injury_applied"]["name"] == "injured_str"


def test_last_ditch_beyond_desperation_fails_without_roll_but_costs():
    c = make(str=8)  # max 20 - 1 + 5 = 24
    r = resolve_skill_check(c, "athletics", 25, last_ditch=True, roller=never())
    assert r.impossible and r.last_ditch and not r.success and r.roll is None
    assert r.consequences[0]["name"] == "injured_str"


def test_last_ditch_ignored_on_auto_success():
    c = make(level=3, skills={"athletics": "proficient"})
    r = resolve_skill_check(c, "athletics", 10, last_ditch=True, roller=never())
    assert r.auto_success and not r.last_ditch and r.consequences == []


def test_injected_rng_is_deterministic():
    c = make(dex=14)
    a = resolve_skill_check(c, "stealth", 14, rng=random.Random(42))
    b = resolve_skill_check(c, "stealth", 14, rng=random.Random(42))
    assert a == b


@pytest.mark.parametrize("name", ["str", "STR", "strength"])
def test_raw_ability_check(name):
    c = make(level=5, str=16, skills={"athletics": "expertise"})
    r = resolve_skill_check(c, name, 12, roller=fixed(9))
    assert r.skill is None and r.ability == "str" and r.modifier == 3  # no PB on raw checks
    assert not r.auto_success and r.success and r.total == 12
    assert skill_modifier(c, name) == 3


def test_raw_ability_last_ditch_cost():
    r = resolve_skill_check(make(con=8), "con", 22, last_ditch=True, roller=fixed(2))
    assert r.consequences[0]["name"] == "injured_con" and r.consequences[0]["source"] == "last_ditch:con"


@pytest.mark.parametrize("roll,outcome,degree", [
    (10, "success", "narrow"),   # margin 0
    (14, "success", "narrow"),   # +4
    (15, "success", "clear"),    # +5
    (20, "success", "overwhelming"),  # +10 (mod 0 vs DC 10)
    (9, "failure", "narrow"),    # -1
    (5, "failure", "clear"),     # -5
])
def test_degree_bands(roll, outcome, degree):
    r = resolve_skill_check(make(), "athletics", 10, roller=fixed(roll))
    assert (r.outcome, r.degree) == (outcome, degree)


def test_degree_failure_overwhelming():
    r = resolve_skill_check(make(str=20), "athletics", 18, roller=fixed(3))  # 3+5-18 = -10
    assert r.degree == "overwhelming"


def test_degree_null_without_roll():
    auto = resolve_skill_check(make(level=3, skills={"athletics": "proficient"}), "athletics", 10)
    imp = resolve_skill_check(make(), "athletics", 25, roller=never())
    ld_imp = resolve_skill_check(make(), "athletics", 40, last_ditch=True, roller=never())
    assert auto.degree is None and imp.degree is None and ld_imp.degree is None
    assert auto.to_narration_dict()["degree"] is None
    assert auto.auto_success and imp.impossible and ld_imp.impossible and ld_imp.last_ditch


def test_resolve_attack_uses_pb_when_proficient_and_never_auto():
    c = make(level=5, dex=16)  # +3, PB +3
    r = rules.resolve_attack(c, "dex", 8, proficient=True, roller=fixed(1))
    assert r.modifier == 6 and not r.auto_success and r.roll == 1 and not r.success  # 7 < 8
    r = rules.resolve_attack(c, "dex", 8, proficient=False, roller=fixed(5))
    assert r.modifier == 3 and r.success


def test_resolve_attack_impossible_and_last_ditch():
    c = make(str=8)
    r = rules.resolve_attack(c, "str", 25, proficient=False, roller=never())
    assert r.impossible and r.degree is None
    r = rules.resolve_attack(c, "str", 20, proficient=False, last_ditch=True, roller=fixed(16))
    assert r.success and r.consequences[0]["name"] == "strained_str"


@pytest.mark.parametrize("expr,lo,hi", [("1d6", 1, 6), ("2d4+2", 4, 10), ("d8", 1, 8),
                                        ("1d4-1", 0, 3), ("3", 3, 3), ("1 fire", 1, 1), (2, 2, 2)])
def test_roll_dice(expr, lo, hi):
    rng = random.Random(0)
    vals = {rules.roll_dice(expr, rng) for _ in range(200)}
    assert min(vals) >= lo and max(vals) <= hi
    if lo != hi:
        assert min(vals) == lo and max(vals) == hi


def test_roll_dice_rejects_garbage():
    with pytest.raises(ValueError):
        rules.roll_dice("lots")


@pytest.mark.parametrize("v,band", [(-100, "hostile"), (-50, "hostile"), (-49, "unfriendly"),
                                    (-10, "unfriendly"), (-9, "neutral"), (0, "neutral"),
                                    (10, "friendly"), (49, "friendly"), (50, "allied")])
def test_disposition_band(v, band):
    assert rules.disposition_band(v) == band


HIDDEN = {"roll", "dc", "modifier", "bonus", "total", "margin", "needed_roll"}


def test_narration_view_strips_numbers():
    r = resolve_skill_check(make(str=8), "athletics", 18, last_ditch=True, roller=fixed(3))
    n = r.to_narration_dict(state_changes=[{"type": "flag_set", "key": "k", "value": True}])
    assert not HIDDEN & set(n)
    assert n["outcome"] == "failure" and n["degree"] in ("narrow", "clear", "overwhelming")
    assert n["reason"] == "last_ditch" and n["last_ditch"] is True
    assert n["consequences"] == [{"type": "condition", "name": "injured_str",
                                  "description": "Hurt yourself in a desperate attempt that failed",
                                  "lasting": True, "severity": 2}]
    assert n["state_changes"][0]["type"] == "flag_set"
    assert HIDDEN <= set(r.to_dict())  # full numbers still available for the debug log


def test_debug_log_gets_numbers(caplog):
    r = resolve_skill_check(make(), "athletics", 10, roller=fixed(12))
    with caplog.at_level("DEBUG", logger="rpg.debug"):
        r.log_debug(turn=3)
    assert "'roll': 12" in caplog.text and "'turn': 3" in caplog.text


@pytest.mark.parametrize("hp,band", [(20, "unhurt"), (19, "scratched"), (15, "scratched"),
                                     (14, "wounded"), (8, "wounded"), (7, "badly_wounded"),
                                     (3, "badly_wounded"), (2, "near_death"), (1, "near_death"),
                                     (0, "down"), (-4, "down")])
def test_hp_band(hp, band):
    assert rules.hp_band(hp, 20) == band


def test_unknown_skill_rejected():
    with pytest.raises(ValueError):
        resolve_skill_check(make(), "flying", 10)


def test_advantage_keeps_higher_and_disadvantage_lower():
    c = make()
    r = resolve_skill_check(c, "athletics", 10, roller=fixed(4, 15), advantage=True)
    assert r.roll == 15 and r.rolls == [4, 15] and r.advantage == 1 and r.success
    r = resolve_skill_check(c, "athletics", 10, roller=fixed(15, 4), disadvantage=True)
    assert r.roll == 4 and r.advantage == -1 and not r.success
    r = resolve_skill_check(c, "athletics", 10, roller=fixed(7), advantage=True, disadvantage=True)
    assert r.roll == 7 and r.rolls == [7] and r.advantage == 0  # cancel


def test_condition_grants_advantage_and_disadvantage():
    braced = Condition("braced", {"advantage": ["str", "con"], "disadvantage": ["wis"]})
    c = make(conditions=[braced])
    assert resolve_skill_check(c, "athletics", 10, roller=fixed(3, 12)).roll == 12
    assert resolve_skill_check(c, "con", 10, roller=fixed(3, 12)).roll == 12
    assert resolve_skill_check(c, "insight", 10, roller=fixed(12, 3)).roll == 3
    assert resolve_skill_check(c, "stealth", 10, roller=fixed(12)).rolls == [12]
    assert rules.resolve_attack(c, "str", 10, proficient=False, roller=fixed(2, 18)).roll == 18
    # condition advantage + external disadvantage cancel
    assert resolve_skill_check(c, "athletics", 10, roller=fixed(5), disadvantage=True).rolls == [5]


def test_advantage_not_rolled_when_auto_or_impossible():
    c = make(level=3, skills={"athletics": "proficient"})
    assert resolve_skill_check(c, "athletics", 10, advantage=True, roller=never()).auto_success
    assert resolve_skill_check(make(), "athletics", 25, advantage=True, roller=never()).impossible


def test_advantage_numbers_stay_out_of_narration():
    r = resolve_skill_check(make(), "athletics", 10, roller=fixed(4, 15), advantage=True)
    n = r.to_narration_dict()
    assert "rolls" not in n and "advantage" not in n


@pytest.mark.parametrize("text,turns", [("1 round", 1), ("10 minutes or 1 check", 10), ("1 hour", 60),
                                        ("2 hours", 120), (None, None), ("until dawn", None), (3, 3)])
def test_duration_turns(text, turns):
    assert rules.duration_turns(text) == turns


def _eq(slot, **stats):
    return {"slot": slot, "stats": stats}


@pytest.mark.parametrize("dex,equipped,ac", [
    (14, [], 12),                                                         # 10 + 2
    (14, [_eq("body", base_ac=11, dex_cap=None)], 13),                    # leather
    (18, [_eq("body", base_ac=12, dex_cap=2)], 14),                       # hide, capped
    (14, [_eq("body", base_ac=16, dex_cap=0)], 16),                       # chain
    (14, [_eq("body", base_ac=11, dex_cap=None), _eq("off_hand", ac_bonus=1)], 14),  # + buckler
    (8, [_eq("feet", ac_bonus=0), _eq("main_hand", damage="1d6")], 9),    # accessories/weapons add nothing
])
def test_armor_defense(dex, equipped, ac):
    assert rules.armor_defense(dex, equipped) == ac

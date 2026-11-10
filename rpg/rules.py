"""Pure D&D 5e-style check math. No I/O; the DB layer lives in rpg.db.

House rules (from DESIGN.md), all tunable via the constants below:

* Auto-success: the character is proficient (or has expertise) in the skill,
  DC <= AUTO_SUCCESS_MAX_DC, and 10 + modifier >= DC ("take 10").
* Impossibility filter: if the d20 roll needed (DC - modifier) is above
  IMPOSSIBLE_NEEDED_ROLL, the action fails without a roll. With the default 18
  anything that only a 19 or 20 could pass is impossible, so a natural 20
  never rescues a hopeless action.
* Last-ditch effort (player explicitly goes all out; ignored if the check is an
  auto-success): bypasses the impossibility filter, adds DESPERATION_BONUS to
  the roll, and ALWAYS costs a lasting condition on the ability used:
  success -> "strained_<ability>" (-1 to that ability's checks),
  failure -> "injured_<ability>" (-2). If even 20 + modifier + bonus < DC it
  fails without a roll, but the injury still applies (you hurt yourself trying).
* Natural 20 / natural 1 have no special meaning: total >= DC succeeds.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional

AUTO_SUCCESS_MAX_DC = 15
IMPOSSIBLE_NEEDED_ROLL = 18
DESPERATION_BONUS = 5

ABILITIES = ("str", "dex", "con", "int", "wis", "cha")

SKILL_ABILITY: Dict[str, str] = {
    "acrobatics": "dex", "animal_handling": "wis", "arcana": "int",
    "athletics": "str", "deception": "cha", "history": "int",
    "insight": "wis", "intimidation": "cha", "investigation": "int",
    "medicine": "wis", "nature": "int", "perception": "wis",
    "performance": "cha", "persuasion": "cha", "religion": "int",
    "sleight_of_hand": "dex", "stealth": "dex", "survival": "wis",
}

Roller = Callable[[], int]


def normalize_skill(skill: str) -> str:
    return skill.strip().lower().replace(" ", "_").replace("-", "_")


def normalize_ability(ability: str) -> str:
    a = ability.strip().lower()[:3]
    if a not in ABILITIES:
        raise ValueError(f"Unknown ability {ability!r}")
    return a


def ability_modifier(score: int) -> int:
    return (score - 10) // 2  # floor division == floor((score-10)/2)


def proficiency_bonus(level: int) -> int:
    if not 1 <= level <= 20:
        raise ValueError(f"Level must be 1-20, got {level}")
    return 2 + (level - 1) // 4  # 1-4:+2, 5-8:+3, 9-12:+4, 13-16:+5, 17-20:+6


@dataclass
class Condition:
    name: str
    effect: Dict = field(default_factory=dict)  # {"all_checks": n, "abilities": {..}, "skills": {..}}
    lasting: bool = False
    duration_turns: Optional[int] = None
    source: Optional[str] = None
    description: Optional[str] = None
    severity: int = 1

    def check_modifier(self, skill: Optional[str], ability: str) -> int:
        e = self.effect or {}
        mod = int(e.get("all_checks", 0))
        mod += int((e.get("abilities") or {}).get(ability, 0))
        if skill:
            mod += int((e.get("skills") or {}).get(skill, 0))
        return mod


@dataclass
class Character:
    name: str
    level: int = 1
    scores: Dict[str, int] = field(default_factory=lambda: {a: 10 for a in ABILITIES})
    skills: Dict[str, str] = field(default_factory=dict)  # skill -> "proficient" | "expertise"
    conditions: List[Condition] = field(default_factory=list)
    id: Optional[int] = None

    def proficiency(self, skill: str) -> Optional[str]:
        return self.skills.get(normalize_skill(skill))


def skill_modifier(char: Character, skill: str, ability: Optional[str] = None) -> int:
    """Ability mod + PB (proficient) or 2*PB (expertise) + condition modifiers."""
    skill = normalize_skill(skill)
    ability = normalize_ability(ability) if ability else SKILL_ABILITY[skill]
    mod = ability_modifier(char.scores[ability])
    prof = char.proficiency(skill)
    if prof == "expertise":
        mod += 2 * proficiency_bonus(char.level)
    elif prof == "proficient":
        mod += proficiency_bonus(char.level)
    mod += sum(c.check_modifier(skill, ability) for c in char.conditions)
    return mod


@dataclass
class CheckResult:
    skill: str
    ability: str
    dc: int
    success: bool
    auto_success: bool
    impossible: bool
    last_ditch: bool
    roll: Optional[int]
    modifier: int
    bonus: int
    total: Optional[int]
    margin: Optional[int]
    needed_roll: int  # d20 face needed to succeed (after modifier and bonus)
    reason: str
    consequences: List[Dict] = field(default_factory=list)  # e.g. [{"type": "condition", ...}]

    @property
    def outcome(self) -> str:
        return "success" if self.success else "failure"

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["outcome"] = self.outcome
        conds = [c for c in self.consequences if c.get("type") == "condition"]
        d["injury_applied"] = conds[0] if conds else None  # starter-compatible key
        return d


def last_ditch_cost(ability: str, success: bool, skill: str) -> Condition:
    if success:
        return Condition(
            name=f"strained_{ability}", effect={"abilities": {ability: -1}}, lasting=True,
            severity=1, source=f"last_ditch:{skill}",
            description="Pushed past your limits and strained yourself",
        )
    return Condition(
        name=f"injured_{ability}", effect={"abilities": {ability: -2}}, lasting=True,
        severity=2, source=f"last_ditch:{skill}",
        description="Hurt yourself in a desperate attempt that failed",
    )


def _default_roller(rng: Optional[random.Random]) -> Roller:
    r = rng or random.Random()
    return lambda: r.randint(1, 20)


def resolve_skill_check(
    char: Character,
    skill: str,
    dc: int,
    *,
    last_ditch: bool = False,
    ability: Optional[str] = None,
    roller: Optional[Roller] = None,
    rng: Optional[random.Random] = None,
) -> CheckResult:
    """Resolve a check. Pure: returns consequences, does not persist them."""
    skill = normalize_skill(skill)
    if skill not in SKILL_ABILITY:
        raise ValueError(f"Unknown skill {skill!r}")
    ability = normalize_ability(ability) if ability else SKILL_ABILITY[skill]
    mod = skill_modifier(char, skill, ability)
    needed = dc - mod  # d20 result needed on a normal roll
    roll_d20 = roller or _default_roller(rng)

    def result(success, *, auto=False, impossible=False, ld=False, roll=None,
               bonus=0, reason="", consequences=()):
        total = None if roll is None else roll + mod + bonus
        return CheckResult(
            skill=skill, ability=ability, dc=dc, success=success, auto_success=auto,
            impossible=impossible, last_ditch=ld, roll=roll, modifier=mod, bonus=bonus,
            total=total, margin=None if total is None else total - dc,
            needed_roll=dc - mod - bonus, reason=reason, consequences=list(consequences),
        )

    # 1. Auto-success: proficient, reasonable DC, and taking 10 clears it.
    if char.proficiency(skill) and dc <= AUTO_SUCCESS_MAX_DC and 10 + mod >= dc:
        return result(True, auto=True, reason="auto_success")

    # 2. Last-ditch: desperation bonus, always costs a lasting condition.
    if last_ditch:
        bonus = DESPERATION_BONUS
        if 20 + mod + bonus < dc:
            cost = last_ditch_cost(ability, False, skill)
            return result(False, impossible=True, ld=True, bonus=bonus,
                          reason="impossible_even_last_ditch",
                          consequences=[{"type": "condition", **asdict(cost)}])
        roll = roll_d20()
        success = roll + mod + bonus >= dc
        cost = last_ditch_cost(ability, success, skill)
        return result(success, ld=True, roll=roll, bonus=bonus, reason="last_ditch",
                      consequences=[{"type": "condition", **asdict(cost)}])

    # 3. Impossibility filter.
    if needed > IMPOSSIBLE_NEEDED_ROLL:
        return result(False, impossible=True, reason="impossible")

    # 4. Normal roll. No natural-20/natural-1 special cases.
    roll = roll_d20()
    return result(roll + mod >= dc, roll=roll, reason="rolled")

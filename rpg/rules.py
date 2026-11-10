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
* Raw ability checks: pass an ability ('str', 'dex', ..., or 'strength') as
  the skill. No proficiency applies, so they never auto-succeed.
* Degree band (narration): |margin| 0-4 narrow, 5-9 clear, 10+ overwhelming.
  null whenever nothing was rolled (auto-success, impossible without a roll).
* Advantage / disadvantage (5e): roll 2d20, keep the higher / lower. Any source
  of each cancels out to a single straight roll. Sources: conditions
  (effect "advantage"/"disadvantage": lists of abilities or skills), tools
  (skill_bonus grants: advantage), darkness (perception/investigation).
* Attacks (resolve_attack): same pipeline vs the target's defense DC, with
  modifier = STR/DEX mod (+ PB if proficient) + conditions; never auto-succeed.
"""
from __future__ import annotations

import logging
import random
import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

AUTO_SUCCESS_MAX_DC = 15
IMPOSSIBLE_NEEDED_ROLL = 18
DESPERATION_BONUS = 5
DEGREE_CLEAR_MARGIN = 5
DEGREE_OVERWHELMING_MARGIN = 10

debug_log = logging.getLogger("rpg.debug")  # check numbers go here, never to the narrator

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


_ABILITY_NAMES = {"strength": "str", "dexterity": "dex", "constitution": "con",
                  "intelligence": "int", "wisdom": "wis", "charisma": "cha"}


def as_ability(name: str) -> Optional[str]:
    """'str' / 'STR' / 'strength' -> 'str'; anything else -> None."""
    a = name.strip().lower()
    a = _ABILITY_NAMES.get(a, a)
    return a if a in ABILITIES else None


def normalize_ability(ability: str) -> str:
    a = as_ability(ability)
    if a is None:
        raise ValueError(f"Unknown ability {ability!r}")
    return a


def split_check(skill_or_ability: str, ability: Optional[str] = None):
    """Return (skill or None, ability) for a skill name or a raw ability abbreviation."""
    raw = as_ability(skill_or_ability)
    if raw is not None:
        return None, raw
    skill = normalize_skill(skill_or_ability)
    if skill not in SKILL_ABILITY:
        raise ValueError(f"Unknown skill or ability {skill_or_ability!r}")
    return skill, normalize_ability(ability) if ability else SKILL_ABILITY[skill]


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

    def grants(self, kind: str, skill: Optional[str], ability: str) -> bool:
        """kind = 'advantage' | 'disadvantage'; matches an ability or skill listed in the effect."""
        wanted = (self.effect or {}).get(kind) or []
        return ability in wanted or (skill is not None and skill in wanted) or "all" in wanted

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
    weapon_proficiencies: List[str] = field(default_factory=lambda: ["simple"])

    def proficiency(self, skill: Optional[str]) -> Optional[str]:
        return self.skills.get(normalize_skill(skill)) if skill else None


def skill_modifier(char: Character, skill: str, ability: Optional[str] = None) -> int:
    """Ability mod + PB (proficient) or 2*PB (expertise) + condition modifiers.
    `skill` may be a raw ability ('str') for a plain ability check."""
    skill, ability = split_check(skill, ability)
    mod = ability_modifier(char.scores[ability])
    prof = char.proficiency(skill)
    if prof == "expertise":
        mod += 2 * proficiency_bonus(char.level)
    elif prof == "proficient":
        mod += proficiency_bonus(char.level)
    mod += sum(c.check_modifier(skill, ability) for c in char.conditions)
    return mod


def degree_band(margin: Optional[int]) -> str:
    m = abs(margin)
    if m >= DEGREE_OVERWHELMING_MARGIN:
        return "overwhelming"
    return "clear" if m >= DEGREE_CLEAR_MARGIN else "narrow"


HP_BANDS = ("unhurt", "scratched", "wounded", "badly_wounded", "near_death", "down")


def hp_band(current_hp: int, max_hp: int) -> str:
    """unhurt = full; scratched >= 75%; wounded >= 40%; badly_wounded >= 15%;
    near_death > 0; down <= 0."""
    if current_hp <= 0:
        return "down"
    if current_hp >= max_hp:
        return "unhurt"
    frac = current_hp / max_hp
    if frac >= 0.75:
        return "scratched"
    if frac >= 0.40:
        return "wounded"
    return "badly_wounded" if frac >= 0.15 else "near_death"


NARRATION_CONSEQUENCE_FIELDS = ("type", "name", "description", "lasting", "severity")


@dataclass
class CheckResult:
    skill: Optional[str]  # None for a raw ability check
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
    advantage: int = 0  # +1 advantage, -1 disadvantage (debug only)
    rolls: List[int] = field(default_factory=list)  # all d20s rolled (debug only)

    @property
    def outcome(self) -> str:
        return "success" if self.success else "failure"

    @property
    def degree(self) -> Optional[str]:
        return None if self.margin is None else degree_band(self.margin)  # None = no roll

    def to_narration_dict(self, state_changes: Optional[List[Dict]] = None) -> Dict:
        """Narrator-safe view: no roll/dc/modifier/bonus/total/margin/needed_roll."""
        return {
            "skill": self.skill,
            "ability": self.ability,
            "outcome": self.outcome,
            "degree": self.degree,
            "reason": self.reason,
            "auto_success": self.auto_success,
            "impossible": self.impossible,
            "last_ditch": self.last_ditch,
            "consequences": [{k: c[k] for k in NARRATION_CONSEQUENCE_FIELDS if k in c}
                             for c in self.consequences],
            "state_changes": list(state_changes or []),
        }

    def log_debug(self, **context) -> None:
        keys = ("skill", "ability", "dc", "roll", "rolls", "advantage", "modifier", "bonus", "total", "margin",
                "needed_roll", "reason")
        info = {k: getattr(self, k) for k in keys}
        info["costs"] = [c.get("name") for c in self.consequences]
        debug_log.debug("check %s", {**info, **context})

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["outcome"] = self.outcome
        d["degree"] = self.degree
        conds = [c for c in self.consequences if c.get("type") == "condition"]
        d["injury_applied"] = conds[0] if conds else None  # starter-compatible key
        return d


def last_ditch_cost(ability: str, success: bool, skill: Optional[str]) -> Condition:
    if success:
        return Condition(
            name=f"strained_{ability}", effect={"abilities": {ability: -1}}, lasting=True,
            severity=1, source=f"last_ditch:{skill or ability}",
            description="Pushed past your limits and strained yourself",
        )
    return Condition(
        name=f"injured_{ability}", effect={"abilities": {ability: -2}}, lasting=True,
        severity=2, source=f"last_ditch:{skill or ability}",
        description="Hurt yourself in a desperate attempt that failed",
    )


def _default_roller(rng: Optional[random.Random]) -> Roller:
    r = rng or random.Random()
    return lambda: r.randint(1, 20)


def net_advantage(advantage: bool, disadvantage: bool) -> int:
    """+1 advantage, -1 disadvantage, 0 straight (both cancel)."""
    return int(advantage) - int(disadvantage)


def condition_advantage(char: Character, skill: Optional[str], ability: str) -> Tuple[bool, bool]:
    adv = any(c.grants("advantage", skill, ability) for c in char.conditions)
    dis = any(c.grants("disadvantage", skill, ability) for c in char.conditions)
    return adv, dis


def resolve_skill_check(
    char: Character,
    skill: str,
    dc: int,
    *,
    last_ditch: bool = False,
    ability: Optional[str] = None,
    roller: Optional[Roller] = None,
    rng: Optional[random.Random] = None,
    advantage: bool = False,
    disadvantage: bool = False,
) -> CheckResult:
    """Resolve a check. `skill` is a 5e skill name or a raw ability ('str').
    Condition advantage/disadvantage is included automatically; pass extra
    sources (tools, darkness) via advantage/disadvantage. Pure."""
    skill, ability = split_check(skill, ability)
    mod = skill_modifier(char, skill or ability, ability)
    cadv, cdis = condition_advantage(char, skill, ability)
    return resolve_check(skill, ability, dc, mod, can_auto=bool(char.proficiency(skill)),
                         last_ditch=last_ditch, roller=roller or _default_roller(rng),
                         adv=net_advantage(advantage or cadv, disadvantage or cdis))


def resolve_attack(
    char: Character,
    ability: str,
    defense_dc: int,
    *,
    proficient: bool,
    last_ditch: bool = False,
    roller: Optional[Roller] = None,
    rng: Optional[random.Random] = None,
    advantage: bool = False,
    disadvantage: bool = False,
) -> CheckResult:
    """Attack check vs a defense DC. Same impossibility/last-ditch rules; no auto-success."""
    ability = normalize_ability(ability)
    mod = skill_modifier(char, ability) + (proficiency_bonus(char.level) if proficient else 0)
    cadv, cdis = condition_advantage(char, None, ability)
    return resolve_check(None, ability, defense_dc, mod, can_auto=False,
                         last_ditch=last_ditch, roller=roller or _default_roller(rng),
                         adv=net_advantage(advantage or cadv, disadvantage or cdis))


def resolve_check(skill: Optional[str], ability: str, dc: int, mod: int, *, can_auto: bool,
                  last_ditch: bool, roller: Roller, adv: int = 0) -> CheckResult:
    """Core rules pipeline shared by skill checks and attacks."""
    needed = dc - mod  # d20 result needed on a normal roll
    rolls: List[int] = []

    def d20() -> int:
        rolls.extend([roller()] + ([roller()] if adv else []))
        return max(rolls) if adv > 0 else min(rolls) if adv < 0 else rolls[0]

    def result(success, *, auto=False, impossible=False, ld=False, roll=None,
               bonus=0, reason="", consequences=()):
        total = None if roll is None else roll + mod + bonus
        return CheckResult(
            skill=skill, ability=ability, dc=dc, success=success, auto_success=auto,
            impossible=impossible, last_ditch=ld, roll=roll, modifier=mod, bonus=bonus,
            total=total, margin=None if total is None else total - dc,
            needed_roll=dc - mod - bonus, reason=reason, consequences=list(consequences),
            advantage=adv, rolls=list(rolls),
        )

    # 1. Auto-success: proficient, reasonable DC, and taking 10 clears it.
    if can_auto and dc <= AUTO_SUCCESS_MAX_DC and 10 + mod >= dc:
        return result(True, auto=True, reason="auto_success")

    # 2. Last-ditch: desperation bonus, always costs a lasting condition.
    if last_ditch:
        bonus = DESPERATION_BONUS
        if 20 + mod + bonus < dc:
            cost = last_ditch_cost(ability, False, skill)
            return result(False, impossible=True, ld=True, bonus=bonus,
                          reason="impossible_even_last_ditch",
                          consequences=[{"type": "condition", **asdict(cost)}])
        roll = d20()
        success = roll + mod + bonus >= dc
        cost = last_ditch_cost(ability, success, skill)
        return result(success, ld=True, roll=roll, bonus=bonus, reason="last_ditch",
                      consequences=[{"type": "condition", **asdict(cost)}])

    # 3. Impossibility filter.
    if needed > IMPOSSIBLE_NEEDED_ROLL:
        return result(False, impossible=True, reason="impossible")

    # 4. Normal roll. No natural-20/natural-1 special cases.
    roll = d20()
    return result(roll + mod >= dc, roll=roll, reason="rolled")


# ---------------------------------------------------------------------------
# Dice and bands
# ---------------------------------------------------------------------------

_DICE_RE = re.compile(r"^\s*(?:(\d*)d(\d+))?\s*([+-]\s*\d+)?\s*$")


def roll_dice(expr, rng: Optional[random.Random] = None) -> int:
    """Roll '2d4+2', '1d6', 'd8', '3', '1 fire' (leading number). Returns the total."""
    r = rng or random.Random()
    if isinstance(expr, int):
        return expr
    text = str(expr).strip()
    m = _DICE_RE.match(text)
    if not m or (m.group(2) is None and m.group(3) is None):
        lead = re.match(r"^\s*(\d+)", text)
        if not lead:
            raise ValueError(f"Bad dice expression {expr!r}")
        return int(lead.group(1))
    total = 0
    if m.group(2):
        n = int(m.group(1) or 1)
        total += sum(r.randint(1, int(m.group(2))) for _ in range(n))
    if m.group(3):
        total += int(m.group(3).replace(" ", ""))
    return total


def disposition_band(value: int) -> str:
    """hostile <= -50 < unfriendly <= -10 < neutral < 10 <= friendly < 50 <= allied."""
    if value <= -50:
        return "hostile"
    if value <= -10:
        return "unfriendly"
    if value < 10:
        return "neutral"
    return "friendly" if value < 50 else "allied"


_DURATION_RE = re.compile(r"(\d+)\s*(round|turn|minute|min|hour|hr)s?", re.I)


def duration_turns(text) -> Optional[int]:
    """'1 round' -> 1, '10 minutes or 1 check' -> 10, '1 hour' -> 60 (1 turn ~ 1 minute,
    a round counts as one turn). None/unparseable -> None."""
    if text is None:
        return None
    if isinstance(text, int):
        return text
    m = _DURATION_RE.search(str(text))
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    return n * 60 if unit in ("hour", "hr") else n


def armor_defense(dex_score: int, equipped: List[Dict]) -> int:
    """Player defense DC (5e AC): body armor with base_ac -> base_ac + DEX mod (capped by
    dex_cap; null = uncapped), else 10 + DEX mod; plus ac_bonus from every other equipped
    item (shields, accessories)."""
    dex = ability_modifier(dex_score)
    body = next((i for i in equipped if i.get("slot") == "body" and i["stats"].get("base_ac") is not None), None)
    if body:
        cap = body["stats"].get("dex_cap")
        base = body["stats"]["base_ac"] + (dex if cap is None else min(dex, cap))
    else:
        base = 10 + dex
    return base + sum(int(i["stats"].get("ac_bonus") or 0) for i in equipped if i is not body)

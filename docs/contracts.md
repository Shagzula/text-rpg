# JSON contracts (DRAFT v0)

Status: **draft** for M1 tasks (2) `process_action_proposal()` and (3) the CLI loop, and for
the Story Designer's interpreter/narrator prompts. Nothing here is implemented yet beyond
the check result fields that `rpg.rules.CheckResult.to_dict()` already returns.

Pipeline: player text -> **LLM interpreter** -> `ActionProposal` -> engine validates/resolves
-> `EngineResult` -> **LLM narrator** -> second-person prose ending "What do you do?".

Principle: the LLM *proposes*, the engine *decides*. The LLM never sets DCs, rolls, HP, or
inventory directly; it gives a difficulty estimate and the engine maps it to a DC.

## (a) Action proposal (LLM interpreter -> engine)

Field names follow the Story Designer's proposal.

```json
{
  "action_type": "skill_check",
  "intent_summary": "Throw my shoe at the lamp to knock it over and plunge the room into darkness",
  "skill": "sleight_of_hand",
  "ability": null,
  "target": {"type": "object", "id": null, "name": "hanging oil lamp"},
  "items_used": [{"inventory_id": 12, "name": "left boot"}],
  "est_difficulty": "medium",
  "last_ditch": false,
  "clarification_needed": false,
  "clarification_question": null
}
```

| field | type | notes |
|---|---|---|
| `action_type` | enum | `skill_check`, `move`, `use_item`, `talk`, `attack`, `look`, `inventory`, `rest`, `other` |
| `intent_summary` | string | one-sentence restatement of what the player is trying to achieve |
| `skill` | string \| null | one of the 18 5e skills, snake_case (`animal_handling`, `sleight_of_hand`); null if no check |
| `ability` | string \| null | *added*: optional override (`str`..`cha`) for off-label checks, e.g. athletics-by-DEX; null = skill default |
| `target` | object \| null | `type`: `npc` / `object` / `location` / `exit` / `self`; `id`: engine id if known; `name`: as referenced |
| `items_used` | array | each `{inventory_id, name}`; engine rejects items the character doesn't hold |
| `est_difficulty` | enum | `trivial` / `easy` / `medium` / `hard` / `very_hard` / `nearly_impossible` -> engine DC 5 / 10 / 15 / 20 / 25 / 30 (engine may adjust for context) |
| `last_ditch` | bool | true only if the player clearly goes all-out / desperate (risking injury) |
| `clarification_needed` | bool | true if the input is too ambiguous to resolve; engine skips resolution |
| `clarification_question` | string \| null | *added*: the question to ask the player when `clarification_needed` is true |

Legacy starter fields (`proposed_dc`, `is_last_ditch`, `success_outcome`, `failure_outcome`)
are accepted by the current `rpg_engine.process_action_proposal()` and will be replaced by
the fields above in task (2).

## (b) Engine result (engine -> LLM narrator)

```json
{
  "turn": 14,
  "action_type": "skill_check",
  "intent_summary": "Throw my shoe at the lamp to knock it over and plunge the room into darkness",
  "status": "resolved",
  "outcome": "failure",
  "check": {
    "skill": "athletics",
    "ability": "str",
    "dc": 18,
    "success": false,
    "auto_success": false,
    "impossible": false,
    "last_ditch": true,
    "roll": 3,
    "modifier": 2,
    "bonus": 5,
    "total": 10,
    "margin": -8,
    "needed_roll": 11,
    "reason": "last_ditch"
  },
  "state_changes": [
    {"type": "condition_added", "name": "injured_str", "lasting": true,
     "description": "Hurt yourself in a desperate attempt that failed"},
    {"type": "item_removed", "inventory_id": 12, "name": "left boot", "quantity": 1},
    {"type": "flag_set", "key": "tavern_lamp_lit", "value": true}
  ],
  "rejections": [],
  "narrative_constraints": {
    "must_mention": ["You hurt yourself and fail", "Hurt yourself in a desperate attempt that failed"],
    "must_not_mention": ["dice", "roll", "modifier", "DC", "skill check"],
    "tone_hint": "strained, painful"
  },
  "scene": {"location": "The Sunken Tankard", "visible_npcs": ["Mara"], "exits": ["north", "cellar door"]}
}
```

| field | notes |
|---|---|
| `status` | `resolved`, `rejected` (invalid item/target/action, see `rejections`), `needs_clarification` |
| `outcome` | `success` / `failure` / `none` (no check, e.g. `look`) |
| `check` | exactly `CheckResult.to_dict()` minus `consequences`/`injury_applied`; null if no check. Hidden from the player: the narrator uses it for tone only |
| `state_changes` | authoritative list of what the engine changed this turn; the narrator must not invent others |
| `rejections` | e.g. `[{"field": "items_used[0]", "reason": "not_in_inventory"}]` |
| `narrative_constraints` | `must_mention` always includes any applied injury; `must_not_mention` hides mechanics |
| `scene` | minimal context so narration stays grounded; ends with "What do you do?" |

## (c) Item template (engine data, matches `item_templates` table)

```json
{
  "id": "shortsword",
  "name": "Shortsword",
  "category": "weapon",
  "power_tier": 1,
  "stats": {"damage_dice": "1d6", "damage_type": "piercing", "properties": ["finesse", "light"], "value_gp": 10},
  "tags": ["blade", "martial", "common"],
  "description": "A plain, serviceable blade."
}
```

`id` is a stable slug (table PK). `category`: `weapon`, `armor`, `shield`, `tool`, `consumable`,
`container`, `misc`. `stats` is free-form JSON per category (weapons: `damage_dice`,
`damage_type`, `properties`; armor: `ac_base`, `dex_cap`, `stealth_disadvantage`;
consumables: `effect`, `uses`). LLM-invented items must name a template `id` and may only
add a `display_name`; they must fit the location's `area_tier`.

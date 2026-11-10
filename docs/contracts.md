# JSON contracts (DRAFT v0.2)

Status: **draft**, for M1 task (2) `process_action_proposal()` and task (3) the CLI loop, and for
the Story Designer's interpreter/narrator prompts. Aligned with
`/home/box/rpg-project/content/schemas_proposed.md` (Nov 10, 2026). If the two differ, this file wins.
Implemented today: `CheckResult.to_narration_dict()`, `rules.hp_band()`, the schema
(`rpg/schema.sql`). Everything else here is a spec for upcoming tasks.

Pipeline: player text -> **LLM interpreter** -> action proposal -> engine validates and resolves
-> engine result -> **LLM narrator** -> second-person prose ending "What do you do?".

Principles:
- The LLM proposes and the engine decides. The engine owns every DC; `est_difficulty` is a hint.
- The narrator never sees check numbers (`roll`, `dc`, `modifier`, `bonus`, `total`, `margin`,
  `needed_roll`). Those go only to the `rpg.debug` log (`CheckResult.log_debug()`).
- Entities are referenced by their **`key`** slug (every content table has a unique TEXT `key`
  besides its INTEGER `id`): `loc_town_square`, `npc_mara`, `fac_watch`, `wpn_short_blade`
  (template), `item_0003` (item instance). Exits are referenced by direction (`north`, `cellar door`).

## (a) Action proposal (interpreter -> engine)

```json
{
  "actions": [
    {
      "action_type": "skill_check",
      "skill": "athletics",
      "target": "exit:cellar door",
      "items_used": [],
      "last_ditch": true,
      "est_difficulty": "hard",
      "intent_summary": "Force the iron-banded cellar door open with your shoulder.",
      "clarification_needed": false,
      "clarification_question": null
    }
  ],
  "ooc": null,
  "unresolved_references": []
}
```

### Wrapper
| field | type | notes |
|---|---|---|
| `actions` | array, 1-3 items (max 3) | Resolved in order. The engine stops at the first action that fails validation or the first failed check that the later actions depend on (slice rule: stop at the first failure). More than 3 -> the engine keeps the first 3. |
| `ooc` | object or null | Out-of-character command, e.g. `{"command": "save"}`, `{"command": "quit"}`, `{"command": "help"}`. When set, `actions` is `[]` and nothing in the world resolves. |
| `unresolved_references` | array of strings | Things the player named that are not in the provided context ("my sword" when none is owned). Non-empty -> the engine may reject with `item_not_owned` / `target_not_present`, or ask. |

### Action object
| field | type | notes |
|---|---|---|
| `action_type` | enum | `move`, `examine`, `talk`, `attack`, `use_item`, `skill_check`, `take`, `drop`, `equip`, `unequip`, `inventory`, `rest`, `wait`, `other` |
| `skill` | string or null | A 5e skill in snake_case (`athletics`, `sleight_of_hand`, ... all 18), **or** a raw ability `str` / `dex` / `con` / `int` / `wis` / `cha` for a plain ability check (`resolve_skill_check` accepts both; raw checks get no proficiency and never auto-succeed), or null when no check is plausible. |
| `target` | string or null | A `key` from the provided context, `exit:<direction>` for exits/`move`, `self`, or null. Never an invented key. |
| `items_used` | array of strings | Item instance keys (`item_0003`) the character holds, or scene item keys for `take`. |
| `last_ditch` | bool | True only for a clear all-out / at-any-cost effort. |
| `est_difficulty` | enum | Hint only: `trivial` 5, `easy` 10, `medium` 15, `hard` 20, `very_hard` 25, `near_impossible` 30. The engine uses its own DC when the target has one (e.g. `location_exits.unlock_dc`). |
| `intent_summary` | string | One plain sentence. Used for logs and passed to the narrator. |
| `clarification_needed` | bool | True -> the engine asks instead of resolving. |
| `clarification_question` | string or null | Short in-voice question when `clarification_needed` is true. |

### Validation codes (engine `reason` when an action is rejected before any check)
| code | meaning |
|---|---|
| `item_not_owned` | an `items_used` key is not held/equipped by the character (or, for `take`, not in the scene) |
| `target_not_present` | `target` key is not in the current location / not a known exit |
| `item_unusable` | item exists but can't do this (wrong category/slot, consumed, two-handed while off-hand full, ...) |
| `out_of_reach` | target is present but not reachable this turn (across a chasm, on a high shelf, behind bars) |
| `impossible_action` | categorically impossible regardless of stats (flying unaided, lifting the building); no roll, and last-ditch doesn't help. Distinct from the stat-based `impossible` check reason. |

Rejected actions produce no state changes and no check. A malformed proposal (bad JSON, unknown
`action_type`) is not narrated; the engine re-asks the interpreter.

## (b) Engine result (engine -> narrator)

```json
{
  "turn": 42,
  "results": [
    {
      "action_type": "skill_check",
      "intent_summary": "Force the iron-banded cellar door open with your shoulder.",
      "target": {"key": "exit:cellar door", "name": "iron-banded door"},
      "items_used": [],
      "status": "resolved",
      "skill": "athletics",
      "ability": "str",
      "outcome": "success",
      "degree": "narrow",
      "reason": "last_ditch",
      "auto_success": false,
      "impossible": false,
      "last_ditch": true,
      "consequences": [
        {"type": "condition", "name": "strained_str", "lasting": true, "severity": 1,
         "description": "Pushed past your limits and strained yourself"}
      ],
      "state_changes": [
        {"type": "exit_unlocked", "key": "exit:cellar door", "name": "iron-banded door"},
        {"type": "flag_set", "key": "cellar_door_broken", "value": true}
      ]
    }
  ],
  "player_status": {"hp_band": "scratched", "conditions": ["strained_str"]},
  "dialogue": [],
  "defeat": null,
  "clarification_question": null
}
```

Each `results[]` entry is the action's echo (`action_type`, `intent_summary`, `target`,
`items_used`) plus `status`, merged with `CheckResult.to_narration_dict(state_changes)` (the
fields from `skill` to `state_changes`), which exists today.

| field | notes |
|---|---|
| `status` | `resolved`, `rejected` (then `reason` is a validation code and there is no check), `needs_clarification` |
| `outcome` | `success` / `failure`. Actions with no check (`examine`, `inventory`) are `success`. |
| `degree` | `narrow` (margin 0-4), `clear` (5-9, and all auto-successes), `overwhelming` (10+, and impossible-without-roll failures). Same bands for success and failure. Lets the narrator scale prose; no crits. |
| `reason` | Check reasons: `auto_success`, `rolled`, `impossible`, `last_ditch`, `impossible_even_last_ditch`. Or a validation code from (a). |
| `auto_success` / `impossible` / `last_ditch` | bools from `CheckResult`. A last-ditch attempt can be `impossible: true` and still carry an injury. |
| `consequences` | Side effects on the actor: `condition` (`name`, `description`, `lasting`, `severity`; `effect` stripped). Planned: `condition_removed`, `damage_taken` / `damage_dealt` with `amount_band` `light` / `moderate` / `heavy`, `item_broken`, `item_consumed`, `noise_made`, `npc_attitude_changed`. Every entry has a `description`. |
| `state_changes` | Authoritative world changes: `item_added`, `item_removed`, `item_equipped`, `item_unequipped`, `moved_to_location`, `exit_unlocked`, `exit_revealed`, `entity_state`, `npc_defeated`, `npc_fled`, `flag_set`. The narrator describes only these. |
| `player_status.hp_band` | `unhurt` (full), `scratched` (>= 75%), `wounded` (>= 40%), `badly_wounded` (>= 15%), `near_death` (> 0), `down` (<= 0). From `rules.hp_band(current, max)`; never raw HP. |
| `dialogue` | `[{"speaker_key", "speaker_name", "line", "source": "authored" \| "npc_llm"}]`. The narrator voices only these lines. |
| `defeat` | null or `{"type": "death" \| "knocked_out" \| "captured", "description": "..."}` |
| `clarification_question` | If set, the narrator asks it in-voice and resolves nothing. |

Debug-only (never sent to the narrator): `CheckResult.to_dict()` = the narration fields plus
`roll`, `dc`, `modifier`, `bonus`, `total`, `margin`, `needed_roll`, full `consequences`
(with `effect`), and `injury_applied` (starter-compatible).

## (c) Item template (content data -> `item_templates` table)

```json
{
  "id": "wpn_short_blade",
  "name": "Short Blade",
  "category": "weapon",
  "subcategory": "simple_melee",
  "power_tier": 1,
  "slot": "main_hand",
  "value": 10,
  "weight": 2,
  "stats": {"damage": "1d6", "damage_type": "piercing", "properties": ["finesse", "light"]},
  "tags": ["blade", "concealable"],
  "notes": "Default finesse sidearm."
}
```

Loaded with `rpg.db.add_item_template()` (all 35 entries of `content/items_tier1.yaml` load cleanly):

| content field | column | notes |
|---|---|---|
| `id` | `key` (TEXT UNIQUE) | the table also has an INTEGER `id` PK used by FKs |
| `name`, `power_tier` | same | |
| `category` | `category` | CHECK: `weapon`, `armor`, `shield`, `ammo`, `consumable`, `tool`, `utility`, `misc` |
| `slot` | `slot` | CHECK: `main_hand`, `off_hand`, `two_hands`, `body`, `head`, `hands`, `feet`, `belt`, `none` (default) |
| `weight` | `weight` REAL | lb-equivalent |
| `value` | `value` INTEGER | base currency units |
| `subcategory` | `stats.subcategory` | everything else category-specific also lives in `stats` |
| `stats`, `tags` | JSON columns | per-category `stats` keys as in schemas_proposed.md (c) |
| `notes` | `description` | |

Item instances live in `items` (`key` like `item_0003`, `template_id`, exactly one of
`character_id` / `location_id`, `display_name`, `quantity`, `equipped`). The `inventory` view =
items held by characters. LLM-invented items must name a template key, may only add a
`display_name`, and must fit the location's `area_tier`.

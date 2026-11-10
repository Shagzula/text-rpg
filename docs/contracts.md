# JSON contracts (v0.3)

Status: **implemented** in `rpg/actions.py` (`process_action_proposal`), `rpg/context.py`
(`build_context`) and `rpg/rules.py`. Aligned with the Story Designer's drafts in
`/home/box/rpg-project/content/` (`schemas_proposed.md`, `interpreter_prompt.md`, `narrator_prompt.md`).
Where v0.2 differed, the content drafts won (see "Changes since v0.2" at the end).

Pipeline: player text -> **interpreter** -> action proposal (a) -> `process_action_proposal(conn,
character_id, proposal, rng=None)` -> engine result (b) -> **narrator** -> prose ending "What do you do?".

Principles
- The LLM proposes and the engine decides. The engine owns every DC, including defense DCs.
- The narrator never sees check numbers (`roll`, `dc`, `modifier`, `bonus`, `total`, `margin`,
  `needed_roll`) or condition `effect` math. Those go only to the `rpg.debug` logger.
- Everything is referenced by its `key` slug: `loc_<slug>`, `npc_<slug>`, `item_<slug>`. **Exits are
  targeted by their destination location key.** Prompts show only item *instance* keys.
- `process_action_proposal` writes through `conn` and does not commit; the caller does (`rpg.db.connect()`
  commits on exit).

## 0. Context block: `build_context(conn, character_id)`

```json
{
  "location": {"key": "loc_storeroom", "name": "Storeroom", "description": "A cramped room of shelves and crates.",
               "exits": [{"direction": "down", "to": "loc_cellar", "to_name": "Cellar",
                          "description": "iron-banded door", "state": "locked"}]},
  "npcs_present": [{"key": "npc_tall_guard", "name": "Tall Guard", "disposition": "unfriendly",
                    "traits": ["alert"], "status": "alive", "hp_band": "unhurt", "out_of_reach": false}],
  "visible_items": [{"key": "item_hanging_lamp", "name": "Hanging lamp", "tags": ["fixture"], "fixture": true,
                     "state": null, "out_of_reach": false}],
  "player_inventory": [{"key": "item_rusty_shortsword", "name": "Rusty Shortsword", "equipped": true,
                        "slot": "main_hand", "quantity": 1}],
  "player_sheet": {"name": "Hero", "level": 1, "proficiency_bonus": 2,
                   "abilities": {"str": {"score": 8, "mod": -1}, "dex": {"score": 14, "mod": 2}, "...": "..."},
                   "skills": {"perception": "proficient", "sleight_of_hand": "proficient"},
                   "weapon_proficiencies": ["simple"],
                   "conditions": [{"name": "strained_str", "description": "...", "lasting": true}],
                   "features": []},
  "active_conditions": [{"name": "strained_str", "description": "...", "lasting": true}],
  "hp_band": "scratched",
  "recent_narrations": ["oldest ...", "...", "newest"]
}
```
- `disposition` band: `hostile` (<= -50), `unfriendly` (<= -10), `neutral`, `friendly` (>= 10), `allied` (>= 50).
- `visible_items` includes fixtures. `status` is `alive` or `defeated`.
- `recent_narrations`: last 3 from the `narration_log` table, oldest first (`context.log_narration()` writes it).
- `active_conditions` duplicates `player_sheet.conditions` for prompts that want it separately.

## (a) Action proposal (interpreter -> engine)

```json
{
  "actions": [
    {
      "action_type": "attack",
      "skill": "dex",
      "target": "npc_tall_guard",
      "items_used": ["item_rusty_shortsword"],
      "last_ditch": false,
      "est_difficulty": "medium",
      "intent_summary": "Attack the tall guard with the rusty shortsword.",
      "clarification_needed": false,
      "clarification_question": null
    }
  ],
  "ooc": null,
  "unresolved_references": []
}
```

| field | type | rule |
|---|---|---|
| `actions` | array, 0-3 | Required. More than 3 is **invalid** (re-prompt). Resolved in order. |
| `ooc` | object or null | `{"command": "save\|load\|quit\|help\|stats\|undo\|other", "text": str}`. When set, `actions` must be `[]`. |
| `unresolved_references` | string[] | **Only for ambiguity** (two or more plausible matches in context). Non-empty -> clarify. |
| `action_type` | enum | `move`, `examine`, `talk`, `attack`, `use_item`, `skill_check`, `take`, `drop`, `equip`, `unequip`, `inventory`, `rest`, `wait`, `other` |
| `skill` | string or null | One of the 18 5e skills (snake_case), a raw ability `str`/`dex`/`con`/`int`/`wis`/`cha`, or null. Required for `skill_check`. For `attack` it is only a hint. |
| `target` | string or null | A context key, a destination `loc_` key for exits, `"self"`, or null. |
| `items_used` | string[] | Item keys. Claimed items the character doesn't have use a best-guess `item_<noun>` slug. Default `[]`. |
| `last_ditch` | bool | Default false. |
| `est_difficulty` | enum or omitted | Hint: `trivial` 5, `easy` 10, `medium` 15, `hard` 20, `very_hard` 25, `near_impossible` 30. Default `medium`. |
| `intent_summary` | string | Required, non-empty. |
| `clarification_needed` | bool | Default false. |
| `clarification_question` | string or null | |

The engine also accepts the proposal as a JSON string.

### Malformed proposal -> re-prompt error (not for the narrator)
```json
{"error": "invalid_proposal",
 "errors": [{"path": "actions[0].action_type", "message": "one of [...]"}],
 "reprompt": "Your previous output did not match the action-proposal schema (actions[0].action_type: ...). Return only one corrected JSON object."}
```
Nothing changes in the DB and no turn is spent.

### OOC-only turn
`{"type": "ooc", "ooc": {"command": "save", "text": "save game"}, "results": []}`. No engine actions run and no turn is spent; the CLI handles the command (task 3).

## (b) Engine result (engine -> narrator)

```json
{
  "turn": 42,
  "results": [
    {
      "action_type": "skill_check",
      "intent_summary": "Desperately ram the cellar door, accepting injury.",
      "target": {"key": "loc_cellar", "name": "iron-banded door"},
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
        {"type": "condition", "name": "strained_str", "description": "Pushed past your limits and strained yourself",
         "lasting": true, "severity": 1}
      ],
      "state_changes": [
        {"type": "exit_state", "key": "loc_cellar", "direction": "down", "from": "locked", "to": "open"}
      ]
    },
    {
      "action_type": "use_item",
      "intent_summary": "Point a dragon wand at the tall guard.",
      "target": {"key": "npc_tall_guard", "name": "Tall Guard"},
      "items_used": ["item_dragon_wand"],
      "status": "rejected",
      "skill": null, "ability": null,
      "outcome": "failure", "degree": null, "reason": "item_not_owned", "detail": "no_such_item",
      "auto_success": false, "impossible": false, "last_ditch": false,
      "consequences": [], "state_changes": []
    }
  ],
  "player_status": {"hp_band": "scratched", "conditions": ["strained_str"]},
  "dialogue": [],
  "defeat": null,
  "clarification_question": null
}
```

### Per-result fields
| field | values |
|---|---|
| `status` | `resolved`, `rejected` (validation code), `clarify` |
| `outcome` | `success`, `failure`, `clarify`. Rejected actions are `failure`. |
| `degree` | `narrow` (margin 0-4), `clear` (5-9), `overwhelming` (10+). The same bands apply to success and failure. **null whenever nothing was rolled** (auto-success, impossible, no_check, rejected, clarify). |
| `reason` | Check: `auto_success`, `rolled`, `last_ditch`, `impossible`, `impossible_even_last_ditch`. Validation: `item_not_owned`, `target_not_present`, `item_unusable`, `out_of_reach`, `impossible_action`. Other: `clarify`, `no_check`. |
| `detail` | Only on rejections. Machine-readable hint for the narrator or logs: `no_such_item`, `elsewhere`, `no_exit`, `unknown_key`, `exit_locked`, `already_here`, `not_an_exit`, `fixture`, `already_held`, `not_equippable`, `not_equipped`, `slot_occupied`, `target_defeated`, `not_a_creature`, `not_attackable`, `self`, `enemies_present`, `lacks_ability`, `no_item`, `no_target`, `no_destination`. |
| `skill`, `ability` | Set only when a check or attack was resolved. Attacks have `skill: null`. |
| `auto_success`, `impossible`, `last_ditch` | Booleans from the check. They are false when there was no check. |
| `target` | `{key, name}` (`examine` adds `description`). `"self"` -> `{"key": "self", "name": "you"}`. |

### Validation codes (rejects **only that action**; the rest still resolve)
| code | when |
|---|---|
| `item_not_owned` | An `items_used` key doesn't exist, or exists but isn't held or (where allowed) in the scene. Checked before the target, so a claimed item wins. `drop`/`equip`/`unequip` require held items. |
| `target_not_present` | The target key is unknown, is somewhere else, or names a location with no exit from here. `take` of an item not here. |
| `item_unusable` | Taking a `fixture` or an item already held, equipping a slot-`none` item, unequipping something not equipped, attacking *with* a fixture. |
| `out_of_reach` | The item or NPC has `out_of_reach` set: no take, use or melee. Ranged and thrown weapons can still hit it, and it can still be examined. |
| `impossible_action` | Categorical: moving through a locked exit, `move` to a non-exit, attacking yourself or a defeated NPC, talking to a non-creature or a defeated NPC, resting with a hostile NPC present, and **`action_type: "other"` + `skill: null` + `est_difficulty: "near_impossible"`** (claimed spells or abilities the character lacks). |

### Clarify
Triggered by a non-empty `unresolved_references` or by `clarification_needed: true` on an action.
- `unresolved_references` -> the whole turn clarifies. One result `{status: "clarify", outcome: "clarify",
  reason: "clarify", degree: null, consequences: [], state_changes: []}` is returned for the flagged action
  (or the first one), and nothing resolves.
- `clarification_needed` on action N -> actions before N resolve normally, N clarifies, and later actions are skipped.
- `clarification_question` (top level) = the action's question, or else
  `Which do you mean: "<ref>", ...?`.

### Resolution order and stopping
Actions resolve in order. A **rejected** action never stops the others. An action whose **check failed**
(status `resolved`, outcome `failure`) stops a later action only if the later one depends on it:
the failed action was a `move`, or the two share a target or item key (e.g. a failed lock-pick on
`loc_cellar` skips `move loc_cellar`). Skipped actions are **left out of `results`** (the narrator narrates
only what is sent) and listed in an optional top-level
`"skipped": [{"action_type", "intent_summary", "reason": "prior_failure" | "prior_clarify"}]` for logs and the CLI.

### DCs (engine-owned)
`est_difficulty` maps to 5/10/15/20/25/30. Authored DCs override it: `location_exits.unlock_dc` when
targeting a locked exit, `stats.dc` on an item, and `npcs.defense_dc` for attacks.

### Actions -> state changes
| action | engine behaviour |
|---|---|
| `move` | The target must be an exit from here and not locked. With a `skill` (e.g. climbing) a check gates it. Changes: `moved_to_location {key, name}`, and on the first visit `flag_set visited:<loc>`. |
| `examine` | `no_check`; with a `skill`, a check. Fixtures and out-of-reach things can be examined. |
| `talk` | NPC target only. `no_check`; with a social `skill`, a check vs the hinted DC. `dialogue` stays `[]` until dialogue generation is decided. |
| `skill_check` / `other` (+skill) | A check. Success on a locked exit -> `exit_state {key, direction, from: "locked", to: "open"}` (both sides unlocked). |
| `attack` | Check vs the target's defense DC. Ability: unarmed STR; a `finesse` weapon uses the better of STR and DEX; `ammunition` or ranged non-thrown uses DEX; otherwise STR. An improvised (non-weapon) item uses the str/dex hint. + PB if proficient (unarmed always; a weapon if `stats.proficiency` or its template key is in the character's `weapon_proficiencies`; improvised never). No auto-success. On a hit vs an NPC, damage = weapon dice (improvised `1d4`, unarmed `1`) + ability mod, min 1 -> `damage_dealt {target, hp_band}`; at 0 HP `npc_defeated` + `flag_set defeated:<npc>`. A surviving attacked NPC turns hostile -> `npc_disposition_changed {key, to: "hostile"}`. A hit on an object -> `entity_state {key, field: "state", from, to: "damaged"}`. |
| `take` | Scene item, not a fixture, in reach -> `item_added {key, name}`. |
| `drop` | Held item -> `item_removed {key, name, to: <loc key>}`. |
| `equip` | Held item with a real slot. Conflicting items are unequipped: `main_hand`/`off_hand` conflict with `two_hands`, and the same slot conflicts; `belt` never conflicts. -> `item_unequipped`..., `item_equipped {key, name, slot}`. |
| `unequip` | -> `item_unequipped {key, name}`. |
| `use_item` | Held, or a scene item in reach. `stats.check` (e.g. bandages: medicine, easy) is rolled first and nothing is spent on failure. `effect: heal` -> `healed {target: "self", hp_band}`. `effect: cure_condition` -> `condition_removed {name}`. `stats.use_sets_flag` -> `flag_set`. `stats.uses` spends one use: `item_used` (uses remain) or `item_consumed` + `item_removed` (used up). A tool used with a `skill` on another target (crowbar on a door) resolves as a `skill_check`. |
| `rest` | Not with a hostile NPC present. Heals half the missing HP (rounded up) -> `healed`; removes non-lasting conditions -> `condition_removed`. Lasting conditions stay. |
| `inventory`, `wait` | `no_check`, no changes. |

Consequence types: `condition`, `condition_removed`, `healed`, `damage_dealt` (resulting `hp_band`,
never numbers), `item_used`, `item_consumed`, `npc_disposition_changed`. Reserved (not emitted yet):
`damage_taken`, `item_broken`, `noise_made`.
State change types: `moved_to_location`, `exit_state`, `item_added`, `item_removed`, `item_equipped`,
`item_unequipped`, `entity_state`, `npc_defeated`, `flag_set`. Reserved: `exit_revealed`, `npc_fled`.

### Top level
| field | notes |
|---|---|
| `turn` | Per-character counter in `save_games.turn`. Increments on every non-error, non-ooc call. |
| `player_status` | `{hp_band, conditions: [names]}`. HP bands: `unhurt` (full), `scratched` (>= 75%), `wounded` (>= 40%), `badly_wounded` (>= 15%), `near_death` (> 0), `down` (<= 0). |
| `dialogue` | `[]` for now. Shape: `[{"speaker_key", "speaker_name", "line", "source": "authored" \| "npc_llm"}]`. |
| `defeat` | null, or `{"type": "knocked_out", "description": ...}` when the player is `down` (death/captured: later). |
| `clarification_question` | Set on clarify turns. |
| `skipped` | Optional, see above. |

## (c) Item template (content data -> `item_templates`)

```json
{
  "id": "item_short_blade",
  "name": "Short Blade",
  "category": "weapon",
  "power_tier": 1,
  "slot": "main_hand",
  "value": 100,
  "weight": 2,
  "stats": {"subcategory": "martial_melee", "damage": "1d6", "damage_type": "piercing",
            "proficiency": "martial", "properties": ["finesse", "light"]},
  "tags": ["rarity_uncommon", "avail_limited_stock", "blade"],
  "notes": "Finesse sidearm."
}
```
- Content `id` -> the `key` column (`item_<slug>`). The table also has an INTEGER `id` used by foreign keys.
- `slot`, `weight` and `value` are columns. `notes` goes to `description`. `subcategory` **lives inside `stats`** (a top-level
  `subcategory` is still accepted on load and moved into `stats`).
- `category`: `weapon`, `armor`, `shield`, `ammo`, `consumable`, `tool`, `utility`, `misc`.
  `slot`: `main_hand`, `off_hand`, `two_hands`, `body`, `head`, `hands`, `feet`, `belt`, `none`.
- The tag `fixture` marks scenery (lamp, crate, lever): it can be examined, used and targeted, but never taken.
- Engine-read `stats` keys: `damage`, `properties`, `range`, `proficiency`, `improvised_damage`, `effect`,
  `amount`, `condition`, `uses`, `check {skill, est_difficulty}`, `dc`, `use_sets_flag`.
- All 35 templates in `content/items_tier1.yaml` load via `rpg.db.add_item_template()`.

Item instances (`items` table): `key`, `template_id`, exactly one of `character_id` / `location_id`,
`display_name`, `quantity`, `equipped`, `uses_left`, `state`, `out_of_reach`. An authored instance's key is
`item_<slug>`. Extra instances of a template get `item_<slug>_2`, `_3`, and so on (`db.next_item_key`), and a
split stack gets the next free one.

## Changes since v0.2
1. Exits are targeted by destination `loc_` key; `exit:<direction>` is gone.
2. Keys are `item_<slug>` for templates and authored instances, with `_2`, `_3` for extras (no `wpn_`, no `item_0003`).
3. `degree` is null whenever nothing was rolled (was `clear` for auto-success and `overwhelming` for impossible).
4. A rejection affects only that action; only a failed check stops dependent later actions. Skipped actions are left out of `results` (new optional `skipped`).
5. `unresolved_references` is ambiguity-only -> clarify. Nonexistent items or targets -> `item_not_owned` / `target_not_present`.
6. Damage reports the target's resulting `hp_band` (no light/moderate/heavy).
7. `subcategory` lives inside `stats`.
8. `other` + null skill + `near_impossible` -> `impossible_action`.
9. New: re-prompt error object, ooc-only shape, `detail` on rejections, `clarify` status/outcome/reason, `no_check`,
   `>3 actions` is invalid (v0.2 said "keep the first 3"), `exit_state` replaces `exit_unlocked`, consequence types
   `healed`/`item_used`, the context block (section 0), and `est_difficulty` key `near_impossible` (was `nearly_impossible`).

# JSON contracts (v0.4)

Status: **implemented** in `rpg/actions.py` (`process_action_proposal`), `rpg/context.py`
(`build_context`), `rpg/effects.py` (NPC phase, light, tools, timers) and `rpg/rules.py`. Aligned with the Story Designer's drafts in
`/home/box/rpg-project/content/` (`schemas_proposed.md`, `interpreter_prompt.md`, `narrator_prompt.md`).
Where v0.2 differed, the content drafts won (see "Changes since v0.3" / "Changes since v0.2" at the end).

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
               "dark": false, "lit": true,
               "exits": [{"direction": "down", "to": "loc_cellar", "to_name": "Cellar",
                          "description": "iron-banded door", "state": "locked", "lockable": true}]},
  "npcs_present": [{"key": "npc_tall_guard", "name": "Tall Guard", "disposition": "unfriendly",
                    "traits": ["alert"], "status": "alive", "hp_band": "unhurt", "out_of_reach": false,
                    "conditions": ["blinded"], "visible": true}],
  "visible_items": [{"key": "item_hanging_lamp", "name": "Hanging lamp", "tags": ["fixture"], "fixture": true,
                     "state": null, "out_of_reach": false}],
  "player_inventory": [{"key": "item_rusty_shortsword", "name": "Rusty Shortsword", "equipped": true,
                        "slot": "main_hand", "quantity": 1, "state": null},
                       {"key": "item_torch", "name": "Torch", "equipped": false, "slot": "off_hand",
                        "quantity": 1, "state": "lit"}],
  "player_sheet": {"name": "Hero", "level": 1, "proficiency_bonus": 2,
                   "abilities": {"str": {"score": 8, "mod": -1}, "dex": {"score": 14, "mod": 2}, "...": "..."},
                   "skills": {"perception": "proficient", "sleight_of_hand": "proficient"},
                   "weapon_proficiencies": ["simple"],
                   "conditions": [{"name": "strained_str", "description": "...", "lasting": true, "timed": false}],
                   "features": []},
  "active_conditions": [{"name": "strained_str", "description": "...", "lasting": true, "timed": false}],
  "hp_band": "scratched",
  "recent_narrations": ["oldest ...", "...", "newest"]
}
```
- `disposition` band: `hostile` (<= -50), `unfriendly` (<= -10), `neutral`, `friendly` (>= 10), `allied` (>= 50).
- `visible_items` includes fixtures. `status` is `alive` or `defeated`.
- `recent_narrations`: last 3 from the `narration_log` table, oldest first (`context.log_narration()` writes it).
- `active_conditions` duplicates `player_sheet.conditions` for prompts that want it separately.
  `timed: true` = it wears off by itself (turns left are engine-only).
- **Darkness:** `location.dark` is authored (`locations.dark`); `lit` is true if the place isn't dark, or a
  lit light source (`items.state = "lit"`) is held by the player or lies here. When `dark && !lit`:
  `description` is replaced by "It is pitch dark; you can see nothing.", `visible_items` is `[]`, and every
  NPC has `visible: false` (still listed: you can hear them, and hostile ones still attack). Exits stay listed.
- `exits[].lockable` = it has an authored `unlock_dc` (so "the lock" can mean it even after it's opened).
- `npcs_present[].conditions`: NPC condition names (e.g. `blinded`); `player_inventory[].state` shows `lit`.

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
| `target` | string or null | A context key, a destination `loc_` key for exits, `"self"`, or null. For `move` it may also be a **direction**: `north`...`southwest`, `up`, `down`, `in`, `out`, aliases `n s e w ne nw se sw u d upstairs downstairs inside outside`, or `back` (the exit to where you came from). |
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
  "player_status": {"hp_band": "wounded", "conditions": ["strained_str"]},
  "dialogue": [],
  "npc_actions": [
    {"npc": "npc_tall_guard", "name": "Tall Guard", "action": "attack", "outcome": "success",
     "degree": "narrow", "hp_band": "wounded"}
  ],
  "expired": [{"type": "condition_expired", "target": "self", "name": "braced"}],
  "defeat": null,
  "clarification_question": null,
  "time_passed": true
}
```

### Per-result fields
| field | values |
|---|---|
| `status` | `resolved`, `rejected` (validation code), `clarify` |
| `outcome` | `success`, `failure`, `clarify`. Rejected actions are `failure`. |
| `degree` | `narrow` (margin 0-4), `clear` (5-9), `overwhelming` (10+). The same bands apply to success and failure. **null whenever nothing was rolled** (auto-success, impossible, no_check, rejected, clarify). |
| `reason` | Check: `auto_success`, `rolled`, `last_ditch`, `impossible`, `impossible_even_last_ditch`. Validation: `item_not_owned`, `target_not_present`, `item_unusable`, `out_of_reach`, `impossible_action`. Other: `clarify`, `no_check`. |
| `detail` | Only on rejections. Machine-readable hint for the narrator or logs: `no_such_item`, `elsewhere`, `no_exit`, `unknown_key`, `exit_locked`, `already_here`, `not_an_exit`, `fixture`, `already_held`, `not_equippable`, `not_equipped`, `slot_occupied`, `target_defeated`, `not_a_creature`, `not_attackable`, `self`, `enemies_present`, `lacks_ability`, `no_item`, `no_target`, `no_destination`; new in v0.4: `unrecognized`, `too_dark`, `needs_tool`, `already_open`, `already_full`, `not_held`, `light_not_held`, `no_way_back`. |
| `skill`, `ability` | Set only when a check or attack was resolved. Attacks have `skill: null`. |
| `auto_success`, `impossible`, `last_ditch` | Booleans from the check. They are false when there was no check. |
| `target` | `{key, name}` (`examine` adds `description`). `"self"` -> `{"key": "self", "name": "you"}`. |

### Validation codes (rejects **only that action**; the rest still resolve)
| code | when |
|---|---|
| `item_not_owned` | An `items_used` key doesn't exist, or exists but isn't held or (where allowed) in the scene. Checked before the target, so a claimed item wins. `drop`/`equip`/`unequip` require held items; so does **attacking with** an item (`not_held`) and lighting a light (`light_not_held`). |
| `target_not_present` | The target key is unknown, is somewhere else, or names a location with no exit from here. `take` of an item not here. |
| `item_unusable` | Taking a `fixture` or an item already held, equipping a slot-`none` item, unequipping something not equipped, a healing item at full HP (`already_full`; nothing is spent). |
| `out_of_reach` | The item or NPC has `out_of_reach` set: no take, use or melee. Ranged and thrown weapons can still hit it, and it can still be examined. |
| `impossible_action` | Categorical: moving through a locked exit, `move` to a non-exit, attacking yourself or a defeated NPC, talking to a non-creature or a defeated NPC, resting with a hostile NPC present, **any `action_type: "other"` with `skill: null`** (`lacks_ability` if `near_impossible`, else `unrecognized`: the engine has nothing to model, so it is never a free success), examining something not held in the dark (`too_dark`), picking a lock without a lock tool (`needs_tool`), picking/forcing an exit that is already open (`already_open`). |

`talk` with no target is `target_not_present` / `no_target`.

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

### Time passing
A turn **spends time only if at least one action resolved** (`time_passed: true`). Then, after the
player's actions: the NPC phase runs (`npc_actions`), then timers tick (`expired`). If every action was
rejected or clarifies, nothing else happens: the turn counter is handed back, no dice are rolled, no NPC
acts, nothing ticks (`time_passed: false`, `npc_actions: []`, `expired: []`).

### DCs (engine-owned)
`est_difficulty` maps to 5/10/15/20/25/30. Authored DCs override it: `location_exits.unlock_dc` when
targeting a locked exit, `stats.dc` on an item, and `npcs.defense_dc` for attacks. A tool that enables
`force_open` caps that DC at `hard` (20).

### Advantage / disadvantage (5e)
Roll 2d20 and keep the higher (advantage) or lower (disadvantage); any of each cancel to one roll.
Disadvantage also rules out auto-success. Sources: player condition effects `advantage` /
`disadvantage` (lists of abilities or skills, or `all`); a relevant tool whose `skill_bonus.grants` is
`advantage`; darkness (perception and investigation at disadvantage when `dark && !lit`); attacking an
NPC with an incapacitating condition (`blinded`, `stunned`, `incapacitated`, `paralyzed`, `unconscious`).
The narrator sees none of this as numbers; a `tool_used {key, name}` or `darkness` consequence says why.

### Tools
- `enables` lets a method proceed. Methods: `pick_lock` (skill `sleight_of_hand`/`dex` on a locked exit,
  or an item whose `state` is `locked`) and `force_open` (`athletics`/`str` on the same). `pick_lock`
  **requires** a tool that enables it (else `needs_tool`); `force_open` works without one, but a tool
  enabling it caps the DC at 20. Success unlocks (exit: both sides; item: `state` -> `open`).
  `disarm_trap`, `stabilize`, `treat_condition` are accepted but have no mechanics yet.
- **Tools do not need to be in `items_used`.** A held tool that enables the check's method is used
  automatically (and added to the result's `items_used`). `skill_bonus` applies when the skill matches
  and the tool is relevant: listed in `items_used`, or the check is one of its `enables` methods. A held
  crowbar does not help you climb unless you say you use it.
- `use_item` with a lock tool on an exit/item resolves as that tool's method: lockpicks ->
  `sleight_of_hand`, crowbar -> `athletics`, whatever skill the interpreter guessed.

### Conditions and durations
- Player conditions: `character_conditions.duration_turns` (NULL = until removed). NPC conditions:
  `npc_conditions` (same column). Timed ones tick down by 1 at the end of each turn that spent time; at 0 they
  are removed and reported in `expired`. One turn ~ one minute; content durations are parsed as
  `N round(s)` = N turns, `N minute(s)` = N turns, `N hour(s)` = 60N turns.
- `effect.expires_after_check: [abilities/skills]` removes the condition after the first matching check
  or attack -> consequence `condition_removed {name, reason: "spent"}`.
- **Re-applying a condition the character already has never stacks:** the one row is kept; per effect key
  the larger magnitude wins, severity is the max, lasting if either is, duration the longer. (Two failed
  last-ditch STR attempts leave one `injured_str` at -2.)
- Lit light sources have `items.timer` (turns left), set from `stats.duration` when first lit. At 0 the
  item is removed -> `expired: light_burnt_out {key, name}`.

### Actions -> state changes
| action | engine behaviour |
|---|---|
| `move` | The target must be an exit from here (a `loc_` key or a direction / `back`) and not locked. With a `skill` (e.g. climbing) a check gates it. Changes: `moved_to_location {key, name}`, and on the first visit `flag_set visited:<loc>`. The engine remembers the previous location for `back` (flag `last_location:<character id>`). |
| `examine` | `no_check`; with a `skill`, a check. Fixtures and out-of-reach things can be examined. In the dark (`dark && !lit`) only held items and yourself can be examined without a skill (`too_dark`); searching with a skill is allowed, at disadvantage. |
| `talk` | NPC target only. `no_check`; with a social `skill`, a check vs the hinted DC. `dialogue` stays `[]` until dialogue generation is decided. |
| `skill_check` / `other` (+skill) | A check. On a locked exit / locked item with a lock method (see Tools) success -> `exit_state {key, direction, from: "locked", to: "open"}` (both sides unlocked) or `entity_state {key, field: "state", from: "locked", to: "open"}`. Other skills on a locked exit roll but never unlock it. |
| `attack` | Check vs the target's defense DC. Ability: unarmed STR; a `finesse` weapon uses the better of STR and DEX; `ammunition` or ranged non-thrown uses DEX; otherwise STR. An improvised (non-weapon) item uses the str/dex hint. + PB if proficient (unarmed always; a weapon if `stats.proficiency` or its template key is in the character's `weapon_proficiencies`; improvised never). No auto-success. On a hit vs an NPC, damage = weapon dice (improvised `1d4`, unarmed `1`) + ability mod, min 1 -> `damage_dealt {target, hp_band}`; at 0 HP `npc_defeated` + `flag_set defeated:<npc>`. A surviving attacked NPC turns hostile -> `npc_disposition_changed {key, to: "hostile"}` (and so counterattacks in the same turn's NPC phase). A hit on an object -> `entity_state {key, field: "state", from, to: "damaged"}`. You attack only with held items. |
| `take` | Scene item, not a fixture, in reach -> `item_added {key, name}`. |
| `drop` | Held item -> `item_removed {key, name, to: <loc key>}`. |
| `equip` | Held item with a real slot. Conflicting items are unequipped: `main_hand`/`off_hand` conflict with `two_hands`, and the same slot conflicts; `belt` never conflicts. -> `item_unequipped`..., `item_equipped {key, name, slot}`. |
| `unequip` | -> `item_unequipped {key, name}`. |
| `use_item` | Held, or a scene item in reach. `stats.check` (e.g. bandages: medicine, easy) is rolled first and nothing is spent on failure. `effect: heal` -> `healed {target: "self", hp_band}` (rejected `already_full` at full HP). `effect: cure_condition` -> `condition_removed {name}`. `effect: buff` -> `condition {name, description, lasting: false, severity: 0}` on the player (see Item effects). `effect: condition` -> NPC save, `npc_condition {key, npc_name, name, outcome: "applied" \| "resisted"}` per NPC, and each NPC hit turns hostile. `effect: light` -> toggles lit/unlit: `entity_state {key, name, field: "state", from, to: "lit" \| "unlit"}` (must be held; not consumed by lighting). `stats.use_sets_flag` -> `flag_set`. `stats.uses` spends one use: `item_used` (uses remain) or `item_consumed` + `item_removed` (used up). A tool used with a `skill` on another target (crowbar on a door) resolves as a `skill_check` (lock tools pick their own skill, see Tools). |
| `rest` | Not with a hostile NPC present. If a ration (`effect: sustenance`) is held, one is eaten (`item_consumed`/`item_used`) and HP heals **to full**; otherwise heals half the missing HP (rounded up) -> `healed`. Removes non-lasting conditions -> `condition_removed`. Lasting conditions stay. Rations are not required to rest. |
| `inventory`, `wait` | `no_check`, no changes. |

Consequence types: `condition`, `condition_removed` (optional `reason: "spent"`), `healed`, `damage_dealt`
(resulting `hp_band`, never numbers), `item_used`, `item_consumed`, `npc_disposition_changed`; new in v0.4:
`npc_condition`, `tool_used {key, name}`, `darkness {description}`. Reserved (not emitted yet):
`damage_taken` (damage to the player is reported in `npc_actions`), `item_broken`, `noise_made`.
State change types: `moved_to_location`, `exit_state`, `item_added`, `item_removed`, `item_equipped`,
`item_unequipped`, `entity_state`, `npc_defeated`, `flag_set`. Reserved: `exit_revealed`, `npc_fled`.

### Item effects (items_tier1.yaml keys)
| item | stats used | engine behaviour |
|---|---|---|
| `item_bracing_tonic` | `effect: buff`, `duration` | Condition `braced` for 10 turns: advantage on STR and CON checks (and STR attacks), disadvantage on WIS checks; spent by the first STR/CON check or STR attack. The YAML's `buff`/`side_effect` are prose only, so the mechanics come from `effects.BUFF_EFFECTS`; a template may instead give `stats.condition_effect` (+ `condition_name`) in the same shape, which wins. |
| `item_flash_powder` | `effect: condition`, `condition`, `duration`, `save` | Aimed at an NPC: that one; with no NPC target: every living, reachable NPC here (the 10 ft area). Each makes a save: d20 + `npcs.save_bonus` vs the `save.est_difficulty` DC (medium 15). On a failure it is `blinded` for `duration` (1 round = 1 turn): it loses its attack this turn (`npc_actions` entry `action: "none", reason: "blinded"`) and attacks against it have advantage. Consumed either way. |
| `item_light_source` | `effect: light`, `duration` | Lit/unlit toggle; 1 hour = 60 turns of burn time. Lights a dark location while held or lying there. |
| `item_rations` | `effect: sustenance` | Eaten on `rest` for a full heal (see `rest`). Eating outside a rest just spends it. |
| `item_lockpicks`, `item_crowbar`, `item_healers_kit` | `enables`, `skill_bonus` | See Tools. `grants: required_for_check` (lockpicks) adds no bonus; the requirement is enforced through `enables: pick_lock`. |

### NPC phase (counterattacks)
After the player's actions on a turn that spent time, each NPC in the player's location (in id order) that
is alive, `hostile`, and not `out_of_reach` acts once. If it has an incapacitating condition it does
nothing (`action: "none"`, `reason`: the condition). Otherwise it attacks: d20 + `npcs.attack_bonus` vs
the **player's defense**; a hit deals `npcs.damage` dice (min 1) to the player's HP. Degree uses the same
margin bands. The phase stops as soon as the player is at HP <= 0. NPC columns (defaults): `attack_bonus`
2, `damage` "1d4", `save_bonus` 0.

**Player defense** = body armor with `base_ac`: `base_ac` + DEX mod (capped at `dex_cap`; null = no cap);
no body armor: 10 + DEX mod. Plus every other equipped item's `ac_bonus` (shields, accessories). The
premade (DEX 14, no armor) has 12.

`npc_actions[]` entry: `{npc, name, action: "attack" | "none", outcome: "success" | "failure" | null,
degree, hp_band}` (`reason` on `none`). `hp_band` is the **player's** band after that action. No numbers;
rolls and damage go to `rpg.debug`.

`expired[]` entry types: `condition_expired {target: "self", name}`, `npc_condition_expired {key, npc_name,
name}`, `light_burnt_out {key, name}`.

### Top level
| field | notes |
|---|---|
| `turn` | Per-character counter in `save_games.turn`. Increments on every turn that spent time; a turn where nothing resolved keeps the previous number. `null` when the player is dead. |
| `npc_actions` | See NPC phase. `[]` when no time passed. |
| `expired` | See Conditions and durations. `[]` when no time passed. |
| `time_passed` | Bool, see Time passing. |
| `player_status` | `{hp_band, conditions: [names]}`. HP bands: `unhurt` (full), `scratched` (>= 75%), `wounded` (>= 40%), `badly_wounded` (>= 15%), `near_death` (> 0), `down` (<= 0). |
| `dialogue` | `[]` for now. Shape: `[{"speaker_key", "speaker_name", "line", "source": "authored" \| "npc_llm"}]`. |
| `defeat` | null, or `{"type": "death", "description": ...}` when the player's HP is <= 0 (`down`). A dead player's later proposals resolve nothing and return `defeat` again. The narrator must **not** end a defeat narration with "What do you do?" (the post-processor strips it); the CLI follows with its provisional death policy (`rpg.cli.death_policy`): load the most recent save slot, start a new game, or quit. (`knocked_out`/`captured`: reserved.) |
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
  `amount`, `condition`, `uses`, `check {skill, est_difficulty}`, `dc`, `use_sets_flag`; new in v0.4:
  `base_ac`, `dex_cap`, `ac_bonus`, `enables`, `skill_bonus {skill, grants}`, `duration`, `save {ability,
  est_difficulty}`, optional `condition_effect` / `condition_name`.
- All 35 templates in `content/items_tier1.yaml` load via `rpg.db.add_item_template()`.

Item instances (`items` table): `key`, `template_id`, exactly one of `character_id` / `location_id`,
`display_name`, `quantity`, `equipped`, `uses_left`, `state`, `out_of_reach`. An authored instance's key is
`item_<slug>`. Extra instances of a template get `item_<slug>_2`, `_3`, and so on (`db.next_item_key`), and a
split stack gets the next free one.

## Changes since v0.3
1. New top-level `npc_actions`, `expired`, `time_passed`. `defeat` is now `{"type": "death"}` (was `knocked_out`); a defeat narration has no closing line.
2. Turns where nothing resolved spend no time: no turn number, no dice, no NPC phase, no ticking.
3. `other` + `skill: null` is always rejected (`impossible_action` / `unrecognized` or `lacks_ability`).
4. `move` targets may be directions, aliases, or `back`.
5. Context: `location.dark`/`lit`, darkness hides the scene, `exits[].lockable`, `npcs_present[].conditions`/`visible`, `player_inventory[].state`, conditions `timed`.
6. Advantage/disadvantage, tools (`enables`, `skill_bonus`), lock methods (`pick_lock` needs a tool), `already_open`, condition durations, non-stacking conditions, buff/condition/light/sustenance item effects, player defense from armor.
7. New rejection details: `unrecognized`, `too_dark`, `needs_tool`, `already_open`, `already_full`, `not_held`, `light_not_held`, `no_way_back`. Attacking with a scene item is now `item_not_owned`/`not_held` (was allowed).
8. Schema v3: `locations.dark`, `items.timer`, `npcs.attack_bonus/damage/save_bonus`, table `npc_conditions`; v2 saves migrate on open. The dice state is stored in the save (`world_flags._rng_state`).

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

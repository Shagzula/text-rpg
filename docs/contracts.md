# JSON contracts (v0.6)

Status: **implemented** in `rpg/actions.py` (`process_action_proposal`), `rpg/context.py`
(`build_context`), `rpg/effects.py` (NPC phase, light, tools, timers), `rpg/talk.py` + `rpg/personas.py`
(talk, trades, voice step) and `rpg/rules.py`. Aligned with the Story Designer's drafts in
`/home/box/rpg-project/content/` (`schemas_proposed.md`, `interpreter_prompt.md`, `narrator_prompt.md`).
Where v0.2 differed, the content drafts won (see "Changes since ..." at the end). v0.5 accepts the
PROPOSED talk drafts (`schemas_proposed.md` §d-§g, `npc_persona_schema.md`, `npc_voice_prompt.md`) as
implemented, with the deviations listed in section (d). v0.6 adds the fixes from retest 9127044 and the
Project Lead's decisions of 2026-11-10 (marked **DECIDED**); see "Changes since v0.5". The Story Designer's
content is vendored into `code/content/` (source of truth stays `/home/box/rpg-project/content/`; a drift
test fails when they differ, `tools/check_content_drift.py --sync` re-copies).

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
                    "conditions": ["blinded"], "visible": true,
                    "topics": [{"key": "guard_post", "label": "his post / why he is here"}],
                    "trades": [{"key": "trade_buy_flash_powder", "label": "a packet of flash powder"}],
                    "recent_dialogue": ["oldest line ...", "newest line"]}],
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
                   "features": [], "gold": 60},
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
- v0.5 `felt_items`: when `dark && !lit`, the fixtures here (`[{key, name, fixture}]`, e.g. a lever)
  that can be found and used by touch. v0.6: once a dark search of this place has succeeded (world flag
  `searched_dark:<loc>`), loose reachable items are listed too (`fixture: false`, e.g. a rope); never
  out-of-reach things. `[]` otherwise. Interpreters should match names against these before falling back to
  inventory items.
- `npcs_present[].conditions`: NPC condition names (e.g. `blinded`); `player_inventory[].state` shows `lit`.
- v0.5 `npcs_present[].topics`: every public `knowledge` topic of the NPC's persona, plus `secrets` whose
  `min_band` <= the current band (`[{key, label}]`, never text). `trades`: trades listed in `reveal_by_band`
  at or below the current band (`[{key, label}]`). v0.6: topics/trades carry optional `aliases`; open trades
  carry `gives`/`wants` (priced, as in the trade block); completed one-off trades stay listed with
  `done: true` (so "buy it again" gets `trade_done`, not `no_such_trade`). `recent_dialogue`: the
  last 5 lines this NPC said (`npc_dialogue_log`), oldest first. Persona-less or defeated NPCs: `[]`.
  Secret text, `never` secrets, `insight_tell` and the disposition integer never appear.
- v0.5 `player_sheet.gold`: the purse, an integer coin count in the same unit as `item_templates.value`.

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
| `detail` | Only on rejections. Machine-readable hint for the narrator or logs: `no_such_item`, `elsewhere`, `no_exit`, `unknown_key`, `exit_locked`, `already_here`, `not_an_exit`, `fixture`, `already_held`, `not_equippable`, `not_equipped`, `slot_occupied`, `target_defeated`, `not_a_creature`, `not_attackable`, `self`, `enemies_present`, `lacks_ability`, `no_item`, `no_target`, `no_destination`; new in v0.6: `no_new_leverage`, `no_such_trade`, `trade_done`, `trade_item_missing`, `bad_quantity`, `hostile_nearby`; new in v0.5: `needs_target`, `no_effect`; new in v0.4: `unrecognized`, `too_dark`, `needs_tool`, `already_open`, `already_full`, `not_held`, `light_not_held`, `no_way_back`. |
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
| `impossible_action` | Categorical: moving through a locked exit, `move` to a non-exit, attacking yourself or a defeated NPC, talking to a non-creature or a defeated NPC, resting with a hostile NPC present (`enemies_present`) or, v0.6 **DECIDED**, in a room connected to this one by any exit (`hostile_nearby`), v0.6 talk rejections (see (d)), **any `action_type: "other"` with `skill: null`** (`lacks_ability` if `near_impossible`, else `unrecognized`: the engine has nothing to model, so it is never a free success), examining something not held and not felt in the dark (`too_dark`), picking a lock without a lock tool (`needs_tool`), picking/forcing an exit that is already open (`already_open`), v0.5: using a tool or weapon on nothing /
yourself (`needs_target`) or an item with no effect on nothing (`no_effect`). |

`talk` with no target is `target_not_present` / `no_target`. v0.6: `talk` to an NPC key that exists but is
elsewhere is `target_not_present` / `elsewhere` (never retargeted to whoever is here; the mock narrates
"Mara isn't here."). No time passes outside combat.

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
A turn **spends time only if at least one action resolved** (`time_passed: true`), or, v0.6 **DECIDED**
(combat stalling), **if a living hostile NPC was in the player's room at the start of the turn and at least
one action was rejected**: a wasted round (`wasted_round: true` at top level; time passes, hostiles act,
timers tick, the turn counter advances). Clarify turns, malformed proposals (re-prompt errors) and OOC input
never cost time, in or out of combat. Outside combat the rules below are unchanged. Then, after the
player's actions: NPC conditions tick (skipping ones applied this turn), the NPC phase runs
(`npc_actions`), then player conditions and lights tick (`expired` lists both, in that order). If every action was
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
  `npc_conditions` (same column). Timed player conditions tick down by 1 at the end of each turn that spent
  time; at 0 they are removed and reported in `expired`. **NPC conditions count the NPC's actions**
  (v0.5, BUG-R01): they tick after the player's actions and before the NPC phase, and a condition applied
  this turn doesn't tick until next turn. So a `1 round` blind thrown on turn T stops the NPC's attack on
  T, still gives advantage to the player's attack on T+1, and expires just before the NPC acts on T+1. One turn ~ one minute; content durations are parsed as
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
| `move` | The target must be an exit from here (a `loc_` key or a direction / `back`) and not locked. With a `skill` (e.g. climbing) a check gates it. Changes: `moved_to_location {key, name, dark}` (`dark: true` = arrived somewhere pitch dark; the narrator says so and lists the exits from context), and on the first visit `flag_set visited:<loc>`. The engine remembers the previous location for `back` (flag `last_location:<character id>`). |
| `examine` | `no_check`; with a `skill`, a check. Fixtures and out-of-reach things can be examined. In the dark (`dark && !lit`) only held items, yourself and **the room itself** (`target` null or the current `loc_`; the result's `target.description` is the darkness text, v0.5) can be examined without a skill; v0.6: so can anything in `felt_items` (by touch, `target.description` "You find it by touch in the dark: the X."), and examining the room adds `felt` (the felt items). Anything else is `too_dark`. Searching with a skill is allowed, at disadvantage; v0.6: success sets `searched_dark:<loc>` and `felt` includes loose items. |
| `talk` | NPC target only. `no_check`; with a social `skill`, a check vs the hinted DC. `dialogue` stays `[]` until dialogue generation is decided. |
| `skill_check` / `other` (+skill) | A check. On a locked exit / locked item with a lock method (see Tools) success -> `exit_state {key, direction, from: "locked", to: "open"}` (both sides unlocked) or `entity_state {key, field: "state", from: "locked", to: "open"}`. Other skills on a locked exit roll but never unlock it. |
| `attack` | Check vs the target's defense DC. Ability: unarmed STR; a `finesse` weapon uses the better of STR and DEX; `ammunition` or ranged non-thrown uses DEX; otherwise STR. An improvised (non-weapon) item uses the str/dex hint. + PB if proficient (unarmed always; a weapon if `stats.proficiency` or its template key is in the character's `weapon_proficiencies`; improvised never). No auto-success. On a hit vs an NPC, damage = weapon dice (improvised `1d4`, unarmed `1`) + ability mod, min 1 -> `damage_dealt {target, hp_band}`; at 0 HP `npc_defeated` + `flag_set defeated:<npc>`. A surviving attacked NPC turns hostile -> `npc_disposition_changed {key, to: "hostile"}` (and so counterattacks in the same turn's NPC phase). A hit on an object -> `entity_state {key, field: "state", from, to: "damaged"}`. You attack only with held items. |
| `take` | Scene item, not a fixture, in reach -> `item_added {key, name}`. |
| `drop` | Held item -> `item_removed {key, name, to: <loc key>}`. |
| `equip` | Held item with a real slot. Conflicting items are unequipped: `main_hand`/`off_hand` conflict with `two_hands`, and the same slot conflicts; `belt` never conflicts. -> `item_unequipped`..., `item_equipped {key, name, slot}`. |
| `unequip` | -> `item_unequipped {key, name}`. |
| `use_item` | Held, or a scene item in reach. `stats.check` (e.g. bandages: medicine, easy) is rolled first and nothing is spent on failure. `effect: heal` -> `healed {target: "self", hp_band}` (rejected `already_full` at full HP). `effect: cure_condition` -> `condition_removed {name}`. `effect: buff` -> `condition {name, description, lasting: false, severity: 0}` on the player (see Item effects). `effect: condition` -> NPC save, `npc_condition {key, npc_name, name, outcome: "applied" \| "resisted"}` per NPC, and each NPC hit turns hostile. `effect: light` -> toggles lit/unlit: `entity_state {key, name, field: "state", from, to: "lit" \| "unlit"}` (must be held; not consumed by lighting). `stats.use_sets_flag` -> `flag_set`. `stats.uses` spends one use: `item_used` (uses remain) or `item_consumed` + `item_removed` (used up; `from: "scene"` when it was used where it lay, not from the pack). A tool used with a `skill` on another target (crowbar on a door) resolves as a `skill_check` (lock tools pick their own skill, see Tools). |
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

### Pursuit (v0.6, **DECIDED**)
On a turn that spent time, if the player ended in a different room from where they started, every NPC in
the starting room that is alive, `hostile`, not `out_of_reach`, not incapacitated and has no world flag
`pursued:<npc_key>` moves to the player's room, gets that flag, and is reported in `npc_actions` as
`{npc, name, action: "follow", outcome: null, degree: null, hp_band}`. Following is its action that turn
(no attack until the next turn). It pursues **once** per game: after that it stays where it is. Runs after
NPC conditions tick and before the NPC phase; works in the dark (no sight needed).

### NPC phase (counterattacks)
After the player's actions on a turn that spent time, each NPC in the player's location (in id order) that
is alive, `hostile`, and not `out_of_reach` acts once. If it has an incapacitating condition it does
nothing (`action: "none"`, `reason`: the condition). v0.5: incapacitation is read from the condition's
`effect` (`no_attack`, `prevents_actions` or `incapacitated` true; `item_flash_powder`'s
`condition_effect: {no_attack: true}`). The name list (`blinded`, `stunned`, `incapacitated`, `paralyzed`,
`unconscious`) is only a fallback for a condition with no effect spec; an explicit spec always wins. Otherwise it attacks: d20 + `npcs.attack_bonus` vs
the **player's defense**; a hit deals `npcs.damage` dice (min 1) to the player's HP. Degree uses the same
margin bands. The phase stops as soon as the player is at HP <= 0. NPC columns (defaults): `attack_bonus`
2, `damage` "1d4", `save_bonus` 0.

**Player defense** = body armor with `base_ac`: `base_ac` + DEX mod (capped at `dex_cap`; null = no cap);
no body armor: 10 + DEX mod. Plus every other equipped item's `ac_bonus` (shields, accessories). The
premade (DEX 14, no armor) has 12.

`npc_actions[]` entry: `{npc, name, action: "attack" | "none" | "follow", outcome: "success" | "failure" | null,
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
| `dialogue` | `[{"speaker_key", "speaker_name", "line", "source": "authored" \| "npc_llm"}]`. Filled by the voice step (section (d)) after the engine resolves and before narration; `[]` otherwise. The narrator uses lines verbatim. |
| `defeat` | null, or `{"type": "death", "description": ...}` when the player's HP is <= 0 (`down`). A dead player's later proposals resolve nothing and return `defeat` again. The narrator must **not** end a defeat narration with "What do you do?" (the post-processor strips it); the CLI follows with its provisional death policy (`rpg.cli.death_policy`): load the most recent save slot, start a new game, or quit. (`knocked_out`/`captured`: reserved.) |
| `clarification_question` | Set on clarify turns. |
| `skipped` | Optional, see above. |
| `wasted_round` | v0.6, optional: `true` when time passed only because a rejected action was a wasted round in combat. |

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

## (d) Talk and dialogue (v0.5, accepted from `schemas_proposed.md` §d-§g)

### Personas (`npc_persona_schema.md`)
- Persona YAML is parsed by `rpg/yamlish.py` (a stdlib YAML subset: block maps/lists, flow `[..]`/`{..}`,
  quoted/plain scalars, comments) and validated by `rpg.personas.validate()` (schema rules 1-4: known
  topics/trades in `reveal_by_band`, all five bands present, secrets never listed below `min_band`,
  `never` secrets never listed, trade items must be known templates). The full persona (secrets included)
  is stored engine-side in `npc_personas(npc_id, persona JSON)` at seed; only `persona_view` (key, name,
  voice, traits, goals) is ever prompt-facing.
- At seed the persona sets `npcs.name/description/traits`, `disposition = base_disposition`, and the
  optional `combat` block (`defense_dc`, `max_hp`, `attack_bonus`, `damage`, `save_bonus`) overrides those
  `npcs` columns (`max_hp` also resets `current_hp` to full).
- Demo personas: `rpg/data/npcs/npc_mara.yaml`, `npc_dozing_guard.yaml` (engine-owned demo content; the
  guard's `combat` numbers are the unchanged v0.4 balance).

### Talk action fields (interpreter -> engine), talk actions only
`approach` (`ask`, `persuade`, `deceive`, `intimidate`, `insight`, `small_talk`, `trade`, `farewell`),
`topic` (a key from the NPC's context `topics` or `trades`, or null), `topic_text` (the player's own words
when `topic` is null). `validate_proposal()` keeps them; non-talk actions never carry them.
- **Skill pairing is corrected, not rejected:** `persuade` -> `persuasion`, `deceive` -> `deception`,
  `intimidate` -> `intimidation`, `insight` -> `insight`; others -> null (agreed with the Project Lead).
- A missing `approach` is inferred from the skill (else `ask`); an unknown value is a re-prompt error.
  `topic`/`topic_text` must be string or null.
- v0.6 `quantity` (int or null, not bool; anything else is a re-prompt error): how many times to make a
  `repeatable` trade. The engine requires 1..10 (`MAX_TRADE_QTY`); 0, negatives, >10, a quantity > 1 on a
  one-off trade, or a quantity on a bare trade -> `impossible_action` / `bad_quantity`. Gold and items are
  multiplied; the trade block gets `quantity` when > 1.
- v0.6 **topic matching** (interpreter side, the mock implements it): `topic` must be an exact topic/trade key
  or one of its `aliases` / label segments (split on "/") / item names in its gives/wants, after stripping
  determiners and singularising. No substring matching. Anything else -> `topic: null` with `topic_text`
  (engine answers `unknown`). Personas may give `aliases: [strings]` on knowledge, secrets and trades
  (engine extension; secret aliases are only listed once the secret is visible).
- A talk with no target: exactly one living NPC here -> that NPC; several -> clarify ("Who do you want to
  talk to: ...?"); none -> `target_not_present` / `no_target`.

### Resolution
- **Social DC** (paired approaches only) = `social.base_dc` (default 12) + `social.<skill>` + band modifier
  (hostile +5, unfriendly +2, neutral 0, friendly -2, allied -5). `est_difficulty` is ignored for persona
  NPCs; persona-less NPCs keep the v0.4 DC (`est_difficulty`).
- **Disposition** (int -100..100, banded; only engine events change it). Deltas, success narrow/clear/
  overwhelming | failure narrow/clear/overwhelming: persuade +10/+15/+25 | 0/-5/-10; deceive +10/+15/+20 |
  -10/-20/-30; intimidate -10/-10/-15 | -10/-15/-25; insight 0; ask/small_talk/trade/farewell 0; a
  completed trade +2. An auto-success counts as `clear`. Clamped after each change. Being attacked still
  sets -100 (hostile). A band change emits `npc_disposition_changed {key, to}`. Applies to persona-less
  NPCs too. (`npcs.disposition` has been an INTEGER since schema v1: no column conversion was needed.)
- **Topics:** available = cumulative `reveal_by_band` topics up to the band *after* this turn's change. A
  successful intimidate evaluates topics one band above the band **before** the drop (cowed). Revealed when
  available and the check (if any) succeeded; once revealed a topic stays known (`npc_topics_revealed`,
  unique per NPC+topic) and is revealed again on request regardless of band or check. A topic that exists
  and is visible in context but isn't available, or whose check failed -> `refused` (label only). A secret
  not visible at the band, a `never` secret, or a key the NPC doesn't have -> `unknown` (so a hidden
  secret's existence is never confirmed). No subject (`topic` and `topic_text` null, or small_talk /
  farewell) -> `none`.
- v0.6 **DECIDED** (Lead, 2026-11-10) **no-retry rule** (5e: you can't retry a social check without new
  leverage): every paired approach (persuade/deceive/intimidate/insight) on an NPC records world flag
  `social_try:<npc>:<approach>:<subject>` (subject = topic key, else lowercased `topic_text`, else empty)
  with `{turn, disposition, items}`. Repeating the same NPC+approach+subject is rejected
  `impossible_action` / `no_new_leverage` (no roll, no time outside combat) unless at least 10 turns have
  passed (`RETRY_TURNS`), the NPC's disposition changed since, or `items_used` includes something not used
  before. A different approach or subject is a new try.
- v0.6 **DECIDED** **social cap at friendly**: a positive disposition change from a social check (persuade,
  deceive) never takes the NPC above 49 (top of `friendly`); `allied` is only reachable by other means
  (quests, story events; trades' +2 is not a social check). An NPC already above 49 is never lowered by it.
- `insight` success -> `insight_read` = the persona's `insight_tell` (narrator only; the voice step never
  sees it).

### Talk block on a resolved talk result
```json
"talk": {"npc": "npc_mara", "approach": "trade", "topic": "trade_buy_torch", "topic_text": null,
         "disposition_band_before": "friendly", "disposition_band_after": "friendly",
         "requested_topic_status": "none", "allowed_topics": [], "refused_topics": [],
         "trade": {"key": "trade_buy_torch", "label": "a torch", "status": "completed",
                   "gives": [{"item": "item_light_source", "name": "Torch", "qty": 1}],
                   "wants": [{"gold": 5}]},
         "insight_read": null}
```
Field meanings as in `schemas_proposed.md` §f. Persona-less NPC: the block is present with empty lists and
`trade: null`. Rejected or clarify talk results have no `talk` block.

### Trades and gold (engine extension: coins)
- Persona `trades.<key>`: `{label, gives: [entry], wants: [entry], repeatable?: bool}`, where an entry is
  `{item: <template key>, qty: N, name?: <display name, gives only, digit-free>}` **or `{gold: N}`** (coins,
  same unit as `item_templates.value`). `gives` = what the NPC gives the player.
- **Band pricing** of coin entries (the `trade` block shows the priced amount): the player pays (`wants`)
  base x {hostile 1.5, unfriendly 1.25, neutral 1.0, friendly 0.9, allied 0.75}, rounded **up**, min 1; the
  player receives (`gives`) base x {hostile 0.5, unfriendly 0.75, neutral 1.0, friendly 1.1, allied 1.25},
  rounded **down**, min 1. (Hostile normally has no trades listed.) Item entries aren't scaled.
- v0.6: `approach: trade` + `topic` null and no `topic_text` -> `offered` with `key/label: null`,
  `gives/wants: []` and `options: [trade entries]` listing every open trade (the NPC names them); `refused`
  if none. **Never falls back to the first listing.** `topic` null with `topic_text` (an unmatched deal) ->
  `impossible_action` / `no_such_trade`; `topic` not a trade key on a trade approach -> `no_such_trade`;
  a completed one-off -> `trade_done`; wanted items not held (x quantity) -> `item_not_owned` /
  `trade_item_missing` (e.g. selling rope you don't have). These rejections don't spend time outside combat.
- `topic` = a trade key (any approach) -> `completed` if open at the band and the player holds every wanted
  item (by template, summed across stacks) and has the coins; short of coins -> `unaffordable` (v0.6: missing
  items are the `trade_item_missing` rejection above). A trade not open at the
  band -> `refused` (its label only if the trade is listed at or below the band; a higher-band trade comes
  back with `label: null`). Non-`repeatable` trades complete once (`world_flags` `trade_done:<npc>:<trade>`).
- **Completion** is engine-owned: wanted items are removed (`item_removed`), coins move
  (`characters.gold`, CHECK >= 0; consequence `purse_changed {direction: "spent" | "received"}`, no amount),
  given items are created as new instances (`item_added`), disposition +2. That is the only way an NPC gives
  an item or coins; intent text or voice lines claiming a gift do nothing.

### Voice step (`voice_npc`)
`LLMClient.voice_npc(npc_persona_view, allowed_topics, result, context) -> {"lines": [1-3 strings]}`,
where `result` = `{player_text, talk (without insight_read and npc, plus outcome/degree), recent_dialogue}`.
`rpg.talk.voice()` runs it once per **resolved** talk result for an NPC **with** a persona, after
`process_action_proposal` and before narration (the CLI does this). Post-processing: invalid JSON/shape or
an exception -> retry once, then no lines (`dialogue` stays `[]` and the narrator falls back to brief
indirect speech of `allowed_topics` text only). Blank lines dropped; each line clipped to 40 words; at most
3 lines. Lint: a line sharing a 6-word run with any secret text not in `allowed_topics` (or with
`insight_tell`) is dropped. Lines are appended to `dialogue` as `{speaker_key, speaker_name, line, source:
"npc_llm"}` and logged to `npc_dialogue_log(npc_id, turn, line)`. Lines are never parsed for claims.
MockLLM has a deterministic `voice_npc`; the OpenAI/Anthropic stubs raise `NotImplementedError` (so play
falls back to indirect speech).

### Deviations from the drafts (for the Story Designer)
1. `approach` missing -> inferred, not a re-prompt; a skill/approach mismatch is corrected (Lead decision).
2. Coin entries `{gold: N}` in trades, band price multipliers, optional `repeatable` and gives `name`.
3. Previously revealed topics stay `revealed` even if the band later drops or a check fails.
4. Intimidate's "one band higher" is relative to the band before the drop.
5. `requested_topic_status: "none"` also when a check-based approach has no subject (e.g. bare insight).
6. Context `topics` lists all public knowledge (§d), not only the band's `reveal_by_band` topics; asking
   about one not yet available gives `refused`.
7. Demo personas live in `code/rpg/data/npcs/` (engine-owned), not in `content/`.
8. v0.6: optional `aliases` on persona topics/secrets/trades; trade `quantity`; bare trade `options`.
9. v0.6: `tools/validate_content.py` (vendored, adapted from `/workspace/validate_content.py`, which is
   untouched) treats inline schema-notation snippets (e.g. `{key, label}`) as notation, not JSON failures,
   and checks `validate_proposal`'s error tuple (the original's dict check never matched).

## Changes since v0.5
Retest of 9127044 (`playtest/2026-11-10-retest-9127044.md`):
1. Content vendored into `code/content/` + drift test + vendored validator run in tests; a clean worktree
   runs with 0 skips.
2. R2-01: talking to an absent NPC -> `target_not_present` / `elsewhere`, no retargeting, no time;
   "ask X about Y" is a talk.
3. R2-02/03/04: trades never fall back to the first listing (`no_such_trade`, `trade_item_missing`,
   `trade_done`), bare trade lists `options`, `quantity` 1..10 (`bad_quantity`).
4. R2-05: dark touch path. "feel for the lever" / "feel around" = examine with no skill (no roll): a felt
   item is examined by touch, the room by touch lists `felt` (`result.felt` = felt items). "search" /
   "rummage" = investigation at disadvantage; success sets `searched_dark:<loc>` and reveals loose items
   (`felt`), which stay in `felt_items`. (Replaces the v0.5 rule that a felt fixture is `too_dark` to
   examine.)
5. R2-06: topic matching is exact keys/aliases only; persona `aliases`.
6. R2-07: low fixes (see the report).
Project Lead decisions (**DECIDED**, not provisional):
7. 5e no-retry rule (`no_new_leverage`) and social cap at friendly (49), section (d).
8. Combat stalling: a rejected action with a living hostile in the room is a wasted round
   (`wasted_round`); outside combat rejections stay free; clarify/malformed/OOC never cost time.
9. No rest with a hostile in the same room (`enemies_present`) or an adjacent connected room
   (`hostile_nearby`). Pursuit: a fled-from hostile follows once (`pursued:<npc>`, `npc_actions` `follow`),
   also in the dark. Guard stats unchanged (DC 11, HP 12, +3, 1d6, save +1).

## Changes since v0.4
1. Talk accepted (section (d)): talk action fields, persona social DCs, disposition deltas, topics,
   `talk` result block, trades with coins, voice step filling `dialogue`.
2. Context: `npcs_present[].topics/trades/recent_dialogue`, `player_sheet.gold`.
3. Schema v4: `characters.gold`, `npc_personas`, `npc_topics_revealed`, `npc_dialogue_log`; v2/v3 saves
   migrate. New consequence `purse_changed`.
4. NPC incapacitation is driven by the condition effect (`no_attack` / `prevents_actions` /
   `incapacitated`); the name list is only a fallback.
5. Persona `combat` blocks set NPC combat stats at seed. Item names may not contain digits (load-time
   check). `improvised_pick` items pick locks at DC 25 (or the authored DC if higher); bare hands stay
   `needs_tool`. `stats.condition_effect` drives tonic/flash powder.
6. Retest fixes (`playtest/2026-11-10-retest-da16509.md`): NPC conditions tick before the NPC phase and skip
   the turn they're applied (R01); `moved_to_location.dark`, looking around in the dark resolves (R02);
   context `felt_items` (R03); bare talk targets the only NPC here or clarifies (R04); new rejection details
   `needs_target` / `no_effect` (R05); `item_removed.from: "scene"` (R06).

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

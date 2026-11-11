# JSON Shapes — AGREED (matches contracts.md v0.6)

> **Status: AGREED (matches contracts.md v0.6).** Updated Nov 10, 2026 (CST). Authoritative source: `/home/box/rpg-project/code/docs/contracts.md` (v0.6, implemented in `rpg/actions.py`, `rpg/context.py`, `rpg/effects.py`, `rpg/talk.py`, `rpg/personas.py`, `rpg/rules.py`). This file is the prompt-layer summary. If they ever differ, contracts.md wins. This directory is vendored into `code/content/` (drift-tested), so it stays the source of truth. Sections (d)–(g) (NPC dialogue) are **ACCEPTED** and implemented (contracts §d). No open engine requests.

Core principle: the LLM proposes and describes, the engine decides. The engine owns every DC (authored `unlock_dc`, item `stats.dc`, NPC `defense_dc`, else the `est_difficulty` hint; a `force_open` tool caps the DC at 20). The narrator never sees `roll`, `dc`, `modifier`, `bonus`, `total`, `margin`, `needed_roll`, advantage/disadvantage, or condition `effect` math.

Keys: `loc_<slug>`, `npc_<slug>`, `item_<slug>` (extras `_2`, `_3`). Exits are targeted by **destination `loc_` key**, or for `move` by direction/alias/`back`. Prompts show only item *instance* keys.

---

## 0. Context block: `build_context(conn, character_id)`

| placeholder | build_context key / contents |
|---|---|
| `{setting_summary}` | (prompt layer) TBD |
| `{tone_notes}` | (prompt layer) TBD |
| `{location}` | `{key, name, description, dark, lit, exits: [{direction, to, to_name, description, state: "open"\|"locked", lockable}]}` |
| `{npcs_present}` | `[{key, name, disposition, traits, status: "alive"\|"defeated", hp_band, out_of_reach, conditions: [names], visible, topics: [{key, label, aliases?}], trades: [{key, label, aliases?, gives, wants, done?}], recent_dialogue: [str]}]` (§d) |
| `{visible_items}` | `[{key, name, tags, fixture, state, out_of_reach}]` (includes fixtures) |
| `{felt_items}` | `[{key, name, fixture}]`: when `dark && !lit`, fixtures findable by touch (e.g. a lever), plus loose reachable items (`fixture: false`) once a dark search here has succeeded; `[]` otherwise. Interpreters match names here before inventory. |
| `{player_inventory}` | `[{key, name, equipped, slot, quantity, state}]` (`state` e.g. `lit`) |
| `{player_sheet}` | `{name, level, proficiency_bonus, abilities: {str: {score, mod}, ...}, skills, weapon_proficiencies, conditions: [{name, description, lasting, timed}], features: [], gold}` (`gold` = purse, integer coins in the `value` unit) |
| `{hp_band}` | `hp_band` |
| `{recent_narrations}` | last 3, oldest first |

`active_conditions` (a duplicate of `player_sheet.conditions`) isn't used by the prompts.

- **Darkness:** when `dark && !lit`, `description` = "It is pitch dark; you can see nothing.", `visible_items` = `[]`, and every NPC has `visible: false` (still listed; hostile ones still attack). Exits stay listed. A lit light source held or lying here lights the place.
- Bands: disposition `hostile` (<= -50) / `unfriendly` (<= -10) / `neutral` / `friendly` (>= 10) / `allied` (>= 50). HP `unhurt`, `scratched`, `wounded`, `badly_wounded`, `near_death`, `down`.
- Fixtures: `fixture: true` / tag `fixture`. They can be examined, used, and targeted. Taking one → `item_unusable` / `fixture`. You can attack only with **held** items (`item_not_owned` / `not_held`).

Prompt-specific inputs: `{player_text}` (both), `{narration_result}` (narrator), and `{npc}`, `{talk}`, `{recent_dialogue}` (voice prompt, §g).

---

## (a) Action proposal: interpreter → engine

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
| `actions` | array, 0–3 | Required. **More than 3 is invalid** (re-prompt; the re-prompt asks to keep the first 3 and note the rest in the last `intent_summary`). |
| `ooc` | object or null | e.g. `{"command": "save", "text": "save game"}`. `command` is one of `save`, `load`, `quit`, `help`, `stats`, `undo`, `other`; `text` is the player's words. When set, `actions` must be `[]`. |
| `unresolved_references` | string[] | **Only for ambiguity.** Non-empty → the whole turn clarifies. |
| `action_type` | enum | `move`, `examine`, `talk`, `attack`, `use_item`, `skill_check`, `take`, `drop`, `equip`, `unequip`, `inventory`, `rest`, `wait`, `other` |
| `skill` | string or null | One of the 18 5e skills, a raw ability, or null. Required for `skill_check`. Only a hint for `attack`. **`other` + `skill: null` is always rejected** (`lacks_ability` if `near_impossible`, else `unrecognized`). |
| `target` | string or null | Context key, destination `loc_` key, `"self"`, or null. For `move` also a direction (`north`…`southwest`, `up`, `down`, `in`, `out`, aliases `n s e w ne nw se sw u d upstairs downstairs inside outside`) or `back`. |
| `items_used` | string[] | Item keys. Unowned claims use a best-guess `item_<noun>` slug. Held tools that enable a method are used automatically (no need to list them). |
| `last_ditch` | bool | Default false |
| `est_difficulty` | enum or omitted | `trivial` 5, `easy` 10, `medium` 15, `hard` 20, `very_hard` 25, `near_impossible` 30. Default `medium`. |
| `intent_summary` | string | Required |
| `clarification_needed` / `clarification_question` | bool / string or null | |

Not for the narrator: the malformed-proposal re-prompt object, and the ooc-only turn (`{"type": "ooc", "ooc": {"command": "save", "text": "save game"}, "results": []}`, handled by the CLI).

---

## (b) Engine result: engine → narrator

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
      "skill": "athletics", "ability": "str",
      "outcome": "success", "degree": "narrow", "reason": "last_ditch",
      "auto_success": false, "impossible": false, "last_ditch": true,
      "consequences": [{"type": "condition", "name": "strained_str", "description": "Pushed past your limits and strained yourself", "lasting": true, "severity": 1}],
      "state_changes": [{"type": "exit_state", "key": "loc_cellar", "direction": "down", "from": "locked", "to": "open"}]
    }
  ],
  "player_status": {"hp_band": "wounded", "conditions": ["strained_str"]},
  "dialogue": [],
  "npc_actions": [{"npc": "npc_tall_guard", "name": "Tall Guard", "action": "attack", "outcome": "success", "degree": "narrow", "hp_band": "wounded"}],
  "expired": [{"type": "condition_expired", "target": "self", "name": "braced"}],
  "defeat": null,
  "clarification_question": null,
  "time_passed": true
}
```

### Per-result fields
| field | values |
|---|---|
| `status` | `resolved`, `rejected`, `clarify` |
| `outcome` | `success`, `failure`, `clarify` (rejected = `failure`) |
| `degree` | `narrow` (0–4), `clear` (5–9), `overwhelming` (10+). **null whenever nothing was rolled.** |
| `reason` | Check: `auto_success`, `rolled`, `last_ditch`, `impossible`, `impossible_even_last_ditch`. Validation: `item_not_owned`, `target_not_present`, `item_unusable`, `out_of_reach`, `impossible_action`. Other: `clarify`, `no_check`. |
| `detail` | Rejections only: `no_such_item`, `elsewhere`, `no_exit`, `unknown_key`, `exit_locked`, `already_here`, `not_an_exit`, `fixture`, `already_held`, `not_equippable`, `not_equipped`, `slot_occupied`, `target_defeated`, `not_a_creature`, `not_attackable`, `self`, `enemies_present`, `lacks_ability`, `no_item`, `no_target`, `no_destination`, `no_new_leverage`, `no_such_trade`, `trade_done`, `trade_item_missing`, `bad_quantity`, `hostile_nearby`, `needs_target`, `no_effect`, `unrecognized`, `too_dark`, `needs_tool`, `already_open`, `already_full`, `not_held`, `light_not_held`, `no_way_back` |
| `skill`, `ability` | Set when a check or attack was resolved (attacks: `skill: null`) |
| `auto_success`, `impossible`, `last_ditch` | Booleans, false without a check |
| `target` | `{key, name}` (`examine` adds `description`; a felt item in the dark: "You find it by touch in the dark: the X."). Self → `{"key": "self", "name": "you"}`. |
| `felt` | `examine` results in the dark, v0.6: `[{key, name}]` found by touch (feeling around the room: fixtures; a successful search: loose items too). Narrated by touch only. |
| `consequences` | `condition`, `condition_removed` (optional `reason: "spent"`), `healed {target, hp_band}`, `damage_dealt {target, hp_band}`, `item_used`, `item_consumed`, `npc_disposition_changed {key, to}`, `npc_condition {key, npc_name, name, outcome: "applied"\|"resisted"}`, `tool_used {key, name}`, `darkness {description}`, `purse_changed {direction: "spent"|"received"}` (no amount). Reserved: `damage_taken` (player damage is in `npc_actions`), `item_broken`, `noise_made`. |
| `state_changes` | `moved_to_location {key, name, dark}` (`dark: true` = arrived somewhere pitch dark), `exit_state {key, direction, from, to}`, `item_added`, `item_removed` (`from: "scene"` when used where it lay), `item_equipped`, `item_unequipped`, `entity_state {key, name?, field, from, to}` (incl. `lit`/`unlit`, `locked`→`open`, `damaged`), `npc_defeated`, `flag_set`. Reserved: `exit_revealed`, `npc_fled`. |

### Validation (rejects only that action)
| code | when (v0.5) |
|---|---|
| `item_not_owned` | Item doesn't exist or isn't held (or in the scene where allowed). Attacking with a non-held item (`not_held`), lighting a light not held (`light_not_held`), trade items not held (`trade_item_missing`). |
| `target_not_present` | Unknown, elsewhere, no exit there. `talk` without a target (`no_target`); `talk` to an NPC who is elsewhere (`elsewhere`, never retargeted). |
| `item_unusable` | Taking a fixture or a held item, equipping slot `none`, unequipping something not equipped, healing at full HP (`already_full`, nothing spent) |
| `out_of_reach` | The item or NPC is out of reach (ranged/thrown and examine still work) |
| `impossible_action` | Locked exit, non-exit move, attacking yourself or the defeated, talking to non-creatures or the defeated, resting with a hostile in the room (`enemies_present`) or in a connected room (`hostile_nearby`), talk rejections (`no_new_leverage`, `no_such_trade`, `trade_done`, `bad_quantity`), `other` + null skill (`lacks_ability` / `unrecognized`), examining something not held and not felt in the dark (`too_dark`), picking a lock without a lock tool (`needs_tool`), picking/forcing an open exit (`already_open`), `back` with no previous location (`no_way_back`), using a tool or weapon on nothing or yourself (`needs_target`), an item with no effect on nothing (`no_effect`) |

### Clarify, stopping, `skipped`
As before: `unresolved_references` → the whole turn clarifies. `clarification_needed` on action N → earlier actions resolve, N clarifies, later ones are skipped. A rejection never stops others. A failed check skips dependent later actions (after a failed `move`, or sharing a target/item). Optional top-level `skipped: [{action_type, intent_summary, reason: "prior_failure"|"prior_clarify"}]` is **never narrated**.

### Time, NPC phase, timers (v0.6)
- `time_passed: true` if at least one action resolved (one turn ≈ one minute), **or** (Lead, DECIDED) a living hostile was in the room at the start of the turn and an action was rejected: a **wasted round** (top-level `wasted_round: true`). Clarify, malformed and OOC never cost time. Then NPC conditions tick (skipping ones applied this turn), pursuit runs (`follow`), the NPC phase runs (`npc_actions`), then player conditions and lights tick (`expired`, in that order). Otherwise `npc_actions: []`, `expired: []`, and the turn number isn't spent.
- **Pursuit** (Lead, DECIDED): if the player left a room holding a living, hostile, reachable, able NPC, it follows once into the new room (`action: "follow"`, outcome/degree null; that's its action this turn), also in the dark.
- `npc_actions[]`: `{npc, name, action: "attack"|"none"|"follow", outcome: "success"|"failure"|null, degree, hp_band, reason?}`. `hp_band` = the **player's** band after it. Every alive, hostile, reachable NPC in the location acts once (incapacitated, read from the condition effect `no_attack`/`prevents_actions`/`incapacitated` → `none` + reason, e.g. `blinded`). It stops when the player is down. A surviving attacked NPC turns hostile and counterattacks the same turn. **Unseen (`visible: false`) NPCs still attack.**
- `expired[]`: `condition_expired {target: "self", name}`, `npc_condition_expired {key, npc_name, name}`, `light_burnt_out {key, name}`.
- Durations: `N round(s)` / `N minute(s)` = N turns, `N hour(s)` = 60N. Conditions don't stack (per effect key the larger wins).

### Top level
| field | notes |
|---|---|
| `turn` | Per-character counter. null when the player is dead. |
| `player_status` | `{hp_band, conditions: [names]}` |
| `dialogue` | `[{speaker_key, speaker_name, line, source: "authored"\|"npc_llm"}]`, filled by the voice step (§g) before narration; `[]` otherwise. Used verbatim. |
| `npc_actions`, `expired`, `time_passed` | See above |
| `wasted_round` | Optional, `true` when time passed only because a rejected action wasted a combat round |
| `defeat` | null or `{"type": "death", "description": "..."}` at HP <= 0. **No closing "What do you do?"** (the engine strips it). The CLI offers load most recent save / new game / quit (provisional). `knocked_out`/`captured` reserved. |
| `clarification_question` | Set on clarify turns |
| `skipped` | Optional |

---

## (c) Item template → `item_templates`

```yaml
- id: item_short_blade        # -> key column (item_<slug>)
  name: Short Blade           # no digits in names; quantities live in stats
  category: weapon            # weapon | armor | shield | ammo | consumable | tool | utility | misc
  power_tier: 1
  slot: main_hand             # main_hand | off_hand | two_hands | body | head | hands | feet | belt | none
  value: 100
  weight: 2
  stats: {subcategory: martial_melee, damage: "1d6", damage_type: piercing, proficiency: martial, properties: [finesse, light]}
  tags: [rarity_uncommon, avail_limited_stock, blade]
  notes: "Finesse sidearm."   # -> description
```

- **Engine-read `stats` keys (v0.5):** `damage`, `properties`, `range`, `proficiency`, `improvised_damage`, `effect`, `amount`, `condition`, `uses`, `check {skill, est_difficulty}`, `dc`, `use_sets_flag`, `base_ac`, `dex_cap`, `ac_bonus`, `enables`, `skill_bonus {skill, grants}`, `duration`, `save {ability, est_difficulty}`, `condition_effect`, `condition_name`.
- `effect` enum: `heal`, `cure_condition`, `buff` (player condition), `condition` (NPC save), `light` (toggle, timer from `duration`), `sustenance` (eaten on rest → full heal). Prose-only notes live in `use_note`, `buff`, `side_effect`.
- `condition_effect` shapes: player buff `{advantage: [...], disadvantage: [...], expires_after_check: [...]}` (`item_bracing_tonic` → `braced`). NPC condition `{no_attack: true}` (`item_flash_powder` → `blinded`, 1 turn).
- Tags: exactly one `rarity_*` and one `avail_*`. `fixture` = scenery. **`improvised_pick`** = a makeshift lockpick: with no lock tool held, `pick_lock` proceeds at DC 25 (or the authored DC if higher), and the item is used automatically. Bare hands → `needs_tool`. Tagged: `item_knife`. Item names contain no digits (load-time check).
- Instances (`items`): `key`, `template_id`, `character_id`|`location_id`, `display_name`, `quantity`, `equipped`, `uses_left`, `state`, `out_of_reach`, `timer`.


---

# NPC dialogue: AGREED (matches contracts.md v0.6 §d)

> **Status: AGREED (matches contracts.md v0.6)** (Nov 10, 2026, CST). Sections (d)–(g) are implemented in `rpg/talk.py`, `rpg/personas.py`, `rpg/context.py` and `LLMClient.voice_npc`, with the deviations listed in contracts §d (all adopted below). Persona file format: `npc_persona_schema.md`. Rationale for the separate voice call: README, "Dialogue architecture".

## (d) Context: `npcs_present[].topics`, `trades`, `recent_dialogue`; `player_sheet.gold`

(Rendered by `build_context()` from `npcs/npc_example_gatekeeper.yaml` at neutral.)

```json
{
 "key": "npc_example_gatekeeper",
 "name": "Gatekeeper",
 "disposition": "neutral",
 "traits": ["dutiful", "tired", "suspicious_of_strangers", "soft_spot_for_children"],
 "status": "alive",
 "hp_band": "unhurt",
 "out_of_reach": false,
 "conditions": [],
 "visible": true,
 "topics": [
   {"key": "gate_rules", "label": "gate rules / opening hours / getting in", "aliases": ["the gate", "gate", "gate rules", "closing time", "when the gate closes", "when the gate opens"]},
   {"key": "the_road_beyond", "label": "the road beyond the gate", "aliases": ["the road", "road", "the fork", "the road ahead"]},
   {"key": "missing_patrol", "label": "the missing night patrol", "aliases": ["the patrol", "patrol", "night patrol", "the night patrol"]},
   {"key": "gatekeeper_self", "label": "the gatekeeper himself / his name / his work", "aliases": ["yourself", "you", "your name", "your work", "the gatekeeper"]}
 ],
 "trades": [
   {"key": "trade_whistle_for_rations", "label": "his spare signal whistle", "gives": [{"item": "item_signal_whistle", "name": "Signal Whistle", "qty": 1}], "wants": [{"item": "item_rations", "name": "Travel Rations", "qty": 1}], "aliases": ["whistle", "spare whistle", "the whistle"]},
   {"key": "trade_buy_rations", "label": "a day's rations from the gate canteen", "gives": [{"item": "item_rations", "name": "Canteen Rations", "qty": 1}], "wants": [{"gold": 6}], "aliases": ["rations", "ration", "food", "canteen rations"]},
   {"key": "trade_sell_rope", "label": "buying a coil of rope for the watch", "gives": [{"gold": 8}], "wants": [{"item": "item_rope", "name": "Hempen Rope", "qty": 1}], "aliases": ["rope", "my rope", "sell rope"]}
 ],
 "recent_dialogue": ["Dawn to dusk, traveller. Same as always."]
}
```
- `topics` = **every public knowledge topic** (even ones not yet open at this band; asking about those gets `refused`), plus secrets whose `min_band` ≤ the current band. A secret's label is a **hint**: the player sees it once the band reaches `min_band`. Text never appears; `never` secrets never appear.
- `topics`/`trades` carry optional `aliases` (a secret's aliases are listed only once it's visible).
- `trades` = trades listed in `reveal_by_band` at or below the current band, with band-priced `gives`/`wants`. Completed one-time trades stay listed with `done: true`.
- `recent_dialogue` = the last 5 lines this NPC said, oldest first.
- Persona-less or defeated NPCs: all three are `[]`.
- `player_sheet.gold`: the purse (integer coins, same unit as item `value`).

## (e) Talk action fields (interpreter → engine)

Added to the action object **only when `action_type` is `talk`** (non-talk actions never carry them):

| field | type | rule |
|---|---|---|
| `target` | string or null | The NPC key. If null: exactly one living NPC here → that NPC; several → clarify ("Who do you want to talk to: ...?"); none → `target_not_present` / `no_target`. |
| `approach` | enum, optional | `ask`, `persuade`, `deceive`, `intimidate`, `insight`, `small_talk`, `trade`, `farewell`. **Missing → inferred from the skill, else `ask`.** An unknown value → re-prompt. Interpreters should still emit it. |
| `topic` | string or null | **Exact match only:** the subject (determiners dropped, singularised) equals a `topics`/`trades` key, one of its `aliases`, a `label` segment split on "/", or (trades) an item name in its gives/wants. Else null. No substring matching. |
| `topic_text` | string or null | The player's subject in their own words when `topic` is null. null when there's no subject. |
| `quantity` | int or null | v0.6, trade only: how many of a **repeatable** trade ("buy three rations" → 3). The engine accepts 1–10. 0, negatives, > 10, > 1 on a one-time trade, or any quantity on a bare trade → `impossible_action` / `bad_quantity`. A non-int (or bool) → re-prompt. |
| `skill` | string or null | Pairing: `persuade` → `persuasion`, `deceive` → `deception`, `intimidate` → `intimidation`, `insight` → `insight`; others null. **A mismatch is corrected, not rejected.** |

## (f) Talk result: the `talk` block on a resolved talk result

Standard result fields (status, outcome, degree, reason, consequences, state_changes...) plus:

```json
{
  "action_type": "talk",
  "intent_summary": "Ask the gatekeeper about the missing patrol.",
  "target": {"key": "npc_example_gatekeeper", "name": "Gatekeeper"},
  "items_used": [],
  "status": "resolved",
  "skill": "persuasion", "ability": "cha",
  "outcome": "success", "degree": "clear", "reason": "rolled",
  "auto_success": false, "impossible": false, "last_ditch": false,
  "consequences": [{"type": "npc_disposition_changed", "key": "npc_example_gatekeeper", "to": "friendly"}],
  "state_changes": [],
  "talk": {
    "npc": "npc_example_gatekeeper",
    "approach": "persuade",
    "topic": "missing_patrol",
    "topic_text": null,
    "disposition_band_before": "neutral",
    "disposition_band_after": "friendly",
    "requested_topic_status": "revealed",
    "allowed_topics": [{"key": "missing_patrol", "text": "Two of the night patrol didn't report back last night. Last seen heading toward the old cellar."}],
    "refused_topics": [],
    "trade": null,
    "insight_read": null
  }
}
```

A completed coin trade (neutral band, so base prices):
```json
"talk": {"npc": "npc_example_gatekeeper", "approach": "trade", "topic": "trade_buy_rations", "topic_text": null,
         "disposition_band_before": "neutral", "disposition_band_after": "neutral",
         "requested_topic_status": "none", "allowed_topics": [], "refused_topics": [],
         "trade": {"key": "trade_buy_rations", "label": "a day's rations from the gate canteen", "status": "completed",
                   "gives": [{"item": "item_rations", "name": "Canteen Rations", "qty": 1}],
                   "wants": [{"gold": 6}]},
         "insight_read": null}
```
with `consequences: [{"type": "purse_changed", "direction": "spent"}]` and `state_changes: [{"type": "item_added", "key": "item_rations_2", "name": "Canteen Rations"}]`. (+2 disposition doesn't change the band here.)

| field | values |
|---|---|
| `npc` | NPC key |
| `approach`, `topic`, `topic_text` | Echoed (approach after inference/correction) |
| `disposition_band_before` / `_after` | Bands only, never the integer. A band change also appears as `npc_disposition_changed` in `consequences`. |
| `requested_topic_status` | `revealed` (in `allowed_topics`; **once revealed, always revealed on request**), `refused` (visible in context but not open at this band, or the check failed; label only), `unknown` (hidden or `never` secret, or a key/subject the NPC doesn't have, including `topic: null` + `topic_text`), `none` (no subject: small talk, farewell, a trade, or a check-based approach with no subject such as a bare insight) |
| `allowed_topics` | `[{key, text}]`: the requested topic when revealed, else `[]`. **Never a secret below its band, never a `never` secret.** |
| `refused_topics` | `[{key, label}]`: the requested topic when refused. Label only. Only topics already visible in context. |
| `trade` | null, or `{key, label, status: "offered" \| "completed" \| "refused" \| "unaffordable", gives: [entry], wants: [entry], quantity?, options?}`, entry = `{item, name, qty}` or `{gold: N}` (**band-priced**, × quantity). `quantity` appears when > 1. A bare trade → `offered`, `key/label: null`, empty gives/wants, `options: [{key, label, aliases?, gives, wants}]` (every open trade). A higher-band trade refused → `key` kept, `label: null`. |
| `insight_read` | null, or the persona's `insight_tell` on a successful insight check (narrator only; stripped from the voice input) |

Rules:
- Persona-less NPC: `talk` present with empty lists and `trade: null`. Rejected or clarify talk results have **no** `talk` block (and no voice call).
- **Social DC** = persona `social.base_dc` (12) + `social.<skill>` + band modifier (hostile +5, unfriendly +2, neutral 0, friendly -2, allied -5). `est_difficulty` is ignored for persona NPCs.
- **No-retry rule** (Lead, DECIDED; 5e): the same NPC + paired approach + subject again → `impossible_action` / `no_new_leverage` (no roll), unless 10+ turns have passed, the disposition changed, or `items_used` has something new.
- **Social cap** (Lead, DECIDED): persuade/deceive gains stop at 49 (top of `friendly`). `allied` comes only from other means (quests, story, a trade's +2).
- **Topics** are evaluated at the band after this turn's change. A successful `intimidate` evaluates them one band above the **pre-drop** band (cowed).
- **Trades:** bare trade (`topic` and `topic_text` null) → `offered` with `options` (never a fallback to one listing); `refused` if nothing is open. `topic` = trade key → `completed` if open and affordable, short of coins → `unaffordable`; not open at the band → `refused`. Rejections (no time outside combat): `no_such_trade` (unmatched `topic_text`, or a non-trade key), `trade_done` (a one-time trade already made), `bad_quantity`, `item_not_owned` / `trade_item_missing`. Completion: `item_removed` for wanted items, `purse_changed {direction}` for coins, `item_added` (display `name`) for given items, disposition +2. One-time trades complete once; `repeatable: true` ones any number of times.
- **Coin pricing:** pay base × {hostile 1.5, unfriendly 1.25, neutral 1.0, friendly 0.9, allied 0.75} rounded up; receive base × {hostile 0.5, unfriendly 0.75, neutral 1.0, friendly 1.1, allied 1.25} rounded down; min 1. Item entries aren't scaled.
- Disposition deltas: see `npc_persona_schema.md` (same table as contracts §d).

## (g) voice_npc call (engine → LLM → engine)

`LLMClient.voice_npc(npc_persona_view, allowed_topics, result, context) -> {"lines": [...]}`. Rendered into `npc_voice_prompt.md` as:
```json
{
  "npc": {"key": "npc_example_gatekeeper", "name": "Gatekeeper",
          "voice": {"style": "Clipped and formal...", "sample_phrases": ["Gate's shut at dusk, traveller. Rules are rules."]},
          "traits": ["dutiful", "tired"], "goals": ["Keep the gate ledger in order and avoid trouble with superiors."]},
  "player_text": "Please, I think your patrol is in danger. What happened to them?",
  "talk": {"approach": "persuade", "topic": "missing_patrol", "topic_text": null,
           "disposition_band_before": "neutral", "disposition_band_after": "friendly",
           "outcome": "success", "degree": "clear", "requested_topic_status": "revealed",
           "allowed_topics": [{"key": "missing_patrol", "text": "..."}], "refused_topics": [], "trade": null},
  "recent_dialogue": ["(last lines this NPC said, oldest first)"]
}
```
`talk` = the result's talk block **without `npc` and `insight_read`**, plus `outcome`/`degree`. Never included: `secrets`, `insight_tell`, knowledge text beyond `allowed_topics`, disposition integers, or check numbers.

**Output:** `{"lines": ["...", "..."]}`, 1–3 lines, each ≤ 40 words.

**Engine post-processing (implemented):**
- Invalid JSON/shape or an exception → retry once, then no lines (`dialogue` stays `[]`; the narrator falls back to brief indirect speech of `allowed_topics` text only).
- Blank lines dropped; each line clipped to 40 words; at most 3 lines.
- Lint: a line sharing a 6-word run with any secret text not in `allowed_topics`, or with `insight_tell`, is dropped.
- Each line → `dialogue[]` as `{"speaker_key": "npc_mara", "speaker_name": "Mara", "line": "...", "source": "npc_llm"}` and logged to `npc_dialogue_log`.
- Lines are **never parsed for claims**.

**When called:** once per **resolved** talk result for an NPC **with** a persona, after `process_action_proposal` and before narration (the CLI does this). Persona-less NPCs get `dialogue: []` and body language only. MockLLM has a deterministic `voice_npc`; the OpenAI/Anthropic stubs raise `NotImplementedError` (so play falls back to indirect speech).

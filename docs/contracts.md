# JSON contracts (v0.10.3)

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
- **v0.7.1 unseen NPCs are never named.** In `dark && !lit` each *living* NPC here is listed as exactly
  `{"key": "npc_unseen_<id>", "visible": false, "sound": "<text>"}`: no name, real key, status, persona,
  topics or trades. `sound` is the persona's optional `dark_sound` (e.g. "slow, heavy breathing"), else the
  generic "something breathing nearby" (narrated as "Something breathes nearby in the dark."). Defeated NPCs
  make no sound and aren't listed. The ref is stable (from the NPC's row id) and opaque; interpreters target
  the NPC with it (`attack it`, `talk to whoever is there`), and the engine maps it back. In the engine
  result, every key of an NPC that was unseen at the start or end of the turn is replaced by its ref, and
  its `name` / `npc_name` / `speaker_name` by "unseen figure" (voiced lines: `speaker_name` "a voice in the
  dark"), anywhere in the result (`results`, `consequences`, `state_changes` incl. compound `flag_set`
  keys such as `defeated:npc_unseen_<id>`, `npc_actions` incl. `follow`, `talk.npc`, `dialogue`). The
  canonical world flag (e.g. `defeated:npc_dozing_guard`) is still written in the DB; only the
  narrator-facing copy is rewritten. Narrators describe such NPCs by sound only.
  v0.10.3: the same masking step (`unseen.for_llm`) runs at the boundary where any engine result or
  context is turned into an LLM-facing payload — interpret, narrate, and voice. Every field
  (`state_changes`, flags, `npc_actions`, targets, `intent_summary`, conditions, `pending_clarify`,
  persona_view, and anything else) goes through it, so a real key or display name of an NPC unseen
  at that moment cannot reach the model. The player's own typed text (`player_text`) is not rewritten.
  v0.9: mock (and narrator) wording does not put an article before a proper-name NPC (a single
  capitalized word such as Mara: "Mara comes after you", not "the Mara"). Multi-word or lowercase
  names take "the" (the Dozing Guard, the unseen figure). A name that already starts with a/an/the
  is left alone. An optional `article` on a result or persona (`""` / `"the"` / `"a"` / `"an"`)
  overrides the rule if present.
  v0.10: the persona `article` is validated on load and copied onto lit `npcs_present` entries,
  action `target` refs (including elsewhere-rejects), and `npc_actions`. Unseen NPC entries and
  masked results never carry `article` or `name`. `build_context` also includes `pending_clarify`
  when a clarify is waiting (opaque `npc_unseen_*` keys and sounds only, never names).
- `exits[].lockable` = it has an authored `unlock_dc` (so "the lock" can mean it even after it's opened).
- v0.5 `felt_items`: when `dark && !lit`, the fixtures here (`[{key, name, fixture}]`, e.g. a lever)
  that can be found and used by touch. v0.6: once a dark search of this place has succeeded (world flag
  `searched_dark:<loc>`), loose reachable items are listed too (`fixture: false`, e.g. a rope); never
  out-of-reach things. `[]` otherwise. Interpreters should match names against these before falling back to
  inventory items. v0.6.1: `look` (examine the room) in the dark resolves with `felt` = these items, and
  the narrator lists them alongside the darkness text and the exits; NPCs with `visible: false` are
  described as heard, not seen.
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
one action was rejected** (v0.6.1: other than `unrecognized`, i.e. a parsed action the engine
refused, such as `target_not_present`, `item_not_owned`, `no_new_leverage`, `enemies_present`): a wasted round (`wasted_round: true` at top level; time passes, hostiles act,
timers tick, the turn counter advances). Clarify turns, malformed proposals (re-prompt errors), OOC input and,
v0.6.1 (Lead, BUG-R3-01), input the engine couldn't map at all (`other` / `unrecognized`: typos such as
`attck guard`, nonsense such as `xyzzy`) never cost time, in or out of combat.
v0.10.1: referring to an unseen NPC **by name or real/invented `npc_` key** (e.g. `attack the guard` /
`target: npc_dozing_guard` / `npc_dragon`) while the room is dark: with **exactly one** living unseen
NPC, the mock and the engine target that NPC's opaque `npc_unseen_<id>` ref and resolve the action.
The result and narration still never confirm or reveal the name (`unseen figure`, sound only;
`intent_summary` is rewritten by sound). With **several** unseen NPCs, this is a **clarify by sound**
("Who do you want to attack: the one making …, or the one making …?"), never resolved (that would
confirm who they are), treated as an unresolved reference, not a parsed miss. A name that matches
nobody **and there is no unseen NPC here** (`attack the dragon` in an empty dark room, or `talk to mara`
in a lit room while she is elsewhere) is unchanged: `unknown_key` / `elsewhere`, and still a wasted
round in combat if a hostile is present.
v0.10 stall cap (combat only): the **first** such clarify (and the first of any other no-time
input) is still free. The **second consecutive clarify on the same unresolved reference** costs
the round (`wasted_round: true`, the foe acts, the question is still shown). Independently, a
streak of **5 consecutive free (no-time) turns** in combat — typo/`unrecognized`, other clarifies,
lit or dark — also becomes a wasted round, and later consecutive free turns keep costing until a
**resolved** action resets the streak. The pending clarify is stored (world flag, and
`pending_clarify` on the next context): a reply of `yes` / `y` / `yeah` / `ok` (when there is one
candidate) or a sound phrase such as `the breathing` carries out the original action against that
`npc_unseen_*` target. OOC commands and malformed proposals (re-prompt errors) never enter this
streak: they are not turns.

v0.10.2: the same-reference cap compares the **resolved candidate set** (opaque refs in the
dark, real keys when lit), not the player's wording, so `attack the guard` and `attack the
dozing guard` are the same reference. A verb-less `yes` / `ok` / `it` (etc.) while a pending
clarify has **several** candidates **re-asks the same sound question**; it is not
`unrecognized`. The first such re-ask is free and does **not** increment the free-streak (it
does not consume a stall slot). The second consecutive affirmation re-ask of the same pending
clarify **costs the round** (foe acts, question still shown), so `yes` cannot be used to stall.
`yes` with nothing pending stays `unrecognized` and counts toward the 5-streak.
OOC commands (`/help`, `/stats`, `/save`, `/load`, and an interpreter `ooc` proposal), malformed
re-prompts, and a blank CLI enter are **not turns**: they neither increment nor reset the streak.
The streak, last-clarify signature, and pending clarify are world flags, so they are part of the
save; `/save` / `/load` (or quitting and continuing the working file) cannot be used to reset them.


| turn contains | outside combat | hostile in the room at turn start |
|---|---|---|
| at least one resolved action | time passes | time passes |
| only rejections, at least one parsed (not `unrecognized`) | free | **wasted round** (`wasted_round: true`) |
| only `unrecognized` rejections | free | free; 5th consecutive free turn is a wasted round |
| clarify / malformed proposal / OOC / empty input | free | first same-reference clarify free; 2nd consecutive same-reference clarify, or 5th consecutive free turn, is a wasted round. Malformed / OOC are not turns. | Outside combat the rules below are unchanged. Then, after the
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
- **Re-applying a condition the character already has never stacks:** the one row is refreshed; per effect key
  the larger magnitude wins, severity is the max, lasting if either is, duration the longer. (Two failed
  last-ditch STR attempts leave one `injured_str` at -2.) v0.9 (BUG-R5-02): the refreshed row gets a new
  `id` (MAX(id)+1) so a refresh applied in the NPC phase, after `player_condition_mark` was taken, skips
  that turn's tick. A 1-round daze re-applied on the turn it would have expired therefore lasts through
  the player's next action, and narration shows it applied, not faded.
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
| `item_rope` (v0.7) | `subcategory: climbing` (tag `climbing`); `use_note` "enables climbing/descending; advantage on athletics climb checks when anchored" | Any template with `stats.subcategory: climbing` gets the default stats `enables: [climb]`, `skill_bonus: {skill: athletics, grants: advantage}` (`inventory.SUBCATEGORY_DEFAULTS`; keys the template sets itself win). A `move` with `skill: athletics` is method `climb`: a held rope gives advantage automatically (`tool_used`). Any other athletics check gets it only when the rope is in `items_used` ("anchored"). No advantage for forcing doors etc. Without a rope a climb is a plain check (the slice has no climb-only exits yet). |
| `item_signal_whistle` (v0.7; v0.7.1 `stats.noise`) | `stats.noise` if set, e.g. `{distract: next_attack, once_per_foe: true}` (also `heard_range: adjacent|room`); else `subcategory: signal` with no `effect` (= the defaults `{heard_range: adjacent, distract: next_attack, once_per_foe: true}`, `effects.DEFAULT_NOISE`; keys given override them). `distract` other than `next_attack` (e.g. `null`) = noise only; `once_per_foe: false` = the flag isn't checked. Old row: `use_note` "loud noise audible ~600 ft; can distract or summon"; `uses: null` (unlimited) | `use_item` (held, else `item_not_owned` / `not_held`) resolves with consequence `noise_made {key, name, heard_by: [npc keys]}`: every living NPC here or one exit away. Each living, hostile, non-incapacitated NPC **here** that no noise has distracted before (world flag `noise_distracted:<npc>`, once per NPC per game) gets condition `distracted` (1 turn, effect `{attack_disadvantage: true}`; consequence `npc_condition {key, npc_name, name: "distracted", outcome: "applied"}`): its attack this turn rolls 2d20 and keeps the lower. "Summon" isn't modelled. The tag `noisy` alone (crowbar, armor) is not a whistle. |

### Equipment perks (v0.7; head / hands / feet house rules)
The YAML gives these perks as `use_note` prose; the engine's defaults (`effects.EQUIP_PERKS`, by template) apply
unless a template sets `stats.perk` (same shape), which wins. Perks work only while the item is **equipped**.
| item (slot) | perk shape | behaviour |
|---|---|---|
| `item_leather_cap` (head) | `{negate_conditions: [stunned, dazed], per: rest}` | The first `stunned` or `dazed` condition that would land on the player is negated (not stored); the result's `condition` consequence stays with `negated_by: {key, name}`. Spent until the next successful `rest` (world flag `perk_used:<item>`). Every player condition goes through `effects.add_player_condition` (check consequences, buffs). Nothing in the slice applies `stunned`/`dazed` to the player yet. |
| `item_work_gloves` (hands) | `{advantage_tags: [climbing, sharp, hot, rough], advantage_methods: [climb]}` | Advantage on a check whose method is `climb`, or where an item in `items_used` has one of the tags (e.g. the rope). Reported as consequence `perk_used {key, name}`. Doesn't stack with a tool's advantage. |
| `item_travel_boots` (feet) | none | The YAML gives no perk ("mostly flavor"); still an improvised thrown weapon. |

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

v0.7.1: a hit with degree `overwhelming` (margin >= 10) that doesn't drop the player also inflicts the
condition `dazed` (1 round; effect `{disadvantage: ["all"]}`: every check and attack at disadvantage) through
`effects.add_player_condition`, so a worn leather cap can cancel it (once per rest). The entry gets
`dazed: "applied"`, or `dazed: "negated"` + `negated_by: {key, name}`. Conditions inflicted in the NPC phase
don't tick at the end of that turn, so the daze covers the player's next turn and expires at its end
(`condition_expired`). v0.9: if another overwhelming hit lands on that expiry turn, the daze is refreshed
(new row id, full 1-round duration) and is narrated as applied, not as faded; it then covers the following
player action. See Conditions and durations.

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
  - v0.6.1 **subject** (BUG-R3-02): only a real topic or trade key of this NPC is a subject. A `topic` that
    isn't one, or `topic: null` with any `topic_text` (a paraphrase such as "what's down below"), is the
    **same subject as the last attempt** with that NPC and approach (world flag
    `social_last:<npc>:<approach>`), or the generic subject `""` if there was none. A different string alone
    never unlocks a retry.
  - v0.6.1 **cited topic as leverage** (design: it must be referenced, so the engine can check it): when
    `topic` is the subject key, `topic_text` may name a topic the player has learned (from **any** NPC) by its
    key, key with spaces, an alias or a label segment ("/"-split), exact after lowercasing, dropping
    punctuation and one leading determiner. If it names a topic revealed **since the last attempt** (the try
    flag stores the revealed set at that time) and not the subject itself, that's new leverage. A topic
    learned but not cited does not count; citing the same one twice doesn't either. Interpreter shape:
    "persuade Mara about the cellar, now that I know about his post" -> `topic: "the_cellar"`,
    `topic_text: "his post"`; "... with the rations" -> `items_used: ["item_rations"]`.
- v0.6 **DECIDED** **cap at friendly**, widened in v0.6.1 (Lead): **every small disposition gain** (a social
  check's delta and a completed trade's +2) never takes the NPC above 49 (top of `friendly`). An NPC already
  above 49 is never lowered by it. Only **gifts** (below) and **quest / story events** (engine API
  `talk.story_disposition(conn, npc_key, delta)`, uncapped, clamped to -100..100) can raise an NPC to allied.
- v0.6.1 **DECIDED** **gifts** (approach `gift`, no skill, no roll): the player hands over the items in
  `items_used` (must be held, else `item_not_owned` / `not_held`; none -> `item_not_owned` / `no_item`).
  Per item (each template once per NPC, world flag `gifted:<npc>:<template>`):
  - valued if the persona's optional `gifts: {<template key>: gain 1..50}` lists it (that gain), or else if
    the template's `value` >= 50 coins (`GIFT_VALUE_THRESHOLD`; gain `GIFT_DELTA` = 10);
  - declined (item kept) as `own_stock` if the NPC gives that template in any of its trades (no buying a
    gift from them to hand back), `already_given` if that template already moved this NPC, else
    `not_valued`.
  Accepted items are removed (`item_removed {key, name, given_to: <npc>}`) and their gains summed and
  applied **without** the friendly cap. The talk block gets `"gift": {"status": "accepted" | "declined",
  "items": [{key, name, accepted, reason?}]}`; `trade` is null and `requested_topic_status` is `none`. A gift
  is a resolved action (time passes). `gifts` is engine-only (never in context or the voice prompt). Demo
  personas: Mara values the crowbar (+20), the guard values rations (+15).
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
   v0.6.1: optional persona `gifts` map and talk approach `gift`.
9. v0.6: `tools/validate_content.py` (vendored, adapted from `/workspace/validate_content.py`, which is
   untouched) treats inline schema-notation snippets (e.g. `{key, label}`) as notation, not JSON failures,
   and checks `validate_proposal`'s error tuple (the original's dict check never matched).

## (e) LLM layer (v0.10)
`LLMClient` (rpg/llm.py): `interpret(player_text, context, reprompt=None) -> proposal dict (or
raw text)`, `narrate(result, context) -> str`, `voice_npc(persona_view, allowed_topics, payload, context) ->
{"lines": [...]}` (or its JSON text). Providers: `RPG_LLM` / `--llm` = `mock` | `openai` | `anthropic`.
v0.10: `interpret()` raises `InputTooLong` if `player_text` is longer than 300 characters (every
caller, not only the CLI). The CLI catches it and prints the same too-long message; no turn is spent.

- **Prompts:** loaded from the vendored `content/*_prompt.md`. System = the text after the `---` line with
  its context section (`## Game context` / `## Setting and tone` up to the first `## Rules` / `## Hard
  rules`) replaced by a pointer; user message = that section with `{placeholders}` filled (JSON values,
  compact). Interpreter/narrator values: the `build_context()` fields, `player_text`, `setting_summary`,
  `tone_notes`, and for the narrator `narration_result` = the engine result (incl. `dialogue`). Voice:
  `npc` = `persona_view`, `talk` (no npc key, no insight_read), `player_text`, `recent_dialogue`.
  Placeholders elsewhere in the prompts are references and stay as written.
  v0.9 / v0.10: `{player_text}` is filled as a fenced data block. The treat-as-data instruction
  lives in the prompt text (exactly once). `fence_player_text` HTML-escapes `&`, `<`, and `>` and
  wraps the result in `<player_input>…</player_input>`, so a typed `</player_input><system>…` cannot
  close the fence or inject tags (BUG-R6-01). This applies to interpret, narrate, and voice.
  Static examples in the system prompts must not use demo NPC keys or names (`npc_dozing_guard` /
  "Dozing Guard"); dark examples use `npc_unseen_<id>` and "unseen figure" only. Unseen context
  entries have no `name` (or `article`) field; prompts and the mock must not require one (BUG-R5-01).
  v0.10: HTML field-doc comments (`<!-- ... -->`) that used to travel in every user message are
  parked under "## Context field notes" on the cached system side.
  v0.10 voice: the payload (and the `{talk}` object already interpolated in the voice user message)
  includes `speaker_unseen: true` when the speaker is not visible to the player, else `false`.
  Dark voice lines replace the NPC's name (and a leading the/a/an) with "I" at the start of a
  sentence and "me" mid-sentence, never "ask for I at the gate".
- **Interpreter output schema:** `docs/action_proposal.schema.json` (`rpg/proposal_schema.py`), generated
  from the same constants as section (a)/(d): every field required, talk-only fields
  (`approach`, `topic`, `topic_text`, `quantity`) nullable on all actions, `ooc` object-or-null,
  `additionalProperties: false`. The 0-3 limit and talk rules stay with `validate_proposal`. Voice schema:
  `{"lines": [string]}`. Narration: plain text, through `narrate_safely` (regenerate once, closing line).
- **Re-prompt:** a non-JSON or invalid proposal -> the engine's `invalid_proposal.reprompt`; the client
  resends with the bad output as an assistant turn and the reprompt as a user turn. Second failure -> no turn.
- **OpenAI:** `POST https://api.openai.com/v1/responses`, `Authorization: Bearer $OPENAI_API_KEY`;
  body `{model, instructions, input: [{role, content}], max_output_tokens, store: false, text.format?:
  {type: json_schema, name, schema, strict: true}}`. Text = the `output_text` parts of `message` items;
  a `refusal` part or no text -> `LLMError`.
- **Anthropic:** `POST https://api.anthropic.com/v1/messages`, `x-api-key`, `anthropic-version:
  2026-10-21`; body `{model, max_tokens, system: [{type: text, text, cache_control: ephemeral}], messages,
  output_config?: {format?: {type: json_schema, schema}, effort?}}`. Text = the `text` blocks; none ->
  `LLMError` (with `stop_reason`).
- **Models:** per role (`interpret`, `narrate`, `voice`): `RPG_<P>_MODEL_<ROLE>` > `RPG_<P>_MODEL` >
  `RPG_LLM_MODEL` > default (OpenAI `gpt-6-luna` / `gpt-6.1-sol` for narrate; Anthropic `claude-haiku-5-5`
  / `claude-sonnet-5-5` for narrate).
- **Keys:** environment only; missing -> `LLMConfigError` naming the variable (except in dry-run). Keys are
  never logged, printed or included in errors (`redact`); `repr(client)` shows models only.
- **HTTP:** stdlib `urllib`; 60 s timeout; retry 408/409/429/500/502/503/504/529 and network errors up to 3
  times, backoff `min(8, 0.5 * 2^n) * (1..1.25)`, `Retry-After` honoured up to 8 s; anything else ->
  `LLMError` at once. The CLI shows interpreter errors without spending a turn.
- **Dry run:** `--dry-run` / `RPG_LLM_DRY_RUN=1`: print `{dry_run, provider, role, url, headers
  (redacted), body}` per request to stderr, send nothing, answer with `MockLLM`. Works without a key.

## Changes in v0.10.3
1. BUG-R8-01: one masking step (`unseen.for_llm`) sits at the interpret / narrate / voice payload
   boundary. Every field of the engine result and context is rewritten so real keys and names of
   NPCs unseen at that moment become their opaque `npc_unseen_<id>` / "unseen figure" form,
   including compound `flag_set` keys such as `defeated:npc_dozing_guard`. The world flag in the
   DB keeps the canonical key. `player_text` is not rewritten.
2. `yes` / `ok` / a bare pronoun with nothing pending is `unrecognized` and counts toward the
   combat free-streak. OOC commands, `/save`, `/load`, and a blank CLI enter are not turns: they
   neither count toward nor reset the streak. The streak is persisted in the save, so load cannot
   clear it.

## Changes in v0.10.2
1. BUG-R7-01: every mock/engine line that names an NPC goes through the article-aware `npc_ref`,
   including the player's attack and miss lines ("Your attack lands on the Gatekeeper.",
   "The Gatekeeper avoids your attack."). `article: ""` drops the default "the".
2. BUG-R7-02: `yes` (and other affirmations/bare pronouns) with several pending sound candidates
   re-asks the clarify by sound. The first re-ask is free and does not increment the combat
   free-streak; the second consecutive re-ask of the same pending question costs the round, so
   it is not a stall loophole. `yes` with nothing pending is still `unrecognized`.
3. BUG-R7-03: the same-reference clarify cap is the action type plus the resolved candidate set,
   not the spelling of the player's words.

## Changes in v0.10.1
1. Interpreter rules 4 and 16 (Story Designer v0.10): with exactly one living unseen NPC, a
   remembered name or real/invented `npc_` key resolves to that NPC's `npc_unseen_<id>` ref,
   without confirming or revealing the name. The engine asks by sound only when several unseen
   NPCs are present. The v0.10 combat stall cap still applies to that multi-NPC clarify (and to
   other free input). An unmatched name with nobody unseen here is still `unknown_key`.
2. No top-level `{speaker_unseen}` placeholder was added to `voice_messages`; the flag already
   travels inside `{talk}` as `talk.speaker_unseen` (and on the voice payload for the engine).

## Changes in v0.10
1. BUG-R6-01: player text is HTML-escaped before it is wrapped in `<player_input>`; a typed closer
   or nested tag cannot break out of the fence (interpret, narrate, and voice). The treat-as-data
   note is left in the prompt text only.
2. BUG-R6-02: in combat, the second consecutive clarify on the same unresolved reference costs the
   round (the question is still shown). Five consecutive free turns in combat, lit or dark, also
   cost, and later consecutive frees keep costing until a resolved action resets the streak. Verb-less replies
   `yes` (one candidate) and sound phrases resolve the pending `npc_unseen_*` target and carry out
   the original action.
3. BUG-R6-03: the persona `article` override is validated, copied onto lit context NPC entries and
   engine results, and masked away when the NPC is unseen. It shows up in actual play narration
   (e.g. "The Gatekeeper isn't here.").
4. `speaker_unseen` is set on the voice payload and on `{talk}`; dark name-swap uses I/me.
5. The 300-character input cap is enforced in `interpret()` (`InputTooLong`) for every caller.
6. Mock and `validate_proposal` always emit all 13 action-schema fields (talk fields null on non-talk).
7. Context field-doc comments move to the cached system prompt (~465 user tokens saved per interpret
   and per narrate, ~161 per voice; ~930 per non-talk turn). System text grows by the same amount.

## Changes in v0.9
1. BUG-R5-01: unseen NPC context entries are `{key, visible: false, sound}` only. The mock no longer
   reads `name` on them (a gift to "it" / "the breathing" in the dark used to crash). Live prompt
   paths fill the same payload; they must not assume a name either.
2. BUG-R5-02: re-applying a timed player condition (e.g. a second overwhelming daze on the expiry
   turn) bumps the row id so this turn's tick skips it; the new duration lasts in full and narration
   shows the condition applied, not faded.
3. Referring to an unseen NPC by name or real key is a free clarify by sound, not a wasted round,
   and is not resolved (see Time passing).
4. Narration does not put an article before a proper-name NPC (Mara, not "the Mara").
5. Player input is fenced in the user message; system-prompt examples no longer name demo unseen NPCs.

## Changes in v0.8
1. Real `OpenAIClient` / `AnthropicClient` (section (e)), prompt loading from content, the generated
   proposal schema, per-role models, dry-run. No engine rule changes (the v0.7.1 freeze holds).
2. Bug fixes from retest 884cdf1 (no new rules):
   - BUG-R4-01: `attack` with `target: null` gets the same default as bare `talk`: the only living NPC
     here (visible or only heard; unseen ones resolve to their `npc_unseen_<id>` ref in the result); with
     several, a free clarify "Who do you want to attack: A or B?" (in the dark: "the one making <sound>",
     never a name; bare `talk` now asks the same way); with none, `target_not_present` / `no_target`,
     narrated as "There's no one here to fight." (not the talk line).
   - Whenever `result.wasted_round` is set, the CLI makes sure the narration carries "You lose precious
     moments, and your foe doesn't wait." (prepended if the narrator left it out), whatever the action.

## Changes in v0.7.1 (engine features FROZEN for the slice after this)
1. Overwhelming NPC hits daze the player for 1 round (`npc_actions[].dazed`); the leather cap's once-per-rest
   cancel applies. Player conditions from the NPC phase skip that turn's tick.
2. Content re-synced (twice): items v0.8 (structured `perk` / rope `enables`+`skill_bonus`, equal to the
   engine defaults) and gatekeeper `gifts`; then the updated prompts/schema/README. Structured fields are
   read when present; `SUBCATEGORY_DEFAULTS` / `EQUIP_PERKS` stay the fallback when a template omits them
   (tested with field-less variants).
3. Darkness never names an unseen NPC: context carries `{key: npc_unseen_<id>, visible: false, sound}`
   only; results are masked (ref + "unseen figure"); optional persona field `dark_sound` (validated
   non-empty string; demo Mara and guard have one), generic fallback. See (a) Darkness.
4. Whistle: structured `stats.noise` (`distract`, `once_per_foe`, `heard_range`) with the subcategory
   `signal` defaults as the fallback.
**Feature freeze:** from v0.7.1 the slice's engine rules are frozen; later changes are bug fixes, content
sync and the LLM layer (v0.8).

## Changes in v0.7
1. Rope: `subcategory: climbing` items are climbing tools; athletics `move` = method `climb`.
2. Equipment perks: leather cap (one daze/stun per rest), work gloves (climb / rough-item advantage);
   boots none. New consequences `perk_used {key, name}` and `condition.negated_by`.
3. Whistle: `subcategory: signal` items make `noise_made`; a hostile here is `distracted` once (attack at
   disadvantage). NPC conditions may carry `attack_disadvantage`.
4. Mock: "blow" maps to `use_item`.
No schema change (still v4).

## Changes in v0.6.1
1. BUG-R3-01 (Lead): `unrecognized` input never wastes a round, even in combat; only parsed-but-rejected
   actions do (time-passing table).
2. BUG-R3-02: paraphrased subjects fold into the last subject for the no-retry rule.
3. New leverage: citing (via `topic_text` next to the subject `topic`) a topic revealed since the last try.
4. Content re-synced to the Story Designer's v0.6 (8 files); its inline snippets are now all JSON.
5. `look` in the dark lists the felt items (see context).
6. Lead: the friendly cap covers trades' +2 too; new `gift` approach (persona `gifts`, value threshold 50)
   and `talk.story_disposition` are the only ways to allied.

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

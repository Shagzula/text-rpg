# NPC Persona Schema (AGREED, matches contracts.md v0.6 §d)

_Status: AGREED (matches contracts.md v0.6 §d), implemented in `rpg/personas.py` + `rpg/talk.py`, Nov 10, 2026 (CST). Setting-neutral. Example: `content/npcs/npc_example_gatekeeper.yaml`. Engine demo personas with the same shape: `code/rpg/data/npcs/npc_mara.yaml`, `npc_dozing_guard.yaml` (engine-owned)._

## Division of labour (decided)
- **The engine owns facts and disposition.** `npcs.disposition` is an INT from -100 to 100, banded by `rules.disposition_band()`: `hostile` (<= -50), `unfriendly` (<= -10), `neutral`, `friendly` (>= 10), `allied` (>= 50).
- **Only the engine changes disposition:** social checks against the NPC's DC (persuasion, deception, intimidation, insight), a completed trade, and engine events (being attacked sets -100).
- **The LLM only voices lines.** The engine never parses or trusts NPC lines. No item, coins, quest update, or disposition change comes from them. Only the engine result counts.
- **Prompt exposure:** only `persona_view` (`key`, `name`, `voice`, `traits`, `goals`) reaches the voice prompt. Topic **text** reaches a prompt only when the engine puts it in `allowed_topics`. `insight_tell` goes only to the narrator, and only after a successful insight check. `never` secrets and the disposition integer never reach a prompt.

## One file per NPC
Parsed by `rpg/yamlish.py` (a stdlib YAML subset: block maps and lists, flow `[..]` / `{..}`, quoted or plain scalars, comments) and validated by `rpg.personas.validate(persona, template_keys)`. At seed the persona is joined to its `npcs` row by `key`, and the full persona (secrets included) is stored engine-side in `npc_personas`. Engine demo personas live in `code/rpg/data/npcs/`. Content personas will live in `content/npcs/` once the setting exists.

| field | type | required | notes |
|---|---|---|---|
| `key` | string | yes | `npc_<slug>`. Same as `npcs.key`. |
| `name` | string | yes | → `npcs.name` at seed |
| `description` | string | no | → `npcs.description` (short, visible to the player) |
| `voice.style` | string | yes | 1–3 sentences: diction, rhythm, verbal habits |
| `voice.sample_phrases` | string[] | yes | 2–4 example lines. Style only, no facts the player shouldn't get. |
| `traits` | string[] | yes | → `npcs.traits`. Snake_case adjectives. |
| `goals` | string[] | yes | **Public** motivations. Anything hidden belongs in `secrets`. |
| `base_disposition` | int | yes | -100..100 → `npcs.disposition` at seed |
| `combat` | map | no | **Read once at seed** into the `npcs` columns. Keys (any subset, no others): `defense_dc` (int > 0, defense), `max_hp` (int > 0, HP; also resets `current_hp` to full), `attack_bonus` (int, attack), `damage` (dice string, e.g. `"1d6"`), `save_bonus` (int, save). Column defaults otherwise: attack_bonus 2, damage "1d4", save_bonus 0. |
| `social.base_dc` | int | no (default 12) | Base DC for social checks against this NPC |
| `social.persuasion` / `deception` / `intimidation` / `insight` | int | no (default 0) | DC modifier per skill |
| `insight_tell` | string | no | What a successful insight check shows the narrator (`insight_read`). Never sent to the voice prompt. |
| `knowledge` | map topic_key → `{label, text, aliases?}` | yes | Public topics. `label` = short phrase the interpreter matches against and the player can see. `text` = the fact. |
| `secrets` | map topic_key → `{label, text, min_band, aliases?}` | no | `min_band` is a band name or `never`. The **label is a hint**: it appears in context `topics` once the band reaches `min_band` (the player sees there's something to ask about). The **text** is revealed only when the engine allows it. `never` = never listed, never revealable through dialogue. |
| `reveal_by_band` | map band → `{topics: [topic_key], trades: [trade_key]}` | yes | **Cumulative upward.** Exactly the five bands (empty lists allowed). |
| `trades` | map trade_key → `{label, aliases?, gives: [entry], wants: [entry], repeatable?}` | no | `gives` = what the NPC hands the player; `wants` = what the player pays. An entry is **`{item: <template key>, qty: N, name?: <display name>}`** or **`{gold: N}`** (coins, same unit as `item_templates.value`). `name` is allowed on `gives` only, digit-free, and becomes the new instance's `display_name`. `repeatable: true` = can be done any number of times, and **1–10 at once** through the talk action's `quantity` (`MAX_TRADE_QTY` 10; gold and items are multiplied). Default false = **one-time**: quantity must be 1, and once done it stays in context with `done: true` (asking again → `trade_done`). |
| `aliases` (on knowledge, secrets, trades) | string[] | no | Exact phrases the interpreter may match (after dropping determiners and singularising), alongside the key, the `label` segments split on "/", and, for trades, the item names in gives/wants. No substring matching. Keep them short and unambiguous across the NPC. A secret's aliases are listed only once the secret is visible. |

Topic keys and trade keys are snake_case and unique within the NPC. Knowledge and secret keys share one namespace.

## Load-time validation (engine, `personas.validate`)
1. Required: `key` (starts `npc_`), `name`, `voice` (`style` + `sample_phrases` list), `traits`, `goals`, `base_disposition` (int -100..100), `knowledge`, `reveal_by_band`.
2. Every topic and secret has `label` and `text`. Every secret `min_band` is a band or `never`. `aliases`, where given (topics, secrets, trades), is a list of non-empty strings.
3. `reveal_by_band` has exactly the five bands. Every listed topic exists, every listed trade exists. A secret may only be listed at a band ≥ its `min_band`, and a `never` secret must not be listed anywhere.
4. `combat` has only the five keys. `damage` must parse as dice; the others are ints (`defense_dc` and `max_hp` > 0).
5. Every trade has a `label`. Entries: `gold` a positive int; otherwise `item` a known template key, `qty` a positive int, optional `name` on `gives` only, with no digits.

## Derived at runtime (engine)
- **Context `npcs_present[].topics`** = `[{key, label, aliases?}]` for **every public `knowledge` topic**, plus secrets whose `min_band` ≤ the current band. Labels and aliases only, never text.
- **Context `trades`** = `[{key, label, aliases?, gives, wants, done?}]` for trades listed in `reveal_by_band` at or below the current band, with band-priced gives/wants. Completed one-time trades stay listed with `done: true`.
- **Available topics this turn** = cumulative `reveal_by_band` topics up to the band **after** this turn's disposition change. A successful intimidate evaluates them **one band above the band before the drop** (cowed).
- **Requested topic status:**
  - `revealed`: available and the check (if any) succeeded. **Once revealed, a topic stays revealed** and is given again on request, whatever the band or check.
  - `refused`: the topic is visible in context but not available yet, or the check failed (label only). This includes public topics not yet opened at this band and visible secrets.
  - `unknown`: a secret not visible at this band, a `never` secret, or a key or subject the NPC doesn't have (so a hidden secret's existence is never confirmed).
  - `none`: no subject (`topic` and `topic_text` null, small_talk / farewell, or a check-based approach with no subject such as a bare insight).
- **No-retry rule** (Lead, DECIDED; 5e): each paired approach on an NPC records `social_try:<npc>:<approach>:<subject>`. The same NPC + approach + subject again → `impossible_action` / `no_new_leverage` (no roll; no time outside combat), unless 10+ turns have passed, the NPC's disposition changed, or `items_used` includes something new. A different approach or subject is a new try.
- **Social cap** (Lead, DECIDED): persuade/deceive gains never push disposition above 49 (top of `friendly`). `allied` comes only from other means (quests, story events, a trade's +2). An NPC already above 49 is never lowered by the cap.
- **Social DC** (paired approaches only) = `social.base_dc` + `social.<skill>` + band modifier (hostile +5, unfriendly +2, neutral 0, friendly -2, allied -5). `est_difficulty` is ignored for persona NPCs.
- **Approach/skill pairing:** `persuade` → persuasion, `deceive` → deception, `intimidate` → intimidation, `insight` → insight, others null. A mismatch is corrected; a missing `approach` is inferred from the skill (else `ask`).

## Trades and coins (engine)
- **Band pricing of coin entries** (the result's `trade` block shows the priced amount). The player pays (`wants`) base × hostile 1.5 / unfriendly 1.25 / neutral 1.0 / friendly 0.9 / allied 0.75, rounded **up**, min 1. The player receives (`gives`) base × hostile 0.5 / unfriendly 0.75 / neutral 1.0 / friendly 1.1 / allied 1.25, rounded **down**, min 1. Item entries are never scaled. (Hostile normally lists no trades.)
- **Bare trade** (`approach: trade`, `topic` and `topic_text` null) → `offered` with `key/label: null` and `options: [{key, label, aliases?, gives, wants}]` listing every open trade; `refused` if none. **Never falls back to a single listing.**
- **Engine rejections** (no time outside combat): `topic` null + `topic_text`, or a non-trade key on a trade approach → `no_such_trade`; a completed one-time trade → `trade_done`; quantity outside 1–10, > 1 on a one-time trade, or on a bare trade → `bad_quantity`; wanted items not held (× quantity) → `item_not_owned` / `trade_item_missing`.
- `topic` = a trade key → `completed` if it's open at the band and the player holds every wanted item (by template, summed across stacks) and has the coins; short of coins → `unaffordable`. With `quantity` > 1 the trade block carries `quantity` and multiplied amounts. Not open at the band → `refused` (with its label only if the trade is listed at or below the current band; a higher-band trade comes back with `label: null`).
- **Completion** (engine only): wanted items removed (`item_removed`), coins move (`characters.gold`; consequence `purse_changed {direction: spent | received}`, no amount), given items created (`item_added`, using `name` as display name), disposition +2. One-time trades set `trade_done:<npc>:<trade>`.

## Disposition deltas (engine, contracts v0.5)
| approach (skill) | success narrow / clear / overwhelming | failure narrow / clear / overwhelming |
|---|---|---|
| persuade (persuasion) | +10 / +15 / +25 (capped at 49) | 0 / -5 / -10 |
| deceive (deception) | +10 / +15 / +20 (they believe you; capped at 49) | -10 / -20 / -30 (caught lying) |
| intimidate (intimidation) | -10 / -10 / -15, **plus topics evaluated one band above the pre-drop band** | -10 / -15 / -25 |
| insight (insight) | 0. Success gives the narrator `insight_read` = `insight_tell`. | 0 |
| ask / small_talk / trade / farewell (no check) | 0; a completed trade +2 (not capped) | n/a |
| engine events | attacked: set to -100 (hostile) | |

An auto-success counts as `clear`. Clamp to -100..100 after each change. A band change emits `npc_disposition_changed {key, to}`.

## Diff against the engine demo personas (v0.6)
`npc_mara.yaml` and `npc_dozing_guard.yaml` use exactly the fields above (`combat`, coin entries, `repeatable`, `gives[].name`, `aliases` on topics, secrets and trades), and both pass the same validator as the gatekeeper example. No field in this schema is unknown to `personas.validate`, and the demo files use no field this schema lacks.

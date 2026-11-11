# content/: LLM prompt layer + tier-1 item data (v0.9)

_Updated Nov 10, 2026 (CST). Everything here is setting-neutral, with no world content until Dan picks a setting. All shapes: **AGREED (matches contracts.md v0.7)**. Authoritative source: `code/docs/contracts.md`. This directory is the source of truth; the Engine Coder vendors it into `code/content/` (drift-tested). Nothing under `code/` was edited. Previous snapshot: `/workspace/content_backups/v08_pre_v07_1826/`._

## Files
| file | what it is |
|---|---|
| `interpreter_prompt.md` | Player text → JSON action proposal. Shared context block (incl. `felt_items` with loose items after a dark search; NPC `topics`/`trades` with `aliases`, priced `gives`/`wants`, `done`). 17 rules: feel (`examine`, no roll, finds fixtures) vs. search (`examine` + `investigation`, a roll, also finds loose items); **exact** topic/alias matching; trade `quantity` ("buy three rations" → 3, never clamped); bare trade vs. unmatched deal; no-retry rule (new leverage goes in `items_used`); rest refused with a hostile here or next door; talk to an absent NPC isn't retargeted. 24 examples (new: feel around, quantity + alias, bare trade, persuasion retry with new leverage). v0.8: `gift` approach; leverage retries cite a learned topic in `topic_text` next to the subject `topic`. Now 26 examples (+ cited-topic retry, + give). |
| `narrator_prompt.md` | Engine result → second-person prose ending in `What do you do?`. Adds `wasted_round` (narrate the fumble or hesitation, then the hostiles act), `follow` NPC actions, touch-only `felt` results, bare-trade `options` (only the listed labels, names, and prices), `quantity`, the social cap, and detail rows `no_new_leverage`, `no_such_trade`, `trade_done`, `trade_item_missing`, `bad_quantity`, `hostile_nearby`. 17 examples (new: wasted round, fleeing with pursuit, feeling in the dark, bare trade without voiced lines). v0.8: gifts (reaction shown only by the band change, decline reasons in-world), leverage retries, "Close by in the dark you hear …" (a sound, never a name), looking around in the dark, `noise_made`/`perk_used`/`negated_by`. Now 18 examples (+ gift). |
| `items_tier1.yaml` | 35 tier-1 templates with `item_` ids. No digits in names (quantities in stats). `condition_effect` on the Bracing Tonic and Flash Powder (both read by the engine since v0.5). `improvised_pick` on the knife (implemented: DC 25). `rarity_*`/`avail_*` tags. Loads 35/35 via `rpg.db.add_item_template()`. v0.8: structured `enables`/`skill_bonus` on the rope and `stats.perk` on the cap and gloves, equal to the engine defaults; boots cosmetic. |
| `schemas_proposed.md` | Prompt-layer summary of contracts.md v0.6: context, (a)–(c), and (d)–(g) dialogue, incl. `felt`, `wasted_round`, `follow`, `quantity`, `options`, exact topic matching, the no-retry rule and the social cap. All inline snippets are valid JSON. |
| `npc_persona_schema.md` | **AGREED** persona format (matches `personas.validate` and Mara/guard): adds `aliases` on topics, secrets and trades; repeatable trades in quantities of 1–10; `done: true`; trade rejections; the no-retry rule; the social cap at friendly. v0.7: `gifts` map with documented defaults, the widened friendly cap, and cited-topic leverage. |
| `npcs/npc_example_gatekeeper.yaml` | Example persona with aliases on every topic and trade, a repeatable coin purchase (quantity 1–10), a one-time sale, an item swap, and a `combat` block. Verified in an in-memory DB: bare trade → `options`, three rations → 18 coins, selling unheld rope → `trade_item_missing`, unmatched deal → `no_such_trade`, quantity 11 → `bad_quantity`. v0.7: `gifts` (bracing tonic +20, torch +10). |
| `npc_voice_prompt.md` | **AGREED** voice_npc prompt: `{"lines": ["...", "..."]}`, 1–3 lines ≤ 40 words, only `allowed_topics`, refused topics deflected with a hint, coin prices as given (× quantity), bare-trade `options` listed without invention, no devotion beyond the band. 7 examples. v0.4: gift thanks and declines, reacting to a cited topic. 9 examples. |

## Settled (contracts.md v0.5)
- The engine owns all DCs. `est_difficulty` is a hint; persona NPCs use social DCs. The narrator never sees check numbers.
- Keys are `loc_`/`npc_`/`item_` slugs (extras `_2`, `_3`). Exits are targeted by destination `loc_` key or direction.
- Shared placeholders filled from `build_context()`: `{location}`, `{npcs_present}`, `{visible_items}`, `{felt_items}`, `{player_inventory}`, `{player_sheet}` (features `[]`, `gold`), `{hp_band}`, `{recent_narrations}`, plus `{setting_summary}` and `{tone_notes}`. The block is byte-identical in both prompts.
- More than 3 actions is invalid (re-prompt). `unresolved_references` is ambiguity-only. Claimed possessions → `item_not_owned`, absent targets → `target_not_present`.
- A failed check skips dependent later actions (`skipped`, never narrated). `degree` is null whenever nothing was rolled. Rejections carry `detail`.
- NPCs counterattack (`npc_actions`), unseen ones included. `expired[]`, `time_passed`. Death: no closing question, and the CLI offers load / new / quit (provisional).
- Tools apply automatically. `pick_lock` needs a lock tool or an `improvised_pick` item (DC 25), else `needs_tool`.
- **Talk:** `approach` is inferred from the skill when missing (interpreters still emit it), and a skill mismatch is corrected. Context `topics` list all public knowledge plus secrets unlocked at the band (labels as hints). A not-yet-open topic → `refused`. Revealed topics stay revealed. A successful intimidate reads topics one band above the pre-drop band. `none` = no subject.
- **Trades are coin or item:** entries `{item, qty, name?}` or `{gold: N}`; coin prices are band-adjusted (buying ×1.25 unfriendly to ×0.75 allied, selling ×0.75 to ×1.25; hostile ×1.5 / ×0.5); one-time or `repeatable`; completion emits `item_removed` / `item_added` / `purse_changed` (no amount).
- **Lead balance rules (DECIDED, v0.6/v0.6.1):** social checks follow the 5e no-retry rule (`no_new_leverage` unless there's new leverage: a new item in `items_used`, a topic learned since the last try cited in `topic_text`, a changed disposition, or 10 turns). Every small gain (social checks *and* trades' +2) caps at friendly (49). Only **gifts** (`approach: gift`; persona `gifts` or items worth 50+ for +10; once per item type; never the NPC's own stock; uncapped) and story events reach allied. In combat a *parsed* rejected action is a wasted round (`unrecognized` input never is). No resting with a hostile in the same or an adjacent room (`hostile_nearby`). A fled-from hostile follows once.
- **Dark touch path (v0.6):** feel = `examine`, no roll, finds fixtures; search = `investigation` roll, which also finds loose items. Results carry `felt`, narrated by touch only.
- **Trades (v0.6):** exact key/alias matching, `quantity` 1–10 on repeatable trades, a bare trade lists `options`, and never a fallback to another listing (`no_such_trade`, `trade_done`, `trade_item_missing`, `bad_quantity`).
- **Items v0.7:** structured perks in the YAML match the engine defaults exactly: the rope has `enables: [climb]` + athletics advantage; `stats.perk` on the leather cap (one stun/daze per rest) and the work gloves (climb / rough-item advantage). The whistle is keyed on `subcategory: signal` (it distracts each hostile once). The boots are cosmetic.
- **Voice step:** a separate voice_npc call (below); lines go to `dialogue[]` verbatim and are never parsed for claims.
- Lead: house rules kept. Scarcity: 60-coin purse, 1 starting draught, draught value 500, rarity/availability tags.

## Dialogue architecture: recommendation (separate voice_npc call)

**Pick: a separate `voice_npc` call**, made by the engine after it resolves a talk action and before the narrator, only for NPCs with a persona file. **Accepted and implemented in contracts.md v0.5 §d.** The returned lines go into `dialogue[]` (`source: "npc_llm"`), and the narrator uses them verbatim.

| criterion | single narrate call | separate voice_npc call |
|---|---|---|
| **Secret leakage** | Safe only if the engine filters to `allowed_topics`. The narrator prompt still has to carry persona voice/goals, and grows. | Same engine filter, **plus** the persona never enters the narrator, and the voice prompt never sees secrets. Defence in depth: the engine can n-gram-lint lines against secret text, because the model never had it. |
| **Consistency** | One voice writes both prose and speech, so tone stays naturally coherent. | Small risk of mismatch (prose vs. line tone). Mitigated: the narrator sees the lines and the `disposition_band_after`, and writes body language around them. Voice consistency per NPC is *better*: a dedicated prompt with style + sample phrases + recent lines. |
| **Parsing / validation** | Speech is buried in prose, so it can't be checked or logged per line. | `{"lines": ["...", "..."]}` is trivial to parse, clip, lint, log, and feed into `recent_dialogue`. The narrator output stays plain prose. |
| **Latency / cost** | 1 call per turn | +1 short call (~200 tokens out), **only on resolved talk turns with a persona NPC**. Can't run in parallel with the narrator (the narrator needs the lines). Acceptable for a text game. |
| **Failure handling** | n/a | Retry once, then `dialogue: []`. The narrator falls back to brief indirect speech of `allowed_topics` text only. |

Honest caveat: the main protection against leaking secrets is that **the engine filters to `allowed_topics`**. A single call could be built just as leak-proof that way. The split's real wins are a smaller, focused narrator, per-NPC voice consistency, and machine-checkable lines. Those outweigh one extra short call on talk turns, so I agree with the lean toward a separate call.

## Requests to the Engine Coder
1. **Whistle override:** the noise and distraction behaviour has no structured stats hook (it's keyed on `subcategory: signal` with no `effect`). If you want the YAML to be the source of truth here too, read something like `stats.noise: {heard_range: adjacent, distract: {condition: distracted, duration: 1 round, effect: {attack_disadvantage: true}, once_per_npc: true}}`. Until then the YAML only describes it in `use_note`/`notes`.
2. **Mock narration names unseen NPCs:** `MockLLM._describe_scene` prints "Close by in the dark you hear Dozing Guard". The narrator prompt uses the same phrasing but with a sound ("…slow, heavy breathing"), never the name, as agreed for `visible: false`. Consider making the mock match.

## Remaining assumptions
1. When there are more than 3 actions, the interpreter keeps the first 3 and notes the rest (matches the engine's re-prompt text).
2. When a reference is ambiguous, the interpreter sets both `clarification_needed` and `unresolved_references`, so the whole turn clarifies.
3. Value unit ≈ 5e silver piece (coins use the same unit). Placeholder premade: STR 8 / INT 16.
4. The narrator may state a trade's coin price (from the `trade` block, which the player was quoted), but never the purse total and never an amount for `purse_changed`.
5. Content personas will live in `content/npcs/` once the setting exists; the engine's demo personas stay in `code/rpg/data/npcs/`.
6. The interpreter copies a spoken quantity as-is (0, 15...) and lets the engine reject it (`bad_quantity`), rather than clamping it, which would silently change what the player asked for.
7. New leverage for a social retry is shown by listing the item the player presents in `items_used`. A merely spoken new argument isn't leverage the engine can see, so it still gets `no_new_leverage`.

## Open questions
1. **Setting, genre, tone** (Dan). These fill `{setting_summary}` and `{tone_notes}`, plus item and NPC renames.
2. **Magic:** does the setting have it? This affects `features`, spell claims, the staff-as-focus item, and the flash powder reskin.
3. **Premade character stats:** waiting on the setting. STR 8 / INT 16 are placeholders.
4. **Post-death flow:** provisional: the run ends, with no closing question, and the CLI offers load latest / new game / quit. Is a permanent design needed later (death vs. knocked out vs. captured)?

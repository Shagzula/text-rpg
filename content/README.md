# content/: LLM prompt layer + tier-1 item data (v0.7)

_Updated Nov 10, 2026 (CST). Everything here is setting-neutral, with no world content until Dan picks a setting. All shapes, including NPC persona and dialogue: **AGREED (matches contracts.md v0.5)**. Authoritative source: `code/docs/contracts.md`. Nothing under `code/` was edited. Snapshot of the previous version: `/workspace/content_backups/v06_pre_v05_1741/`._

## Files
| file | what it is |
|---|---|
| `interpreter_prompt.md` | Player text → JSON action proposal (`actions[]` 0–3, `ooc`, `unresolved_references`). Context block from `build_context()` (v0.5: `felt_items`, NPC `topics`/`trades`/`recent_dialogue`, `player_sheet.gold`), 17 rules (incl. darkness and `felt_items`, tools, engine-side refusals such as `needs_target`/`no_effect`, and talk with optional-but-preferred `approach` and coin trades), schema, 20 examples (incl. lockpicking without tools, acting in darkness, using a felt lever in the dark, and talk: ask, persuade with `topic: null`, trade, insight). |
| `narrator_prompt.md` | Engine result → second-person plain prose ending in `What do you do?`. Turn order (results → `npc_actions` → `expired`), `time_passed`, NPC counterattacks, darkness and unseen NPCs, arriving in the dark (`moved_to_location.dark`), `detail` table incl. `needs_target`/`no_effect`, clarify, `skipped` (never narrated), `dialogue[]` verbatim, the `talk` block incl. coin trades and `purse_changed` (a trade's price may be stated; never the purse total), HP bands, death (no closing question). 13 examples, incl. a completed coin trade. |
| `items_tier1.yaml` | 35 tier-1 templates with `item_` ids. No digits in names (quantities in stats). `condition_effect` on the Bracing Tonic and Flash Powder (both read by the engine since v0.5). `improvised_pick` on the knife (implemented: DC 25). `rarity_*`/`avail_*` tags. Loads 35/35 via `rpg.db.add_item_template()`. |
| `schemas_proposed.md` | Prompt-layer summary of contracts.md v0.5: context block, (a) proposal, (b) engine result, (c) item template, and the **ACCEPTED** dialogue sections (d) context topics/trades/gold, (e) talk fields, (f) `talk` block incl. coin trades and band pricing, (g) the voice_npc call and its post-processing. |
| `npc_persona_schema.md` | **ACCEPTED** persona format (matches `personas.validate` and the engine's Mara and guard demo personas): voice, traits, goals, knowledge, secrets (`min_band`/`never`; labels act as hints), cumulative `reveal_by_band`, trades (item or coin entries, `repeatable`, display `name`), optional `combat` block read at seed, social DC modifiers. Plus validation, topic statuses, trade pricing, and the disposition-delta table. |
| `npcs/npc_example_gatekeeper.yaml` | Setting-neutral example persona with an item swap, a repeatable coin purchase with a display name, a one-time coin sale, and a `combat` block. Passes `rpg.personas.load_file()`. |
| `npc_voice_prompt.md` | **ACCEPTED** system prompt for voice_npc: `{"lines": [...]}`, 1–3 lines of at most 40 words, reveals only `allowed_topics`, deflects refused topics with a hint of more (secret labels included), flat deflection for `unknown`, states coin prices exactly as given, never grants or promises. 6 examples. |

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
- **Voice step:** a separate voice_npc call (below); lines go to `dialogue[]` verbatim and are never parsed for claims.
- Lead: house rules kept. Scarcity: 60-coin purse, 1 starting draught, draught value 500, rarity/availability tags.

## Dialogue architecture: recommendation (separate voice_npc call)

**Pick: a separate `voice_npc` call**, made by the engine after it resolves a talk action and before the narrator, only for NPCs with a persona file. **Accepted and implemented in contracts.md v0.5 §d.** The returned lines go into `dialogue[]` (`source: "npc_llm"`), and the narrator uses them verbatim.

| criterion | single narrate call | separate voice_npc call |
|---|---|---|
| **Secret leakage** | Safe only if the engine filters to `allowed_topics`. The narrator prompt still has to carry persona voice/goals, and grows. | Same engine filter, **plus** the persona never enters the narrator, and the voice prompt never sees secrets. Defence in depth: the engine can n-gram-lint lines against secret text, because the model never had it. |
| **Consistency** | One voice writes both prose and speech, so tone stays naturally coherent. | Small risk of mismatch (prose vs. line tone). Mitigated: the narrator sees the lines and the `disposition_band_after`, and writes body language around them. Voice consistency per NPC is *better*: a dedicated prompt with style + sample phrases + recent lines. |
| **Parsing / validation** | Speech is buried in prose, so it can't be checked or logged per line. | `{"lines": [...]}` is trivial to parse, clip, lint, log, and feed into `recent_dialogue`. The narrator output stays plain prose. |
| **Latency / cost** | 1 call per turn | +1 short call (~200 tokens out), **only on resolved talk turns with a persona NPC**. Can't run in parallel with the narrator (the narrator needs the lines). Acceptable for a text game. |
| **Failure handling** | n/a | Retry once, then `dialogue: []`. The narrator falls back to brief indirect speech of `allowed_topics` text only. |

Honest caveat: the main protection against leaking secrets is that **the engine filters to `allowed_topics`**. A single call could be built just as leak-proof that way. The split's real wins are a smaller, focused narrator, per-NPC voice consistency, and machine-checkable lines. Those outweigh one extra short call on talk turns, so I agree with the lean toward a separate call.

## Remaining assumptions
1. When there are more than 3 actions, the interpreter keeps the first 3 and notes the rest (matches the engine's re-prompt text).
2. When a reference is ambiguous, the interpreter sets both `clarification_needed` and `unresolved_references`, so the whole turn clarifies.
3. Value unit ≈ 5e silver piece (coins use the same unit). Placeholder premade: STR 8 / INT 16.
4. The narrator may state a trade's coin price (from the `trade` block, which the player was quoted), but never the purse total and never an amount for `purse_changed`.
5. Content personas will live in `content/npcs/` once the setting exists; the engine's demo personas stay in `code/rpg/data/npcs/`.

## Open questions
1. **Setting, genre, tone** (Dan). These fill `{setting_summary}` and `{tone_notes}`, plus item and NPC renames.
2. **Magic:** does the setting have it? This affects `features`, spell claims, the staff-as-focus item, and the flash powder reskin.
3. **Premade character stats:** waiting on the setting. STR 8 / INT 16 are placeholders.
4. **Post-death flow:** provisional: the run ends, with no closing question, and the CLI offers load latest / new game / quit. Is a permanent design needed later (death vs. knocked out vs. captured)?

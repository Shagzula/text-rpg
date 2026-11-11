# Text RPG engine (M1.5: talk and gold)

Hybrid text RPG: an LLM interprets free-form player actions and narrates; this Python +
SQLite engine owns all state and rules. Spec: `../DESIGN.md`. JSON contracts: `docs/contracts.md`.

## Layout
- `rpg_engine.py` - Dan's starter, now a thin API + demo on top of `rpg/`
- `rpg/schema.sql` - SQLite schema (v4; v2/v3 saves migrate on open; foreign keys on; content tables have INTEGER `id` + unique TEXT `key`)
- `rpg/db.py` - `init_db(path)`, connections, character/condition load+save
- `rpg/rules.py` - pure 5e math, `resolve_skill_check()`, `resolve_attack()`, dice, HP/disposition bands
- `rpg/inventory.py` - item helpers by key: add/remove/transfer/list/has, equip/unequip with slot conflicts
- `rpg/actions.py` - `process_action_proposal(conn, character_id, proposal, rng=None)`: proposal -> narrator result
- `rpg/effects.py` - NPC counterattacks, player defense, light/darkness, tools, item conditions, timers
- `rpg/talk.py` - talk resolution (social DCs, disposition, topics, trades/coins) and the `voice_npc` step
- `rpg/personas.py` - NPC persona load/validation (`npc_persona_schema.md`), band helpers, prompt-safe views
- `rpg/yamlish.py` - stdlib YAML-subset parser for content files (personas, `items_tier1.yaml`)
- `rpg/data/npcs/` - demo personas (Mara, the dozing guard)
- `rpg/context.py` - `build_context(conn, character_id)`: the prompt context block; narration log
- `rpg/llm.py` - `LLMClient` protocol, `MockLLM`, narration post-processing, provider factory (`RPG_LLM`)
- `rpg/world.py` - built-in demo slice
- `rpg/cli.py` / `rpg/__main__.py` - terminal turn loop (`python3 -m rpg`)
- `tests/` - pytest suite

## Play (mock mode, no API key needed)
```bash
cd /home/box/rpg-project/code
python3 -m rpg --new            # start a new game (built-in demo slice)
python3 -m rpg                  # continue the working save
python3 -m rpg --load mysave    # load a save slot at start
python3 -m rpg --new --seed 5   # deterministic dice
python3 rpg_engine.py play ...  # same CLI via Dan's starter file
```
Type plain actions: `look`, `go east`, `take crowbar`, `pick the lock`, `use crowbar on door`,
`desperately kick the door`, `light torch`, `down` / `d`, `up` / `u`, `back`, `pull lever`, `attack guard`,
`throw flash powder at guard`, `drink tonic`, `drink healing draught`, `i`, `rest`, `wait`.
In the dark: `look` (shows the darkness and the ways out), `search` / `feel around` (investigation at disadvantage), `pull lever` (fixtures can be found by touch).
Talk: `talk to mara`, `ask mara about the cellar`, `persuade mara about ...`, `lie to the guard about ...`,
`threaten the guard`, `read mara` (insight), `hello mara`, `goodbye mara`, `trade with mara`,
`buy a torch from mara`, `sell rope to mara`. Bare `talk` talks to the only person here (or asks which).
Directions: `north`...`southwest`, `up`, `down`, `in`, `out`, and `n s e w ne nw se sw u d`, alone or after
`go`/`walk`/`head`/`climb`. Input over 300 characters is refused without spending a turn.
Anything the engine can't model ("cast fireball", "xyzzy") is answered "Nothing happens" and costs no turn,
except in combat: with a hostile NPC in your room any refused action is a wasted round and it gets to act.
In the dark, `feel around` / `feel for the lever` find things by touch (no roll); `search` rolls
investigation and, on a success, finds loose items too.
Meta commands: `/save [name]`, `/load [name]`, `/saves`, `/stats`, `/debug` (show hidden check numbers),
`/help`, `/quit`. Typing `quit`, `save` or `help` without the slash works too.

Demo slice: Town Square (Mara, a crowbar, a bracing tonic) -> east -> Storeroom (dozing guard, hanging
lamp, flash powder, locked iron-banded door, DC 15) -> down -> Cellar (**dark**: light your torch first;
rope, a lever that sets a flag). You start with a shortsword, boots, a healing draught, lockpicks, a torch,
rations and a purse of 60 coins (`/stats` shows it).

**Talk** (contracts v0.6 §d): NPCs with a persona (`rpg/data/npcs/*.yaml`) have topics they'll share at
their current disposition band, secrets they share only when they like you enough (or never), and trades
priced in coins by band. Persuading, lying and intimidating roll against the persona's social DC and move
disposition; the engine decides what may be said, and a separate `voice_npc` LLM call only phrases it
(the mock has a deterministic voice). If the voice call fails, the narrator reports the facts indirectly.
You can't retry the same persuasion/lie/threat on the same subject without new leverage (10 turns, a
changed attitude, or a new item), and talking or trading someone up stops at friendly; only a gift they value (`give the crowbar to
mara`) or a story event takes them further. `trade` alone lists the deals;
`buy 3 torches` trades a repeatable deal up to 10 times.

Gear (contracts v0.7): a rope helps you climb (athletics moves, or when you use it); worn work gloves help
with climbing and rough or sharp things; a worn leather cap shrugs off one daze or stun per rest; blowing a
signal whistle is heard next door and throws a hostile off its next swing (once per foe).

Combat: hostile NPCs in your room attack once after your actions each turn (d20 + their attack bonus vs
your defense = 10 + DEX mod, or armor `base_ac` + capped DEX, plus shield bonus). The guard (defense 11,
12 HP, +3, 1d6) is a real fight for the 10-HP premade (defense 12). Rest heals half your missing HP, or
to full if you carry rations (one is eaten); it's blocked while a hostile NPC is here or in a room next
door. Flee and a hostile follows you once into the next room, then stays there (even in the dark).

**Death** (provisional, `rpg/cli.py: death_policy`): at 0 HP the run ends after the narration and you
choose: load your most recent save slot, start a new game, or quit. Continuing a dead working save goes
straight to that choice.

### Saves
- The **working save** is one SQLite file: `--db PATH`, else `$RPG_DB_PATH`, else `saves/rpg_state.db`
  (not tied to the current directory). Every turn commits to it, so quitting never loses progress and
  the next start continues it. `--new` replaces it with a fresh demo slice.
- `/save NAME` snapshots the working file to `slot_NAME.db` in the same directory with SQLite's online
  backup API (a consistent copy). `/load NAME` copies a slot back over the working file. Each slot is a
  complete, independent game, and `save_games` inside it records the name and turn. `/saves` lists slots.
- The **dice state is saved with the game** (after every turn, in `world_flags._rng_state`), so continuing
  or loading a slot resumes the same dice stream. `--seed` only seeds a game with no stored dice yet
  (a `--new` game).
- `saves/` and `*.db` are git-ignored.

### Environment variables
| var | meaning |
|---|---|
| `RPG_DB_PATH` | working save file (overridden by `--db`) |
| `RPG_LLM` | `mock` (default), `openai`, `anthropic` (overridden by `--llm`) |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | read from the environment only, never stored in code. Real providers are stubs for now: they fail clearly without a key and raise `NotImplementedError` with one. |
| `RPG_LLM_MODEL` | model name for a real provider |

Mock mode (`rpg/llm.py: MockLLM`) maps text to proposals with deterministic keyword rules and narrates
with templates. Narration ends with "What do you do?" (except when you die): a missing closing line is appended, and
empty or malformed output (JSON, markup, code fences) is regenerated once. A malformed proposal
re-prompts the interpreter once, then shows a short "try rephrasing" message without spending a turn.

## Tests and the engine demo
```bash
python3 -m venv .venv && .venv/bin/pip install pytest   # stdlib only otherwise
.venv/bin/python -m pytest -q                            # run tests
python3 rpg_engine.py [db_path] [seed]                   # engine JSON demo (throwaway DB unless db_path given)
```

## Skill check rules (tunable constants in `rpg/rules.py`)
- **Auto-success**: proficient/expertise, DC <= 15, and 10 + modifier >= DC. No roll.
- **Impossible**: needs more than 18 on the d20 (DC - modifier > 18). Fails, no roll.
- **Last-ditch** (player goes all out): bypasses the impossibility filter, +5 to the roll,
  and always applies a lasting condition: success -> `strained_<ability>` (-1 to that
  ability's checks), failure -> `injured_<ability>` (-2). If even 25 + modifier < DC it fails
  without a roll and still injures.
- **No natural 20/1 rules**: total >= DC succeeds.
- **Raw ability checks**: pass `str`/`dex`/... (or `strength`) as the skill; no proficiency, no auto-success.
- **Narration view**: `CheckResult.to_narration_dict()` gives outcome, degree
  (`narrow` 0-4 / `clear` 5-9 / `overwhelming` 10+ margin; null when nothing was rolled), reason,
  flags, consequences, state changes, and no numbers. Numbers go to the `rpg.debug` logger via `log_debug()`.
- **HP bands**: `rules.hp_band()` returns unhurt / scratched / wounded / badly_wounded / near_death / down.
- **Advantage / disadvantage**: 2d20 keep higher / lower; both cancel. Disadvantage blocks auto-success.
  From conditions (e.g. the bracing tonic), relevant tools (crowbar: athletics), darkness (perception /
  investigation), and attacking a blinded NPC.
- **Conditions don't stack**: re-applying one you have keeps a single row (the stronger effect wins).
  Timed conditions and lit lights tick down each turn that spends time.

## Content
`content/` is a vendored copy of the Story Designer's `/home/box/rpg-project/content/` (the source of truth;
don't edit either from here). `tests/test_vendored_content.py` runs `tools/validate_content.py` and fails
if the copy drifts; `python3 tools/check_content_drift.py --sync` re-copies.

## Turn API
See `docs/contracts.md` (v0.6) for the proposal and result shapes, validation codes, DC rules and
state changes. `process_action_proposal` writes through the connection you pass and does not commit:
```python
from rpg import db
from rpg.actions import process_action_proposal
from rpg.context import build_context
with db.connect() as conn:            # commits on exit
    ctx = build_context(conn, cid)    # render into the interpreter prompt
    result = process_action_proposal(conn, cid, proposal_json)
```

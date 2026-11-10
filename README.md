# Text RPG engine (M1)

Hybrid text RPG: an LLM interprets free-form player actions and narrates; this Python +
SQLite engine owns all state and rules. Spec: `../DESIGN.md`. JSON contracts: `docs/contracts.md`.

## Layout
- `rpg_engine.py` - Dan's starter, now a thin API + demo on top of `rpg/`
- `rpg/schema.sql` - SQLite schema (v2, foreign keys on; content tables have INTEGER `id` + unique TEXT `key`)
- `rpg/db.py` - `init_db(path)`, connections, character/condition load+save
- `rpg/rules.py` - pure 5e math, `resolve_skill_check()`, `resolve_attack()`, dice, HP/disposition bands
- `rpg/inventory.py` - item helpers by key: add/remove/transfer/list/has, equip/unequip with slot conflicts
- `rpg/actions.py` - `process_action_proposal(conn, character_id, proposal, rng=None)`: proposal -> narrator result
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
Type plain actions: `look`, `go east`, `take crowbar`, `pick the lock`, `desperately kick the door`,
`down`, `pull lever`, `attack guard`, `drink healing draught`, `i`, `rest`, `wait`.
Meta commands: `/save [name]`, `/load [name]`, `/saves`, `/stats`, `/debug` (show hidden check numbers),
`/help`, `/quit`. Typing `quit`, `save` or `help` without the slash works too.

Demo slice: Town Square (Mara, a crowbar) -> east -> Storeroom (dozing guard, hanging lamp, locked
iron-banded door, DC 15) -> down -> Cellar (rope, a lever that sets a flag).

### Saves
- The **working save** is one SQLite file: `--db PATH`, else `$RPG_DB_PATH`, else `saves/rpg_state.db`
  (not tied to the current directory). Every turn commits to it, so quitting never loses progress and
  the next start continues it. `--new` replaces it with a fresh demo slice.
- `/save NAME` snapshots the working file to `slot_NAME.db` in the same directory with SQLite's online
  backup API (a consistent copy). `/load NAME` copies a slot back over the working file. Each slot is a
  complete, independent game, and `save_games` inside it records the name and turn. `/saves` lists slots.
- `saves/` and `*.db` are git-ignored.

### Environment variables
| var | meaning |
|---|---|
| `RPG_DB_PATH` | working save file (overridden by `--db`) |
| `RPG_LLM` | `mock` (default), `openai`, `anthropic` (overridden by `--llm`) |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | read from the environment only, never stored in code. Real providers are stubs for now: they fail clearly without a key and raise `NotImplementedError` with one. |
| `RPG_LLM_MODEL` | model name for a real provider |

Mock mode (`rpg/llm.py: MockLLM`) maps text to proposals with deterministic keyword rules and narrates
with templates. Narration always ends with "What do you do?": a missing closing line is appended, and
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

## Turn API
See `docs/contracts.md` (v0.3) for the proposal and result shapes, validation codes, DC rules and
state changes. `process_action_proposal` writes through the connection you pass and does not commit:
```python
from rpg import db
from rpg.actions import process_action_proposal
from rpg.context import build_context
with db.connect() as conn:            # commits on exit
    ctx = build_context(conn, cid)    # render into the interpreter prompt
    result = process_action_proposal(conn, cid, proposal_json)
```

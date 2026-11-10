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
- `tests/` - pytest suite

## Run
```bash
python3 -m venv .venv && .venv/bin/pip install pytest   # stdlib only otherwise
.venv/bin/python -m pytest -q                            # run tests
python3 rpg_engine.py [db_path] [seed]                   # demo (throwaway DB unless db_path given)
```

## Save file location
`db_path` argument > `$RPG_DB_PATH` > `<project>/saves/rpg_state.db` (independent of the
current directory). `saves/` and `*.db` are git-ignored.

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

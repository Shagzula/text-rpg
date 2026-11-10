# Text RPG engine (M1)

Hybrid text RPG: an LLM interprets free-form player actions and narrates; this Python +
SQLite engine owns all state and rules. Spec: `../DESIGN.md`. JSON contracts: `docs/contracts.md`.

## Layout
- `rpg_engine.py` - Dan's starter, now a thin starter-compatible API + demo on top of `rpg/`
- `rpg/schema.sql` - SQLite schema (v1, foreign keys on)
- `rpg/db.py` - `init_db(path)`, connections, character/condition load+save
- `rpg/rules.py` - pure 5e math and `resolve_skill_check()` (house rules in its docstring)
- `tests/` - pytest suite

## Run
```bash
python3 -m venv .venv && .venv/bin/pip install pytest   # stdlib only otherwise
.venv/bin/python -m pytest -q                            # run tests
python3 rpg_engine.py [db_path] [seed]                   # demo
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

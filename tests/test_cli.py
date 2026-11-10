import random
import subprocess
import sys
from pathlib import Path

import pytest

from rpg import cli, db, llm
from rpg.llm import CLOSING_LINE, MockLLM

ROOT = Path(__file__).resolve().parent.parent


class Script:
    def __init__(self, *lines):
        self.lines = list(lines)
        self.prompts = 0

    def __call__(self, prompt):
        self.prompts += 1
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)


def play(tmp_path, *lines, seed=3, new=False, load=None, client=None, db_name="game.db"):
    out = []
    g = cli.Game(tmp_path / db_name, llm=client or MockLLM(), rng=random.Random(seed),
                 input_fn=Script(*lines), output_fn=out.append)
    g.run(new=new, load=load)
    return g, out


def location(g):
    with db.connect(g.path) as conn:
        return conn.execute("SELECT l.key FROM characters c JOIN locations l ON l.id = c.location_id "
                            "WHERE c.id = ?", (g.cid,)).fetchone()[0]


def held(g):
    with db.connect(g.path) as conn:
        return {r[0] for r in conn.execute("SELECT key FROM items WHERE character_id = ?", (g.cid,))}


def test_new_game_opening_and_scripted_session(tmp_path):
    g, out = play(tmp_path, "look", "take crowbar", "go east", "i", "/quit", new=True)
    assert "Town Square" in out[0] and out[0].endswith(CLOSING_LINE)
    narrations = [o for o in out if o.endswith(CLOSING_LINE)]
    assert len(narrations) == 5  # opening + 4 turns
    assert any("You take the Crowbar" in o for o in out)
    assert any("Storeroom" in o for o in out)
    assert out[-1] == "Your progress is kept. Farewell."
    assert location(g) == "loc_storeroom" and "item_crowbar" in held(g)
    with db.connect(g.path) as conn:
        log = conn.execute("SELECT turn, text FROM narration_log ORDER BY id").fetchall()
        assert [r["turn"] for r in log] == [0, 1, 2, 3, 4]
        assert conn.execute("SELECT turn FROM save_games").fetchone()[0] == 4


def test_seeded_runs_are_deterministic(tmp_path):
    lines = ("go east", "pick the lock", "desperately kick the door", "/quit")
    _, a = play(tmp_path, *lines, new=True, db_name="a.db")
    _, b = play(tmp_path, *lines, new=True, db_name="b.db")
    assert a == b


def test_full_demo_route(tmp_path):
    g, out = play(tmp_path, "go east", "desperately kick the door", "light torch", "down", "take rope", "pull lever",
                  "/quit", seed=5, new=True)
    assert location(g) == "loc_cellar" and "item_rope" in held(g)
    with db.connect(g.path) as conn:
        assert db.get_flag(conn, "cellar_gate_opened") is True
        assert [r[0] for r in conn.execute("SELECT name FROM character_conditions")] == ["strained_str"]


def test_save_then_load_round_trip_restores_state(tmp_path):
    g, out = play(tmp_path, "take crowbar", "go east", "/save before_cellar", "/saves",
                  "go west", "drop crowbar", "/load before_cellar", "/quit", new=True)
    assert "(Saved to slot 'before_cellar', turn 2.)" in out
    assert any(o.strip().startswith("before_cellar") and "turn 2" in o for o in out)
    assert "(Loaded slot 'before_cellar'.)" in out
    assert location(g) == "loc_storeroom" and "item_crowbar" in held(g)  # changes after the save undone
    assert (tmp_path / "slot_before_cellar.db").exists()
    with db.connect(g.path) as conn:
        assert conn.execute("SELECT name, turn FROM save_games").fetchone()[:] == ("before_cellar", 2)


def test_load_at_startup_and_continue(tmp_path):
    play(tmp_path, "go east", "/save s1", "go west", "/quit", new=True)
    g, out = play(tmp_path, "/quit", load="s1")
    assert location(g) == "loc_storeroom" and "(Loaded slot 's1'.)" in out
    g, out = play(tmp_path, "/quit")  # plain start continues the working save
    assert out[0].startswith("(Continuing your game") and location(g) == "loc_storeroom"


def test_load_missing_slot_and_bad_names(tmp_path):
    g, out = play(tmp_path, "/load nope", "/save ../evil", "/saves", "/quit", new=True)
    assert "(No save slot named 'nope'. Type /saves to list them.)" in out
    assert any("Save names may use" in o for o in out)
    assert "(No save slots yet. Use /save [name].)" in out
    assert not (tmp_path.parent / "evil.db").exists()


def test_new_replaces_working_save(tmp_path):
    play(tmp_path, "go east", "/quit", new=True)
    g, _ = play(tmp_path, "/quit", new=True)
    assert location(g) == "loc_town_square"


def test_ooc_words_and_meta_commands(tmp_path):
    g, out = play(tmp_path, "help", "/stats", "/bogus", "save", "quit", "look", new=True)
    assert any("Meta commands" in o for o in out)
    assert any(o.startswith("You, level 1") for o in out)
    assert "(Unknown command /bogus. Type /help.)" in out
    assert "(Saved to slot 'quicksave', turn 0.)" in out
    assert out[-1] == "Your progress is kept. Farewell."  # "look" never ran


def test_debug_toggle_prints_check_numbers(tmp_path):
    _, out = play(tmp_path, "go east", "/debug", "pick the lock", "/debug", "pick the lock", "/quit", new=True)
    debug = [o for o in out if o.startswith("[debug]")]
    assert len(debug) == 1 and "'dc': 15" in debug[0] and "'roll':" in debug[0]
    narr = [o for o in out if o.endswith(CLOSING_LINE)]
    assert not any("'dc'" in n for n in narr)


def test_eof_ends_cleanly(tmp_path):
    _, out = play(tmp_path, "look", new=True)
    assert out[-1] == "Farewell."


class BadThenGood(MockLLM):
    def __init__(self, bad_times):
        self.bad_times = bad_times
        self.calls = []

    def interpret(self, player_text, context, reprompt=None):
        self.calls.append(reprompt)
        if len(self.calls) <= self.bad_times:
            return {"actions": [{"action_type": "dance"}]}
        return super().interpret(player_text, context, reprompt)


def test_malformed_proposal_reprompts_once(tmp_path):
    client = BadThenGood(1)
    g, out = play(tmp_path, "go east", "/quit", new=True, client=client)
    assert client.calls[0] is None and "corrected JSON" in client.calls[1]
    assert location(g) == "loc_storeroom"


def test_malformed_twice_gives_graceful_message(tmp_path):
    client = BadThenGood(99)
    g, out = play(tmp_path, "go east", "/quit", new=True, client=client)
    assert len(client.calls) == 2 and cli.GRACEFUL_MISS in out and location(g) == "loc_town_square"
    with db.connect(g.path) as conn:
        assert conn.execute("SELECT turn FROM save_games").fetchone()[0] == 0  # no turn spent


def test_main_rejects_provider_without_key(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert cli.main(["--llm", "openai", "--db", str(tmp_path / "x.db")]) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().out


def _run(args, stdin, tmp_path):
    env = {"PATH": "/usr/bin:/bin", "RPG_DB_PATH": str(tmp_path / "env.db"), "RPG_LLM": "mock"}
    return subprocess.run([sys.executable, *args], input=stdin, capture_output=True, text=True, cwd=ROOT,
                          env=env, timeout=30)


def test_python_m_rpg_uses_env_db(tmp_path):
    p = _run(["-m", "rpg", "--new", "--seed", "1"], "go east\n/quit\n", tmp_path)
    assert p.returncode == 0, p.stderr
    assert "Storeroom" in p.stdout and (tmp_path / "env.db").exists()


def test_rpg_engine_play_passthrough_and_demo(tmp_path):
    p = _run(["rpg_engine.py", "play", "--db", str(tmp_path / "p.db"), "--new"], "/quit\n", tmp_path)
    assert p.returncode == 0 and "Town Square" in p.stdout
    p = _run(["rpg_engine.py", "", "7"], "", tmp_path)
    assert p.returncode == 0 and "Last-ditch door" in p.stdout

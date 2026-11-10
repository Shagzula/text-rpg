"""Terminal turn loop.

    python -m rpg [--db PATH] [--new] [--load NAME] [--seed N] [--llm mock|openai|anthropic]

Save design: the *working* game is one SQLite file (--db, else $RPG_DB_PATH, else
saves/rpg_state.db). Every turn is committed to it, so quitting never loses
progress and the next start continues it. /save NAME snapshots the working file
into a slot file `slot_NAME.db` next to it (SQLite online backup API, so the copy
is consistent); /load NAME copies a slot back over the working file. Slots are
complete, independent games; /saves lists them.

Dice: the RNG state is stored in the save (world_flags `_rng_state`) after every
turn, so continuing a save or loading a slot resumes the same dice stream.
--seed only seeds a game that has no stored dice state yet (i.e. a new game).

Death: when a turn's result carries defeat {"type": "death"}, the run ends and
`death_policy` offers: load the most recent save slot, start a new game, or quit.
"""
from __future__ import annotations

import argparse
import logging
import random
import re
import sqlite3
from pathlib import Path
from typing import Callable, Optional

from . import db, llm as llm_mod
from . import talk
from .actions import process_action_proposal
from .context import build_context, log_narration, recent_narrations
from .world import find_player, seed_demo_slice

HELP = """Type what you want to do in plain words, e.g. "look", "go east", "take crowbar",
"pick the lock", "light torch", "desperately kick the door", "attack guard", "rest", "i".
Meta commands:
  /save [name]   save to a slot (default: quicksave)
  /load [name]   load a slot (default: quicksave)
  /saves         list save slots
  /stats         show your character sheet
  /debug         toggle printing hidden check numbers
  /help          this help
  /quit          quit (progress is kept in the working save)"""
SLOT_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
DEFAULT_SLOT = "quicksave"
MAX_INPUT_CHARS = 300  # longer lines are refused before the interpreter sees them
TOO_LONG = f"(That's too long for one turn; keep it under {MAX_INPUT_CHARS} characters.)"
GRACEFUL_MISS = "(The world doesn't quite follow that. Try rephrasing, or type /help.)"


class _OutHandler(logging.Handler):
    def __init__(self, out):
        super().__init__(logging.DEBUG)
        self.out = out

    def emit(self, record):
        self.out(f"[debug] {record.getMessage()}")


class Game:
    def __init__(self, db_path=None, llm: Optional[llm_mod.LLMClient] = None, rng: Optional[random.Random] = None,
                 input_fn: Callable[[str], str] = input, output_fn: Callable[[str], None] = print):
        self.path = db.resolve_path(db_path)
        self.llm = llm or llm_mod.get_llm()
        self.rng = rng or random.Random()
        self.input, self.out = input_fn, output_fn
        self.cid: Optional[int] = None
        self._debug: Optional[_OutHandler] = None

    # -- setup -----------------------------------------------------------------
    @property
    def slots_dir(self) -> Path:
        return self.path.parent

    def slot_path(self, name: str) -> Path:
        return self.slots_dir / f"slot_{name}.db"

    def start(self, new: bool = False, load: Optional[str] = None) -> None:
        if new and self.path.exists():
            self.path.unlink()
        db.init_db(self.path)
        with db.connect(self.path) as conn:
            fresh = find_player(conn) is None
            self.cid = seed_demo_slice(conn)
            if not db.load_rng_state(conn, self.rng):
                db.save_rng_state(conn, self.rng)  # a new game: the seed starts the stored stream
        if load and not self.load(load):
            raise SystemExit(f"No save slot named {load!r} in {self.slots_dir}")
        if load:
            return
        with db.connect(self.path) as conn:
            if fresh or not recent_narrations(conn, self.cid):
                text = self._opening(conn)
                log_narration(conn, self.cid, 0, text)
                self.out(text)
            elif self._dead(conn):
                self.out("(Continuing your game.)")  # BUG-R07: straight to the death offer, no greeting
            else:
                self.out("(Continuing your game. Type /help for commands.)")
                self.out(self._opening(conn))

    def player_dead(self) -> bool:
        with db.connect(self.path) as conn:
            row = conn.execute("SELECT current_hp FROM characters WHERE id = ?", (self.cid,)).fetchone()
        return row is not None and row[0] <= 0

    def _dead(self, conn) -> bool:
        row = conn.execute("SELECT current_hp FROM characters WHERE id = ?", (self.cid,)).fetchone()
        return row is not None and row[0] <= 0

    def _opening(self, conn) -> str:
        ctx = build_context(conn, self.cid)
        return llm_mod.finalize_narration(self.llm.narrate({"opening": True, "results": []}, ctx))

    # -- loop --------------------------------------------------------------------
    def run(self, new: bool = False, load: Optional[str] = None) -> None:
        self.start(new=new, load=load)
        try:
            if self.player_dead() and death_policy(self) == "quit":
                return
            while True:
                try:
                    line = self.input("> ")
                except (EOFError, KeyboardInterrupt):
                    self.out("Farewell.")
                    break
                line = (line or "").strip()
                if not line:
                    continue
                if line.startswith("/"):
                    if self.meta(line) == "quit":
                        break
                    continue
                if self.turn(line) == "quit":
                    break
        finally:
            self._set_debug(False)

    def turn(self, text: str) -> Optional[str]:
        if len(text) > MAX_INPUT_CHARS:
            self.out(TOO_LONG)  # no turn, nothing logged
            return None
        with db.connect(self.path) as conn:
            ctx = build_context(conn, self.cid)
            result = self._interpret_and_process(conn, text, ctx)
            if result is None:
                self.out(GRACEFUL_MISS)
                return None
            if result.get("type") == "ooc":
                cmd = result["ooc"]["command"]
                if cmd in ("quit", "save", "load", "help", "stats"):
                    conn.commit()
                    return self.meta("/" + cmd)
                self.out(f"(/{cmd} isn't supported yet. Type /help.)")
                return None
            after = build_context(conn, self.cid)
            talk.voice(conn, self.llm, result, text, after)  # NPC lines -> result["dialogue"] (§g)
            narration = llm_mod.narrate_safely(self.llm, result, after)
            log_narration(conn, self.cid, result["turn"] or 0, narration)
            db.save_rng_state(conn, self.rng)
        self.out(narration)
        if result.get("defeat"):
            return death_policy(self)
        return None

    def _interpret_and_process(self, conn, text, ctx):
        reprompt = None
        for _ in range(2):  # first try + one re-prompt
            try:
                proposal = self.llm.interpret(text, ctx, reprompt=reprompt)
            except NotImplementedError as e:
                self.out(f"(LLM provider not available: {e})")
                return None
            result = process_action_proposal(conn, self.cid, proposal, rng=self.rng)
            if not result.get("error"):
                return result
            reprompt = result["reprompt"]
        return None

    # -- meta commands -------------------------------------------------------------
    def meta(self, line: str) -> Optional[str]:
        cmd, _, arg = line[1:].strip().partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd in ("quit", "q", "exit"):
            self.out("Your progress is kept. Farewell.")
            return "quit"
        if cmd in ("help", "h", "?"):
            self.out(HELP)
        elif cmd == "save":
            self.save(arg or DEFAULT_SLOT)
        elif cmd == "load":
            self.load(arg or DEFAULT_SLOT)
        elif cmd == "saves":
            self.list_saves()
        elif cmd == "debug":
            self._set_debug(self._debug is None)
            self.out(f"(debug {'on' if self._debug else 'off'})")
        elif cmd == "stats":
            self.stats()
        else:
            self.out(f"(Unknown command /{cmd}. Type /help.)")
        return None

    def save(self, name: str) -> bool:
        if not SLOT_RE.match(name):
            self.out("(Save names may use letters, digits, - and _, up to 40 characters.)")
            return False
        dest = self.slot_path(name)
        src = db.get_connection(self.path)
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
            dst.execute("UPDATE save_games SET name = ?, updated_at = CURRENT_TIMESTAMP", (name,))
            dst.commit()
            turn = dst.execute("SELECT MAX(turn) FROM save_games").fetchone()[0] or 0
        finally:
            dst.close()
            src.close()
        self.out(f"(Saved to slot '{name}', turn {turn}.)")
        return True

    def load(self, name: str) -> bool:
        src_path = self.slot_path(name)
        if not SLOT_RE.match(name) or not src_path.exists():
            self.out(f"(No save slot named '{name}'. Type /saves to list them.)")
            return False
        src = sqlite3.connect(src_path)
        dst = sqlite3.connect(self.path)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
        db.init_db(self.path)  # verifies schema_version
        with db.connect(self.path) as conn:
            self.cid = find_player(conn)
            if not db.load_rng_state(conn, self.rng):
                db.save_rng_state(conn, self.rng)
            self.out(f"(Loaded slot '{name}'.)")
            if not self._dead(conn):
                self.out(self._opening(conn))
        return True

    def latest_slot(self) -> Optional[str]:
        slots = sorted(self.slots_dir.glob("slot_*.db"), key=lambda p: p.stat().st_mtime)
        return slots[-1].stem[len("slot_"):] if slots else None

    def list_saves(self) -> None:
        slots = sorted(self.slots_dir.glob("slot_*.db"))
        if not slots:
            self.out("(No save slots yet. Use /save [name].)")
            return
        for p in slots:
            c = sqlite3.connect(p)
            try:
                row = c.execute("SELECT MAX(turn), MAX(updated_at) FROM save_games").fetchone()
            except sqlite3.Error:
                row = (None, None)
            finally:
                c.close()
            self.out(f"  {p.stem[len('slot_'):]:<20} turn {row[0] or 0:<4} saved {row[1] or '?'} UTC")

    def stats(self) -> None:
        with db.connect(self.path) as conn:
            ctx = build_context(conn, self.cid)
            char = db.load_character(conn, self.cid)
        s = ctx["player_sheet"]

        def ab(a, v):  # show condition penalties next to the raw modifier (BUG-09)
            extra = sum(c.check_modifier(None, a) for c in char.conditions)
            eff = f", {v['mod'] + extra:+d} with conditions" if extra else ""
            return f"{a.upper()} {v['score']} ({v['mod']:+d}{eff})"
        abil = "  ".join(ab(a, v) for a, v in s["abilities"].items())
        self.out(f"{s['name']}, level {s['level']} | {abil}")
        self.out("Skills: " + (", ".join(f"{k} ({v})" for k, v in s["skills"].items()) or "none"))
        self.out(f"Purse: {s['gold']} coins")
        self.out(f"Condition: {ctx['hp_band']}" + ("; " + ", ".join(c["name"] for c in s["conditions"])
                                                     if s["conditions"] else ""))

    def _set_debug(self, on: bool) -> None:
        logger = logging.getLogger("rpg.debug")
        if on and self._debug is None:
            self._debug = _OutHandler(self.out)
            logger.addHandler(self._debug)
            logger.setLevel(logging.DEBUG)
        elif not on and self._debug is not None:
            logger.removeHandler(self._debug)
            self._debug = None


DEATH_PROMPT = "Your story ends here."


def death_policy(game: "Game") -> str:
    """PROVISIONAL death policy (one place to change it): death ends the run and offers
    [L]oad the most recent save slot, [N]ew game, or [Q]uit. Returns "continue" (a game
    is running again) or "quit"."""
    slot = game.latest_slot()
    options = ([f"[L]oad your most recent save ('{slot}')"] if slot else []) + ["start a [N]ew game", "[Q]uit"]
    offer = ", ".join(options[:-1]) + f", or {options[-1]}?"
    game.out(f"{DEATH_PROMPT} " + offer[0].upper() + offer[1:])
    while True:
        try:
            choice = (game.input("(l/n/q) > " if slot else "(n/q) > ") or "").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "q"
        if choice in ("l", "load") and slot:
            if game.load(slot):
                return "continue"
        elif choice in ("n", "new"):
            game.start(new=True)
            return "continue"
        elif choice in ("q", "quit", "exit"):
            game.out("Farewell.")
            return "quit"
        else:
            game.out("(Choose " + ("l, " if slot else "") + "n or q.)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rpg", description="Text RPG (demo slice).")
    ap.add_argument("--db", help="working save file (default: $RPG_DB_PATH or saves/rpg_state.db)")
    ap.add_argument("--new", action="store_true", help="start a new game (replaces the working save)")
    ap.add_argument("--load", metavar="NAME", help="load a save slot at start")
    ap.add_argument("--seed", type=int, help="seed the dice for a deterministic run")
    ap.add_argument("--llm", help="provider: mock (default), openai, anthropic; or set $RPG_LLM")
    args = ap.parse_args(argv)
    try:
        client = llm_mod.get_llm(args.llm)
    except llm_mod.LLMConfigError as e:
        print(f"error: {e}")
        return 2
    rng = random.Random(args.seed) if args.seed is not None else None
    Game(args.db, llm=client, rng=rng).run(new=args.new, load=args.load)
    return 0

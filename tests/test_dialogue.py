"""Voice step (§g), mock talk interpreter/narrator, talk context, and the secrets-never-leak proof."""
import json
import re

import pytest

from rpg import actions, db, llm as llm_mod, personas, talk, world as demo
from rpg.context import build_context
from rpg.llm import MockLLM
from test_actions import Script
from test_cli import play
from test_talk import g, gold, run, say  # noqa: F401  (fixture)


def interp(g, text):
    return MockLLM().interpret(text, build_context(g["conn"], g["cid"]))["actions"][0]


def turn(g, text, llm=None, rng=None):
    llm = llm or MockLLM()
    ctx = build_context(g["conn"], g["cid"])
    p = llm.interpret(text, ctx)
    r = actions.process_action_proposal(g["conn"], g["cid"], p, rng=rng or Script(*([10] * 10)))
    after = build_context(g["conn"], g["cid"])
    talk.voice(g["conn"], llm, r, text, after)
    return r, llm_mod.narrate_safely(llm, r, after), p


# --- mock interpreter -----------------------------------------------------------------------------

@pytest.mark.parametrize("text,approach,topic,skill", [
    ("ask mara about the cellar", "ask", "the_cellar", None),
    ("talk to mara about the storeroom", "ask", "the_storeroom", None),
    ("persuade mara to tell me about the cellar", "persuade", "the_cellar", "persuasion"),
    ("lie to mara about the market news", "deceive", "market_news", "deception"),
    ("threaten mara", "intimidate", None, "intimidation"),
    ("read mara", "insight", None, "insight"),
    ("buy a torch from mara", "trade", "trade_buy_torch", None),
    ("buy rations", "trade", "trade_buy_rations", None),
    ("sell rope to mara", "trade", "trade_sell_rope", None),
    ("trade with mara", "trade", None, None),
    ("hello mara", "small_talk", None, None),
    ("goodbye mara", "farewell", None, None),
])
def test_mock_interprets_talk(g, text, approach, topic, skill):
    a = interp(g, text)
    assert (a["action_type"], a["target"], a["approach"], a["topic"], a["skill"]) == \
        ("talk", "npc_mara", approach, topic, skill)


def test_mock_unknown_subject_goes_to_topic_text(g):
    a = interp(g, "ask mara about the stables")
    assert a["topic"] is None and a["topic_text"] == "stables"


def test_mock_never_matches_hidden_secret_labels(g):
    a = interp(g, "ask mara about the lever hidden in the cellar")  # cellar_lever is allied-only
    assert a["topic"] != "cellar_lever"


# --- voice step -----------------------------------------------------------------------------------

class Recorder(MockLLM):
    def __init__(self, replies=None):
        self.calls, self.replies = [], list(replies or [])

    def voice_npc(self, view, allowed, result, context):
        self.calls.append((view, allowed, result))
        if self.replies:
            r = self.replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return super().voice_npc(view, allowed, result, context)


def test_voice_lines_go_into_dialogue_verbatim_and_narration(g):
    r, text, _ = turn(g, "ask mara about the cellar")
    lines = [d["line"] for d in r["dialogue"]]
    cellar = personas.get(g["conn"], 1)["knowledge"]["the_cellar"]["text"]
    assert lines == [cellar] and r["dialogue"][0]["source"] == "npc_llm"
    assert r["dialogue"][0]["speaker_key"] == "npc_mara" and f'"{cellar}"' in text
    assert "What do you do?" in text


def test_voice_input_is_public_persona_and_allowed_topics_only(g):
    rec = Recorder()
    turn(g, "read mara", llm=rec, rng=Script(20))
    view, allowed, payload = rec.calls[0]
    assert set(view) == {"key", "name", "voice", "traits", "goals"}
    assert "insight_read" not in payload["talk"] and payload["player_text"] == "read mara"
    assert allowed == payload["talk"]["allowed_topics"]


def test_voice_retries_once_then_falls_back_to_indirect_speech(g):
    rec = Recorder([RuntimeError("boom"), "not json"])
    r, text, _ = turn(g, "ask mara about the cellar", llm=rec)
    assert len(rec.calls) == 2 and r["dialogue"] == []
    assert "Mara tells you that nobody has been down in the cellar" in text
    assert '"' not in text  # no invented quotes


def test_voice_retry_succeeds_second_time(g):
    rec = Recorder([{"lines": "nope"}, {"lines": ["Fine.", "  ", "Ask away."]}])
    r, _, _ = turn(g, "hello mara", llm=rec)
    assert [d["line"] for d in r["dialogue"]] == ["Fine.", "Ask away."]


def test_voice_clips_to_three_lines_and_forty_words(g):
    rec = Recorder([{"lines": ["one " * 50, "b", "c", "d"]}])
    r, _, _ = turn(g, "hello mara", llm=rec)
    assert len(r["dialogue"]) == 3 and len(r["dialogue"][0]["line"].split()) == 40


def test_voice_lint_drops_lines_echoing_a_secret(g):
    secret = personas.get(g["conn"], 1)["secrets"]["mara_past"]["text"]
    rec = Recorder([{"lines": [secret, "Nothing to say."]}])
    r, _, _ = turn(g, "hello mara", llm=rec)
    assert [d["line"] for d in r["dialogue"]] == ["Nothing to say."]


def test_voice_skipped_for_personaless_rejected_and_stub(g):
    rec = Recorder()
    g["conn"].execute("UPDATE npcs SET location_id = NULL WHERE key = 'npc_mara'")
    turn(g, "talk to mara", llm=rec)  # rejected: elsewhere
    assert rec.calls == []
    g["conn"].execute("UPDATE npcs SET location_id = (SELECT id FROM locations WHERE key='loc_town_square')")
    g["conn"].execute("DELETE FROM npc_personas")
    r, _, _ = turn(g, "talk to mara", llm=rec)
    assert rec.calls == [] and r["dialogue"] == []


def test_provider_stubs_have_voice_npc_that_raises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    with pytest.raises(NotImplementedError):
        llm_mod.OpenAIClient().voice_npc({}, [], {}, {})


def test_dialogue_log_feeds_recent_dialogue_last_five(g):
    for _ in range(4):
        turn(g, "ask mara about the cellar")
        turn(g, "hello mara")
    ctx = build_context(g["conn"], g["cid"])
    mara = next(n for n in ctx["npcs_present"] if n["key"] == "npc_mara")
    assert len(mara["recent_dialogue"]) == 5
    n = g["conn"].execute("SELECT COUNT(*) FROM npc_dialogue_log").fetchone()[0]
    assert n == 8


# --- context --------------------------------------------------------------------------------------

def test_context_topics_trades_and_gold(g):
    ctx = build_context(g["conn"], g["cid"])
    mara = next(n for n in ctx["npcs_present"] if n["key"] == "npc_mara")
    keys = {t["key"] for t in mara["topics"]}
    assert {"mara_self", "the_cellar", "guard_sleeps"} <= keys and "cellar_lever" not in keys
    assert {"key": "trade_buy_torch", "label": "a torch"} in mara["trades"]
    assert "trade_tonic_for_rope" not in {t["key"] for t in mara["trades"]}
    assert ctx["player_sheet"]["gold"] == 60
    assert all(set(t) == {"key", "label"} for t in mara["topics"] + mara["trades"])


def test_context_drops_completed_one_off_trade(g):
    db.add_item(g["conn"], "item_rope", character_id=g["cid"])
    run(g, say("trade", "trade_sell_rope"))
    mara = next(n for n in build_context(g["conn"], g["cid"])["npcs_present"] if n["key"] == "npc_mara")
    assert "trade_sell_rope" not in {t["key"] for t in mara["trades"]}


# --- secrets never leak ---------------------------------------------------------------------------

def _strings(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
    elif isinstance(obj, str):
        yield obj


def test_secrets_never_appear_in_prompt_facing_output(g):
    """Walk every prompt-facing surface over a varied talk session at friendly band: build_context,
    results, voice inputs, dialogue and narration. Below-band secrets, `never` secrets and (without
    a successful insight) the insight_tell must never appear, by text or label."""
    conn = g["conn"]
    rec = Recorder()
    p_mara = personas.get(conn, 1)
    p_guard = personas.get(conn, 2)
    forbidden = [p_mara["secrets"]["cellar_lever"]["text"], p_mara["secrets"]["cellar_lever"]["label"],
                 p_mara["secrets"]["mara_past"]["text"], p_mara["secrets"]["mara_past"]["label"],
                 p_mara["insight_tell"], p_guard["secrets"]["who_pays"]["text"], p_guard["secrets"]["who_pays"]["label"],
                 p_guard["insight_tell"], p_guard["secrets"]["the_lever"]["text"]]
    surfaces = []
    for text in ("ask mara about the cellar", "ask mara about the lever hidden in the cellar",
                 "ask mara about her past", "persuade mara about the market news",
                 "lie to mara about the storeroom", "trade with mara", "buy a torch from mara", "hello mara",
                 "read mara", "goodbye mara"):
        surfaces.append(build_context(conn, g["cid"]))
        r, narration, proposal = turn(g, text, llm=rec, rng=Script(*([2] * 6)))  # insight fails
        surfaces += [r, narration, proposal]
    g["conn"].execute("UPDATE characters SET location_id = (SELECT id FROM locations WHERE key='loc_storeroom')")
    for text in ("ask guard about his wages", "ask guard about the cellar door", "threaten guard"):
        surfaces.append(build_context(conn, g["cid"]))
        r, narration, proposal = turn(g, text, llm=rec, rng=Script(*([2] * 6)))
        surfaces += [r, narration, proposal]
    surfaces += [c for c in rec.calls]
    blob = "\n".join(_strings(surfaces))
    for f in forbidden:
        assert f not in blob, f
    # and the raw disposition integer never shows up as a field
    assert "base_disposition" not in blob and not re.search(r'"disposition":\s*-?\d', json.dumps(surfaces))


def test_insight_tell_only_via_successful_insight_and_never_to_voice(g):
    rec = Recorder()
    r, narration, _ = turn(g, "read mara", llm=rec, rng=Script(20))
    tell = personas.get(g["conn"], 1)["insight_tell"]
    assert r["results"][0]["talk"]["insight_read"] == tell and tell in narration
    assert tell not in json.dumps(rec.calls)


# --- CLI ------------------------------------------------------------------------------------------

def test_cli_talk_and_trade_and_purse(tmp_path):
    g_, out = play(tmp_path, "ask mara about the cellar", "buy a torch from mara", "/stats", "/quit", new=True)
    text = "\n".join(out)
    assert "Nobody has been down in the cellar" in text
    assert "Purse: 55 coins" in text
    narr = [o for o in out if "Mara" in o and "Purse" not in o]
    assert narr and not any(ch.isdigit() for o in narr for ch in o)  # narration stays number-free

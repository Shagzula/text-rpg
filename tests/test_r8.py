"""v0.10.3: LLM-boundary masking of unseen NPCs (BUG-R8-01) and stall-streak edges."""
import json
import re

import pytest

from rpg import actions, context, db, llm as llm_mod, personas, prompts, talk, unseen
from rpg.providers import AnthropicClient, OpenAIClient
from rpg.proposal_schema import SCHEMA_NAME, proposal_schema, voice_schema
from test_dark_npcs import GUARD_SOUND, dark_guard, leaks, npc_id, put  # noqa: F401
from test_retest import demo, go, hostile_guard, say  # noqa: F401


FENCE = re.compile(r"<player_input>.*?</player_input>", re.S)


def _hidden(demo):
    conn, cid = demo
    return unseen.unseen_npcs(conn, cid)


def _persona(conn, key="npc_dozing_guard"):
    return personas.persona_view(personas.get(conn, npc_id(conn, key)))


def _request_bodies(player_text, ctx, result, persona=None, payload=None):
    hidden = unseen.hidden_of(result) or unseen.hidden_of(ctx) or unseen.hidden_of(persona) or unseen.hidden_of(payload)
    if persona is not None and hidden and not unseen.hidden_of(persona):
        persona = unseen.carry(persona, hidden)
    if payload is not None and hidden and not unseen.hidden_of(payload):
        payload = unseen.carry(payload, hidden)
    if payload is None:
        payload = unseen.carry(
            {"player_text": player_text, "talk": {"approach": "ask", "speaker_unseen": True},
             "recent_dialogue": []},
            hidden,
        )
    if persona is None:
        persona = {"key": "npc_x", "name": "X", "voice": {"style": "s", "sample_phrases": []},
                   "traits": [], "goals": []}
        if hidden:
            persona = unseen.carry(persona, hidden)
    messages = {
        "interpret": prompts.interpret_messages(ctx, player_text),
        "narrate": prompts.narrate_messages(result, ctx, player_text),
        "voice": prompts.voice_messages(persona, payload),
    }
    schemas = {"interpret": (SCHEMA_NAME, proposal_schema()), "voice": ("npc_lines", voice_schema())}
    out = []
    for Client in (OpenAIClient, AnthropicClient):
        client = Client(dry_run=True, out=lambda s: None)
        for role, (system, user) in messages.items():
            body = client.build(role, system, [("user", user)], schema=schemas.get(role))
            out.append((client.name, role, body))
    return out


def _user_text(body: dict) -> str:
    """The filled user/context part of an OpenAI or Anthropic request, not the static system prompt.

    Story Designer system examples may still mention demo names (npc_mara, Mara); those files
    are not edited here. The engine-owned payload is the user turns.
    """
    chunks = []
    for turn in body.get("input") or body.get("messages") or []:
        if isinstance(turn, dict) and turn.get("role") == "user":
            chunks.append(turn.get("content") or "")
    return FENCE.sub(" ", "\n".join(chunks))


def assert_requests_hide_unseen(bodies, hidden, player_text=""):
    banned = unseen.identities(hidden)
    assert banned, "expected unseen identities to check"
    for provider, role, body in bodies:
        blob = _user_text(body)
        for token in banned:
            assert token.lower() not in blob.lower(), (
                f"{provider}/{role} leaked {token!r} in the user payload"
            )


def _kill_rolls():
    # attack hits; damage is 1d6, leftover rolls cover a possible NPC phase
    return (20, 6, 1, 1, 1, 1)


# --- BUG-R8-01: unseen defeat flag in the narrator-facing result ----------------

@pytest.mark.parametrize("text", ["attack the breathing", "attack the guard", "attack the dragon"])
def test_unseen_killing_blow_masks_the_defeated_flag_but_keeps_the_real_db_flag(dark_guard, text):
    conn, cid = dark_guard
    hostile_guard(dark_guard)
    conn.execute("UPDATE npcs SET current_hp = 1 WHERE key = 'npc_dozing_guard'")
    r, out = say(dark_guard, text, *_kill_rolls())
    res = r["results"][0]
    assert res["status"] == "resolved" and res["outcome"] == "success"
    assert any(c.get("type") == "npc_defeated" for c in res["state_changes"])
    ref = f"npc_unseen_{npc_id(conn, 'npc_dozing_guard')}"
    assert {"type": "flag_set", "key": f"defeated:{ref}", "value": True} in res["state_changes"]
    assert all("npc_dozing_guard" not in json.dumps(c) for c in res["state_changes"])
    assert db.get_flag(conn, "defeated:npc_dozing_guard") is True
    assert db.get_flag(conn, f"defeated:{ref}") is None
    assert leaks(r, conn, "npc_dozing_guard") is None
    assert leaks(out, conn, "npc_dozing_guard") is None
    assert "unseen figure" in out.lower() or "goes down" in out.lower()


def test_mask_rewrites_compound_flag_keys_and_names_in_any_field():
    hidden = {"npc_dozing_guard": {"ref": "npc_unseen_2", "name": "Dozing Guard",
                                   "aliases": ["Gatekeeper"], "sound": "breathing", "alive": False}}
    raw = {
        "results": [{
            "target": {"key": "npc_dozing_guard", "name": "Dozing Guard", "article": "the"},
            "intent_summary": "Attack the Dozing Guard.",
            "state_changes": [
                {"type": "npc_defeated", "key": "npc_dozing_guard", "name": "Dozing Guard"},
                {"type": "flag_set", "key": "defeated:npc_dozing_guard", "value": True},
            ],
        }],
        "flags": {"defeated:npc_dozing_guard": True},
        "npc_actions": [{"npc": "npc_dozing_guard", "name": "Gatekeeper"}],
        "pending_clarify": {"candidates": [{"key": "npc_dozing_guard"}]},
        "player_text": "attack the Dozing Guard",
    }
    out = unseen.for_llm(raw, hidden)
    public = {k: v for k, v in out.items() if k != "player_text"}
    blob = json.dumps(public)
    assert "npc_dozing_guard" not in blob
    assert "Dozing Guard" not in blob
    assert "Gatekeeper" not in blob
    assert out["results"][0]["state_changes"][1]["key"] == "defeated:npc_unseen_2"
    assert out["results"][0]["target"]["key"] == "npc_unseen_2"
    assert out["results"][0]["target"]["name"] == unseen.UNSEEN_NAME
    assert "article" not in out["results"][0]["target"]
    # Player text is preserved so the interpreter still sees what was typed.
    assert out["player_text"] == "attack the Dozing Guard"
    # Internal mapping must not serialize.
    assert "_hidden" not in blob


def test_llm_payload_masking_is_applied_at_the_prompt_boundary(dark_guard):
    """Would fail if narrate/interpret/voice_messages skipped for_llm and sent the raw result."""
    conn, cid = dark_guard
    hidden = _hidden(dark_guard)
    assert "npc_dozing_guard" in hidden
    raw = unseen.carry({
        "turn": 1,
        "results": [{
            "action_type": "attack", "status": "resolved", "outcome": "success",
            "target": {"key": "npc_dozing_guard", "name": "Dozing Guard"},
            "state_changes": [
                {"type": "npc_defeated", "key": "npc_dozing_guard", "name": "Dozing Guard"},
                {"type": "flag_set", "key": "defeated:npc_dozing_guard", "value": True},
            ],
            "intent_summary": "Attack the Dozing Guard.",
        }],
        "player_status": {"hp_band": "unhurt", "conditions": []},
        "dialogue": [], "npc_actions": [], "expired": [], "defeat": None,
        "clarification_question": None, "time_passed": True,
    }, hidden)
    ctx = context.build_context(conn, cid)
    persona = unseen.carry(_persona(conn), hidden)
    payload = unseen.carry(
        {"player_text": "attack the breathing", "talk": {"approach": "ask", "speaker_unseen": True},
         "recent_dialogue": [{"speaker_name": "Dozing Guard", "line": "Stay back."}]},
        hidden,
    )
    bodies = _request_bodies("attack the breathing", ctx, raw, persona=persona, payload=payload)
    assert_requests_hide_unseen(bodies, hidden)
    # And the engine result path, once masked, still leaves the canonical flag in the DB alone.
    assert db.get_flag(conn, "defeated:npc_dozing_guard") is None  # this test never set it



@pytest.mark.parametrize("name,key,ordinary,mention", [
    ("Will", "npc_will", "will you help", "Will you help"),
    ("Hope", "npc_hope", "I hope so", "Hope arrived"),
    ("Stone", "npc_stone", "damp stone walls", "Stone walls"),
])
def test_for_llm_does_not_rewrite_ordinary_words_that_match_a_display_name(name, key, ordinary, mention):
    """BUG-R8-02: whole-word, case-sensitive for a capitalized proper name that is also a common word."""
    hidden = {key: {"ref": "npc_unseen_9", "name": name, "aliases": [], "sound": "a scrape", "alive": True}}
    assert unseen.for_llm(ordinary, hidden) == ordinary
    assert name not in unseen.for_llm(mention, hidden)
    assert unseen.UNSEEN_NAME in unseen.for_llm(mention, hidden)
    # Keys still rewrite, case-insensitively.
    assert unseen.for_llm(f"defeated:{key.upper()}", hidden) == "defeated:npc_unseen_9"


def test_for_llm_key_match_is_case_insensitive_for_compound_flags():
    hidden = {"npc_dozing_guard": {"ref": "npc_unseen_2", "name": "Dozing Guard",
                                   "aliases": [], "sound": "breathing", "alive": False}}
    out = unseen.for_llm({"key": "defeated:NPC_DOZING_GUARD"}, hidden)
    assert out["key"] == "defeated:npc_unseen_2"



# --- Broad dark-scenario request-body guard ------------------------------------

def _prepare_dark_combat(demo, two=False):
    conn, cid = demo
    put(conn, "npc_dozing_guard", "loc_cellar")
    if two:
        put(conn, "npc_mara", "loc_cellar")
    go(demo, "loc_cellar")
    hostile_guard(demo)
    return demo


def _run(demo, text, *rolls):
    conn, cid = demo
    ctx = context.build_context(conn, cid)
    r, out = say(demo, text, *rolls)
    after = context.build_context(conn, cid)
    hidden = unseen.hidden_of(r) or unseen.hidden_of(after) or unseen.unseen_npcs(conn, cid)
    # If the foe just died they drop out of living context, but hidden_of(result) still has them.
    persona = None
    payload = None
    if hidden:
        key = next(iter(hidden))
        try:
            persona = unseen.carry(_persona(conn, key), hidden)
        except Exception:
            persona = unseen.carry(_persona(conn, "npc_dozing_guard"), hidden)
        talk_block = {"approach": "ask", "speaker_unseen": True}
        for res in r.get("results") or []:
            if res.get("talk"):
                talk_block = {k: v for k, v in res["talk"].items() if k != "insight_read"}
                talk_block["speaker_unseen"] = True
        payload = unseen.carry({"player_text": text, "talk": talk_block, "recent_dialogue": []}, hidden)
    # Interpret uses the pre-turn context (names are allowed if the room was still lit).
    # Narrate/voice use the result and the after-context, masked for anyone unseen this turn.
    after_ctx = after if after.get("location") else ctx
    interpret_bodies = _request_bodies(text, ctx, r, persona=persona, payload=payload)
    after_bodies = _request_bodies(text, after_ctx, r, persona=persona, payload=payload)
    return r, out, unseen.hidden_of(ctx) or {}, hidden, interpret_bodies, after_bodies


@pytest.mark.parametrize("label,setup", [
    ("attack_hit", "hit"),
    ("attack_miss", "miss"),
    ("kill_by_sound", "kill_sound"),
    ("kill_by_name", "kill_name"),
    ("talk", "talk"),
    ("give", "give"),
    ("trade", "trade"),
    ("flee_pursuit", "flee"),
    ("clarify", "clarify"),
    ("npc_counterattack", "counter"),
    ("daze", "daze"),
    ("several_unseen", "several"),
])
def test_dark_llm_requests_never_leak_unseen_identities(demo, label, setup):
    """Both providers, interpret/narrate/voice. Fails if for_llm is bypassed on a leaking field."""
    conn, cid = demo
    text = "look"
    rolls = (1, 1, 1, 1, 1, 1)
    if setup == "hit":
        _prepare_dark_combat(demo)
        conn.execute("UPDATE npcs SET current_hp = max_hp WHERE key = 'npc_dozing_guard'")
        text, rolls = "attack the breathing", (20, 4, 1, 1, 1)
    elif setup == "miss":
        _prepare_dark_combat(demo)
        text, rolls = "attack the breathing", (1, 1, 1, 1)
    elif setup == "kill_sound":
        _prepare_dark_combat(demo)
        conn.execute("UPDATE npcs SET current_hp = 1 WHERE key = 'npc_dozing_guard'")
        text, rolls = "attack the breathing", _kill_rolls()
    elif setup == "kill_name":
        _prepare_dark_combat(demo)
        conn.execute("UPDATE npcs SET current_hp = 1 WHERE key = 'npc_dozing_guard'")
        text, rolls = "attack the guard", _kill_rolls()
    elif setup == "talk":
        put(conn, "npc_mara", "loc_cellar")
        go(demo, "loc_cellar")
        text, rolls = "talk to the apron", (20, 1, 1, 1)
    elif setup == "give":
        _prepare_dark_combat(demo)
        text, rolls = "give the rations to the breathing", (1, 1, 1, 1)
    elif setup == "trade":
        put(conn, "npc_mara", "loc_cellar")
        go(demo, "loc_cellar")
        text, rolls = "trade with the apron", (1, 1, 1, 1)
    elif setup == "flee":
        go(demo, "loc_storeroom")
        hostile_guard(demo)
        conn.execute("UPDATE location_exits SET locked = 0")
        text, rolls = "go down", (1, 1, 1, 1)
    elif setup == "clarify":
        _prepare_dark_combat(demo, two=True)
        text, rolls = "attack the guard", (1, 1, 1, 1)
    elif setup == "counter":
        _prepare_dark_combat(demo)
        text, rolls = "attack the breathing", (1, 20, 6, 1)  # miss, then a solid NPC hit
    elif setup == "daze":
        _prepare_dark_combat(demo)
        # Overwhelming NPC hit: high roll + high damage. Player action can be wait or a miss.
        text, rolls = "wait", (20, 6, 1, 1)
    elif setup == "several":
        _prepare_dark_combat(demo, two=True)
        text, rolls = "look", (1, 1, 1, 1)
    r, out, hidden_before, hidden, interpret_bodies, after_bodies = _run(demo, text, *rolls)
    assert hidden or hidden_before, f"{label}: expected unseen NPCs at the start or end of the turn"
    # Interpret: only identities unseen in the pre-turn context must be absent (a lit departure may name them).
    if hidden_before:
        assert_requests_hide_unseen([b for b in interpret_bodies if b[1] == "interpret"], hidden_before, text)
    # Narrate and voice, and the after-turn interpret, see the post-turn / result mask.
    if hidden:
        nv = [b for b in after_bodies if b[1] in ("narrate", "voice")]
        assert_requests_hide_unseen(nv, hidden, text)
        assert_requests_hide_unseen([b for b in after_bodies if b[1] == "interpret"], hidden, text)
    if hidden:
        blob = json.dumps({k: v for k, v in r.items() if k != "player_text"})
        for token in unseen.identities(hidden):
            assert token.lower() not in blob.lower(), (label, token)
        if isinstance(out, str):
            low = out.lower()
            for token in unseen.identities(hidden):
                assert token.lower() not in low, (label, token, out)

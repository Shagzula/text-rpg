"""M2 / contracts v0.8: real provider clients, with the HTTP layer faked (no network)."""
import json
import random

import pytest

from rpg import actions, cli, db, llm, prompts, providers, talk
from rpg.context import build_context
from rpg.proposal_schema import proposal_schema
from rpg.world import seed_demo_slice

KEY = "sk-test-SECRET-123456"
GOOD = {"actions": [{"action_type": "examine", "skill": None, "target": None, "items_used": [], "last_ditch": False,
                     "est_difficulty": "medium", "intent_summary": "Look around.", "clarification_needed": False,
                     "clarification_question": None, "approach": None, "topic": None, "topic_text": None,
                     "quantity": None}],
        "ooc": None, "unresolved_references": []}


class FakeHTTP:
    """Scripted transport: each reply is (status, body dict|str|bytes) or an exception."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body), "timeout": timeout})
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        status, payload = r[0], r[1]
        hdrs = r[2] if len(r) > 2 else {}
        raw = payload if isinstance(payload, bytes) else (
            payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())
        return status, hdrs, raw


def oa_text(text, status="completed"):
    return {"status": status, "output": [{"type": "reasoning", "summary": []},
                                         {"type": "message", "content": [{"type": "output_text", "text": text}]}]}


def an_text(text, stop="end_turn"):
    return {"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": text}], "stop_reason": stop}


REPLY = {"openai": oa_text, "anthropic": an_text}
CLS = {"openai": providers.OpenAIClient, "anthropic": providers.AnthropicClient}
ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


@pytest.fixture
def world(tmp_path):
    conn = db.get_connection(tmp_path / "p.db")
    db.create_schema(conn)
    cid = seed_demo_slice(conn)
    yield conn, cid
    conn.close()


def make(monkeypatch, name, *replies, key=KEY, **kw):
    if key:
        monkeypatch.setenv(ENV[name], key)
    else:
        monkeypatch.delenv(ENV[name], raising=False)
    http = FakeHTTP(*replies)
    sleeps = []
    out = []
    c = CLS[name](transport=http, sleep=sleeps.append, out=out.append, rng=random.Random(0), **kw)
    return c, http, sleeps, out


both = pytest.mark.parametrize("name", ["openai", "anthropic"])


# --- request shape ----------------------------------------------------------------------------

def test_openai_interpret_request_shape(monkeypatch, world):
    c, http, _, _ = make(monkeypatch, "openai", (200, oa_text(json.dumps(GOOD))))
    ctx = build_context(*world)
    assert c.interpret("look around", ctx) == GOOD
    call = http.calls[0]
    assert call["url"] == "https://api.openai.com/v1/responses" and call["timeout"] == providers.TIMEOUT_S
    assert call["headers"]["Authorization"] == f"Bearer {KEY}"
    b = call["body"]
    assert b["model"] == "gpt-6-luna" and b["store"] is False and b["max_output_tokens"] > 0
    fmt = b["text"]["format"]
    assert fmt == {"type": "json_schema", "name": "action_proposal", "schema": proposal_schema(), "strict": True}
    system, user = prompts.interpret_messages(ctx, "look around")
    assert b["instructions"] == system and b["input"] == [{"role": "user", "content": user}]
    assert "temperature" not in b


def test_anthropic_interpret_request_shape(monkeypatch, world):
    c, http, _, _ = make(monkeypatch, "anthropic", (200, an_text(json.dumps(GOOD))))
    assert c.interpret("look around", build_context(*world)) == GOOD
    call = http.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == KEY and call["headers"]["anthropic-version"] == "2026-10-21"
    b = call["body"]
    assert b["model"] == "claude-haiku-5-5" and b["max_tokens"] > 0
    assert b["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert b["output_config"]["format"] == {"type": "json_schema", "schema": proposal_schema()}
    assert b["messages"][0]["role"] == "user" and "### Player's input this turn\nlook around" in b["messages"][0]["content"]
    assert not {"temperature", "top_p", "top_k"} & set(b)


@both
def test_narrate_uses_the_stronger_model_and_no_schema(monkeypatch, world, name):
    c, http, _, _ = make(monkeypatch, name, (200, REPLY[name]("You look about. What do you do?")))
    c._last_player_text = "look"
    r = {"turn": 1, "results": [], "npc_actions": []}
    assert c.narrate(r, build_context(*world)) == "You look about. What do you do?"
    b = http.calls[0]["body"]
    assert b["model"] == {"openai": "gpt-6.1-sol", "anthropic": "claude-sonnet-5-5"}[name]
    assert "text" not in b and "format" not in b.get("output_config", {})
    blob = json.dumps(b)
    assert "### NarrationResult (JSON, authoritative)" in blob and '\\"turn\\":1' in blob
    assert "### Player's input this turn\\nlook" in blob


@both
def test_voice_request_and_lines(monkeypatch, world, name):
    c, http, _, _ = make(monkeypatch, name, (200, REPLY[name]('{"lines": ["Mind the step."]}')))
    out = c.voice_npc({"key": "npc_mara", "name": "Mara"}, [], {"player_text": "hi", "talk": {"approach": "ask"},
                                                                 "recent_dialogue": []}, {})
    assert talk._clean_lines(out) == ["Mind the step."]
    b = json.dumps(http.calls[0]["body"])
    assert "npc_lines" in b or '"lines"' in b
    assert "Mara" in b and "secrets" not in b


@both
def test_model_env_overrides(monkeypatch, name):
    P = name.upper()
    monkeypatch.setenv(f"RPG_{P}_MODEL", "all-roles")
    monkeypatch.setenv(f"RPG_{P}_MODEL_NARRATE", "narrator-only")
    c, *_ = make(monkeypatch, name)
    assert c.models == {"interpret": "all-roles", "voice": "all-roles", "narrate": "narrator-only"}


def test_prompts_come_from_vendored_content(world):
    ctx = build_context(*world)
    system, user = prompts.interpret_messages(ctx, "go {north}")
    assert system.startswith("You are the **action interpreter**")
    assert "## Output schema" in system and "## Context for this turn" in system
    assert "{npcs_present}" not in user and json.dumps(ctx["npcs_present"], ensure_ascii=False,
                                                       separators=(",", ":")) in user
    assert user.rstrip().endswith("go {north}")  # player text is inserted verbatim, never re-filled
    assert "{player_inventory}" in system  # placeholders in rules/examples are references, untouched
    for kind in prompts.FILES:
        s, sec = prompts.template(kind)
        assert "Recommended call settings" not in s  # the human header isn't sent


def test_setting_and_tone_can_be_set(monkeypatch, world):
    monkeypatch.setenv("RPG_SETTING_SUMMARY", "A drowned city.")
    _, user = prompts.interpret_messages(build_context(*world), "look")
    assert "Setting summary: A drowned city." in user


# --- keys and redaction ---------------------------------------------------------------------------

@both
def test_missing_key_is_a_clear_error(monkeypatch, name):
    monkeypatch.delenv(ENV[name], raising=False)
    with pytest.raises(llm.LLMConfigError) as e:
        CLS[name]()
    assert ENV[name] in str(e.value) and "--dry-run" in str(e.value)


@both
def test_key_never_leaks_in_errors_or_repr(monkeypatch, world, name, caplog):
    caplog.set_level("DEBUG")
    echo = {"error": {"message": f"bad key {KEY}"}}
    c, http, sleeps, _ = make(monkeypatch, name, (401, echo))
    with pytest.raises(providers.LLMError) as e:
        c.interpret("look", build_context(*world))
    assert KEY not in str(e.value) and providers.REDACTED in str(e.value)
    assert KEY not in repr(c) and KEY not in caplog.text and sleeps == []  # 401: no retry


# --- retries ------------------------------------------------------------------------------------------

@both
def test_retries_429_and_5xx_with_capped_backoff(monkeypatch, world, name):
    c, http, sleeps, _ = make(monkeypatch, name, (429, {}), (503, {}), (500, "oops"),
                              (200, REPLY[name](json.dumps(GOOD))))
    assert c.interpret("look", build_context(*world)) == GOOD
    assert len(http.calls) == 4 and len(sleeps) == 3
    assert sleeps[0] < sleeps[1] < sleeps[2] <= providers.BACKOFF_CAP_S * 1.25


@both
def test_retry_after_is_honoured_but_capped(monkeypatch, world, name):
    c, http, sleeps, _ = make(monkeypatch, name, (429, {}, {"Retry-After": "3"}), (429, {}, {"retry-after": "999"}),
                              (200, REPLY[name](json.dumps(GOOD))))
    c.interpret("look", build_context(*world))
    assert sleeps[0] >= 3 and sleeps[1] <= providers.BACKOFF_CAP_S * 1.25


@both
def test_gives_up_after_max_retries(monkeypatch, world, name):
    c, http, sleeps, _ = make(monkeypatch, name, *[(503, {})] * (providers.MAX_RETRIES + 1))
    with pytest.raises(providers.LLMError, match="attempts"):
        c.interpret("look", build_context(*world))
    assert len(http.calls) == providers.MAX_RETRIES + 1 and len(sleeps) == providers.MAX_RETRIES


@both
def test_network_errors_are_retried(monkeypatch, world, name):
    c, http, sleeps, _ = make(monkeypatch, name, TimeoutError(), OSError("reset"),
                              (200, REPLY[name](json.dumps(GOOD))))
    assert c.interpret("look", build_context(*world)) == GOOD and len(sleeps) == 2


@both
def test_400_is_not_retried(monkeypatch, world, name):
    c, http, sleeps, _ = make(monkeypatch, name, (400, {"error": {"message": "bad schema"}}))
    with pytest.raises(providers.LLMError, match="400"):
        c.interpret("look", build_context(*world))
    assert len(http.calls) == 1


# --- response parsing / re-prompt -----------------------------------------------------------------------

@both
def test_malformed_json_goes_through_the_single_reprompt(monkeypatch, world, name):
    conn, cid = world
    c, http, _, _ = make(monkeypatch, name, (200, REPLY[name]("Sure! {not json")),
                         (200, REPLY[name](json.dumps(GOOD))))
    g = cli.Game.__new__(cli.Game)
    g.llm, g.cid, g.rng, g.out = c, cid, random.Random(1), (lambda s: None)
    r = g._interpret_and_process(conn, "look around", build_context(conn, cid))
    assert r and not r.get("error") and r["results"][0]["action_type"] == "examine"
    assert len(http.calls) == 2
    second = http.calls[1]["body"]
    msgs = second.get("input") or second.get("messages")
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"] == "Sure! {not json" and "did not match the action-proposal schema" in msgs[2]["content"]


@both
def test_schema_violation_twice_spends_no_turn(monkeypatch, world, name):
    conn, cid = world
    bad = json.dumps({"actions": [{"action_type": "dance"}], "ooc": None, "unresolved_references": []})
    c, http, _, _ = make(monkeypatch, name, (200, REPLY[name](bad)), (200, REPLY[name](bad)))
    g = cli.Game.__new__(cli.Game)
    g.llm, g.cid, g.rng, g.out = c, cid, random.Random(1), (lambda s: None)
    assert g._interpret_and_process(conn, "dance", build_context(conn, cid)) is None
    assert len(http.calls) == 2


def test_openai_refusal_and_empty_output_raise(monkeypatch, world):
    refusal = {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal",
                                                                                  "refusal": "no"}]}]}
    c, *_ = make(monkeypatch, "openai", (200, refusal))
    with pytest.raises(providers.LLMError, match="refused"):
        c.interpret("look", build_context(*world))


def test_anthropic_empty_content_raises(monkeypatch, world):
    c, *_ = make(monkeypatch, "anthropic", (200, {"content": [], "stop_reason": "refusal"}))
    with pytest.raises(providers.LLMError, match="refusal"):
        c.interpret("look", build_context(*world))


def test_non_json_body_raises(monkeypatch, world):
    c, *_ = make(monkeypatch, "openai", (200, b"<html>gateway</html>"))
    with pytest.raises(providers.LLMError, match="not JSON"):
        c.interpret("look", build_context(*world))


@both
def test_narration_goes_through_the_post_processor(monkeypatch, world, name):
    c, http, _, _ = make(monkeypatch, name, (200, REPLY[name]('{"oops": 1}')), (200, REPLY[name]("You wait.")))
    out = llm.narrate_safely(c, {"turn": 1, "results": []}, build_context(*world))
    assert out == "You wait.\n\nWhat do you do?" and len(http.calls) == 2


@both
def test_provider_errors_in_the_cli_spend_no_turn(monkeypatch, world, name):
    conn, cid = world
    c, *_ = make(monkeypatch, name, (401, {}))
    said = []
    g = cli.Game.__new__(cli.Game)
    g.llm, g.cid, g.rng, g.out = c, cid, random.Random(1), said.append
    assert g._interpret_and_process(conn, "look", build_context(conn, cid)) is None
    assert "LLM provider not available" in said[0] and KEY not in said[0]


# --- dry run -------------------------------------------------------------------------------------------

@both
def test_dry_run_prints_requests_and_never_calls_out(monkeypatch, world, name):
    c, http, _, out = make(monkeypatch, name, dry_run=True)
    ctx = build_context(*world)
    p = c.interpret("look around", ctx)
    assert p == llm.MockLLM().interpret("look around", ctx)  # the mock answers
    assert http.calls == []
    req = json.loads(out[0])
    assert req["dry_run"] and req["role"] == "interpret" and req["url"] == CLS[name].url
    assert KEY not in out[0] and providers.REDACTED in json.dumps(req["headers"])
    assert req["body"] == c.build("interpret", *prompts.interpret_messages(ctx, "look around")[:1],
                                  [("user", prompts.interpret_messages(ctx, "look around")[1])],
                                  schema=("action_proposal", proposal_schema()))
    assert c.narrate({"turn": 1, "results": []}, ctx)  # mock prose
    assert json.loads(out[1])["role"] == "narrate" and http.calls == []


@both
def test_dry_run_works_without_a_key(monkeypatch, world, name):
    c, http, _, out = make(monkeypatch, name, key=None, dry_run=True)
    c.interpret("look", build_context(*world))
    assert out and http.calls == [] and f"<unset: {ENV[name]}>" in out[0]


def test_dry_run_from_env_and_cli_flag(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("RPG_LLM_DRY_RUN", "1")
    assert llm.get_llm("openai").dry_run is True
    monkeypatch.delenv("RPG_LLM_DRY_RUN")
    with pytest.raises(llm.LLMConfigError):
        llm.get_llm("openai")
    assert llm.get_llm("anthropic", dry_run=True).dry_run is True
    seen = {}

    class FakeGame:
        def __init__(self, path, llm=None, rng=None):
            seen["llm"] = llm

        def run(self, new=False, load=None):
            seen["llm"].narrate({"opening": True, "results": []}, {})

    monkeypatch.setattr(cli, "Game", FakeGame)
    assert cli.main(["--db", str(tmp_path / "d.db"), "--llm", "openai", "--dry-run"]) == 0
    assert seen["llm"].dry_run is True
    err = capsys.readouterr().err
    assert '"dry_run": true' in err and '"role": "narrate"' in err and "<unset: OPENAI_API_KEY>" in err


def test_schema_matches_the_engine_validator():
    s = proposal_schema()
    a = s["properties"]["actions"]["items"]
    assert set(a["required"]) == set(a["properties"]) and a["additionalProperties"] is False
    assert a["properties"]["action_type"]["enum"] == list(actions.ACTION_TYPES)
    assert actions.validate_proposal(GOOD)[1] == []


def test_schema_enums_match_contracts_and_the_committed_file():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    contracts = (root / "docs" / "contracts.md").read_text()
    row = next(ln for ln in contracts.splitlines() if ln.startswith("| `action_type` | enum |"))
    assert [w.strip("` ") for w in row.split("|")[3].split(",")] == list(actions.ACTION_TYPES)
    ooc_row = next(ln for ln in contracts.splitlines() if ln.startswith("| `ooc` |"))
    assert "|".join(actions.OOC_COMMANDS) in ooc_row.replace("\\|", "|")
    assert json.loads((root / "docs" / "action_proposal.schema.json").read_text()) == proposal_schema()


@both
def test_a_full_cli_turn_through_a_faked_provider(monkeypatch, tmp_path, name):
    from test_cli import Script
    c, http, _, _ = make(monkeypatch, name, (200, REPLY[name]("You stand in the square.")),
                         (200, REPLY[name](json.dumps(GOOD))), (200, REPLY[name]("You look around. Quiet.")))
    out = []
    g = cli.Game(tmp_path / "f.db", llm=c, rng=random.Random(3), input_fn=Script("look around", "/quit"),
                 output_fn=out.append)
    g.run(new=True)
    assert "You stand in the square.\n\nWhat do you do?" in out
    assert "You look around. Quiet.\n\nWhat do you do?" in out
    assert [json.loads(json.dumps(x["body"])).get("model") for x in http.calls] == \
        [c.models["narrate"], c.models["interpret"], c.models["narrate"]]

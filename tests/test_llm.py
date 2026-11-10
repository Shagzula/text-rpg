import pytest

from rpg import actions, db, llm
from rpg.context import build_context
from rpg.llm import CLOSING_LINE, MockLLM
from rpg.world import seed_demo_slice


@pytest.fixture
def game(tmp_path):
    conn = db.get_connection(tmp_path / "g.db")
    db.create_schema(conn)
    cid = seed_demo_slice(conn)
    yield conn, cid
    conn.close()


def move_to(conn, cid, key):
    conn.execute("UPDATE characters SET location_id = (SELECT id FROM locations WHERE key = ?) WHERE id = ?", (key, cid))


def interp(conn, cid, text):
    p = MockLLM().interpret(text, build_context(conn, cid))
    assert actions.validate_proposal(p)[1] == []  # always contract-valid
    return p


def one(conn, cid, text):
    p = interp(conn, cid, text)
    assert p["ooc"] is None
    return p["actions"][0], p


@pytest.mark.parametrize("text,expect", [
    ("go east", {"action_type": "move", "target": "loc_storeroom"}),
    ("move to the storeroom", {"action_type": "move", "target": "loc_storeroom"}),
    ("east", {"action_type": "move", "target": "loc_storeroom"}),
    ("look", {"action_type": "examine", "target": "loc_town_square"}),
    ("examine the crowbar", {"action_type": "examine", "target": "item_crowbar"}),
    ("look at mara", {"action_type": "examine", "target": "npc_mara"}),
    ("take crowbar", {"action_type": "take", "items_used": ["item_crowbar"]}),
    ("pick up the crowbar", {"action_type": "take", "items_used": ["item_crowbar"]}),
    ("drop lockpick set", {"action_type": "drop", "items_used": ["item_lockpicks"]}),
    ("equip shortsword", {"action_type": "equip", "items_used": ["item_rusty_shortsword"]}),
    ("unequip boots", {"action_type": "unequip", "items_used": ["item_travel_boots"]}),
    ("attack mara", {"action_type": "attack", "target": "npc_mara", "items_used": ["item_rusty_shortsword"]}),
    ("attack mara with crowbar", {"action_type": "attack", "items_used": ["item_crowbar"]}),
    ("talk to mara", {"action_type": "talk", "target": "npc_mara", "skill": None}),
    ("i", {"action_type": "inventory"}),
    ("inventory", {"action_type": "inventory"}),
    ("rest", {"action_type": "rest"}),
    ("wait", {"action_type": "wait"}),
    ("drink healing draught", {"action_type": "use_item", "items_used": ["item_healing_draught"], "target": "self"}),
    ("sing a sad song", {"action_type": "other", "skill": None, "intent_summary": "Try to sing a sad song."}),
])
def test_mock_interpreter_mapping(game, text, expect):
    a, _ = one(*game, text)
    for k, v in expect.items():
        assert a[k] == v, (k, a)
    assert a["last_ditch"] is False


def test_mock_unknown_things_get_best_guess_slugs(game):
    a, p = one(*game, "attack the dragon with my greatsword")
    assert a["target"] == "npc_dragon" and a["items_used"] == ["item_greatsword"] and p["unresolved_references"] == []
    a, _ = one(*game, "go north")  # no such exit: the direction goes to the engine as-is
    assert a["target"] == "north"


@pytest.mark.parametrize("text", ["desperately kick the door", "try hard to force the door",
                                  "kick the door with everything i have"])
def test_mock_last_ditch_and_door(game, text):
    conn, cid = game
    move_to(conn, cid, "loc_storeroom")
    a, _ = one(conn, cid, text)
    assert a["action_type"] == "skill_check" and a["skill"] == "athletics" and a["target"] == "loc_cellar"
    assert a["last_ditch"] is True


def test_mock_pick_lock_targets_the_locked_exit(game):
    conn, cid = game
    move_to(conn, cid, "loc_storeroom")
    for text in ("pick the lock on the door", "pick the lock", "unlock door"):
        a, _ = one(conn, cid, text)
        assert (a["skill"], a["target"]) == ("sleight_of_hand", "loc_cellar"), text


def test_mock_use_fixture_targets_it(game):
    conn, cid = game
    move_to(conn, cid, "loc_cellar")
    conn.execute("UPDATE items SET state = 'lit' WHERE key = 'item_torch'")  # the demo cellar is dark
    a, _ = one(conn, cid, "pull lever")
    assert a["items_used"] == ["item_rusty_lever"] and a["target"] == "item_rusty_lever"


def test_mock_ambiguity_clarifies(game):
    conn, cid = game
    db.add_item(conn, "item_crowbar", location_id=1, key="item_crowbar_2", display_name="Bent Crowbar")
    a, p = one(conn, cid, "examine crowbar")
    assert p["unresolved_references"] == ["crowbar"] and a["clarification_needed"] and a["clarification_question"]


@pytest.mark.parametrize("text,cmd", [("quit", "quit"), ("exit", "quit"), ("save", "save"), ("help", "help")])
def test_mock_ooc(game, text, cmd):
    p = interp(*game, text)
    assert p["actions"] == [] and p["ooc"]["command"] == cmd


def test_mock_narration_is_second_person_without_numbers(game):
    conn, cid = game
    import random
    m = MockLLM()
    for text in ("look", "take crowbar", "go east", "desperately kick the door", "attack guard", "i", "rest"):
        p = m.interpret(text, build_context(conn, cid))
        r = actions.process_action_proposal(conn, cid, p, rng=random.Random(5))
        out = llm.narrate_safely(m, r, build_context(conn, cid))
        assert out.endswith(CLOSING_LINE) and out.strip() != CLOSING_LINE
        body = out[: -len(CLOSING_LINE)]
        assert not any(ch.isdigit() for ch in body), out
        assert "you" in out.lower()


def test_mock_opening_describes_scene(game):
    out = MockLLM().narrate({"opening": True, "results": []}, build_context(*game))
    assert "Town Square" in out and "Mara" in out and out.endswith(CLOSING_LINE)


# --- post-processing ---------------------------------------------------------------

def test_closing_line_appended_once():
    assert llm.finalize_narration("You wait.") == f"You wait.\n\n{CLOSING_LINE}"
    assert llm.finalize_narration(f"You wait. {CLOSING_LINE}") == f"You wait. {CLOSING_LINE}"
    assert llm.finalize_narration(f"You wait.\n{CLOSING_LINE}\n\n").count(CLOSING_LINE) == 1


@pytest.mark.parametrize("bad", ["", "   ", None, '{"text": "hi"}', "[1, 2]", "```\nYou wait\n```", "<p>x</p>"])
def test_malformed_regenerates_once(bad):
    calls = []

    def regen():
        calls.append(1)
        return "You blink and look again."
    assert llm.finalize_narration(bad, regenerate=regen) == f"You blink and look again.\n\n{CLOSING_LINE}"
    assert len(calls) == 1


def test_still_malformed_after_regen_falls_back():
    calls = []

    def regen():
        calls.append(1)
        return ""
    out = llm.finalize_narration("{}", regenerate=regen)
    assert out == f"{llm.FALLBACK_NARRATION}\n\n{CLOSING_LINE}" and len(calls) == 1


def test_good_narration_not_regenerated():
    def regen():
        raise AssertionError("should not regenerate")
    assert llm.finalize_narration("You wait.", regenerate=regen).startswith("You wait.")


class FlakyNarrator(MockLLM):
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def narrate(self, result, context):
        self.calls += 1
        v = self.outputs.pop(0)
        if isinstance(v, Exception):
            raise v
        return v


def test_narrate_safely_regenerates_and_survives_errors():
    f = FlakyNarrator(['{"oops": 1}', "You stand still."])
    assert llm.narrate_safely(f, {}, {}) == f"You stand still.\n\n{CLOSING_LINE}" and f.calls == 2
    f = FlakyNarrator([RuntimeError("boom"), RuntimeError("boom")])
    assert llm.narrate_safely(f, {}, {}).startswith(llm.FALLBACK_NARRATION) and f.calls == 2


# --- providers -----------------------------------------------------------------------

def test_factory_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("RPG_LLM", raising=False)
    assert isinstance(llm.get_llm(), MockLLM)
    assert isinstance(llm.get_llm(), llm.LLMClient)
    monkeypatch.setenv("RPG_LLM", "MOCK")
    assert isinstance(llm.get_llm(), MockLLM)


@pytest.mark.parametrize("name,env", [("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")])
def test_factory_fails_clearly_without_key(monkeypatch, name, env):
    monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("RPG_LLM", name)
    with pytest.raises(llm.LLMConfigError) as e:
        llm.get_llm()
    assert env in str(e.value) and "RPG_LLM=mock" in str(e.value)


@pytest.mark.parametrize("name,env,cls", [("openai", "OPENAI_API_KEY", llm.OpenAIClient),
                                          ("anthropic", "ANTHROPIC_API_KEY", llm.AnthropicClient)])
def test_provider_stub_with_key(monkeypatch, name, env, cls):
    monkeypatch.setenv(env, "sk-test-not-real")
    monkeypatch.setenv("RPG_LLM_MODEL", "some-model")
    client = llm.get_llm(name)
    assert isinstance(client, cls) and client.model == "some-model"
    assert "sk-test" not in repr(client)
    with pytest.raises(NotImplementedError):
        client.interpret("look", {})
    with pytest.raises(NotImplementedError):
        client.narrate({}, {})


def test_factory_unknown_provider():
    with pytest.raises(llm.LLMConfigError) as e:
        llm.get_llm("gemini-ultra")
    assert "mock" in str(e.value)


def test_no_api_key_literals_in_source():
    import pathlib
    src = pathlib.Path(llm.__file__).read_text()
    assert "sk-" not in src

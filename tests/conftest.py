import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import pytest  # noqa: E402

PROVIDER_ENV = ("RPG_LLM_DRY_RUN", "RPG_LLM_MODEL", "RPG_OPENAI_MODEL", "RPG_ANTHROPIC_MODEL",
                "RPG_OPENAI_REASONING_EFFORT", "RPG_ANTHROPIC_EFFORT", "RPG_SETTING_SUMMARY", "RPG_TONE_NOTES")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never reach a real API: the default transport and urlopen both fail loudly."""
    import urllib.request
    from rpg import providers

    def blocked(*a, **k):
        raise AssertionError("network access in tests")
    monkeypatch.setattr(providers, "urllib_transport", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    for k in PROVIDER_ENV:
        monkeypatch.delenv(k, raising=False)
    for role in ("INTERPRET", "NARRATE", "VOICE"):
        monkeypatch.delenv(f"RPG_OPENAI_MODEL_{role}", raising=False)
        monkeypatch.delenv(f"RPG_ANTHROPIC_MODEL_{role}", raising=False)

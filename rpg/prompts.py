"""Prompt templates from the vendored content/ (the Story Designer's files, never edited here).

Each prompt file is: a header for humans, a `---` line, then the system prompt. The system
prompt's context section (from "## Game context" / "## Setting and tone" up to the first rules
heading) holds the {placeholders}. We send:

- system = the system prompt with that section replaced by a pointer (static, so it caches), and
- user   = the context section with its placeholders filled from build_context() / the engine result.

Placeholders in the rules and examples are references, not slots, and are left alone.
"""
from __future__ import annotations

import html
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Tuple

from . import unseen

CONTENT_DIR = Path(__file__).resolve().parents[1] / "content"
FILES = {"interpret": "interpreter_prompt.md", "narrate": "narrator_prompt.md", "voice": "npc_voice_prompt.md"}
CONTEXT_HEADINGS = ("## Game context", "## Setting and tone")
RULES_HEADING = re.compile(r"^## (Hard rules|Rules)\b", re.M)
POINTER = "## Context for this turn\nThe filled-in context for this turn is in the user message, under the same headings.\n\n"
# Dan hasn't chosen the setting yet (content/README.md open question 1); override with env vars.
DEFAULT_SETTING = "A small, grounded fantasy world (placeholder until the setting is chosen)."
DEFAULT_TONE = "Grounded, concise, second person; danger is real but not grim."
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)


class PromptError(RuntimeError):
    pass


@lru_cache(maxsize=None)
def template(kind: str, content_dir: str = str(CONTENT_DIR)) -> Tuple[str, str]:
    """(static system text, context section with {placeholders}) for interpret|narrate|voice."""
    text = (Path(content_dir) / FILES[kind]).read_text(encoding="utf-8")
    head, sep, body = text.partition("\n---\n")
    if not sep:
        raise PromptError(f"{FILES[kind]}: no '---' line before the system prompt")
    starts = [body.find(h) for h in CONTEXT_HEADINGS if body.find(h) >= 0]
    m = RULES_HEADING.search(body)
    if not starts or not m or m.start() < min(starts):
        raise PromptError(f"{FILES[kind]}: can't find the context section and the rules heading")
    a, b = min(starts), m.start()
    system = (body[:a] + POINTER + body[b:]).strip() + "\n"
    section = body[a:b]
    # Field-doc comments used to travel with every user message; park them on the
    # cached system side so the per-call user text is shorter.
    comments = HTML_COMMENT.findall(section)
    if comments:
        system = system.rstrip() + "\n\n## Context field notes\n" + "\n".join(comments) + "\n"
        section = HTML_COMMENT.sub("", section)
        section = re.sub(r"\n{3,}", "\n\n", section)
    return system, section.strip() + "\n"


def _render(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def fill(section: str, values: Dict) -> str:
    def sub(m):
        if m.group(1) not in values:
            raise PromptError(f"no value for {{{m.group(1)}}}")
        return _render(values[m.group(1)])
    return PLACEHOLDER.sub(sub, section)


def setting_values() -> Dict[str, str]:
    return {"setting_summary": os.environ.get("RPG_SETTING_SUMMARY") or DEFAULT_SETTING,
            "tone_notes": os.environ.get("RPG_TONE_NOTES") or DEFAULT_TONE}


CONTEXT_FIELDS = ("location", "npcs_present", "visible_items", "felt_items", "player_inventory", "player_sheet",
                  "hp_band", "recent_narrations")


def fence_player_text(player_text: str) -> str:
    """Wrap player input in <player_input> after escaping &, <, and >.

    The treat-as-data instruction lives in the Story Designer's prompt text (exactly
    once). Escaping means a typed '</player_input>' cannot close the fence.
    """
    safe = html.escape(player_text or "", quote=False)
    return "<player_input>\n" + safe + "\n</player_input>"


def context_values(context: Dict, player_text: str) -> Dict:
    vals = {k: context.get(k, [] if k != "hp_band" else "unhurt") for k in CONTEXT_FIELDS}
    vals.update(setting_values(), player_text=fence_player_text(player_text))
    return vals


def interpret_messages(context: Dict, player_text: str) -> Tuple[str, str]:
    context = unseen.for_llm(context)
    system, section = template("interpret")
    return system, fill(section, context_values(context, player_text))


def narrate_messages(result: Dict, context: Dict, player_text: str) -> Tuple[str, str]:
    hidden = unseen.hidden_of(result) or unseen.hidden_of(context)
    result = unseen.for_llm(result, hidden)
    context = unseen.for_llm(context, hidden)
    system, section = template("narrate")
    return system, fill(section, {**context_values(context, player_text), "narration_result": result})


def voice_messages(persona_view: Dict, payload: Dict) -> Tuple[str, str]:
    hidden = unseen.hidden_of(persona_view) or unseen.hidden_of(payload)
    persona_view = unseen.for_llm(persona_view, hidden)
    payload = unseen.for_llm(payload, hidden)
    system, section = template("voice")
    vals = {**setting_values(), "npc": persona_view, "talk": payload.get("talk", {}),
            "player_text": fence_player_text(payload.get("player_text", "")),
            "recent_dialogue": payload.get("recent_dialogue", [])}
    return system, fill(section, vals)

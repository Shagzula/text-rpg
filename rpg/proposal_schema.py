"""JSON Schema for the interpreter's action proposal (contracts.md (a), talk fields from (d)).

Built from the same constants the engine validates against, in the "strict" subset both
OpenAI structured outputs and Anthropic JSON outputs accept: every property is listed in
`required`, optional values are nullable, `additionalProperties: false` everywhere. The
engine's validate_proposal stays the authority (e.g. the 0-3 actions limit and the talk-only
fields are checked there; a schema failure goes through the usual single re-prompt).

    python -m rpg.proposal_schema > docs/action_proposal.schema.json
"""
from __future__ import annotations

import json
from typing import Dict

from . import rules
from .actions import ACTION_TYPES, DIFFICULTY_DC, OOC_COMMANDS, TALK_APPROACHES

SCHEMA_NAME = "action_proposal"


def _nullable_enum(values) -> Dict:
    return {"type": ["string", "null"], "enum": [*values, None]}


def proposal_schema() -> Dict:
    skills = list(rules.SKILL_ABILITY) + list(rules.ABILITIES)
    action = {
        "type": "object",
        "properties": {
            "action_type": {"type": "string", "enum": list(ACTION_TYPES)},
            "skill": _nullable_enum(skills),
            "target": {"type": ["string", "null"],
                       "description": "context key, destination loc_ key, direction, 'back', 'self', or null"},
            "items_used": {"type": "array", "items": {"type": "string"}},
            "last_ditch": {"type": "boolean"},
            "est_difficulty": {"type": "string", "enum": list(DIFFICULTY_DC)},
            "intent_summary": {"type": "string"},
            "clarification_needed": {"type": "boolean"},
            "clarification_question": {"type": ["string", "null"]},
            "approach": _nullable_enum(TALK_APPROACHES),
            "topic": {"type": ["string", "null"]},
            "topic_text": {"type": ["string", "null"]},
            "quantity": {"type": ["integer", "null"]},
        },
        "additionalProperties": False,
    }
    action["required"] = list(action["properties"])
    ooc = {"type": "object",
           "properties": {"command": {"type": "string", "enum": list(OOC_COMMANDS)}, "text": {"type": "string"}},
           "required": ["command", "text"], "additionalProperties": False}
    return {
        "type": "object",
        "properties": {
            "actions": {"type": "array", "items": action, "description": "0-3 actions; [] only when ooc is set"},
            "ooc": {"anyOf": [ooc, {"type": "null"}]},
            "unresolved_references": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["actions", "ooc", "unresolved_references"],
        "additionalProperties": False,
    }


def voice_schema() -> Dict:
    return {"type": "object", "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
            "required": ["lines"], "additionalProperties": False}


if __name__ == "__main__":
    print(json.dumps(proposal_schema(), indent=2))

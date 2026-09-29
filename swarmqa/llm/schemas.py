"""JSON schemas for structured model output, one per provider method.

Written to the subset both Anthropic structured outputs and OpenAI strict
mode accept: every object has `additionalProperties: false`, every
property is required, optional values are `anyOf [T, null]`, and there are
no numeric or length constraints (ranges are checked in `parse.py`).
"""

from __future__ import annotations

from typing import Any

STEP_KINDS = ["tap", "tap_point", "type", "swipe", "key", "back", "done", "give_up"]
COMPUTER_KINDS = ["tap_point", "swipe", "type", "key", "done", "give_up"]
CATEGORIES = ["broken", "visual", "confusing", "crash"]
SEVERITIES = ["critical", "high", "medium", "low"]


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_NUMBER = {"type": "number"}
_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": _STRING}


def _action(kinds: list[str], *, with_element: bool) -> dict[str, Any]:
    properties: dict[str, Any] = {"kind": {"type": "string", "enum": kinds}}
    if with_element:
        properties["element_id"] = _nullable(_STRING)
    properties.update(
        {
            "x": _nullable(_NUMBER),
            "y": _nullable(_NUMBER),
            "end_x": _nullable(_NUMBER),
            "end_y": _nullable(_NUMBER),
            "text": _nullable(_STRING),
            "keys": _STRINGS,
            "rationale": _STRING,
            "confidence": _NUMBER,
        }
    )
    return _object(properties)


STEP_SCHEMA = _action(STEP_KINDS, with_element=True)
COMPUTER_USE_SCHEMA = _action(COMPUTER_KINDS, with_element=False)

JUDGE_SCHEMA = _object(
    {
        "summary": _STRING,
        "issues": {
            "type": "array",
            "items": _object(
                {
                    "category": {"type": "string", "enum": CATEGORIES},
                    "title": _STRING,
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "confidence": _NUMBER,
                    "rationale": _STRING,
                    "element_id": _nullable(_STRING),
                    "bbox": _nullable(
                        _object({"x": _NUMBER, "y": _NUMBER, "width": _NUMBER, "height": _NUMBER})
                    ),
                }
            ),
        },
    }
)

FLOWS_SCHEMA = _object(
    {
        "flows": {
            "type": "array",
            "items": _object(
                {
                    "name": _STRING,
                    "goal": _STRING,
                    "priority": {"type": "integer"},
                    "steps": _STRINGS,
                    "expected": _STRINGS,
                    "touched_files": _STRINGS,
                }
            ),
        }
    }
)

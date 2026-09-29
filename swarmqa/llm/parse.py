"""Turn validated JSON from a model into protocol dataclasses.

Anything that does not fit (wrong type, missing field for the action
kind, unknown element id, confidence outside 0..1) raises `ModelRefused`,
so callers fail open exactly as they do for a refusal.
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable

from swarmqa.llm.protocol import (
    FlowProposal,
    JudgedIssue,
    ModelRefused,
    ProposedFlow,
    ScreenJudgment,
    StepDecision,
)
from swarmqa.llm.schemas import CATEGORIES, COMPUTER_KINDS, SEVERITIES, STEP_KINDS
from swarmqa.llm.serialize import query_for
from swarmqa.models import UIElement

PointMap = Callable[[float, float], tuple[float, float]]


def _identity(x: float, y: float) -> tuple[float, float]:
    return (x, y)


def load_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ModelRefused("model output is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ModelRefused("model output is not a JSON object")
    return data


def parse_step(
    data: dict[str, Any],
    elements: dict[str, UIElement],
    *,
    allowed: list[str] | None = None,
    to_points: PointMap = _identity,
) -> StepDecision:
    kinds = allowed or STEP_KINDS
    kind = data.get("kind")
    if kind not in kinds:
        raise ModelRefused(f"unknown action kind: {kind!r}")
    decision = StepDecision(
        kind=kind,
        rationale=_string(data.get("rationale", ""), "rationale"),
        confidence=_confidence(data.get("confidence", 1.0)),
    )
    element_id = data.get("element_id")
    if element_id not in (None, ""):
        if not isinstance(element_id, str) or element_id not in elements:
            raise ModelRefused(f"unknown element_id: {element_id!r}")
        decision.target = query_for(elements[element_id])
    point = _point(data, "x", "y", to_points)
    end = _point(data, "end_x", "end_y", to_points)
    if kind == "tap":
        if decision.target is None:
            raise ModelRefused("tap needs element_id")
    elif kind == "tap_point":
        if point is None:
            raise ModelRefused("tap_point needs x and y")
        decision.point = point
    elif kind == "type":
        text = data.get("text")
        if not isinstance(text, str) or not text:
            raise ModelRefused("type needs text")
        decision.text = text
        decision.point = point
    elif kind == "swipe":
        if point is None or end is None:
            raise ModelRefused("swipe needs x, y, end_x and end_y")
        decision.point, decision.end = point, end
    elif kind == "key":
        keys = data.get("keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k for k in keys):
            raise ModelRefused("key needs keys")
        decision.keys = list(keys)
    if kind not in ("tap", "type"):
        decision.target = None
    return decision


def parse_computer_action(data: dict[str, Any], *, to_points: PointMap) -> StepDecision:
    return parse_step({**data, "element_id": None}, {}, allowed=COMPUTER_KINDS, to_points=to_points)


def parse_judgment(
    data: dict[str, Any],
    elements: dict[str, UIElement],
    *,
    min_confidence: float,
    to_points: PointMap = _identity,
) -> ScreenJudgment:
    raw_issues = data.get("issues")
    if not isinstance(raw_issues, list):
        raise ModelRefused("issues must be a list")
    issues: list[JudgedIssue] = []
    for raw in raw_issues:
        if not isinstance(raw, dict):
            raise ModelRefused("issue must be an object")
        category = raw.get("category")
        severity = raw.get("severity", "medium")
        if category not in CATEGORIES:
            raise ModelRefused(f"unknown category: {category!r}")
        if severity not in SEVERITIES:
            raise ModelRefused(f"unknown severity: {severity!r}")
        title = _string(raw.get("title"), "title").strip()
        if not title:
            raise ModelRefused("issue title is empty")
        confidence = _confidence(raw.get("confidence"))
        if confidence < min_confidence:
            continue
        issue = JudgedIssue(
            category=category,
            title=title,
            severity=severity,
            confidence=confidence,
            rationale=_string(raw.get("rationale", ""), "rationale"),
        )
        element_id = raw.get("element_id")
        if isinstance(element_id, str) and element_id in elements:
            issue.element = query_for(elements[element_id])
        issue.bbox = _bbox(raw.get("bbox"), to_points)
        issues.append(issue)
    return ScreenJudgment(issues=issues, summary=_string(data.get("summary", ""), "summary"))


def parse_flows(data: dict[str, Any], *, max_flows: int) -> FlowProposal:
    raw_flows = data.get("flows")
    if not isinstance(raw_flows, list):
        raise ModelRefused("flows must be a list")
    flows: list[ProposedFlow] = []
    for raw in raw_flows:
        if not isinstance(raw, dict):
            raise ModelRefused("flow must be an object")
        name = _string(raw.get("name"), "name").strip()
        goal = _string(raw.get("goal"), "goal").strip()
        if not name or not goal:
            raise ModelRefused("flow needs a name and a goal")
        priority = raw.get("priority", 3)
        if (
            isinstance(priority, bool)
            or not isinstance(priority, (int, float))
            or not math.isfinite(priority)
            or priority != int(priority)
        ):
            raise ModelRefused("priority must be an integer")
        flows.append(
            ProposedFlow(
                name=name,
                goal=goal,
                priority=min(5, max(1, int(priority))),
                steps=_strings(raw.get("steps", []), "steps"),
                expected=_strings(raw.get("expected", []), "expected"),
                touched_files=_strings(raw.get("touched_files", []), "touched_files"),
            )
        )
    flows.sort(key=lambda flow: flow.priority)
    return FlowProposal(flows=flows[: max(0, max_flows)])


def _string(value: Any, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ModelRefused(f"{name} must be a string")
    return value


def _strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ModelRefused(f"{name} must be a list of strings")
    return [item for item in value if item.strip()]


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ModelRefused(f"{name} must be a number")
    return float(value)


def _confidence(value: Any) -> float:
    number = _number(value, "confidence")
    if not 0.0 <= number <= 1.0:
        raise ModelRefused(f"confidence {number} is outside 0..1")
    return number


def _point(data: dict[str, Any], kx: str, ky: str, to_points: PointMap) -> tuple[float, float] | None:
    x, y = data.get(kx), data.get(ky)
    if x is None or y is None:
        return None
    return to_points(_number(x, kx), _number(y, ky))


def _bbox(value: Any, to_points: PointMap) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ModelRefused("bbox must be an object")
    x, y = _number(value.get("x"), "bbox.x"), _number(value.get("y"), "bbox.y")
    w, h = _number(value.get("width"), "bbox.width"), _number(value.get("height"), "bbox.height")
    left, top = to_points(x, y)
    right, bottom = to_points(x + w, y + h)
    return (left, top, round(right - left, 1), round(bottom - top, 1))

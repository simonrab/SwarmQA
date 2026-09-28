"""Resolve steps_gold for friction differentials."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

GoldSource = Literal["scripted", "provided", "shortest_success", "synthesized"]


@dataclass(frozen=True)
class GoldResolution:
    steps_gold: int
    source: GoldSource


def resolve_gold(
    *,
    gold_steps: int | None = None,
    scripted_steps: int | None = None,
    shortest_success: int | None = None,
    expected_controls: int = 0,
) -> GoldResolution:
    """Pick a gold path length.

    Preference order: explicit ``gold_steps``, scripted sibling step count,
    shortest recorded success, then a synthesized floor of
    ``max(1, expected_controls)``.
    """
    if gold_steps is not None and gold_steps > 0:
        return GoldResolution(steps_gold=int(gold_steps), source="provided")
    if scripted_steps is not None and scripted_steps > 0:
        return GoldResolution(steps_gold=int(scripted_steps), source="scripted")
    if shortest_success is not None and shortest_success > 0:
        return GoldResolution(steps_gold=int(shortest_success), source="shortest_success")
    return GoldResolution(
        steps_gold=max(1, int(expected_controls)),
        source="synthesized",
    )


def scripted_steps_from_shard(shard) -> int | None:
    """Return related scripted step count when the shard carries actions or a tag."""
    actions = getattr(shard, "actions", None) or []
    if actions:
        return len(actions)
    tags = getattr(shard, "tags", None) or []
    for tag in tags:
        text = str(tag).strip().lower()
        if text.startswith("gold_steps:"):
            raw = text.split(":", 1)[1].strip()
            try:
                value = int(raw)
            except ValueError:
                continue
            if value > 0:
                return value
        if text.startswith("gold:"):
            raw = text.split(":", 1)[1].strip()
            try:
                value = int(raw)
            except ValueError:
                continue
            if value > 0:
                return value
    return None

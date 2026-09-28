"""Deterministic friction score 0–100 from gold-relative session metrics."""

from __future__ import annotations


def clip(value: float, low: float, high: float) -> float:
    if value < low:
        return low
    if value > high:
        return high
    return value


def friction_score(
    *,
    step_ratio: float,
    backtrack_rate: float,
    recovery_loops: int | float,
    dead_end_count: int | float,
    klm_ratio: float,
    rage_events: int | float,
) -> int:
    """Return an integer score in ``[0, 100]``.

    Weights match docs/friction.md:

    ``35 * clip((step_ratio - 1) / 2, 0, 1)`` + backtrack / recovery /
    dead-end / KLM / rage terms.
    """
    raw = (
        35.0 * clip((step_ratio - 1.0) / 2.0, 0.0, 1.0)
        + 20.0 * clip(backtrack_rate / 0.25, 0.0, 1.0)
        + 15.0 * clip(float(recovery_loops) / 3.0, 0.0, 1.0)
        + 15.0 * clip(float(dead_end_count) / 2.0, 0.0, 1.0)
        + 10.0 * clip((klm_ratio - 1.0) / 2.0, 0.0, 1.0)
        + 5.0 * clip(float(rage_events) / 2.0, 0.0, 1.0)
    )
    return int(round(min(100.0, max(0.0, raw))))

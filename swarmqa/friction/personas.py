"""First-time and expert persona policy hints for exploratory friction.

Decision backends may consume these later; friction metering records the
persona name on the session today.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PersonaPolicy:
    """Hints for how a hunt should behave when a persona is selected."""

    name: str
    prefer_visible_labels: bool
    prefer_menus: bool
    allow_wrong_label_picks: int
    prefer_back_cancel_on_dead_end: bool
    gold_biased: bool


FIRST_TIME = PersonaPolicy(
    name="first_time",
    prefer_visible_labels=True,
    prefer_menus=False,
    allow_wrong_label_picks=1,
    prefer_back_cancel_on_dead_end=True,
    gold_biased=False,
)

EXPERT = PersonaPolicy(
    name="expert",
    prefer_visible_labels=False,
    prefer_menus=True,
    allow_wrong_label_picks=0,
    prefer_back_cancel_on_dead_end=False,
    gold_biased=True,
)

PERSONAS: dict[str, PersonaPolicy] = {
    FIRST_TIME.name: FIRST_TIME,
    EXPERT.name: EXPERT,
}


def persona_named(name: str) -> PersonaPolicy:
    """Return a known persona, or a label-first fallback with the given name."""
    key = (name or "").strip().lower()
    if key in PERSONAS:
        return PERSONAS[key]
    return PersonaPolicy(
        name=key or "expert",
        prefer_visible_labels=True,
        prefer_menus=False,
        allow_wrong_label_picks=0,
        prefer_back_cancel_on_dead_end=True,
        gold_biased=False,
    )

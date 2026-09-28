"""C13 — UX friction metering for exploratory hunts.

Advisory ``friction_path`` findings from gold-relative session metrics.
See docs/friction.md.
"""

from __future__ import annotations

from swarmqa.friction.emit import maybe_emit_friction
from swarmqa.friction.gold import GoldResolution, GoldSource, resolve_gold
from swarmqa.friction.personas import PERSONAS, PersonaPolicy, persona_named
from swarmqa.friction.score import clip, friction_score
from swarmqa.friction.session import FrictionSession, state_fingerprint

__all__ = [
    "FrictionSession",
    "GoldResolution",
    "GoldSource",
    "PERSONAS",
    "PersonaPolicy",
    "clip",
    "friction_score",
    "maybe_emit_friction",
    "persona_named",
    "resolve_gold",
    "state_fingerprint",
]

"""Decision evaluator protocol and observation/action types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from swarmqa.models import ElementQuery, UIElement

CandidateKind = Literal["click", "menu", "done", "missing"]
ActionKind = Literal["click", "menu", "done", "missing", "noop"]


@dataclass
class Candidate:
    kind: CandidateKind
    query: ElementQuery | None = None
    menu_path: list[str] | None = None
    label: str | None = None


@dataclass
class Observation:
    goal: str
    tokens: list[str]
    expected: list[str]
    elements: list[UIElement]
    maturity: str
    stall_count: int = 0
    steps_left: int = 0
    tried_clicks: frozenset[str] = field(default_factory=frozenset)
    tried_menus: frozenset[tuple[str, ...]] = field(default_factory=frozenset)
    tree_summary: list[dict] = field(default_factory=list)


@dataclass
class DecisionAction:
    kind: ActionKind
    query: ElementQuery | None = None
    menu_path: list[str] | None = None
    label: str | None = None
    rationale: str | None = None


@runtime_checkable
class DecisionEvaluator(Protocol):
    def decide(self, obs: Observation) -> DecisionAction:
        """Choose the next exploratory action for this observation."""
...
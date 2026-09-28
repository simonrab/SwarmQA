"""Pluggable exploratory decision backends."""

from __future__ import annotations

from swarmqa.decision.cascade import CascadeEvaluator
from swarmqa.decision.heuristic import HeuristicEvaluator
from swarmqa.decision.protocol import DecisionAction, DecisionEvaluator, Observation
from swarmqa.models import CampaignConfig

__all__ = [
    "CascadeEvaluator",
    "DecisionAction",
    "DecisionEvaluator",
    "HeuristicEvaluator",
    "Observation",
    "build_evaluator",
]


def build_evaluator(config: CampaignConfig) -> DecisionEvaluator:
    """Build the DecisionEvaluator for ``config.explorer.decision``."""
    decision = config.explorer.decision
    heuristic = HeuristicEvaluator()
    if decision.mode == "heuristic":
        return CascadeEvaluator(decision, fallback=heuristic)
    # system_one / computer_use / cascade: fail-open heuristic until PR2/PR3.
    return CascadeEvaluator(decision, fallback=heuristic)

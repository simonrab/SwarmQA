"""Pluggable exploratory decision backends."""

from __future__ import annotations

from swarmqa.decision.cascade import CascadeEvaluator
from swarmqa.decision.computer_use import ComputerUseEvaluator, map_point_to_element
from swarmqa.decision.heuristic import HeuristicEvaluator
from swarmqa.decision.protocol import DecisionAction, DecisionEvaluator, Observation
from swarmqa.models import CampaignConfig

__all__ = [
    "CascadeEvaluator",
    "ComputerUseEvaluator",
    "DecisionAction",
    "DecisionEvaluator",
    "HeuristicEvaluator",
    "Observation",
    "build_evaluator",
    "map_point_to_element",
]


def build_evaluator(config: CampaignConfig) -> DecisionEvaluator:
    """Build the DecisionEvaluator for ``config.explorer.decision``."""
    decision = config.explorer.decision
    # Default heuristic mode keeps classic button→menu order (exploratory parity).
    # Cascade / system_one may reorder by persona when Observation.persona is set.
    heuristic = HeuristicEvaluator(respect_persona=(decision.mode != "heuristic"))

    system_one = None
    if decision.mode in ("system_one", "cascade"):
        if decision.system_one.provider == "fake":
            from swarmqa.decision.providers.fake import FakeSystemOne

            system_one = FakeSystemOne(decision)
        else:
            from swarmqa.decision.system_one import SystemOneEvaluator

            system_one = SystemOneEvaluator(decision)

    computer_use = ComputerUseEvaluator(decision)
    return CascadeEvaluator(
        decision,
        fallback=heuristic,
        system_one=system_one,
        computer_use=computer_use,
    )

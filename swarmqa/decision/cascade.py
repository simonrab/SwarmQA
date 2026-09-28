"""Cascade DecisionEvaluator — heuristic today; model stages later."""

from __future__ import annotations

from swarmqa.decision.heuristic import HeuristicEvaluator
from swarmqa.decision.protocol import DecisionAction, DecisionEvaluator, Observation
from swarmqa.models import DecisionConfig


class CascadeEvaluator:
    """Fail-open cascade. PR1 always delegates to the heuristic evaluator.

    Later PRs add System One and computer-use stages when
    ``mode`` is ``cascade`` / ``system_one`` / ``computer_use``. Until then
    every mode falls back to heuristic so CI and FakeDriver stay identical.
    """

    def __init__(
        self,
        config: DecisionConfig | None = None,
        *,
        fallback: DecisionEvaluator | None = None,
    ):
        self.config = config or DecisionConfig()
        self._fallback = fallback or HeuristicEvaluator()

    def decide(self, obs: Observation) -> DecisionAction:
        # PR2/PR3: escalate on stall when mode requests model backends.
        # Fail-open: always have a heuristic answer.
        try:
            return self._fallback.decide(obs)
        except Exception:
            return HeuristicEvaluator().decide(obs)

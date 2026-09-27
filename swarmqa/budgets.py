"""Campaign wall-time and worker-minute budgets."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BudgetDecision:
    allowed: bool
    stop_reason: str | None = None


class CampaignClock:
    """Stop scheduling new shards once a campaign budget is exhausted.

    In-flight work is the orchestrator's overrun policy (`drain` or `cancel`).
    This clock only answers "may another shard start?".
    """

    def __init__(
        self,
        max_wall_time_s: float | None = None,
        max_worker_minutes: float | None = None,
    ):
        if max_wall_time_s is not None and max_wall_time_s <= 0:
            raise ValueError("max wall time must be > 0 when set")
        if max_worker_minutes is not None and max_worker_minutes <= 0:
            raise ValueError("max worker minutes must be > 0 when set")
        self.max_wall_time_s = max_wall_time_s
        self.max_worker_minutes = max_worker_minutes
        self.worker_minutes = 0.0
        self.stop_reason: str | None = None

    def can_schedule(self, elapsed_s: float) -> BudgetDecision:
        if self.max_wall_time_s is not None and elapsed_s >= self.max_wall_time_s:
            self.stop_reason = "budget"
            return BudgetDecision(False, stop_reason="budget")
        if (
            self.max_worker_minutes is not None
            and self.worker_minutes >= self.max_worker_minutes
        ):
            self.stop_reason = "budget"
            return BudgetDecision(False, stop_reason="budget")
        return BudgetDecision(True)

    def add_worker_minutes(self, minutes: float) -> None:
        if minutes < 0:
            raise ValueError("worker minutes must be >= 0")
        self.worker_minutes += minutes

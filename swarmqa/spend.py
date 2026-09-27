"""Campaign spend meter.

The orchestrator consults this before scheduling another shard. Adapters only
report a cost rate; they do not decide whether the campaign may continue.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SpendDecision:
    allowed: bool
    stop_reason: str | None = None
    note: str | None = None


class SpendMeter:
    """Track estimated spend against an optional currency cap.

    `cap is None` means the backend is unmetered (pure local, or a VM adapter
    that reported a zero rate and the user set no cap). A positive cap refuses
    a shard whose estimated cost would pass the ceiling. Comparison uses a
    tiny tolerance so decimal currency does not flap on floating point dust.
    """

    def __init__(self, cap: float | None, currency: str = "USD"):
        if cap is not None and cap <= 0:
            raise ValueError("max_spend must be > 0 when set")
        self.cap = cap
        self.currency = currency or "USD"
        self.spent = 0.0
        self.stop_reason: str | None = None

    def can_start(self, estimated_cost: float) -> SpendDecision:
        if estimated_cost < 0:
            raise ValueError("estimated cost must be >= 0")
        if self.cap is None:
            return SpendDecision(True, note="spend cap is not enforced")
        if self.spent + estimated_cost <= self.cap + 1e-9:
            return SpendDecision(True)
        self.stop_reason = "spend_cap"
        return SpendDecision(False, stop_reason="spend_cap")

    def add(self, amount: float) -> None:
        if amount < 0:
            raise ValueError("spend amount must be >= 0")
        self.spent += amount

    @property
    def remaining(self) -> float | None:
        if self.cap is None:
            return None
        return max(0.0, self.cap - self.spent)

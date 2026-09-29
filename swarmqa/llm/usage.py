"""Aggregate `Usage` records across calls and feed the campaign spend meter."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from swarmqa.llm.protocol import ModelBudgetExceeded, Usage
from swarmqa.spend import SpendMeter


@dataclass
class ModelTotals:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost: float = 0.0


@dataclass
class UsageMeter:
    """Thread-safe running totals. Each `add` also forwards cost to `spend` when set.

    `calls` counts provider calls started (including ones that failed), so
    call caps hold even when every call errors. Token and cost totals come
    only from responses the API billed.
    """

    spend: SpendMeter | None = None
    calls: int = 0
    cost: float = 0.0
    by_model: dict[str, ModelTotals] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def start_call(self, max_calls: int | None = None) -> None:
        """Count a call. With `max_calls`, check and count in one step so
        threads sharing the meter cannot pass the cap together."""
        with self._lock:
            if max_calls is not None and self.calls >= max_calls:
                raise ModelBudgetExceeded(f"call cap reached ({max_calls} calls)")
            self.calls += 1

    def add(self, usage: Usage) -> None:
        with self._lock:
            totals = self.by_model.setdefault(f"{usage.provider}/{usage.model}", ModelTotals())
            totals.calls += 1
            totals.input_tokens += usage.input_tokens
            totals.output_tokens += usage.output_tokens
            totals.cache_read_tokens += usage.cache_read_tokens
            totals.cost += usage.cost
            self.cost += usage.cost
            if self.spend is not None and usage.cost > 0:
                self.spend.add(usage.cost)

    def summary(self) -> dict:
        with self._lock:
            return {
                "calls": self.calls,
                "cost": round(self.cost, 6),
                "models": {
                    name: {
                        "calls": t.calls,
                        "input_tokens": t.input_tokens,
                        "output_tokens": t.output_tokens,
                        "cache_read_tokens": t.cache_read_tokens,
                        "cost": round(t.cost, 6),
                    }
                    for name, t in sorted(self.by_model.items())
                },
            }

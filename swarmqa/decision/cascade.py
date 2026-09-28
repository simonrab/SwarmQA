"""Cascade DecisionEvaluator — heuristic → System One → computer-use."""

from __future__ import annotations

from swarmqa.decision.cache import ObservationCache
from swarmqa.decision.computer_use import ComputerUseError, ComputerUseEvaluator
from swarmqa.decision.heuristic import HeuristicEvaluator
from swarmqa.decision.protocol import DecisionAction, DecisionEvaluator, Observation
from swarmqa.models import DecisionConfig


def _load_system_one():
    """Import System One when PR2 has landed; otherwise ``None``."""
    try:
        from swarmqa.decision.system_one import SystemOneError, SystemOneEvaluator
    except ImportError:
        return None, None
    return SystemOneEvaluator, SystemOneError


def _build_system_one(config: DecisionConfig) -> DecisionEvaluator | None:
    SystemOneEvaluator, _ = _load_system_one()
    if SystemOneEvaluator is None:
        return None
    if config.system_one.provider == "fake":
        from swarmqa.decision.providers.fake import FakeSystemOne

        return FakeSystemOne(config)
    return SystemOneEvaluator(config)


class CascadeEvaluator:
    """Fail-open cascade with additive model stages.

    Stage 1 — always: heuristic (or injected ``fallback``).

    Stage 2 — System One when ``mode`` is ``system_one`` / ``cascade``:
      - ``system_one``: try immediately (escalate threshold 0).
      - ``cascade``: only when ``obs.stall_count >= escalate_after``.
      Cached by tree fingerprint when ``cache_observations`` is true.
      Fail-open: ``system_one`` mode returns heuristic; ``cascade`` continues.

    Stage 3 — computer-use when ``mode`` is ``computer_use`` / ``cascade``
      and ``stall_count >= escalate_after``.

    Shared ``max_model_calls`` meters both model stages; computer-use also
    respects ``computer_use.max_calls``.
    """

    def __init__(
        self,
        config: DecisionConfig | None = None,
        *,
        fallback: DecisionEvaluator | None = None,
        system_one: DecisionEvaluator | None = None,
        computer_use: ComputerUseEvaluator | None = None,
    ):
        self.config = config or DecisionConfig()
        self._fallback = fallback or HeuristicEvaluator()
        self._model_calls = 0
        self._cache = (
            ObservationCache() if self.config.cache_observations else None
        )

        if system_one is not None:
            self._system_one = system_one
        elif self.config.mode in ("system_one", "cascade"):
            self._system_one = _build_system_one(self.config)
        else:
            self._system_one = None

        self._SystemOneError = _load_system_one()[1]
        self._computer_use = computer_use or ComputerUseEvaluator(self.config)

    @property
    def model_calls(self) -> int:
        return self._model_calls

    def decide(self, obs: Observation) -> DecisionAction:
        try:
            base = self._fallback.decide(obs)
        except Exception:
            base = HeuristicEvaluator().decide(obs)

        mode = self.config.mode
        if mode == "heuristic":
            return base

        # Stage 2 — System One.
        if mode in ("system_one", "cascade") and self._system_one is not None:
            threshold = 0 if mode == "system_one" else self.config.escalate_after
            if obs.stall_count >= threshold:
                action = self._try_system_one(obs)
                if action is not None:
                    return action
                if mode == "system_one":
                    return base

        elif mode == "system_one":
            return base

        # Stage 3 — computer-use.
        if mode in ("computer_use", "cascade"):
            if obs.stall_count < self.config.escalate_after:
                return base
            self._computer_use.set_model_calls_used(self._model_calls)
            if self._computer_use.remaining_calls() <= 0:
                return base
            try:
                action = self._computer_use.decide(obs)
                self._model_calls += 1
                return action
            except ComputerUseError:
                return base
            except Exception:
                return base

        return base

    def _try_system_one(self, obs: Observation) -> DecisionAction | None:
        cache_key: str | None = None
        if self._cache is not None:
            cache_key = ObservationCache.key_for(obs)
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        if self._model_calls >= self.config.max_model_calls:
            return None
        if self._system_one is None:
            return None

        self._model_calls += 1
        self._computer_use.set_model_calls_used(self._model_calls)
        try:
            action = self._system_one.decide(obs)
        except Exception:
            return None
        if action is None:
            return None
        if cache_key is not None and self._cache is not None:
            self._cache.put(cache_key, action)
        return action

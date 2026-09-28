"""Fake decision providers for CI — no live network or shell commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from swarmqa.decision.heuristic import collect_candidates
from swarmqa.decision.protocol import DecisionAction, Observation
from swarmqa.decision.system_one import (
    SystemOneError,
    candidate_to_action,
    resolve_choice,
)
from swarmqa.driver.query import walk
from swarmqa.models import DecisionConfig, SystemOneConfig


class FakeSystemOne:
    """Pick the first candidate, or a scripted ``choice_id``, for tests.

    Set ``raise_error`` to force fail-open behavior in the cascade.
    """

    def __init__(
        self,
        config: DecisionConfig | None = None,
        *,
        system_one: SystemOneConfig | None = None,
        choice_id: str | None = None,
        confidence: float | None = None,
        raise_error: BaseException | None = None,
    ):
        self.config = config or DecisionConfig()
        self.system_one = system_one or self.config.system_one
        self.choice_id = choice_id
        self.confidence = 1.0 if confidence is None else confidence
        self.raise_error = raise_error
        self.calls = 0
        self.last_obs: Observation | None = None
        self.last_candidates: list = []

    def decide(self, obs: Observation) -> DecisionAction:
        self.calls += 1
        self.last_obs = obs
        if self.raise_error is not None:
            raise self.raise_error
        candidates = collect_candidates(obs)
        self.last_candidates = list(candidates)
        if not candidates:
            raise SystemOneError("no candidates")
        choice_id = self.choice_id or candidates[0].choice_id
        chosen = resolve_choice(
            candidates,
            choice_id=choice_id,
            confidence=self.confidence,
            min_confidence=self.system_one.min_confidence,
        )
        return candidate_to_action(
            chosen,
            rationale=(
                f"fake_system_one choice_id={chosen.choice_id} "
                f"confidence={self.confidence}"
            ),
        )


@dataclass
class FakeComputerUseProvider:
    """Scripted computer-use responses for tests.

    Each ``run`` call returns the next dict in ``script`` (cycling if exhausted).
    When ``script`` is empty, returns a click at the center of the first element
    that has a frame, or ``{"action": "done"}`` if none exist.
    """

    script: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0
    last_screenshot: str | None = None
    last_hint: dict[str, Any] | None = None

    def run(
        self,
        *,
        screenshot_path: str | None,
        hint: dict[str, Any] | None,
        obs: Observation,
    ) -> dict[str, Any]:
        self.calls += 1
        self.last_screenshot = screenshot_path
        self.last_hint = hint
        if self.script:
            index = min(self.calls - 1, len(self.script) - 1)
            return dict(self.script[index])
        for element in walk(obs.elements):
            frame = element.frame
            if frame is None or len(frame) != 4:
                continue
            x, y, width, height = frame
            return {
                "action": "click",
                "x": x + width / 2.0,
                "y": y + height / 2.0,
            }
        return {"action": "done"}

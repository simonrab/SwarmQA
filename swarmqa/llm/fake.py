"""Deterministic ModelProvider for tests and dry runs. No network.

Each method returns the next scripted answer when one is queued, else a
default: `decide_step` taps the first enabled element with a label or
identifier that history has not tapped, then says `done`; `judge_screen`
finds nothing; `propose_flows` proposes nothing; `computer_use` taps the
centre of the first framed element. Every call is recorded in `calls`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swarmqa.driver.protocol import ScreenObservation
from swarmqa.driver.query import walk
from swarmqa.llm.protocol import (
    ChangedFile,
    FlowProposal,
    HistoryStep,
    ModelError,
    Rubric,
    ScreenJudgment,
    StepDecision,
    Usage,
)
from swarmqa.models import ElementQuery


def _usage() -> Usage:
    return Usage(provider="fake", model="fake")


@dataclass
class FakeModelProvider:
    name: str = "fake"
    steps: list[StepDecision] = field(default_factory=list)
    judgments: list[ScreenJudgment] = field(default_factory=list)
    proposals: list[FlowProposal] = field(default_factory=list)
    computer_actions: list[StepDecision] = field(default_factory=list)
    error: ModelError | None = None
    calls: list[str] = field(default_factory=list)

    def decide_step(
        self,
        obs: ScreenObservation,
        goal: str,
        history: list[HistoryStep],
        *,
        timeout_s: float = 30.0,
    ) -> StepDecision:
        self._call("decide_step")
        if self.steps:
            return self._stamp(self.steps.pop(0))
        tapped = {
            (step.action.target.identifier, step.action.target.label)
            for step in history
            if step.action.kind == "tap" and step.action.target is not None
        }
        for element in walk(obs.tree):
            if not element.enabled or not (element.label or element.identifier):
                continue
            if element.role in ("window", "text", "statictext", "image", "other"):
                continue
            key = (element.identifier, element.label or None)
            if key in tapped:
                continue
            query = ElementQuery(role=element.role, label=element.label or None, identifier=element.identifier)
            return StepDecision(kind="tap", target=query, rationale="fake: first untried control", usage=_usage())
        return StepDecision(kind="done", rationale="fake: nothing left to try", usage=_usage())

    def judge_screen(
        self,
        obs: ScreenObservation,
        rubric: Rubric,
        *,
        timeout_s: float = 60.0,
    ) -> ScreenJudgment:
        self._call("judge_screen")
        if obs.screenshot is None:
            raise ModelError("judge_screen needs a screenshot")
        if self.judgments:
            judgment = self.judgments.pop(0)
            judgment.usage = judgment.usage or _usage()
            return judgment
        return ScreenJudgment(summary="fine", usage=_usage())

    def propose_flows(
        self,
        diff: str,
        files: list[ChangedFile],
        *,
        max_flows: int = 10,
        timeout_s: float = 120.0,
    ) -> FlowProposal:
        self._call("propose_flows")
        if self.proposals:
            proposal = self.proposals.pop(0)
            proposal.flows = proposal.flows[:max_flows]
            proposal.usage = proposal.usage or _usage()
            return proposal
        return FlowProposal(usage=_usage())

    def computer_use(
        self,
        obs: ScreenObservation,
        instruction: str,
        *,
        timeout_s: float = 60.0,
    ) -> StepDecision:
        self._call("computer_use")
        if self.computer_actions:
            return self._stamp(self.computer_actions.pop(0))
        for element in walk(obs.tree):
            if element.frame is None:
                continue
            x, y, width, height = element.frame
            return StepDecision(kind="tap_point", point=(x + width / 2, y + height / 2), usage=_usage())
        return StepDecision(kind="give_up", rationale="fake: no framed element", usage=_usage())

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.error is not None:
            raise self.error

    @staticmethod
    def _stamp(decision: StepDecision) -> StepDecision:
        decision.usage = decision.usage or _usage()
        return decision

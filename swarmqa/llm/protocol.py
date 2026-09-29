"""ModelProvider contract.

A provider answers four questions, each with a typed result and a `Usage`
record for the spend meter:

- `decide_step`: the next action toward a goal on the current screen.
- `judge_screen`: what looks broken, wrong, or confusing on a screen.
- `propose_flows`: which flows a code change puts at risk.
- `computer_use`: a coordinate action from a screenshot alone.

Adapters (Anthropic, OpenAI, fake) implement this protocol and nothing else
in SwarmQA imports a model SDK. Adapters use structured (JSON-schema)
outputs, enforce `timeout_s`, and retry transient errors themselves. They
raise `ModelError` subclasses; callers treat any `ModelError` as fail-open
and fall back to heuristics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from swarmqa.driver.protocol import ScreenObservation
from swarmqa.models import ElementQuery, FindingCategory, Severity

StepKind = Literal["tap", "tap_point", "type", "swipe", "key", "back", "done", "give_up"]
RubricName = Literal["visual", "confusion"]


class ModelError(Exception):
    """The provider could not produce a usable answer."""


class ModelTimeout(ModelError):
    """The call ran past its timeout, after retries."""


class ModelRefused(ModelError):
    """The model declined, or its output did not match the schema."""


class ModelBudgetExceeded(ModelError):
    """The call would pass the provider's call or spend cap."""


@dataclass
class Usage:
    """What one call cost. `cost` is in `currency`; 0 for the fake provider."""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost: float = 0.0
    currency: str = "USD"
    latency_s: float = 0.0


@dataclass
class HistoryStep:
    """One earlier step in the same session, oldest first in `history`."""

    action: "StepDecision"
    screen: str = ""
    outcome: str = ""


@dataclass
class StepDecision:
    """The next action. Fields beyond `kind` are set as the kind needs.

    `tap` and `type` use `target`; `tap_point` uses `point`; `type` uses
    `text`; `swipe` uses `point` and `end`; `key` uses `keys`. `done` means
    the goal is met; `give_up` means the goal cannot be reached from here.
    """

    kind: StepKind
    target: ElementQuery | None = None
    point: tuple[float, float] | None = None
    end: tuple[float, float] | None = None
    text: str | None = None
    keys: list[str] = field(default_factory=list)
    rationale: str = ""
    confidence: float = 1.0
    usage: Usage | None = None


@dataclass
class Rubric:
    """What to look for when judging a screen. `instructions` override the default prompt."""

    name: RubricName
    instructions: str = ""
    min_confidence: float = 0.5


@dataclass
class JudgedIssue:
    category: FindingCategory
    title: str
    severity: Severity = "medium"
    confidence: float = 0.5
    rationale: str = ""
    element: ElementQuery | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass
class ScreenJudgment:
    """`issues` is empty when the screen looks fine."""

    issues: list[JudgedIssue] = field(default_factory=list)
    summary: str = ""
    usage: Usage | None = None


@dataclass
class ChangedFile:
    path: str
    content: str = ""


@dataclass
class ProposedFlow:
    """A flow worth testing, in the intent format `intent/ingest.py` reads.

    `priority` 1 is most important. `steps` are plain-language hints, not
    recorded actions.
    """

    name: str
    goal: str
    priority: int = 3
    steps: list[str] = field(default_factory=list)
    expected: list[str] = field(default_factory=list)
    touched_files: list[str] = field(default_factory=list)


@dataclass
class FlowProposal:
    flows: list[ProposedFlow] = field(default_factory=list)
    usage: Usage | None = None


@runtime_checkable
class ModelProvider(Protocol):
    name: str

    def decide_step(
        self,
        obs: ScreenObservation,
        goal: str,
        history: list[HistoryStep],
        *,
        timeout_s: float = 30.0,
    ) -> StepDecision: ...

    def judge_screen(
        self,
        obs: ScreenObservation,
        rubric: Rubric,
        *,
        timeout_s: float = 60.0,
    ) -> ScreenJudgment:
        """Judge `obs.screenshot` with `obs.tree` as context. Raise ModelError without a screenshot."""

    def propose_flows(
        self,
        diff: str,
        files: list[ChangedFile],
        *,
        max_flows: int = 10,
        timeout_s: float = 120.0,
    ) -> FlowProposal: ...

    def computer_use(
        self,
        obs: ScreenObservation,
        instruction: str,
        *,
        timeout_s: float = 60.0,
    ) -> StepDecision:
        """Return a coordinate action (`tap_point`, `swipe`, `type`, `key`, `done`, `give_up`)."""

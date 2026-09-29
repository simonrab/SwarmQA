"""Check contract between the agent loop and the checks.

The agent loop builds a `StepContext` after every action (and one with
`action=None` for the first screen) and runs each `Check` on it. A check
returns `CheckIssue`s and never raises for app problems; an internal error
is the loop's to log and skip. `CheckIssue.to_finding` is the one place an
issue becomes a `Finding`, so fingerprints stay stable across the loop, the
checks, and the report.

Checks that cost money (model judges) set `per_screen = True`; the loop runs
them once per new screen fingerprint instead of every step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from swarmqa.driver.protocol import AppDriverV2, ScreenObservation
from swarmqa.llm.protocol import StepDecision
from swarmqa.models import (
    ElementQuery,
    Evidence,
    Finding,
    FindingCategory,
    FindingKind,
    Severity,
)
from swarmqa.reporter.findings import fingerprint_for


@dataclass
class StepContext:
    """What happened in one step.

    `before` is None for the first screen. `action` is what the loop just did
    (None for the first screen). `since_ts` is when the action started, for
    `logs_since` and `crash_reports_since`. `screen_id` is the agent loop's
    fingerprint of `after`. `crashed` is set when the driver raised
    AppCrashedError during the action.
    """

    after: ScreenObservation
    driver: AppDriverV2
    before: ScreenObservation | None = None
    action: StepDecision | None = None
    since_ts: float = 0.0
    screen_id: str = ""
    step_index: int = 0
    goal: str = ""
    crashed: bool = False
    error: str = ""


@dataclass
class CheckIssue:
    kind: FindingKind
    category: FindingCategory
    title: str
    severity: Severity = "medium"
    confidence: float = 1.0
    advisory: bool = False
    details: str = ""
    element: ElementQuery | None = None
    bbox: tuple[float, float, float, float] | None = None
    screenshot: Path | None = None
    check: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def target(self) -> str:
        """The dedup target: element identifier, else label, else screen-level."""
        if self.element is not None:
            return self.element.identifier or self.element.label or self.element.role or ""
        return ""

    def to_finding(
        self,
        *,
        finding_id: str,
        worker_id: str,
        shard_id: str,
        backend: str,
        steps: list[str],
        media_root: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> Finding:
        """Build the Finding. Screenshot paths are made relative to `media_root` when given."""
        shots: list[str] = []
        if self.screenshot is not None:
            path = Path(self.screenshot)
            if media_root is not None:
                try:
                    path = path.resolve().relative_to(Path(media_root).resolve())
                except ValueError:
                    pass
            shots.append(str(path))
        details = self.details
        if self.check:
            details = f"{details}\n\ncheck: {self.check}".strip()
        return Finding(
            id=finding_id,
            title=self.title,
            severity=self.severity,
            kind=self.kind,
            steps=list(steps),
            fingerprint=fingerprint_for(self.kind, self.title, self.target()),
            worker_id=worker_id,
            backend=backend,
            shard_id=shard_id,
            screenshots=shots,
            environment=dict(environment or {}),
            details=details,
            category=self.category,
            confidence=self.confidence,
            advisory=self.advisory,
            evidence=Evidence(frames=list(shots)),
        )


@runtime_checkable
class Check(Protocol):
    name: str
    per_screen: bool

    def run(self, ctx: StepContext) -> list[CheckIssue]: ...

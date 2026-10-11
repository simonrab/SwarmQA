"""What the GitHub trigger works on: an event (a PR head or a branch push) and
the per-platform outcome of testing it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from swarmqa.models import Finding

EventKind = Literal["pr", "push"]


@dataclass
class Event:
    kind: EventKind
    sha: str
    pr: int | None = None
    ref: str = ""
    title: str = ""
    url: str = ""
    # The PR's base branch; empty for pushes and when unknown.
    base: str = ""

    def label(self) -> str:
        where = f"PR #{self.pr}" if self.pr is not None else (self.ref or "push")
        return f"{where} @ {self.sha[:12]}"


@dataclass
class PlatformOutcome:
    """One platform of one event. `error` is set when the build or the campaign
    could not run; a build failure also appears as a critical finding."""

    platform: str
    campaign_id: str | None = None
    report_dir: str | None = None
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None
    exit_code: int = 0


@dataclass
class EventResult:
    event: Event
    outcomes: list[PlatformOutcome] = field(default_factory=list)
    superseded: bool = False
    posted: list[str] = field(default_factory=list)

    @property
    def findings(self) -> list[Finding]:
        return [finding for outcome in self.outcomes for finding in outcome.findings]

    @property
    def exit_code(self) -> int:
        codes = [outcome.exit_code for outcome in self.outcomes]
        return max(codes, default=0)

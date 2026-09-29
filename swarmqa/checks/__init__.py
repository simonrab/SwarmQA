"""Checks that turn observations into issues: functional, layout, baseline, judge, friction.

`default_checks` builds the usual set from `ChecksSettings`. The friction
check is session-scoped (it needs `finish()` at session end), so callers
build it per session with `FrictionCheck.for_shard`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swarmqa.checks.baseline import BaselineCheck, BaselineSettings
from swarmqa.checks.friction import FrictionCheck
from swarmqa.checks.functional import FunctionalCheck, FunctionalSettings
from swarmqa.checks.judge import CommandJudgeCheck, JudgeCheck, JudgeSettings
from swarmqa.checks.layout import LayoutCheck, LayoutSettings
from swarmqa.checks.protocol import Check, CheckIssue, StepContext
from swarmqa.llm.protocol import ModelProvider
from swarmqa.models import VisualJudgmentConfig


@dataclass
class ChecksSettings:
    """Which checks run. `baseline` and `command_judge` are off unless set.

    The model judge runs when `judge` is true and a provider is given; the
    command judge is the fallback when there is no provider.
    """

    functional: FunctionalSettings | None = field(default_factory=FunctionalSettings)
    layout: LayoutSettings | None = field(default_factory=LayoutSettings)
    baseline: BaselineSettings | None = None
    judge: bool = True
    judge_settings: JudgeSettings = field(default_factory=JudgeSettings)
    command_judge: VisualJudgmentConfig | None = None


def default_checks(settings: ChecksSettings | None = None, provider: ModelProvider | None = None) -> list[Check]:
    settings = settings or ChecksSettings()
    checks: list[Check] = []
    if settings.functional is not None:
        checks.append(FunctionalCheck(settings.functional))
    if settings.layout is not None:
        checks.append(LayoutCheck(settings.layout))
    if settings.baseline is not None:
        checks.append(BaselineCheck(settings.baseline))
    if settings.judge and provider is not None:
        checks.append(JudgeCheck(provider, settings.judge_settings))
    elif settings.command_judge is not None and settings.command_judge.enabled:
        checks.append(CommandJudgeCheck(settings.command_judge))
    return checks


__all__ = [
    "BaselineCheck",
    "BaselineSettings",
    "Check",
    "CheckIssue",
    "ChecksSettings",
    "CommandJudgeCheck",
    "FrictionCheck",
    "FunctionalCheck",
    "FunctionalSettings",
    "JudgeCheck",
    "JudgeSettings",
    "LayoutCheck",
    "LayoutSettings",
    "StepContext",
    "default_checks",
]

"""Model judges: `JudgeCheck` on the ModelProvider, `CommandJudgeCheck` on the shell command.

Both run once per new screen (`per_screen = True`), need a screenshot, and
fail open: a model or command error yields no issues and is kept in
`last_error` for the caller to log. Everything a judge reports is advisory,
with the model's confidence (capped below 1, since no deterministic rule
confirmed it).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swarmqa.checks.protocol import CheckIssue, StepContext
from swarmqa.llm.protocol import ModelError, ModelProvider, Rubric, RubricName, Usage
from swarmqa.models import VisualJudgmentConfig
from swarmqa.visual.judge import JudgeError, judge_screenshot

_MAX_MODEL_CONFIDENCE = 0.99
_TITLE_LIMIT = 120


@dataclass
class JudgeSettings:
    """Which rubrics to ask for, and how.

    `min_confidence` overrides each rubric's own floor when set;
    `instructions` maps a rubric name to a prompt override.
    """

    rubrics: list[RubricName] = field(default_factory=lambda: ["visual", "confusion"])
    timeout_s: float = 60.0
    min_confidence: float | None = None
    instructions: dict[str, str] = field(default_factory=dict)


class JudgeCheck:
    name = "judge"
    per_screen = True

    def __init__(self, provider: ModelProvider, settings: JudgeSettings | None = None):
        self.provider = provider
        self.settings = settings or JudgeSettings()
        self.last_error: str | None = None
        self.usages: list[Usage] = []

    def rubrics(self) -> list[Rubric]:
        settings = self.settings
        out: list[Rubric] = []
        for name in settings.rubrics:
            rubric = Rubric(name=name, instructions=settings.instructions.get(name, ""))
            if settings.min_confidence is not None:
                rubric.min_confidence = settings.min_confidence
            out.append(rubric)
        return out

    def run(self, ctx: StepContext) -> list[CheckIssue]:
        self.last_error = None
        obs = ctx.after
        if obs.screenshot is None:
            return []
        issues: list[CheckIssue] = []
        errors: list[str] = []
        for rubric in self.rubrics():
            try:
                judgment = self.provider.judge_screen(obs, rubric, timeout_s=self.settings.timeout_s)
            except ModelError as exc:
                errors.append(f"{rubric.name}: {type(exc).__name__}: {exc}")
                continue
            if judgment.usage is not None:
                self.usages.append(judgment.usage)
            for judged in judgment.issues:
                if judged.confidence < rubric.min_confidence:
                    continue
                details = judged.rationale.strip()
                details = f"{details}\n\nrubric: {rubric.name}".strip()
                issues.append(
                    CheckIssue(
                        kind="visual_judgment",
                        category=judged.category,
                        title=_title(judged.title),
                        severity=judged.severity,
                        confidence=min(float(judged.confidence), _MAX_MODEL_CONFIDENCE),
                        advisory=True,
                        details=details,
                        element=judged.element,
                        bbox=judged.bbox,
                        screenshot=obs.screenshot,
                        check=f"{self.name}:{rubric.name}",
                        extra={"provider": getattr(self.provider, "name", ""), "rubric": rubric.name},
                    )
                )
        if errors:
            self.last_error = "judge failed open: " + "; ".join(errors)
        return issues


class CommandJudgeCheck:
    """The `visual/judge.py` shell-command backend as a check (the `command` fallback)."""

    name = "command_judge"
    per_screen = True

    def __init__(self, settings: VisualJudgmentConfig, *, confidence: float = 0.5):
        self.settings = settings
        self.confidence = confidence
        self.last_error: str | None = None

    def run(self, ctx: StepContext) -> list[CheckIssue]:
        self.last_error = None
        shot = ctx.after.screenshot
        if shot is None:
            return []
        try:
            text = judge_screenshot(shot, self.settings)
        except (JudgeError, OSError) as exc:
            self.last_error = f"visual judgment failed open: {exc}"
            return []
        if text == "fine":
            return []
        return [
            CheckIssue(
                kind="visual_judgment",
                category="visual",
                title=_title(text),
                severity="medium",
                confidence=self.confidence,
                advisory=True,
                details=text,
                screenshot=shot,
                check=self.name,
            )
        ]


def _title(text: str) -> str:
    one_line = " ".join(str(text).split()) or "Screen judged problematic"
    if len(one_line) <= _TITLE_LIMIT:
        return one_line
    return one_line[: _TITLE_LIMIT - 3].rstrip() + "..."

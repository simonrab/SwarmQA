"""WP-B3 — model judge on the ModelProvider, and the command-judge fallback."""

from __future__ import annotations

import sys

import pytest

from swarmqa.checks import (
    ChecksSettings,
    Check,
    CommandJudgeCheck,
    FunctionalCheck,
    JudgeCheck,
    JudgeSettings,
    LayoutCheck,
    default_checks,
)
from swarmqa.checks.protocol import StepContext
from swarmqa.driver.fake import FakeDriver
from swarmqa.driver.protocol import ScreenObservation
from swarmqa.llm.fake import FakeModelProvider
from swarmqa.llm.protocol import JudgedIssue, ModelTimeout, ScreenJudgment, Usage
from swarmqa.models import ElementQuery, VisualJudgmentConfig
from swarmqa.testing import make_app


@pytest.fixture
def driver(tmp_path):
    return FakeDriver(make_app(tmp_path), tmp_path / "work")


@pytest.fixture
def shot(tmp_path):
    from PIL import Image

    path = tmp_path / "screen.png"
    Image.new("RGB", (10, 10), "white").save(path)
    return path


def _ctx(driver, shot):
    return StepContext(after=ScreenObservation(tree=[], ts=1.0, screenshot=shot), driver=driver, screen_id="s1")


def test_judge_contract():
    check = JudgeCheck(FakeModelProvider())
    assert isinstance(check, Check) and check.per_screen


def test_judge_maps_issues_and_drops_low_confidence(driver, shot):
    provider = FakeModelProvider(
        judgments=[
            ScreenJudgment(
                issues=[
                    JudgedIssue(
                        category="visual",
                        title="Button label clipped",
                        severity="high",
                        confidence=0.9,
                        rationale="The Save label is cut off.",
                        element=ElementQuery(role="button", label="Save"),
                        bbox=(1, 2, 3, 4),
                    ),
                    JudgedIssue(category="visual", title="Maybe misaligned", confidence=0.3),
                ],
                usage=Usage(provider="fake", model="m", cost=0.01),
            ),
            ScreenJudgment(issues=[JudgedIssue(category="confusing", title="Two Save buttons", confidence=1.0)]),
        ]
    )
    check = JudgeCheck(provider)
    issues = check.run(_ctx(driver, shot))
    assert provider.calls == ["judge_screen", "judge_screen"]
    assert [i.title for i in issues] == ["Button label clipped", "Two Save buttons"]
    first, second = issues
    assert first.kind == "visual_judgment" and first.category == "visual"
    assert first.advisory and first.confidence == 0.9 and first.severity == "high"
    assert first.element.label == "Save" and first.bbox == (1, 2, 3, 4)
    assert "rubric: visual" in first.details and first.check == "judge:visual"
    assert second.category == "confusing" and second.confidence < 1.0
    assert check.last_error is None
    assert len(check.usages) == 2
    finding = first.to_finding(finding_id="f1", worker_id="w", shard_id="s", backend="local", steps=[])
    assert finding.advisory and finding.confidence == 0.9


def test_judge_min_confidence_override(driver, shot):
    provider = FakeModelProvider(
        judgments=[ScreenJudgment(issues=[JudgedIssue(category="visual", title="x", confidence=0.6)])]
    )
    check = JudgeCheck(provider, JudgeSettings(rubrics=["visual"], min_confidence=0.7))
    assert check.run(_ctx(driver, shot)) == []


def test_judge_model_error_fails_open(driver, shot):
    provider = FakeModelProvider(error=ModelTimeout("slow"))
    check = JudgeCheck(provider)
    assert check.run(_ctx(driver, shot)) == []
    assert "ModelTimeout" in check.last_error and "slow" in check.last_error


def test_judge_without_screenshot_makes_no_call(driver):
    provider = FakeModelProvider()
    assert JudgeCheck(provider).run(_ctx(driver, None)) == []
    assert provider.calls == []


def test_command_judge_fake_provider(driver, shot):
    fine = CommandJudgeCheck(VisualJudgmentConfig(enabled=True, provider="fake", judgment="fine"))
    assert fine.run(_ctx(driver, shot)) == []
    bad = CommandJudgeCheck(VisualJudgmentConfig(enabled=True, provider="fake", judgment="Header overlaps the list"))
    issues = bad.run(_ctx(driver, shot))
    assert len(issues) == 1
    assert issues[0].kind == "visual_judgment" and issues[0].advisory
    assert issues[0].title == "Header overlaps the list"


def test_command_judge_runs_command(driver, shot, tmp_path, monkeypatch):
    script = tmp_path / "judge.py"
    script.write_text('import json; print(json.dumps({"ok": False, "judgment": "Text is clipped"}))\n')
    monkeypatch.setenv("AQA_TEST_JUDGE", f"{sys.executable} {script}")
    check = CommandJudgeCheck(VisualJudgmentConfig(enabled=True, command_env="AQA_TEST_JUDGE"))
    issues = check.run(_ctx(driver, shot))
    assert [i.title for i in issues] == ["Text is clipped"]


def test_command_judge_fails_open(driver, shot, monkeypatch):
    monkeypatch.delenv("AQA_TEST_JUDGE_UNSET", raising=False)
    check = CommandJudgeCheck(VisualJudgmentConfig(enabled=True, command_env="AQA_TEST_JUDGE_UNSET"))
    assert check.run(_ctx(driver, shot)) == []
    assert "command is unset" in check.last_error


def test_default_checks_factory():
    names = [c.name for c in default_checks()]
    assert names == ["functional", "layout"]
    with_provider = default_checks(provider=FakeModelProvider())
    assert [c.name for c in with_provider] == ["functional", "layout", "judge"]
    fallback = default_checks(
        ChecksSettings(command_judge=VisualJudgmentConfig(enabled=True, provider="fake")),
    )
    assert [c.name for c in fallback][-1] == "command_judge"
    assert all(isinstance(c, Check) for c in with_provider + fallback)
    bare = default_checks(ChecksSettings(functional=None, layout=None, judge=False))
    assert bare == []
    assert isinstance(default_checks()[0], FunctionalCheck)
    assert isinstance(default_checks()[1], LayoutCheck)

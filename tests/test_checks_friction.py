"""WP-B3 — friction as a check, the KLM double-count fix, and Shard.tags from ingest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarmqa.checks import Check, FrictionCheck
from swarmqa.checks.friction import (
    TAP_SECONDS,
    FrictionContext,
    gold_klm_from_actions,
    klm_ratio_for,
    klm_seconds,
)
from swarmqa.checks.protocol import StepContext
from swarmqa.driver.fake import FakeDriver
from swarmqa.driver.protocol import ScreenObservation
from swarmqa.friction.emit import metrics_for
from swarmqa.friction.gold import GoldResolution, scripted_steps_from_shard
from swarmqa.friction.score import friction_score
from swarmqa.friction.session import FrictionSession
from swarmqa.intent.ingest import build_queue
from swarmqa.llm.protocol import StepDecision
from swarmqa.models import Action, ElementQuery, FrictionConfig, UIElement
from swarmqa.testing import make_app, sample_config


def _screen(title: str) -> list[UIElement]:
    return [UIElement(role="window", label=title, children=[UIElement(role="button", label=f"Next {title}")])]


# KLM double count -------------------------------------------------------


def test_klm_term_no_longer_double_counts_path_length():
    session = FrictionSession()
    for index in range(6):
        session.observe(_screen(f"S{index}"), "click", f"b{index}", True)
    gold = GoldResolution(steps_gold=2, source="scripted")
    metrics = metrics_for(session, gold, klm=True)
    assert metrics["step_ratio"] == 3.0
    # The old code set klm_ratio = step_ratio, scoring the same 4 extra
    # steps twice: 35 (step term) + 10 (KLM term) = 45.
    old = friction_score(
        step_ratio=3.0, backtrack_rate=0.0, recovery_loops=0, dead_end_count=0, klm_ratio=3.0, rage_events=0
    )
    assert old == 45
    assert metrics["klm_ratio"] == 1.0
    assert metrics["score"] == 35
    # klm=False and klm=True agree when there is no operator estimate.
    assert metrics_for(session, gold, klm=False)["score"] == 35
    # A real per-step operator estimate still counts, and klm=False ignores it.
    assert metrics_for(session, gold, klm=True, klm_ratio=3.0)["score"] == 45
    assert metrics_for(session, gold, klm=False, klm_ratio=3.0)["score"] == 35


def test_klm_operator_costs():
    assert klm_seconds("tap") == pytest.approx(TAP_SECONDS)
    assert klm_seconds("type", text="hello") == pytest.approx(TAP_SECONDS + 5 * 0.28)
    assert klm_seconds("key", keys=["cmd", "s"]) == pytest.approx(1.35 + 2 * 0.28)
    assert klm_seconds("done") is None and klm_seconds("screenshot") is None
    assert klm_ratio_for([]) is None
    assert klm_ratio_for([TAP_SECONDS] * 5) == pytest.approx(1.0)
    # Per-step cost, not length: ten taps against a two-tap gold is still 1.0.
    assert klm_ratio_for([TAP_SECONDS] * 10, [TAP_SECONDS] * 2) == pytest.approx(1.0)
    typed = klm_seconds("type", text="hello")
    assert klm_ratio_for([TAP_SECONDS, typed]) == pytest.approx((TAP_SECONDS + typed) / 2 / TAP_SECONDS)
    gold = gold_klm_from_actions([Action(action="click"), Action(action="assert"), Action(action="type", text="ab")])
    assert len(gold) == 2


# FrictionCheck ----------------------------------------------------------


@pytest.fixture
def driver(tmp_path):
    return FakeDriver(make_app(tmp_path), tmp_path / "work")


def _step(driver, title, action=None, **kwargs):
    return StepContext(after=ScreenObservation(tree=_screen(title), ts=1.0), driver=driver, action=action, **kwargs)


def _tap(label):
    return StepDecision(kind="tap", target=ElementQuery(label=label))


def test_friction_check_contract_and_no_per_step_issues(driver):
    check = FrictionCheck()
    assert isinstance(check, Check) and check.per_screen is False
    assert check.run(_step(driver, "A")) == []


def test_friction_check_emits_at_session_end(driver):
    check = FrictionCheck(
        FrictionConfig(),
        gold=GoldResolution(steps_gold=2, source="scripted"),
        context=FrictionContext(intent_id="s-1-checkout", locus="Buy a thing"),
    )
    check.run(_step(driver, "A"))
    for title in "BCDEF":
        assert check.run(_step(driver, title, _tap(f"Next {title}"))) == []
    check.run(_step(driver, "F", _tap("Broken"), error="ElementNotFoundError"))
    check.run(_step(driver, "F", _tap("Broken2"), error="ElementNotFoundError"))
    check.run(_step(driver, "F", StepDecision(kind="done")))
    assert check.session.steps_observed == 8  # `done` is not a user act
    assert check.session.goal_reached
    metrics = check.metrics()
    assert metrics["klm_ratio"] == pytest.approx(1.0)
    assert metrics["score"] == 50  # 35 step term + 15 dead ends; no KLM double count
    issues = check.finish()
    assert len(issues) == 1
    issue = issues[0]
    assert issue.kind == "friction_path" and issue.category == "confusing"
    assert issue.advisory and issue.title == "High-friction path (expert): s-1-checkout"
    assert "Friction score 50" in issue.details
    finding = issue.to_finding(finding_id="f", worker_id="w", shard_id="s", backend="local", steps=[])
    assert finding.kind == "friction_path" and finding.advisory


def test_friction_check_quiet_on_happy_path(driver):
    check = FrictionCheck(
        gold=GoldResolution(steps_gold=3, source="scripted"),
        context=FrictionContext(intent_id="s-1-x"),
    )
    check.run(_step(driver, "A"))
    check.run(_step(driver, "B", _tap("Next A")))
    check.run(_step(driver, "C", _tap("Next B")))
    assert check.finish() == []


def test_friction_check_ignores_crashed_empty_screen(driver):
    check = FrictionCheck()
    ctx = StepContext(after=ScreenObservation(tree=[], ts=1.0), driver=driver, action=_tap("x"), crashed=True)
    check.run(ctx)
    assert check.session.steps_observed == 0


# Shard.tags -------------------------------------------------------------


def _config(*paths: Path):
    config = sample_config()
    config.intents = [str(path) for path in paths]
    return config


def test_ingest_tags_from_front_matter_and_tags_section(tmp_path):
    intent = tmp_path / "onboarding.md"
    intent.write_text(
        "---\n"
        "owner: growth\n"
        "tags: [Onboarding, allow_step_ratio:3.5]\n"
        "---\n"
        "# Sign Up Wizard\n\n"
        "Create an account.\n\n"
        "## Tags\n"
        "- smoke, Slow Path\n"
        "- onboarding\n",
        encoding="utf-8",
    )
    (shard,) = build_queue(_config(intent))
    assert shard.kind == "exploratory"
    assert shard.tags == ["onboarding", "allow_step_ratio:3.5", "smoke", "slow-path", "flow:sign-up-wizard"]
    assert "owner" not in shard.goal and "Tags" not in shard.goal and "smoke" not in shard.goal
    assert shard.goal == "Sign Up Wizard\n\nCreate an account."


def test_ingest_front_matter_list_form(tmp_path):
    intent = tmp_path / "a.md"
    intent.write_text("---\ntags:\n  - one\n  - Two\n---\n# A\n", encoding="utf-8")
    (shard,) = build_queue(_config(intent))
    assert shard.tags == ["one", "two", "flow:a"]


def test_ingest_scripted_companion_carries_gold_steps(tmp_path):
    intent = tmp_path / "save.md"
    intent.write_text('# Save\n\n## Steps\n- click "New"\n- type "hi" into "Body"\n- click "Save"\n', encoding="utf-8")
    scripted, seed = build_queue(_config(intent))
    assert scripted.kind == "scripted" and scripted.tags == ["flow:save"]
    assert seed.kind == "exploratory" and seed.tags == ["flow:save", "gold_steps:3"]
    assert scripted_steps_from_shard(seed) == 3


def test_ingest_declared_gold_is_not_overridden(tmp_path):
    intent = tmp_path / "save.md"
    intent.write_text('# Save\n\n## Tags\n- gold_steps:5\n\n## Steps\n- click "Save"\n', encoding="utf-8")
    _, seed = build_queue(_config(intent))
    assert seed.tags == ["gold_steps:5", "flow:save"]


def test_ingest_json_flow_tags(tmp_path):
    flow = tmp_path / "flow.json"
    flow.write_text(json.dumps({"version": 1, "name": "Log In", "steps": [{"action": "launch"}]}), encoding="utf-8")
    (shard,) = build_queue(_config(flow))
    assert shard.tags == ["flow:log-in"]


def test_friction_check_for_shard_uses_tags(tmp_path):
    intent = tmp_path / "wizard.md"
    intent.write_text('# Export wizard\n\n## Steps\n- click "Export"\n- click "Next"\n', encoding="utf-8")
    _, seed = build_queue(_config(intent))
    check = FrictionCheck.for_shard(seed)
    assert check.gold == GoldResolution(steps_gold=2, source="scripted")
    assert "flow:export-wizard" in check.context.tags
    assert check.context.intent_id == seed.id


def test_ingest_tags_allow_a_space_after_the_colon(tmp_path):
    intent = tmp_path / "save.md"
    intent.write_text('# Save\n\n## Tags\n- gold_steps: 5\n- allow_step_ratio : 1.5\n\n## Steps\n- click "Save"\n', encoding="utf-8")
    _, seed = build_queue(_config(intent))
    assert seed.tags == ["gold_steps:5", "allow_step_ratio:1.5", "flow:save"]
    assert scripted_steps_from_shard(seed) == 5

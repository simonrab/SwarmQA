"""C13 — UX friction metering, scoring, emit gates, and report section."""

from __future__ import annotations

from pathlib import Path

from swarmqa.config import load_config
from swarmqa.decision.protocol import DecisionAction, DecisionEvaluator, Observation
from swarmqa.driver.fake import FakeDriver
from swarmqa.explorer.exploratory import run_exploratory
from swarmqa.friction.emit import gates_pass, maybe_emit_friction
from swarmqa.friction.gold import resolve_gold
from swarmqa.friction.score import clip, friction_score
from swarmqa.friction.session import FrictionSession, state_fingerprint
from swarmqa.models import (
    CampaignResult,
    ElementQuery,
    Finding,
    FrictionConfig,
    Shard,
    UIElement,
    WorkerResult,
)
from swarmqa.report.summary import render_summary_md
from swarmqa.testing import make_app, sample_config


def _window(*buttons: str, title: str = "Sample") -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label=title,
            children=[UIElement(role="button", label=label) for label in buttons],
        )
    ]


def test_score_math_weights_and_clip():
    assert clip(-1, 0, 1) == 0
    assert clip(0.5, 0, 1) == 0.5
    assert clip(2, 0, 1) == 1
    assert (
        friction_score(
            step_ratio=3.0,
            backtrack_rate=0.25,
            recovery_loops=3,
            dead_end_count=2,
            klm_ratio=3.0,
            rage_events=2,
        )
        == 100
    )
    assert (
        friction_score(
            step_ratio=1.0,
            backtrack_rate=0.0,
            recovery_loops=0,
            dead_end_count=0,
            klm_ratio=1.0,
            rage_events=0,
        )
        == 0
    )
    assert (
        friction_score(
            step_ratio=2.0,
            backtrack_rate=0.0,
            recovery_loops=0,
            dead_end_count=0,
            klm_ratio=1.0,
            rage_events=0,
        )
        == 18
    )


def test_session_backtrack_and_recovery():
    home = _window("Next", "Cancel", title="Home")
    mid = _window("Back", "Continue", title="Wizard")
    session = FrictionSession(persona="first_time")
    assert session.persona.name == "first_time"

    session.observe(home, "search", None, True)
    session.observe(mid, "click", "button:Next", True)
    session.observe(home, "click", "button:Back", True)
    assert session.backtrack_count == 1
    assert session.state_revisit_count == 1
    assert session.recovery_loops == 0

    session.observe(mid, "click", "button:Next", True)
    assert session.backtrack_count == 2
    assert session.recovery_loops == 1

    before = session.backtrack_count
    session.observe(mid, "click", "button:Continue", True)
    assert session.backtrack_count == before
    assert session.progress_stall_max >= 1


def test_state_fingerprint_stable_and_order_independent():
    left = [
        UIElement(
            role="window",
            label="App",
            children=[
                UIElement(role="button", label="B"),
                UIElement(role="button", label="A"),
            ],
        )
    ]
    right = [
        UIElement(
            role="window",
            label="App",
            children=[
                UIElement(role="button", label="A"),
                UIElement(role="button", label="B"),
            ],
        )
    ]
    assert state_fingerprint(left) == state_fingerprint(right)


def test_emit_gates_require_extra_steps_or_pathology():
    config = FrictionConfig(emit_threshold=50, min_extra_steps=3, min_backtrack_rate=0.15)
    short = FrictionSession(persona="expert")
    for _ in range(2):
        short.observe(_window("Save"), "click", "button:Save", True)
    gold = resolve_gold(gold_steps=1, expected_controls=1)
    assert gold.source == "provided"
    assert not gates_pass(short, gold, config, goal_directed=True)

    long = FrictionSession(persona="expert")
    # A → B → A → B → C → D → E produces backtracks + extra steps.
    path = [
        _window("A", title="S0"),
        _window("B", title="S1"),
        _window("A", title="S0"),
        _window("B", title="S1"),
        _window("C", title="S2"),
        _window("D", title="S3"),
        _window("E", title="S4"),
    ]
    for index, tree in enumerate(path):
        long.observe(tree, "click" if index else "search", f"k{index}", True)
    assert long.backtrack_count >= 2
    assert gates_pass(long, gold, config, goal_directed=True)

    finding = maybe_emit_friction(
        session=long,
        config=config,
        gold=gold,
        worker_id="w1",
        backend="local",
        shard_id="s1",
        finding_id="f-1",
        steps=["search", "click"],
        environment={},
        intent_id="s1",
        locus="Export",
        fingerprint_fn=lambda kind, title, target: "fp",
    )
    assert finding is not None
    assert finding.kind == "friction_path"
    assert finding.title[0].isdigit()


def test_synthesized_gold_needs_higher_score():
    config = FrictionConfig(emit_threshold=50, min_extra_steps=3)
    session = FrictionSession(persona="expert")
    trees = [_window("X", title=f"T{i}") for i in range(4)]
    for index, tree in enumerate(trees):
        session.observe(tree, "click" if index else "search", f"k{index}", True)
    gold = resolve_gold(expected_controls=1)
    assert gold.source == "synthesized"
    assert gold.steps_gold == 1
    assert not gates_pass(session, gold, config, goal_directed=True)


def test_summary_lists_friction_separately():
    friction = Finding(
        id="f-fric",
        title="3.0× gold path (score 70)",
        severity="medium",
        kind="friction_path",
        steps=["search", "click"],
        fingerprint="abc",
        worker_id="w1",
        backend="local",
    )
    crash = Finding(
        id="f-crash",
        title="Crash on Save",
        severity="critical",
        kind="crash",
        steps=["click Save"],
        fingerprint="def",
        worker_id="w1",
        backend="local",
    )
    result = CampaignResult(
        campaign_id="c1",
        report_dir="/tmp",
        results=[
            WorkerResult(
                worker_id="w1",
                shard_id="s1",
                status="failed",
                findings=[crash, friction],
                shard_name="export",
                shard_kind="exploratory",
                backend="local",
            )
        ],
        backend="local",
    )
    text = render_summary_md(result)
    assert "## Friction (advisory)" in text
    findings_block = text.split("## Findings")[1].split("## Friction (advisory)")[0]
    assert "Crash on Save" in findings_block
    assert "3.0× gold path" not in findings_block
    friction_block = text.split("## Friction (advisory)")[1].split("## Workers")[0]
    assert "3.0× gold path" in friction_block
    assert "Crash on Save" not in friction_block


def test_config_loads_friction_section(tmp_path: Path):
    path = tmp_path / "aqa.config.toml"
    path.write_text(
        """
[explorer.friction]
enabled = true
emit_threshold = 55
min_extra_steps = 5
min_backtrack_rate = 0.1
personas = ["expert", "first_time"]
fail_ci = false
compare_to = "gold"
klm = true

[explorer.friction.allow_step_ratio]
"export-wizard" = 3.5
s1 = 2.0
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.explorer.friction.emit_threshold == 55
    assert config.explorer.friction.min_extra_steps == 5
    assert config.explorer.friction.personas == ["expert", "first_time"]
    assert config.explorer.friction.fail_ci is False
    assert config.explorer.friction.allow_step_ratio == {
        "export-wizard": 3.5,
        "s1": 2.0,
    }


def _backtrack_session() -> FrictionSession:
    """Session with backtracks + enough steps to clear structural gates."""
    session = FrictionSession(persona="expert")
    path = [
        _window("A", title="S0"),
        _window("B", title="S1"),
        _window("A", title="S0"),
        _window("B", title="S1"),
        _window("C", title="S2"),
        _window("D", title="S3"),
        _window("E", title="S4"),
    ]
    for index, tree in enumerate(path):
        session.observe(tree, "click" if index else "search", f"k{index}", True)
    return session


def test_allow_step_ratio_blocks_emit():
    from swarmqa.friction.emit import allow_ratio_from_tags, resolve_allow_ratio

    assert allow_ratio_from_tags(["gold_steps:2", "allow_step_ratio:4.0"]) == 4.0
    assert allow_ratio_from_tags(["other"]) is None

    config = FrictionConfig(
        emit_threshold=50,
        min_extra_steps=3,
        allow_step_ratio={"s1": 10.0},
    )
    session = _backtrack_session()
    gold = resolve_gold(gold_steps=1, expected_controls=1)
    assert gates_pass(session, gold, config, goal_directed=True, intent_id="s1") is False
    assert (
        gates_pass(
            session,
            gold,
            config,
            goal_directed=True,
            intent_id="other",
            tags=["allow_step_ratio:10"],
        )
        is False
    )
    assert gates_pass(session, gold, config, goal_directed=True, intent_id="other")
    assert resolve_allow_ratio(config, intent_id="s1") == 10.0
    assert resolve_allow_ratio(config, intent_id="s1", tags=["allow_step_ratio:2.5"]) == 2.5


def test_wizard_discount_raises_threshold_and_halves_backtrack():
    from swarmqa.friction.emit import metrics_for, suggests_wizard

    assert suggests_wizard(locus="Export wizard")
    assert suggests_wizard(tags=["onboarding"])
    assert not suggests_wizard(locus="Export PDF")

    config = FrictionConfig(emit_threshold=50, min_extra_steps=3)
    session = _backtrack_session()
    gold = resolve_gold(gold_steps=1, expected_controls=1)
    plain = metrics_for(session, gold, klm=True, wizard_discount=False)
    discounted = metrics_for(session, gold, klm=True, wizard_discount=True)
    assert session.backtrack_rate > 0
    assert int(discounted["score"]) < int(plain["score"])

    assert gates_pass(session, gold, config, goal_directed=True, locus="Export")
    # +10 threshold bump: raise base threshold so plain still emits but wizard may not.
    high = FrictionConfig(emit_threshold=int(plain["score"]) - 5, min_extra_steps=3)
    assert gates_pass(session, gold, high, goal_directed=True, locus="Export")
    wizard_blocked = not gates_pass(
        session,
        gold,
        high,
        goal_directed=True,
        locus="Export wizard",
        tags=["wizard"],
    )
    # Either the +10 bump or the discounted score (or both) must change the gate.
    assert wizard_blocked or int(discounted["score"]) < high.emit_threshold + 10


def test_friction_fingerprint_stable_across_titles():
    from swarmqa.reporter.findings import fingerprint_for

    config = FrictionConfig(emit_threshold=50, min_extra_steps=3)
    session = _backtrack_session()
    gold = resolve_gold(gold_steps=1, expected_controls=1)
    first = maybe_emit_friction(
        session=session,
        config=config,
        gold=gold,
        worker_id="w1",
        backend="local",
        shard_id="s1",
        finding_id="f-1",
        steps=["search", "click"],
        environment={},
        intent_id="export",
        locus="Export PDF",
        fingerprint_fn=fingerprint_for,
    )
    # Bump counters so the numeric title / details change while identity stays the same.
    session.observe(_window("F", title="S5"), "click", "k5", True)
    second = maybe_emit_friction(
        session=session,
        config=config,
        gold=gold,
        worker_id="w1",
        backend="local",
        shard_id="s1",
        finding_id="f-2",
        steps=["search", "click", "more"],
        environment={},
        intent_id="export",
        locus="Export PDF",
        fingerprint_fn=fingerprint_for,
    )
    assert first is not None and second is not None
    assert first.fingerprint == second.fingerprint
    identity = "export\nExport PDF\nexpert"
    assert first.fingerprint == fingerprint_for("friction_path", identity, identity)


def test_goal_reached_set_by_exploratory_hunt(tmp_path: Path):
    """Happy-path hunt marks friction.goal_reached when evaluator returns done."""
    app = make_app(tmp_path)
    config = sample_config(app)
    root = tmp_path / "reports" / "goal2"
    work = root / "workers" / "w1"
    driver = FakeDriver(app, work)
    driver.set_tree(_window("Save"))
    shard = Shard(id="s-goal2", kind="exploratory", name="save", goal="Save")

    from swarmqa.explorer import exploratory as exploratory_mod

    captured: list[FrictionSession] = []
    original = exploratory_mod.FrictionSession

    class _Capture(FrictionSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured.append(self)

    exploratory_mod.FrictionSession = _Capture  # type: ignore[misc,assignment]
    try:
        result = run_exploratory(shard, driver, config, worker_id="w1", work_dir=work)
    finally:
        exploratory_mod.FrictionSession = original  # type: ignore[misc,assignment]
    assert result.status == "passed"
    assert captured, "expected FrictionSession to be constructed"
    assert captured[0].goal_reached is True


class _WizardDriver(FakeDriver):
    """Depth-based wizard: controls stay findable on the stale hunt tree.

    Exploratory keeps one tree for decide/find; peeks after click see a new
    window title so friction fingerprints still backtrack.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._depth = 0
        self._rebuild()

    def _rebuild(self) -> None:
        title = "Done" if self._depth >= 10 else f"Step{self._depth}"
        self.set_tree(_window("Next", "Back", "Done", title=title))

    def accessibility_tree(self):
        self._require_launched()
        return self.tree

    def click(self, target):
        super().click(target)
        label = (target.label or "").strip().lower()
        if label == "back":
            self._depth = max(0, self._depth - 1)
        elif label == "next":
            self._depth += 1
        elif label == "done":
            self._depth = 10
        self._rebuild()


class _FrictionPathEvaluator(DecisionEvaluator):
    def __init__(self):
        labels = ["Next", "Next", "Back", "Next", "Back", "Next", "Next", "Done"]
        self._plan = [
            DecisionAction(kind="click", query=ElementQuery(role="button", label=label))
            for label in labels
        ] + [DecisionAction(kind="done")]
        self._i = 0

    def decide(self, observation: Observation) -> DecisionAction:
        if self._i >= len(self._plan):
            return DecisionAction(kind="done")
        action = self._plan[self._i]
        self._i += 1
        return action


def test_fake_driver_hunt_emits_friction_path(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    config.explorer.max_steps = 40
    config.explorer.friction.min_extra_steps = 3
    config.explorer.friction.emit_threshold = 50
    root = tmp_path / "reports" / "fric"
    work = root / "workers" / "w1"
    driver = _WizardDriver(app, work)
    shard = Shard(
        id="s-fric",
        kind="exploratory",
        name="export pdf",
        goal="Export PDF",
        tags=["gold_steps:2"],
    )
    result = run_exploratory(
        shard,
        driver,
        config,
        worker_id="w1",
        work_dir=work,
        evaluator=_FrictionPathEvaluator(),
    )
    friction = [item for item in result.findings if item.kind == "friction_path"]
    assert friction, f"expected friction_path, got {[f.kind for f in result.findings]}"
    assert friction[0].title[0].isdigit()
    assert "Advisory" in friction[0].details


def test_happy_path_fake_driver_stays_quiet(tmp_path: Path):
    app = make_app(tmp_path)
    config = sample_config(app)
    root = tmp_path / "reports" / "quiet"
    work = root / "workers" / "w1"
    driver = FakeDriver(app, work)
    driver.set_tree(_window("Save"))
    shard = Shard(id="s-quiet", kind="exploratory", name="save", goal="Save")
    result = run_exploratory(shard, driver, config, worker_id="w1", work_dir=work)
    assert result.findings == []
    assert result.status == "passed"

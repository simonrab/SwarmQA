"""Decision backends — heuristic evaluator and injectable fakes."""

from __future__ import annotations

from pathlib import Path

from swarmqa.decision import build_evaluator
from swarmqa.decision.heuristic import HeuristicEvaluator, click_key, parse_goal
from swarmqa.decision.protocol import DecisionAction, Observation
from swarmqa.driver.fake import FakeDriver
from swarmqa.explorer.exploratory import run_exploratory
from swarmqa.models import (
    CampaignConfig,
    DecisionConfig,
    ElementQuery,
    Shard,
    UIElement,
)
from swarmqa.testing import make_app, sample_config


def _obs(
    *,
    goal: str = "Save",
    tree: list[UIElement] | None = None,
    maturity: str = "shipped",
    tried_clicks: frozenset[str] | None = None,
    tried_menus: frozenset[tuple[str, ...]] | None = None,
) -> Observation:
    tokens, expected = parse_goal(goal)
    return Observation(
        goal=goal,
        tokens=tokens,
        expected=expected,
        elements=tree
        or [
            UIElement(
                role="window",
                label="Sample",
                children=[UIElement(role="button", label="Save")],
            )
        ],
        maturity=maturity,
        tried_clicks=tried_clicks or frozenset(),
        tried_menus=tried_menus or frozenset(),
    )


def test_heuristic_prefers_matching_enabled_button():
    tree = [
        UIElement(
            role="window",
            label="Sample",
            children=[
                UIElement(role="button", label="Cancel"),
                UIElement(role="button", label="Save"),
                UIElement(role="button", label="Quiet", enabled=False),
            ],
        )
    ]
    action = HeuristicEvaluator().decide(_obs(goal="Save", tree=tree))
    assert action.kind == "click"
    assert action.query is not None
    assert action.query.label == "Save"


def test_heuristic_skips_tried_buttons_then_menus():
    tree = [
        UIElement(
            role="window",
            label="Sample",
            children=[
                UIElement(role="button", label="Save"),
                UIElement(
                    role="menubar",
                    label="Menu",
                    children=[
                        UIElement(
                            role="menu",
                            label="File",
                            children=[UIElement(role="menuitem", label="Export")],
                        )
                    ],
                ),
            ],
        )
    ]
    first = HeuristicEvaluator().decide(_obs(goal="Save Export", tree=tree))
    assert first.kind == "click"
    assert first.query is not None
    assert first.query.label == "Save"
    tried = frozenset({click_key(UIElement(role="button", label="Save"))})
    second = HeuristicEvaluator().decide(
        _obs(goal="Save Export", tree=tree, tried_clicks=tried)
    )
    # Export is menu-only, so after buttons the heuristic opens the menu path.
    assert second.kind == "menu"
    assert second.menu_path == ["File", "Export"]

    menu_only = HeuristicEvaluator().decide(_obs(goal="Export", tree=tree))
    assert menu_only.kind == "menu"
    assert menu_only.menu_path == ["File", "Export"]


def test_heuristic_prototype_missing_after_menus():
    tree = [UIElement(role="window", label="Sample", children=[])]
    action = HeuristicEvaluator().decide(
        _obs(goal="Open Settings", tree=tree, maturity="prototype")
    )
    # First: synthetic one-item menu for absent expected name.
    assert action.kind == "menu"
    assert action.menu_path == ["Settings"]
    tried = frozenset({("Settings",)})
    action = HeuristicEvaluator().decide(
        _obs(
            goal="Open Settings",
            tree=tree,
            maturity="prototype",
            tried_menus=tried,
        )
    )
    assert action.kind == "missing"
    assert action.label == "Settings"


def test_heuristic_shipped_done_when_absent():
    tree = [UIElement(role="window", label="Sample", children=[])]
    action = HeuristicEvaluator().decide(
        _obs(
            goal="Open Settings",
            tree=tree,
            maturity="shipped",
            tried_menus=frozenset({("Settings",)}),
        )
    )
    assert action.kind == "done"


def test_build_evaluator_defaults_to_heuristic_cascade():
    config = CampaignConfig()
    assert config.explorer.decision.mode == "heuristic"
    evaluator = build_evaluator(config)
    action = evaluator.decide(_obs())
    assert action.kind == "click"


def test_injectable_fake_evaluator(tmp_path: Path):
    class ScriptedEvaluator:
        def __init__(self) -> None:
            self.calls = 0

        def decide(self, obs: Observation) -> DecisionAction:
            self.calls += 1
            if self.calls == 1:
                return DecisionAction(
                    kind="click",
                    query=ElementQuery(role="button", label="Save"),
                    label="Save",
                )
            return DecisionAction(kind="done")

    app = make_app(tmp_path)
    config = sample_config(app)
    work = tmp_path / "reports" / "inj" / "workers" / "w1"
    driver = FakeDriver(app, work)
    driver.set_tree(
        [
            UIElement(
                role="window",
                label="Sample",
                children=[
                    UIElement(role="button", label="Save"),
                    UIElement(role="button", label="Cancel"),
                ],
            )
        ]
    )
    fake = ScriptedEvaluator()
    shard = Shard(id="s-1", kind="exploratory", name="goal", goal="Save Cancel")
    result = run_exploratory(
        shard, driver, config, worker_id="w1", work_dir=work, evaluator=fake
    )
    assert result.status == "passed"
    assert fake.calls >= 2
    assert "click:Save" in driver.actions_log
    assert "click:Cancel" not in driver.actions_log


def test_decision_config_defaults():
    decision = DecisionConfig()
    assert decision.mode == "heuristic"
    assert decision.escalate_after == 3
    assert decision.max_model_calls == 8
    assert decision.model_timeout_s == 30.0
    assert decision.cache_observations is True
    assert decision.system_one.provider == "http"
    assert decision.system_one.min_confidence == 0.55
    assert decision.computer_use.provider == "command"
    assert decision.computer_use.max_calls == 3

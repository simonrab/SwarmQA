"""Decision backends — heuristic evaluator and injectable fakes."""

from __future__ import annotations

from pathlib import Path

from swarmqa.decision import build_evaluator
from swarmqa.decision.cascade import CascadeEvaluator
from swarmqa.decision.heuristic import HeuristicEvaluator, click_key, parse_goal
from swarmqa.decision.protocol import DecisionAction, Observation
from swarmqa.decision.providers.fake import FakeSystemOne
from swarmqa.driver.fake import FakeDriver
from swarmqa.explorer.exploratory import run_exploratory
from swarmqa.models import (
    CampaignConfig,
    DecisionConfig,
    ElementQuery,
    ExplorerConfig,
    Shard,
    SystemOneConfig,
    UIElement,
)
from swarmqa.testing import make_app, sample_config


def _obs(
    *,
    goal: str = "Save",
    tree: list[UIElement] | None = None,
    maturity: str = "shipped",
    stall_count: int = 0,
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
        stall_count=stall_count,
        tried_clicks=tried_clicks or frozenset(),
        tried_menus=tried_menus or frozenset(),
    )


def _two_button_tree() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Sample",
            children=[
                UIElement(role="button", label="Save"),
                UIElement(role="button", label="Help"),
            ],
        )
    ]


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


def _framed_tree() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Sample",
            frame=(0.0, 0.0, 200.0, 100.0),
            children=[
                UIElement(
                    role="button",
                    label="Cancel",
                    frame=(10.0, 40.0, 40.0, 20.0),
                ),
                UIElement(
                    role="button",
                    label="Save",
                    frame=(100.0, 40.0, 40.0, 20.0),
                ),
            ],
        )
    ]


def test_map_point_to_nearest_element_by_frame_center():
    from swarmqa.decision.computer_use import map_point_to_element

    tree = _framed_tree()
    # Near Save center (120, 50)
    hit = map_point_to_element(tree, 118.0, 52.0)
    assert hit is not None
    assert hit.label == "Save"
    # Near Cancel center (30, 50)
    hit = map_point_to_element(tree, 28.0, 49.0)
    assert hit is not None
    assert hit.label == "Cancel"
    assert map_point_to_element([UIElement(role="button", label="Bare")], 0, 0) is None


def test_fake_computer_use_click_by_coords():
    from swarmqa.decision.cascade import CascadeEvaluator
    from swarmqa.decision.computer_use import ComputerUseEvaluator
    from swarmqa.decision.providers.fake import FakeComputerUseProvider

    # Save center is (120, 50)
    fake = FakeComputerUseProvider(
        script=[{"action": "click", "x": 120, "y": 50}]
    )
    decision = DecisionConfig(
        mode="computer_use",
        escalate_after=1,
        computer_use=DecisionConfig().computer_use,
    )
    decision.computer_use.provider = "fake"
    decision.computer_use.max_calls = 3
    cu = ComputerUseEvaluator(decision, provider=fake)
    cascade = CascadeEvaluator(decision, computer_use=cu)

    tokens, expected = parse_goal("Export")
    obs = Observation(
        goal="Export",
        tokens=tokens,
        expected=expected,
        elements=_framed_tree(),
        maturity="shipped",
        stall_count=1,
        tried_menus=frozenset({("Export",)}),
        screenshot_path="/tmp/unused.png",
    )
    action = cascade.decide(obs)
    assert action.kind == "click"
    assert action.query is not None
    assert action.query.label == "Save"
    assert fake.calls == 1


def test_computer_use_max_calls_cap():
    from swarmqa.decision.cascade import CascadeEvaluator
    from swarmqa.decision.computer_use import ComputerUseEvaluator
    from swarmqa.decision.providers.fake import FakeComputerUseProvider
    from swarmqa.models import ComputerUseConfig

    fake = FakeComputerUseProvider(
        script=[
            {"action": "click", "x": 120, "y": 50},
            {"action": "click", "x": 120, "y": 50},
            {"action": "click", "x": 120, "y": 50},
        ]
    )
    decision = DecisionConfig(
        mode="computer_use",
        escalate_after=1,
        max_model_calls=8,
        computer_use=ComputerUseConfig(provider="fake", max_calls=1),
    )
    cu = ComputerUseEvaluator(decision, provider=fake)
    cascade = CascadeEvaluator(decision, computer_use=cu)

    tokens, expected = parse_goal("Export")
    obs = Observation(
        goal="Export",
        tokens=tokens,
        expected=expected,
        elements=_framed_tree(),
        maturity="shipped",
        stall_count=1,
        tried_menus=frozenset({("Export",)}),
        screenshot_path="/tmp/unused.png",
    )
    first = cascade.decide(obs)
    assert first.kind == "click"
    assert fake.calls == 1

    # Budget exhausted → fail-open to heuristic (done: no matching button for Export)
    second = cascade.decide(obs)
    assert second.kind == "done"
    assert fake.calls == 1


def test_computer_use_shared_max_model_calls_cap():
    from swarmqa.decision.cascade import CascadeEvaluator
    from swarmqa.decision.computer_use import ComputerUseEvaluator
    from swarmqa.decision.providers.fake import FakeComputerUseProvider
    from swarmqa.models import ComputerUseConfig

    fake = FakeComputerUseProvider(
        script=[{"action": "click", "x": 120, "y": 50}]
    )
    decision = DecisionConfig(
        mode="computer_use",
        escalate_after=1,
        max_model_calls=0,
        computer_use=ComputerUseConfig(provider="fake", max_calls=3),
    )
    cu = ComputerUseEvaluator(decision, provider=fake)
    cascade = CascadeEvaluator(decision, computer_use=cu)
    tokens, expected = parse_goal("Export")
    obs = Observation(
        goal="Export",
        tokens=tokens,
        expected=expected,
        elements=_framed_tree(),
        maturity="shipped",
        stall_count=1,
        tried_menus=frozenset({("Export",)}),
        screenshot_path="/tmp/unused.png",
    )
    action = cascade.decide(obs)
    assert action.kind == "done"
    assert fake.calls == 0


def test_computer_use_fail_open_on_error():
    from swarmqa.decision.cascade import CascadeEvaluator
    from swarmqa.decision.computer_use import ComputerUseError, ComputerUseEvaluator
    from swarmqa.models import ComputerUseConfig

    class Boom(ComputerUseEvaluator):
        def decide(self, obs: Observation) -> DecisionAction:
            raise ComputerUseError("boom")

    decision = DecisionConfig(
        mode="computer_use",
        escalate_after=1,
        computer_use=ComputerUseConfig(provider="fake", max_calls=3),
    )
    cascade = CascadeEvaluator(decision, computer_use=Boom(decision))
    tokens, expected = parse_goal("Export")
    obs = Observation(
        goal="Export",
        tokens=tokens,
        expected=expected,
        elements=_framed_tree(),
        maturity="shipped",
        stall_count=1,
        tried_menus=frozenset({("Export",)}),
        screenshot_path="/tmp/unused.png",
    )
    action = cascade.decide(obs)
    assert action.kind == "done"


def test_computer_use_skips_when_not_stalled():
    from swarmqa.decision.cascade import CascadeEvaluator
    from swarmqa.decision.computer_use import ComputerUseEvaluator
    from swarmqa.decision.providers.fake import FakeComputerUseProvider
    from swarmqa.models import ComputerUseConfig

    fake = FakeComputerUseProvider(
        script=[{"action": "click", "x": 120, "y": 50}]
    )
    decision = DecisionConfig(
        mode="computer_use",
        escalate_after=3,
        computer_use=ComputerUseConfig(provider="fake", max_calls=3),
    )
    cu = ComputerUseEvaluator(decision, provider=fake)
    cascade = CascadeEvaluator(decision, computer_use=cu)
    tokens, expected = parse_goal("Save")
    obs = Observation(
        goal="Save",
        tokens=tokens,
        expected=expected,
        elements=_framed_tree(),
        maturity="shipped",
        stall_count=0,
        screenshot_path="/tmp/unused.png",
    )
    action = cascade.decide(obs)
    assert action.kind == "click"
    assert action.query is not None
    assert action.query.label == "Save"
    assert fake.calls == 0


def test_cascade_heuristic_to_computer_use_when_system_one_missing(monkeypatch):
    """If System One is unavailable, cascade still escalates to CU."""
    from swarmqa.decision.cascade import CascadeEvaluator
    from swarmqa.decision.computer_use import ComputerUseEvaluator
    from swarmqa.decision.providers.fake import FakeComputerUseProvider
    from swarmqa.models import ComputerUseConfig

    fake = FakeComputerUseProvider(
        script=[{"action": "menu", "path": ["File", "Export"]}]
    )
    decision = DecisionConfig(
        mode="cascade",
        escalate_after=1,
        computer_use=ComputerUseConfig(provider="fake", max_calls=3),
    )
    cu = ComputerUseEvaluator(decision, provider=fake)
    cascade = CascadeEvaluator(
        decision, computer_use=cu, system_one=None
    )
    # Force no system_one even if module exists on disk.
    cascade._system_one = None

    tokens, expected = parse_goal("Export")
    obs = Observation(
        goal="Export",
        tokens=tokens,
        expected=expected,
        elements=_framed_tree(),
        maturity="shipped",
        stall_count=1,
        tried_menus=frozenset({("Export",)}),
        screenshot_path="/tmp/unused.png",
    )
    action = cascade.decide(obs)
    assert action.kind == "menu"
    assert action.menu_path == ["File", "Export"]
    assert fake.calls == 1


def test_fake_system_one_returns_chosen_click():
    tree = _two_button_tree()
    fake = FakeSystemOne(choice_id="click:1")
    config = DecisionConfig(
        mode="system_one",
        system_one=SystemOneConfig(provider="fake"),
        cache_observations=False,
    )
    evaluator = CascadeEvaluator(config, system_one=fake)
    action = evaluator.decide(_obs(goal="Save Help", tree=tree, stall_count=0))
    assert fake.calls == 1
    assert action.kind == "click"
    assert action.query is not None
    assert action.query.label == "Help"
    # Heuristic alone would have preferred Save (first matching button).
    heuristic = HeuristicEvaluator().decide(_obs(goal="Save Help", tree=tree))
    assert heuristic.query is not None
    assert heuristic.query.label == "Save"


def test_cascade_escalates_after_stall():
    tree = _two_button_tree()
    fake = FakeSystemOne(choice_id="click:1")
    config = DecisionConfig(
        mode="cascade",
        escalate_after=2,
        system_one=SystemOneConfig(provider="fake"),
        cache_observations=False,
    )
    evaluator = CascadeEvaluator(config, system_one=fake)

    before = evaluator.decide(_obs(goal="Save Help", tree=tree, stall_count=1))
    assert fake.calls == 0
    assert before.query is not None
    assert before.query.label == "Save"

    after = evaluator.decide(_obs(goal="Save Help", tree=tree, stall_count=2))
    assert fake.calls == 1
    assert after.query is not None
    assert after.query.label == "Help"
    assert evaluator.model_calls == 1


def test_cascade_fail_open_when_fake_raises():
    tree = _two_button_tree()
    fake = FakeSystemOne(raise_error=RuntimeError("boom"))
    config = DecisionConfig(
        mode="system_one",
        system_one=SystemOneConfig(provider="fake"),
        cache_observations=False,
    )
    evaluator = CascadeEvaluator(config, system_one=fake)
    action = evaluator.decide(_obs(goal="Save Help", tree=tree, stall_count=0))
    assert fake.calls == 1
    assert action.kind == "click"
    assert action.query is not None
    assert action.query.label == "Save"
    assert evaluator.model_calls == 1


def test_build_evaluator_wires_fake_provider():
    config = CampaignConfig(
        explorer=ExplorerConfig(
            decision=DecisionConfig(
                mode="cascade",
                escalate_after=1,
                system_one=SystemOneConfig(provider="fake"),
                cache_observations=False,
            )
        )
    )
    evaluator = build_evaluator(config)
    assert isinstance(evaluator, CascadeEvaluator)
    action = evaluator.decide(
        _obs(goal="Save Help", tree=_two_button_tree(), stall_count=1)
    )
    assert action.kind == "click"
    assert action.query is not None
    # Fake picks first candidate (Save).
    assert action.query.label == "Save"
    assert evaluator.model_calls == 1


def test_observation_cache_skips_duplicate_system_one_calls():
    tree = _two_button_tree()
    fake = FakeSystemOne(choice_id="click:1")
    config = DecisionConfig(
        mode="system_one",
        system_one=SystemOneConfig(provider="fake"),
        cache_observations=True,
        max_model_calls=8,
    )
    evaluator = CascadeEvaluator(config, system_one=fake)
    obs = _obs(goal="Save Help", tree=tree, stall_count=0)
    first = evaluator.decide(obs)
    second = evaluator.decide(obs)
    assert fake.calls == 1
    assert evaluator.model_calls == 1
    assert first.query is not None and second.query is not None
    assert first.query.label == second.query.label == "Help"

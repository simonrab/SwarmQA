"""WP-D2: replay cache and self-heal in the agent loop."""

from __future__ import annotations

import json
from pathlib import Path

from swarmqa.driver.fake import FakeDriver
from swarmqa.explorer.agent_loop import AgentLoopSettings, run_agent_loop
from swarmqa.flows.cache import CachedFlow, CachedStep, FlowCache
from swarmqa.llm.fake import FakeModelProvider
from swarmqa.llm.protocol import StepDecision
from swarmqa.models import ElementQuery, Shard, UIElement
from swarmqa.testing import make_app, sample_config

GOAL = "Save the profile name"


def home(profile_label: str = "Profile") -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Demo",
            children=[
                UIElement(role="button", label="Settings", identifier="settings"),
                UIElement(role="button", label=profile_label, identifier="profile"),
            ],
        )
    ]


def profile() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Demo",
            children=[
                UIElement(role="navigationbar", label="Profile"),
                UIElement(role="textfield", label="Name", identifier="name"),
                UIElement(role="button", label="Save", identifier="save"),
            ],
        )
    ]


def saved() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Demo",
            children=[UIElement(role="navigationbar", label="Saved"), UIElement(role="button", label="Done")],
        )
    ]


class App(FakeDriver):
    def __init__(self, *args, profile_label: str = "Profile", **kwargs):
        super().__init__(*args, **kwargs)
        self.profile_label = profile_label
        self.transitions = {profile_label: profile(), "Save": saved()}

    def launch(self) -> None:
        super().launch()
        self.tree = home(self.profile_label)


def tap(label: str) -> StepDecision:
    return StepDecision(kind="tap", target=ElementQuery(role="button", label=label))


def type_name() -> StepDecision:
    return StepDecision(kind="type", target=ElementQuery(role="textfield", label="Name"), text="Ada")


def done() -> StepDecision:
    return StepDecision(kind="done")


def ui_actions(driver: FakeDriver) -> list[str]:
    return [a for a in driver.actions_log if a.startswith(("click:", "type:"))]


def setup(tmp_path: Path):
    config = sample_config(make_app(tmp_path))
    config.app.source_dir = str(tmp_path / "src")
    return config, FlowCache.from_config(config)


def run(tmp_path, config, cache, provider, *, run_no: int, **app):
    work = tmp_path / f"run{run_no}" / "workers" / "w1"
    driver = App(config.app, work, **app)
    shard = Shard(id="s1", kind="exploratory", name="Save profile", goal=GOAL)
    result = run_agent_loop(
        shard,
        driver,
        config,
        provider=provider,
        settings=AgentLoopSettings(max_steps=20, heuristic_first=False),
        work_dir=work,
        worker_id="w1",
        flow_cache=cache,
    )
    return result, driver, shard


def first_run(tmp_path, config, cache):
    provider = FakeModelProvider(steps=[tap("Profile"), type_name(), tap("Save"), done()])
    return run(tmp_path, config, cache, provider, run_no=1)


def test_reaching_the_goal_saves_the_path_under_the_app_repo(tmp_path):
    config, cache = setup(tmp_path)
    result, _, shard = first_run(tmp_path, config, cache)
    assert result.status == "passed"
    path = cache.path_for(shard, "macos")
    assert path.parent == tmp_path / "src" / ".aqa" / "flows" / "macos"
    flow = cache.load(shard, "macos")
    assert [s.action["kind"] for s in flow.steps] == ["tap", "type", "tap"]
    assert flow.runs == 1 and flow.heals == 0 and flow.end_screen == flow.steps[-1].after


def test_a_path_that_never_left_the_screen_is_not_cached(tmp_path):
    config, cache = setup(tmp_path)
    # "Settings" goes nowhere in this app; the model then (wrongly) says done.
    provider = FakeModelProvider(steps=[tap("Settings"), done()])
    _, _, shard = run(tmp_path, config, cache, provider, run_no=1)
    assert cache.load(shard, "macos") is None


def test_a_cached_flow_replays_without_the_model(tmp_path):
    config, cache = setup(tmp_path)
    first_run(tmp_path, config, cache)
    provider = FakeModelProvider()
    result, driver, shard = run(tmp_path, config, cache, provider, run_no=2)
    assert result.status == "passed"
    assert provider.calls == []
    assert ui_actions(driver) == ["click:Profile", "type:Name:Ada", "click:Save"]
    assert any(s.action == "cache" and s.message.startswith("replayed 3/3") for s in result.steps)
    assert any(s.action == "done" and s.message.startswith("cache:") for s in result.steps)
    flow = cache.load(shard, "macos")
    assert flow.runs == 2 and flow.heals == 0


def test_a_broken_step_is_re_explored_alone_and_the_cache_heals(tmp_path):
    config, cache = setup(tmp_path)
    first_run(tmp_path, config, cache)
    # The Profile button is now "Account": only that step needs the model.
    provider = FakeModelProvider(steps=[tap("Account")])
    result, driver, shard = run(tmp_path, config, cache, provider, run_no=2, profile_label="Account")
    assert result.status == "passed"
    assert provider.calls == ["decide_step"]
    assert ui_actions(driver) == ["click:Account", "type:Name:Ada", "click:Save"]
    flow = cache.load(shard, "macos")
    assert flow.heals == 1 and flow.runs == 2
    assert flow.steps[0].action["target"]["label"] == "Account"


def test_a_step_that_leads_elsewhere_counts_as_a_miss(tmp_path):
    config, cache = setup(tmp_path)
    first_run(tmp_path, config, cache)
    shard = Shard(id="s1", kind="exploratory", name="Save profile", goal=GOAL)
    flow = cache.load(shard, "macos")
    flow.steps[0] = CachedStep(flow.steps[0].action, flow.steps[0].screen, "somewhere-else")
    cache.save(shard, "macos", flow.steps, flow.end_screen)
    result, _, _ = run(tmp_path, config, cache, FakeModelProvider(), run_no=2)
    assert any(s.action == "cache" and "not somewhere-else" in s.message for s in result.steps)


def test_a_flow_that_keeps_failing_is_dropped(tmp_path):
    config, _ = setup(tmp_path)
    config.flows.settings = {"max_failures": 2}
    cache = FlowCache.from_config(config)
    _, _, shard = first_run(tmp_path, config, cache)
    path = cache.path_for(shard, "macos")
    for attempt in (2, 3):
        provider = FakeModelProvider(steps=[StepDecision(kind="give_up")] * 3)
        run(tmp_path, config, cache, provider, run_no=attempt, profile_label="Gone")
        if attempt == 2:
            assert CachedFlow.from_dict(json.loads(path.read_text())).failures == 1
    assert not path.exists()


def test_no_cache_without_a_place_to_keep_it_or_when_turned_off(tmp_path):
    config = sample_config(make_app(tmp_path))
    assert FlowCache.from_config(config) is None
    config.flows.settings = {"cache_dir": str(tmp_path / "flows")}
    assert FlowCache.from_config(config).root == tmp_path / "flows"
    config.flows.settings = {"cache_dir": str(tmp_path / "flows"), "cache": False}
    assert FlowCache.from_config(config) is None
    assert FlowCache.from_config(config, force=True) is not None


def test_a_different_goal_does_not_reuse_the_cache(tmp_path):
    config, cache = setup(tmp_path)
    _, _, shard = first_run(tmp_path, config, cache)
    other = Shard(id="s1", kind="exploratory", name=shard.name, goal="Something else")
    assert cache.load(other, "macos") is None
    assert cache.load(shard, "ios") is None


def test_flows_list_cli(tmp_path, capsys, monkeypatch):
    from swarmqa.flows import cli

    config, cache = setup(tmp_path)
    first_run(tmp_path, config, cache)
    monkeypatch.setattr("swarmqa.config.load_config_or_defaults", lambda path: config)
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "1 cached flow(s)" in out and "macos/Save profile: 3 step(s), runs 1" in out

"""Explorer v2 agent loop against the fake driver, fake provider, and fake checks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from swarmqa.checks.protocol import CheckIssue, StepContext
from swarmqa.driver.fake import FakeDriver
from swarmqa.driver.protocol import UnsupportedAction
from swarmqa.errors import AppCrashedError, UITimeoutError
from swarmqa.explorer.agent_loop import AgentLoopSettings, run_agent_loop
from swarmqa.explorer.scripted import run_scripted
from swarmqa.explorer.screen_graph import ScreenGraph, fingerprint
from swarmqa.intent.ingest import action_from_dict
from swarmqa.llm.fake import FakeModelProvider
from swarmqa.llm.protocol import ModelError, StepDecision
from swarmqa.models import ElementQuery, Shard, UIElement
from swarmqa.reporter.findings import fingerprint_for
from swarmqa.testing import make_app, sample_config

# A four-screen app. iOS-style back buttons carry the previous screen's title
# as their label and "back" as their identifier; FakeDriver.transitions is
# keyed by label, so "Home" goes home and "Settings" goes to settings.


def home() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Demo",
            children=[
                UIElement(role="text", label="Welcome"),
                UIElement(role="button", label="Settings", identifier="settings"),
                UIElement(role="button", label="Profile", identifier="profile"),
            ],
        )
    ]


def settings() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Demo",
            children=[
                UIElement(
                    role="navigationbar",
                    label="Settings",
                    children=[UIElement(role="button", label="Home", identifier="back")],
                ),
                UIElement(role="switch", label="Dark Mode", identifier="dark"),
                UIElement(role="button", label="About", identifier="about"),
            ],
        )
    ]


def about() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Demo",
            children=[
                UIElement(
                    role="navigationbar",
                    label="About",
                    children=[UIElement(role="button", label="Settings", identifier="back")],
                ),
                UIElement(role="text", label="Version 1.0"),
            ],
        )
    ]


def profile() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Demo",
            children=[
                UIElement(
                    role="navigationbar",
                    label="Profile",
                    children=[UIElement(role="button", label="Home", identifier="back")],
                ),
                UIElement(role="textfield", label="Name", identifier="name"),
                UIElement(role="button", label="Save", identifier="save"),
            ],
        )
    ]


ALL_CONTROLS = 9  # Settings, Profile | Home, Dark Mode, About | Settings | Home, Name, Save


class ObservingFake(FakeDriver):
    """Counts observations and resets to the home screen on (re)launch."""

    def __init__(self, *args, reset_on_launch: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.observe_calls = 0
        self.reset_on_launch = reset_on_launch

    def launch(self) -> None:
        super().launch()
        if self.reset_on_launch:
            self.tree = home()

    def observe(self, name=None, *, screenshot=True):
        self.observe_calls += 1
        return super().observe(name, screenshot=screenshot)


class V1Only:
    """A driver that speaks only the v1 protocol."""

    def __init__(self, fake: FakeDriver):
        self._fake = fake

    def launch(self):
        self._fake.launch()

    def relaunch(self):
        self._fake.relaunch()

    def close(self):
        self._fake.close()

    def accessibility_tree(self):
        return self._fake.accessibility_tree()

    def click(self, target):
        self._fake.click(target)

    def type_text(self, target, text):
        self._fake.type_text(target, text)

    def keychord(self, keys):
        self._fake.keychord(keys)

    def scroll(self, delta, target=None):
        self._fake.scroll(delta, target)

    def select_menu(self, path):
        self._fake.select_menu(path)

    def wait_for(self, target, timeout_s):
        return self._fake.wait_for(target, timeout_s)

    def screenshot(self, name):
        return self._fake.screenshot(name)

    def start_video(self):
        self._fake.start_video()

    def stop_video(self):
        return self._fake.stop_video()

    def metadata(self):
        return self._fake.metadata()


@dataclass
class RecordingCheck:
    """Records every context; `rule` turns a context into issues."""

    name: str = "recording"
    per_screen: bool = False
    rule: object = None
    contexts: list[StepContext] = field(default_factory=list)

    def run(self, ctx: StepContext) -> list[CheckIssue]:
        self.contexts.append(ctx)
        if self.rule is None:
            return []
        return list(self.rule(ctx))


def make_driver(tmp_path: Path, cls=ObservingFake, **kwargs) -> FakeDriver:
    app = make_app(tmp_path)
    driver = cls(app, work_dir(tmp_path), **kwargs)
    driver.set_tree(home())
    driver.transitions = {
        "Settings": settings(),
        "About": about(),
        "Profile": profile(),
        "Home": home(),
    }
    return driver


def config_for(tmp_path: Path, **video):
    config = sample_config(make_app(tmp_path))
    if video:
        config.video.mode = video["mode"]
    return config


def work_dir(tmp_path: Path) -> Path:
    return tmp_path / "campaign" / "workers" / "w1"


def run(tmp_path, driver, *, goal="", settings_=None, provider=None, checks=(), clock=None, config=None):
    shard = Shard(id="s1", kind="exploratory", name="hunt", goal=goal)
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    return run_agent_loop(
        shard,
        driver,
        config or config_for(tmp_path),
        provider=provider,
        checks=checks,
        settings=settings_ or AgentLoopSettings(max_steps=60),
        work_dir=work_dir(tmp_path),
        worker_id="w1",
        **kwargs,
    )


def load_graph(tmp_path) -> ScreenGraph:
    return ScreenGraph.load(work_dir(tmp_path) / "screen_graph.json")


def tap(label: str) -> StepDecision:
    return StepDecision(kind="tap", target=ElementQuery(role="button", label=label))


# Crawl mode ---------------------------------------------------------------


def test_crawl_visits_every_control_and_stops_when_frontier_is_empty(tmp_path):
    driver = make_driver(tmp_path)
    result = run(tmp_path, driver)
    assert result.status == "passed"
    assert result.steps[-1].action == "stop" and result.steps[-1].message == "frontier_empty"
    graph = load_graph(tmp_path)
    stats = graph.coverage()
    assert stats["screens"] == 4
    assert stats["controls"] == ALL_CONTROLS
    assert stats["untried"] == 0 and stats["frontier"] == 0
    for label in ("Settings", "Profile", "Home", "Dark Mode", "About", "Save"):
        assert f"click:{label}" in driver.actions_log
    assert "type:Name:test" in driver.actions_log


def test_crawl_needs_no_model(tmp_path):
    provider = FakeModelProvider()
    run(tmp_path, make_driver(tmp_path), provider=provider, settings_=AgentLoopSettings(mode="crawl", max_steps=60))
    assert provider.calls == []


def test_crawl_relaunches_to_reach_a_frontier_screen_with_no_way_back(tmp_path):
    driver = make_driver(tmp_path)
    # About has no back button here: the loop must relaunch and replay the path.
    driver.transitions["About"] = [UIElement(role="window", children=[UIElement(role="button", label="Licenses")])]
    driver.transitions["Settings"] = [
        UIElement(
            role="window",
            children=[UIElement(role="button", label="About"), UIElement(role="button", label="Legal")],
        )
    ]
    result = run(tmp_path, driver)
    assert "relaunch" in driver.actions_log
    assert "click:Legal" in driver.actions_log
    assert load_graph(tmp_path).coverage()["untried"] == 0
    assert result.status == "passed"


def test_crawl_gives_up_on_unreachable_screens_without_looping(tmp_path):
    driver = make_driver(tmp_path, reset_on_launch=False)
    driver.transitions["About"] = [UIElement(role="window", children=[UIElement(role="button", label="Licenses")])]
    result = run(tmp_path, driver, settings_=AgentLoopSettings(max_steps=200))
    assert result.steps[-1].message == "frontier_empty"
    assert sum(1 for s in result.steps if s.action not in ("stop",)) < 60


# Goal mode ----------------------------------------------------------------


def test_goal_mode_follows_the_model_and_stops_on_done(tmp_path):
    provider = FakeModelProvider(
        steps=[tap("Settings"), tap("About"), StepDecision(kind="done", rationale="version visible")]
    )
    driver = make_driver(tmp_path)
    result = run(
        tmp_path,
        driver,
        goal="Find the version number in About",
        provider=provider,
        settings_=AgentLoopSettings(heuristic_first=False),
    )
    assert provider.calls == ["decide_step"] * 3
    assert driver.tree is driver.transitions["About"]
    assert result.status == "passed"
    assert result.steps[-2].action == "done"
    assert result.steps[-1].message == "done"


def test_goal_mode_passes_history_to_the_model(tmp_path):
    seen: list[int] = []

    class Spy(FakeModelProvider):
        def decide_step(self, obs, goal, history, *, timeout_s=30.0):
            seen.append(len(history))
            return super().decide_step(obs, goal, history, timeout_s=timeout_s)

    provider = Spy(steps=[tap("Settings"), tap("About"), StepDecision(kind="done")])
    run(tmp_path, make_driver(tmp_path), goal="open about", provider=provider, settings_=AgentLoopSettings(heuristic_first=False))
    assert seen == [0, 1, 2]


def test_confident_heuristic_skips_the_model(tmp_path):
    provider = FakeModelProvider(steps=[tap("Settings"), StepDecision(kind="done")])
    driver = make_driver(tmp_path)
    run(tmp_path, driver, goal='Find the version number on the "About" screen', provider=provider)
    # Home has no control matching the goal, so the model picks Settings; on
    # Settings exactly one control ("About") matches, so the heuristic taps it.
    assert driver.actions_log.count("click:About") == 1
    assert provider.calls == ["decide_step", "decide_step"]


def test_goal_mode_without_a_provider_uses_the_heuristic(tmp_path):
    driver = make_driver(tmp_path)
    result = run(tmp_path, driver, goal='toggle "Dark Mode" in Settings')
    assert "click:Settings" in driver.actions_log
    assert "click:Dark Mode" in driver.actions_log
    assert result.steps[-1].message == "done"


def test_model_error_falls_back_and_counts_against_the_cap(tmp_path):
    provider = FakeModelProvider(error=ModelError("offline"))
    driver = make_driver(tmp_path)
    result = run(
        tmp_path,
        driver,
        goal="find the version number",
        provider=provider,
        settings_=AgentLoopSettings(max_steps=12, max_model_calls=3, heuristic_first=False),
    )
    assert result.status == "passed"
    assert provider.calls == ["decide_step"] * 3
    assert sum(1 for s in result.steps if s.action == "model" and s.status == "failed") == 3
    # Fallback kept exploring.
    assert "click:Settings" in driver.actions_log


def test_max_model_calls_is_honoured(tmp_path):
    provider = FakeModelProvider()  # default: taps untried controls, never done early
    run(
        tmp_path,
        make_driver(tmp_path),
        goal="look around",
        provider=provider,
        settings_=AgentLoopSettings(max_steps=30, max_model_calls=2, heuristic_first=False),
    )
    assert len(provider.calls) == 2


def test_stall_ends_goal_mode(tmp_path):
    provider = FakeModelProvider(steps=[tap("Nope")] * 10)
    result = run(
        tmp_path,
        make_driver(tmp_path),
        goal="reach nowhere",
        provider=provider,
        settings_=AgentLoopSettings(stall_limit=3, heuristic_first=False),
    )
    assert result.steps[-1].message == "stall"
    assert sum(1 for s in result.steps if s.status == "failed") == 3


# Budgets -------------------------------------------------------------------


def test_max_steps_is_honoured(tmp_path):
    driver = make_driver(tmp_path)
    result = run(tmp_path, driver, settings_=AgentLoopSettings(max_steps=3))
    actions = [a for a in driver.actions_log if a.startswith(("click:", "type:", "key:", "swipe:", "tap:"))]
    assert len(actions) == 3
    assert result.steps[-1].message == "max_steps"


def test_wall_time_budget_uses_the_injected_clock(tmp_path):
    ticks = iter(range(0, 10_000, 10))
    driver = make_driver(tmp_path)
    result = run(
        tmp_path,
        driver,
        clock=lambda: next(ticks),
        settings_=AgentLoopSettings(max_steps=100, max_wall_time_s=35),
    )
    assert result.steps[-1].message == "max_wall_time"
    actions = [a for a in driver.actions_log if a.startswith(("click:", "type:"))]
    assert len(actions) == 3


# Observation and checks ---------------------------------------------------


def test_every_step_takes_a_fresh_observation(tmp_path):
    check = RecordingCheck()
    driver = make_driver(tmp_path)
    result = run(tmp_path, driver, checks=[check], settings_=AgentLoopSettings(max_steps=4))
    actions = 4
    assert driver.observe_calls == actions + 1
    assert len(check.contexts) == actions + 1
    first, second = check.contexts[0], check.contexts[1]
    assert first.before is None and first.action is None
    assert second.before is first.after
    assert second.after.tree is driver.transitions["Settings"]
    assert second.screen_id == fingerprint(settings())
    assert result.status == "passed"


def test_screenshot_only_on_first_visit_when_every_step_is_off(tmp_path):
    check = RecordingCheck(per_screen=True)
    driver = make_driver(tmp_path)
    run(tmp_path, driver, checks=[check], settings_=AgentLoopSettings(max_steps=60, screenshot_every_step=False))
    assert len(check.contexts) == 4
    assert all(ctx.after.screenshot is not None for ctx in check.contexts)
    assert all(node.screenshot for node in load_graph(tmp_path).nodes.values())


def test_per_screen_checks_run_once_per_screen_and_step_checks_every_step(tmp_path):
    per_screen = RecordingCheck(name="judge", per_screen=True)
    per_step = RecordingCheck(name="functional")
    result = run(tmp_path, make_driver(tmp_path), checks=[per_screen, per_step])
    screens = {ctx.screen_id for ctx in per_screen.contexts}
    assert len(per_screen.contexts) == len(screens) == 4
    action_steps = sum(1 for s in result.steps if s.action not in ("launch", "stop"))
    assert len(per_step.contexts) == action_steps + 1


def test_per_screen_checks_can_be_switched_off(tmp_path):
    per_screen = RecordingCheck(per_screen=True)
    run(tmp_path, make_driver(tmp_path), checks=[per_screen], settings_=AgentLoopSettings(per_screen_checks=False))
    assert per_screen.contexts == []


def _about_issue(ctx: StepContext):
    if any(e.label == "Version 1.0" for e in _walk(ctx.after.tree)):
        yield CheckIssue(
            kind="visual",
            category="visual",
            title="Version text clipped",
            severity="low",
            element=ElementQuery(role="text", label="Version 1.0"),
        )


def _walk(tree):
    for element in tree:
        yield element
        yield from _walk(element.children)


def test_issues_become_deduplicated_findings_with_replays(tmp_path):
    check = RecordingCheck(name="layout", rule=_about_issue)
    driver = make_driver(tmp_path)
    result = run(tmp_path, driver, checks=[check])
    assert result.status == "failed"
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.fingerprint == fingerprint_for("visual", "Version text clipped", "Version 1.0")
    assert finding.category == "visual"
    assert "check: layout" in finding.details
    assert finding.steps[0] == "launch"
    assert 'tap "About"' in finding.steps
    assert finding.screenshots and not Path(finding.screenshots[0]).is_absolute()
    campaign = tmp_path / "campaign"
    assert finding.replay_json == "findings/f-1.replay.json"
    assert (campaign / "findings" / "f-1.md").is_file()
    assert finding.video == "workers/w1/media/session.mp4"

    # The replay is a version-1 flow that the scripted explorer runs to the same screen.
    document = json.loads((campaign / finding.replay_json).read_text())
    assert document["version"] == 1
    actions = [action_from_dict(step, origin="replay") for step in document["steps"]]
    replay_driver = make_driver(tmp_path / "replay")
    shard = Shard(id="r", kind="scripted", name="replay", actions=actions)
    replayed = run_scripted(shard, replay_driver, config_for(tmp_path / "replay"), worker_id="r", work_dir=tmp_path / "replay" / "run")
    assert replayed.status == "passed"
    assert replay_driver.tree is replay_driver.transitions["About"]


def test_advisory_findings_do_not_fail_the_shard(tmp_path):
    def rule(ctx):
        yield CheckIssue(kind="visual_judgment", category="confusing", title="Unclear", advisory=True, confidence=0.4)

    result = run(tmp_path, make_driver(tmp_path), checks=[RecordingCheck(rule=rule)], settings_=AgentLoopSettings(max_steps=2))
    assert len(result.findings) == 1 and result.findings[0].advisory
    assert result.status == "passed"


def test_broken_check_is_logged_and_skipped(tmp_path):
    def rule(ctx):
        raise RuntimeError("bug in check")

    result = run(tmp_path, make_driver(tmp_path), checks=[RecordingCheck(name="bad", rule=rule)], settings_=AgentLoopSettings(max_steps=2))
    assert result.status == "passed"
    assert any(s.action == "check bad" and s.status == "failed" for s in result.steps)


# Crashes and failures -------------------------------------------------------


def test_crash_is_passed_to_checks_then_the_loop_relaunches_and_continues(tmp_path):
    def rule(ctx):
        if ctx.crashed:
            yield CheckIssue(kind="crash", category="crash", title="App crashed", severity="critical")

    check = RecordingCheck(name="functional", rule=rule)
    driver = make_driver(tmp_path)
    driver.crash_labels = {"Profile"}
    result = run(tmp_path, driver, checks=[check])
    crashed = [ctx for ctx in check.contexts if ctx.crashed]
    assert len(crashed) == 1
    assert crashed[0].action.target.label == "Profile"
    assert crashed[0].before is not None
    assert "relaunch" in driver.actions_log
    assert result.status == "failed"
    assert [f.title for f in result.findings] == ["App crashed"]
    # The crawl carried on after the crash.
    assert "click:About" in driver.actions_log
    replay = json.loads((tmp_path / "campaign" / result.findings[0].replay_json).read_text())
    assert replay["steps"][-1] == {
        "action": "click",
        "target": {"identifier": "profile", "label": "Profile", "role": "button"},
    }


def test_crash_without_a_crash_check_still_becomes_a_finding(tmp_path):
    driver = make_driver(tmp_path)
    driver.crash_labels = {"Settings"}
    result = run(tmp_path, driver)
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.kind == "crash" and finding.title == "Crash on Settings"
    assert finding.fingerprint == fingerprint_for("crash", "Crash on Settings", "Settings")


def test_missing_element_is_a_failed_step_not_a_crash(tmp_path):
    provider = FakeModelProvider(steps=[tap("Ghost"), StepDecision(kind="done")])
    result = run(tmp_path, make_driver(tmp_path), goal="ghost hunt", provider=provider, settings_=AgentLoopSettings(heuristic_first=False))
    failed = [s for s in result.steps if s.status == "failed"]
    assert len(failed) == 1 and "ElementNotFoundError" in failed[0].message
    assert result.status == "passed"


def test_unsupported_action_is_a_failed_step(tmp_path):
    provider = FakeModelProvider(
        steps=[StepDecision(kind="swipe", point=(10, 400), end=(300, 400)), StepDecision(kind="done")]
    )
    fake = make_driver(tmp_path, cls=FakeDriver)
    result = run(tmp_path, V1Only(fake), goal="swipe sideways", provider=provider, settings_=AgentLoopSettings(heuristic_first=False))
    failed = [s for s in result.steps if s.status == "failed"]
    assert len(failed) == 1 and "UnsupportedAction" in failed[0].message
    assert result.steps[-1].message == "done"


def test_launch_failure_is_an_error_result(tmp_path):
    driver = make_driver(tmp_path)
    driver.target.path = str(tmp_path / "missing.app")
    result = run(tmp_path, driver)
    assert result.status == "error"
    assert result.error
    assert [f.kind for f in result.findings] == ["launch"]


# Actions, back navigation and drivers ----------------------------------------


def test_back_taps_the_navigation_bar_back_button(tmp_path):
    provider = FakeModelProvider(steps=[tap("Settings"), StepDecision(kind="back"), StepDecision(kind="done")])
    driver = make_driver(tmp_path)
    result = run(tmp_path, driver, goal="go and come back", provider=provider, settings_=AgentLoopSettings(heuristic_first=False))
    assert driver.actions_log[-1] == "click:Home" or "click:Home" in driver.actions_log
    assert driver.tree is driver.transitions["Home"]
    assert result.status == "passed"


def test_back_without_a_button_is_escape_on_macos_and_edge_swipe_on_ios(tmp_path):
    for platform, expected in (("macos", "key:escape"), ("ios", "swipe:1,422->273,422")):
        base = tmp_path / platform
        provider = FakeModelProvider(steps=[StepDecision(kind="back"), StepDecision(kind="done")])
        driver = make_driver(base)
        run(base, driver, goal="leave", provider=provider, settings_=AgentLoopSettings(heuristic_first=False, platform=platform))
        assert expected in driver.actions_log


def test_coordinate_and_key_actions_reach_the_driver(tmp_path):
    driver = make_driver(tmp_path, reset_on_launch=False)
    driver.tree[0].children[1].frame = (0, 100, 200, 44)
    provider = FakeModelProvider(
        steps=[
            StepDecision(kind="tap_point", point=(50, 120)),
            StepDecision(kind="key", keys=["cmd", "r"]),
            StepDecision(kind="swipe", point=(100, 600), end=(100, 200)),
            StepDecision(kind="type", target=ElementQuery(role="textfield", label="Name"), text="Ada"),
            StepDecision(kind="done"),
        ]
    )
    driver.transitions["Settings"] = profile()
    run(tmp_path, driver, goal="mixed", provider=provider, settings_=AgentLoopSettings(heuristic_first=False))
    assert "tap:50,120" in driver.actions_log
    assert "key:cmd+r" in driver.actions_log
    assert "swipe:100,600->100,200" in driver.actions_log
    assert "type:Name:Ada" in driver.actions_log


def test_works_with_a_v1_only_driver(tmp_path):
    fake = make_driver(tmp_path, cls=FakeDriver)
    result = run(tmp_path, V1Only(fake))
    assert result.status == "passed"
    assert load_graph(tmp_path).coverage()["untried"] == 0


def test_video_policy_is_honoured(tmp_path):
    driver = make_driver(tmp_path)
    result = run(tmp_path, driver, config=config_for(tmp_path, mode="on_failure"), settings_=AgentLoopSettings(max_steps=2))
    assert result.status == "passed"
    assert "video:start" in driver.actions_log
    assert not (work_dir(tmp_path) / "media" / "session.mp4").exists()


def test_settings_from_config():
    config = sample_config()
    config.explorer.max_steps = 7
    config.explorer.max_time_s = 11
    config.explorer.decision.max_model_calls = 2
    config.app.platform = "ios"
    loop_settings = AgentLoopSettings.from_config(config, mode="crawl")
    assert (loop_settings.max_steps, loop_settings.max_wall_time_s, loop_settings.max_model_calls) == (7, 11, 2)
    assert loop_settings.platform == "ios" and loop_settings.mode == "crawl"


# Review regressions -----------------------------------------------------------


def test_crawl_drops_a_control_that_vanished_under_the_same_fingerprint(tmp_path):
    # "Delete 3 items" becomes "Delete 2 items": digits are masked in the screen
    # fingerprint but not in the control key, so the old control is stale.
    def screen(count: int) -> list[UIElement]:
        return [
            UIElement(
                role="window",
                children=[
                    UIElement(role="button", label="Refresh"),
                    UIElement(role="button", label=f"Delete {count} items"),
                ],
            )
        ]

    driver = make_driver(tmp_path, cls=FakeDriver)
    driver.set_tree(screen(3))
    driver.transitions = {"Refresh": screen(2)}
    result = run(tmp_path, driver, settings_=AgentLoopSettings(max_steps=30))
    assert result.steps[-1].message == "frontier_empty"
    assert "click:Delete 2 items" in driver.actions_log
    assert sum(1 for s in result.steps if s.action.startswith("tap ")) == 2


def test_crawl_stops_after_repeated_failures(tmp_path):
    class Broken(FakeDriver):
        def click(self, target):
            raise UnsupportedAction("stuck")

    driver = make_driver(tmp_path, cls=Broken)
    driver.set_tree([UIElement(role="window", children=[UIElement(role="button", label=f"B{i}") for i in range(10)])])
    result = run(tmp_path, driver, settings_=AgentLoopSettings(max_steps=50, stall_limit=3))
    assert result.steps[-1].message == "stall"
    assert sum(1 for s in result.steps if s.status == "failed") == 3


def test_crawl_taps_the_exact_control_when_labels_overlap(tmp_path):
    driver = make_driver(tmp_path, cls=FakeDriver)
    driver.set_tree(
        [
            UIElement(
                role="window",
                children=[
                    UIElement(role="button", label="Save As", frame=(0, 0, 100, 40)),
                    UIElement(role="button", label="Save", frame=(0, 50, 100, 40)),
                ],
            )
        ]
    )
    driver.transitions = {}
    result = run(tmp_path, driver, settings_=AgentLoopSettings(max_steps=20))
    assert driver.actions_log.count("click:Save As") == 1
    assert "tap:50,70" in driver.actions_log
    assert result.steps[-1].message == "frontier_empty"
    assert load_graph(tmp_path).coverage()["untried"] == 0


def test_model_tap_reaches_the_exact_control_by_identifier(tmp_path):
    driver = make_driver(tmp_path, cls=FakeDriver)
    driver.set_tree(
        [
            UIElement(
                role="window",
                children=[
                    UIElement(role="button", label="Save As", identifier="save-as"),
                    UIElement(role="button", label="Save", identifier="save"),
                ],
            )
        ]
    )
    provider = FakeModelProvider(steps=[tap("Save"), StepDecision(kind="done")])
    run(tmp_path, driver, goal="save it", provider=provider, settings_=AgentLoopSettings(heuristic_first=False))
    assert "click:Save" in driver.actions_log
    assert "click:Save As" not in driver.actions_log
    graph = load_graph(tmp_path)
    assert graph.nodes[graph.start].tried == {"button|Save|save"}


def test_observe_timeout_is_a_failed_step_and_the_loop_carries_on(tmp_path):
    class SlowSettings(ObservingFake):
        timed_out = False

        def observe(self, name=None, *, screenshot=True):
            if self.tree is self.transitions["Settings"] and not self.timed_out:
                self.timed_out = True
                raise UITimeoutError("tree read timed out")
            return super().observe(name, screenshot=screenshot)

    check = RecordingCheck()
    driver = make_driver(tmp_path, cls=SlowSettings)
    result = run(tmp_path, driver, checks=[check])
    hung = [ctx for ctx in check.contexts if ctx.error.startswith("timeout:")]
    assert len(hung) == 1 and hung[0].action.target.label == "Settings"
    assert any(s.action == "observe" and s.status == "failed" for s in result.steps)
    assert result.status == "passed"
    assert result.steps[-1].message == "frontier_empty"
    assert load_graph(tmp_path).coverage()["untried"] == 0


def test_crash_during_the_screenshot_read_is_blamed_on_its_step(tmp_path):
    class CrashOnSettingsShot(ObservingFake):
        crashed = False

        def screenshot(self, name):
            if self.tree is self.transitions["Settings"] and not self.crashed:
                self.crashed = True
                raise AppCrashedError("died while drawing settings")
            return super().screenshot(name)

    check = RecordingCheck()
    driver = make_driver(tmp_path, cls=CrashOnSettingsShot)
    result = run(
        tmp_path,
        driver,
        checks=[check],
        settings_=AgentLoopSettings(max_steps=60, screenshot_every_step=False),
    )
    crashed = [ctx for ctx in check.contexts if ctx.crashed]
    assert len(crashed) == 1 and crashed[0].action.target.label == "Settings"
    assert [f.title for f in result.findings] == ["Crash on Settings"]
    # Every screen id matches the tree the loop kept for it.
    for ctx in check.contexts:
        if ctx.screen_id:
            assert ctx.screen_id == fingerprint(ctx.after.tree)


def test_replay_of_an_ambiguous_label_taps_the_same_element(tmp_path):
    saved = [UIElement(role="window", children=[UIElement(role="text", label="Saved ok")])]
    save_as = [UIElement(role="window", children=[UIElement(role="text", label="Choose a name")])]

    def build(base: Path) -> FakeDriver:
        driver = make_driver(base, cls=FakeDriver)
        driver.set_tree(
            [
                UIElement(
                    role="window",
                    children=[
                        UIElement(role="button", label="Save As", frame=(0, 0, 100, 40)),
                        UIElement(role="button", label="Save", frame=(0, 50, 100, 40)),
                    ],
                )
            ]
        )
        driver.transitions = {"Save As": save_as, "Save": saved}
        return driver

    def rule(ctx):
        if any(e.label == "Saved ok" for e in _walk(ctx.after.tree)):
            yield CheckIssue(kind="error_state", category="broken", title="Saved banner shown")

    driver = build(tmp_path)
    provider = FakeModelProvider(steps=[tap("Save"), StepDecision(kind="done")])
    result = run(
        tmp_path,
        driver,
        goal="save",
        provider=provider,
        checks=[RecordingCheck(rule=rule)],
        settings_=AgentLoopSettings(heuristic_first=False),
    )
    assert driver.tree is saved
    finding = result.findings[0]
    document = json.loads((tmp_path / "campaign" / finding.replay_json).read_text())
    assert document["steps"][-1] == {"action": "tap_point", "point": [50.0, 70.0]}

    actions = [action_from_dict(step, origin="replay") for step in document["steps"]]
    replay_driver = build(tmp_path / "replay")
    shard = Shard(id="r", kind="scripted", name="replay", actions=actions)
    replayed = run_scripted(
        shard, replay_driver, config_for(tmp_path / "replay"), worker_id="r", work_dir=tmp_path / "replay" / "run"
    )
    assert replayed.status == "passed"
    assert replay_driver.tree is saved


def test_swipes_and_the_ios_edge_swipe_are_replayed_as_swipes(tmp_path):
    def rule(ctx):
        if ctx.action is not None and ctx.action.kind == "back":
            yield CheckIssue(kind="error_state", category="broken", title="Went back")

    provider = FakeModelProvider(
        steps=[
            StepDecision(kind="swipe", point=(100, 600), end=(100, 200)),
            StepDecision(kind="back"),
            StepDecision(kind="done"),
        ]
    )
    driver = make_driver(tmp_path)
    result = run(
        tmp_path,
        driver,
        goal="scroll and leave",
        provider=provider,
        checks=[RecordingCheck(rule=rule)],
        settings_=AgentLoopSettings(heuristic_first=False, platform="ios"),
    )
    document = json.loads((tmp_path / "campaign" / result.findings[0].replay_json).read_text())
    assert document["steps"][-2:] == [
        {"action": "swipe", "point": [100.0, 600.0], "end": [100.0, 200.0]},
        {"action": "swipe", "point": [1.0, 422.0], "end": [273.0, 422.0]},
    ]
    for step in document["steps"]:
        action_from_dict(step, origin="replay")


def test_goal_mode_drops_a_broken_route_instead_of_retrying_it(tmp_path):
    # "Inbox 1" becomes "Inbox 2" under the same home fingerprint, so the known
    # route home -> inbox (tap "Inbox 1") stops working.
    def home_screen(count: int) -> list[UIElement]:
        return [
            UIElement(
                role="window",
                children=[
                    UIElement(role="button", label="Profile"),
                    UIElement(role="button", label=f"Inbox {count}"),
                ],
            )
        ]

    def child(title: str, *extra: str) -> list[UIElement]:
        return [
            UIElement(
                role="window",
                children=[
                    UIElement(role="navigationbar", label=title, children=[UIElement(role="button", label="Home")]),
                    *[UIElement(role="button", label=label) for label in extra],
                ],
            )
        ]

    driver = make_driver(tmp_path, cls=FakeDriver)
    driver.set_tree(home_screen(1))
    driver.transitions = {
        "Inbox 1": child("Inbox", "Archive"),
        "Profile": child("Profile"),
        "Home": home_screen(2),
    }
    # Profile first in tree order, so tap Inbox 1 by hand before the crawl fallback.
    provider = FakeModelProvider(
        steps=[StepDecision(kind="tap", target=ElementQuery(role="button", label="Inbox 1"))]
    )
    result = run(
        tmp_path,
        driver,
        goal="find the zebra",
        provider=provider,
        settings_=AgentLoopSettings(heuristic_first=False, max_model_calls=1, stall_limit=5, max_steps=40),
    )
    failed = [s for s in result.steps if s.status == "failed" and "Inbox 1" in s.action]
    assert len(failed) == 1
    assert result.steps[-1].message == "give_up"


def _close_screen() -> list[UIElement]:
    return [
        UIElement(
            role="window",
            frame=(0, 0, 390, 844),
            children=[
                UIElement(role="button", label="Close Account", frame=(10, 100, 200, 44)),
                UIElement(role="button", label="Close", frame=(10, 200, 100, 44)),
            ],
        )
    ]


def _back_after_settings() -> FakeModelProvider:
    return FakeModelProvider(
        steps=[tap("Settings"), StepDecision(kind="back"), StepDecision(kind="done")]
    )


def test_back_taps_the_exact_close_button(tmp_path):
    driver = make_driver(tmp_path)
    driver.transitions["Settings"] = _close_screen()
    driver.transitions["Close Account"] = [UIElement(role="window", label="Danger")]
    driver.transitions["Close"] = home()
    run(tmp_path, driver, goal="go back", provider=_back_after_settings(),
        settings_=AgentLoopSettings(max_steps=5, heuristic_first=False))
    assert "tap:60,222" in driver.actions_log
    assert "click:Close Account" not in driver.actions_log


def test_a_crash_during_back_stays_in_the_replay(tmp_path):
    driver = make_driver(tmp_path)
    driver.transitions["Settings"] = _close_screen()
    driver.crash_labels = {"Close"}
    result = run(tmp_path, driver, goal="go back", provider=_back_after_settings(),
                 settings_=AgentLoopSettings(max_steps=5, heuristic_first=False))
    crash = next(f for f in result.findings if f.kind == "crash")
    document = json.loads((tmp_path / "campaign" / crash.replay_json).read_text())
    assert document["steps"][-1] == {"action": "tap_point", "point": [60.0, 222.0]}

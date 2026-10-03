"""Replay cache and self-heal (WP-D2) against the fake driver and fake provider."""

from __future__ import annotations

import json
from pathlib import Path

from swarmqa.driver.fake import FakeDriver
from swarmqa.explorer.agent_loop import AgentLoopSettings, run_agent_loop
from swarmqa.explorer.scripted import StepFailure, run_scripted
from swarmqa.flows import CACHED_TAG
from swarmqa.flows.cache import CachedFlow, FlowCache, cache_root, cached_shards, shard_key
from swarmqa.flows.heal import Healer
from swarmqa.flows.settings import FlowsSettings
from swarmqa.intent.ingest import build_queue
from swarmqa.llm.fake import FakeModelProvider
from swarmqa.llm.protocol import StepDecision
from swarmqa.models import Action, ElementQuery, Shard, UIElement
from swarmqa.testing import make_app, sample_config

GOAL = 'toggle "Dark Mode" in Settings'

# A three-screen app. FakeDriver.transitions is keyed by the clicked label.


def home(settings_label: str = "Settings", settings_id: str = "settings") -> list[UIElement]:
    return [
        UIElement(
            role="window",
            label="Demo",
            children=[
                UIElement(role="text", label="Welcome"),
                UIElement(role="button", label=settings_label, identifier=settings_id),
                UIElement(role="button", label="Profile", identifier="profile"),
            ],
        )
    ]


def settings(toggle: str = "Dark Mode", toggle_id: str = "dark") -> list[UIElement]:
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
                UIElement(role="switch", label=toggle, identifier=toggle_id),
                UIElement(role="button", label="About", identifier="about"),
            ],
        )
    ]


def make_driver(tmp_path: Path, *, settings_label: str = "Settings", toggle: str = "Dark Mode") -> FakeDriver:
    driver = FakeDriver(make_app(tmp_path), work_dir(tmp_path))
    settings_id = "settings" if settings_label == "Settings" else settings_label.lower()
    toggle_id = "dark" if toggle == "Dark Mode" else toggle.lower().replace(" ", "-")
    driver.set_tree(home(settings_label, settings_id))
    driver.transitions = {settings_label: settings(toggle, toggle_id), "Home": home(settings_label, settings_id)}
    return driver


def work_dir(tmp_path: Path, worker: str = "w1") -> Path:
    return tmp_path / "campaign" / "workers" / worker


def config_for(tmp_path: Path, **flows):
    config = sample_config(make_app(tmp_path))
    config.app.source_dir = str(tmp_path / "repo")
    config.flows.settings = {"cache": True, **flows}
    return config


def goal_shard(name: str = "dark mode", goal: str = GOAL, **kwargs) -> Shard:
    return Shard(id="s-1-dark-mode", kind="exploratory", name=name, goal=goal, tags=["diff", "priority:1"], **kwargs)


def tap(label: str) -> StepDecision:
    return StepDecision(kind="tap", target=ElementQuery(label=label))


def explore(tmp_path: Path, shard: Shard, *, provider=None, driver=None):
    return run_agent_loop(
        shard,
        driver or make_driver(tmp_path),
        config_for(tmp_path),
        provider=provider,
        settings=AgentLoopSettings(max_steps=20),
        work_dir=work_dir(tmp_path),
        worker_id="w1",
    )


def cache_for(tmp_path: Path) -> FlowCache:
    config = config_for(tmp_path)
    return FlowCache(cache_root(config, FlowsSettings.from_config(config)))


def seeded(tmp_path: Path) -> tuple[FlowCache, Shard]:
    """Explore the goal once on the original app and cache it; return the cached shard."""
    shard = goal_shard()
    explore(tmp_path, shard)
    cache = cache_for(tmp_path)
    assert cache.record_session(shard, work_dir(tmp_path) / "session.json") is not None
    [cached] = cached_shards(config_for(tmp_path), [shard], cache=cache)
    return cache, cached


def labels(steps: list[dict]) -> list[str]:
    return [step.get("target", {}).get("label", "") for step in steps]


def replay(tmp_path: Path, shard: Shard, driver: FakeDriver, *, healer=None, worker: str = "w2"):
    return run_scripted(
        shard,
        driver,
        config_for(tmp_path),
        worker_id=worker,
        work_dir=work_dir(tmp_path, worker),
        on_step_failed=healer,
    )


def healer_for(tmp_path: Path, shard: Shard, cache: FlowCache, *, provider=None, **flows) -> Healer:
    config = config_for(tmp_path, **flows)
    return Healer.for_shard(
        shard,
        config,
        provider=provider,
        cache=cache,
        work_dir=work_dir(tmp_path, "w2") / "heal",
    )


# Keys ---------------------------------------------------------------------


def test_shard_key_is_stable_across_case_and_whitespace():
    key = shard_key(goal_shard())
    assert key == shard_key(goal_shard(name="  Dark   Mode ", goal='Toggle  "Dark Mode"   in settings'))
    assert key.startswith("dark-mode-")
    assert len(key.rsplit("-", 1)[1]) == 12
    assert key != shard_key(goal_shard(goal="open About"))
    assert key != shard_key(goal_shard(name="night mode"))


def test_shard_key_is_a_safe_file_name():
    key = shard_key(goal_shard(name="Settings / ../Dark: mode?", goal="x"))
    assert "/" not in key and ":" not in key and "." not in key


# Store --------------------------------------------------------------------


def test_save_load_round_trip_and_flow_file_reads_as_a_v1_flow(tmp_path):
    cache = FlowCache(tmp_path / "flows")
    flow = CachedFlow(
        key="dark-mode-abc",
        name="dark mode",
        goal=GOAL,
        steps=[{"action": "click", "target": {"role": "button", "label": "Settings"}}],
        hits=2,
        heals=1,
        sha="deadbeef",
    )
    path = cache.save(flow)
    assert path == tmp_path / "flows" / "dark-mode-abc.json"
    assert json.loads(path.read_text()) == {
        "version": 1,
        "name": "dark mode",
        "steps": [{"action": "click", "target": {"role": "button", "label": "Settings"}}],
    }
    loaded = cache.load("dark-mode-abc")
    assert loaded == flow
    assert loaded.created and loaded.updated
    assert cache.keys() == ["dark-mode-abc"]

    config = sample_config(make_app(tmp_path))
    config.intents = [str(path)]
    [shard] = build_queue(config)
    assert shard.kind == "scripted" and shard.actions[0].target.label == "Settings"


def test_load_misses_and_drop(tmp_path):
    cache = FlowCache(tmp_path / "flows")
    assert cache.load("nope") is None
    (tmp_path / "flows").mkdir()
    (tmp_path / "flows" / "bad.json").write_text("{not json")
    assert cache.load("bad") is None
    cache.save(CachedFlow(key="k", name="n", goal="g"))
    assert cache.drop("k") is True
    assert cache.load("k") is None and cache.drop("k") is False


# record_session -------------------------------------------------------------


def test_record_session_saves_a_goal_reached_session_without_its_launch(tmp_path):
    shard = goal_shard()
    explore(tmp_path, shard)
    cache = cache_for(tmp_path)
    flow = cache.record_session(shard, work_dir(tmp_path) / "session.json", sha="abc123")
    assert flow is not None
    assert labels(flow.steps) == ["Settings", "Dark Mode"]
    assert flow.steps[0]["action"] == "click"
    assert cache.root == tmp_path / "repo" / ".aqa" / "flows"
    stored = cache.load(shard_key(shard))
    assert stored.goal == GOAL and stored.sha == "abc123" and stored.hits == 0


def test_record_session_skips_sessions_that_did_not_reach_the_goal(tmp_path):
    crawl = Shard(id="s1", kind="exploratory", name="hunt")
    explore(tmp_path, crawl)
    cache = cache_for(tmp_path)
    assert cache.record_session(crawl, work_dir(tmp_path) / "session.json") is None

    given_up = goal_shard()
    explore(tmp_path, given_up, provider=FakeModelProvider(steps=[StepDecision(kind="give_up")]))
    session = json.loads((work_dir(tmp_path) / "session.json").read_text())
    assert session["goal_reached"] is False
    assert cache.record_session(given_up, work_dir(tmp_path) / "session.json") is None
    assert cache.record_session(given_up, tmp_path / "missing.json") is None
    assert cache.keys() == []


# cached_shards --------------------------------------------------------------


def test_cached_shards_replaces_hits_and_keeps_misses(tmp_path):
    cache, _ = seeded(tmp_path)
    hit = goal_shard()
    miss = goal_shard(name="profile", goal="edit the profile name")
    crawl = Shard(id="s-3-crawl", kind="exploratory", name="crawl")
    scripted = Shard(id="s-4-x", kind="scripted", name="dark mode", goal=GOAL)
    out = cached_shards(config_for(tmp_path), [hit, miss, crawl, scripted], cache=cache)
    assert out[1:] == [miss, crawl, scripted]
    cached = out[0]
    assert cached.kind == "scripted"
    assert (cached.id, cached.name, cached.goal) == (hit.id, hit.name, hit.goal)
    assert cached.tags == ["diff", "priority:1", CACHED_TAG]
    assert [a.target.label for a in cached.actions] == ["Settings", "Dark Mode"]
    assert cached.source_path == str(cache.flow_path(shard_key(hit)))
    assert hit.kind == "exploratory" and hit.tags == ["diff", "priority:1"]


def test_cached_shards_resolves_the_cache_from_the_config(tmp_path):
    seeded(tmp_path)
    [cached] = cached_shards(config_for(tmp_path), [goal_shard()])
    assert cached.kind == "scripted"


def test_cached_shards_is_a_no_op_when_the_cache_is_off(tmp_path):
    cache, _ = seeded(tmp_path)
    shard = goal_shard()
    assert cached_shards(config_for(tmp_path, cache=False), [shard], cache=cache) == [shard]


def test_cache_root_resolution(tmp_path, monkeypatch):
    config = sample_config()
    monkeypatch.chdir(tmp_path)
    assert cache_root(config) == tmp_path / ".aqa" / "flows"
    assert cache_root(config, config_path=tmp_path / "app" / "aqa.config.toml") == (
        tmp_path / "app" / ".aqa" / "flows"
    ).resolve()
    config.app.source_dir = str(tmp_path / "src")
    assert cache_root(config, config_path=tmp_path / "app" / "aqa.config.toml") == tmp_path / "src" / ".aqa" / "flows"
    config.flows.settings = {"cache_dir": str(tmp_path / "abs")}
    assert cache_root(config) == tmp_path / "abs"


# Replay and heal ------------------------------------------------------------


def test_cached_flow_replays_as_scripted_and_passes(tmp_path):
    cache, cached = seeded(tmp_path)
    driver = make_driver(tmp_path)
    healer = healer_for(tmp_path, cached, cache)
    result = replay(tmp_path, cached, driver, healer=healer)
    healer.finish(result)
    assert result.status == "passed"
    assert [s.status for s in result.steps] == ["passed", "passed"]
    assert "click:Settings" in driver.actions_log and "click:Dark Mode" in driver.actions_log
    assert healer.attempts == []
    assert cache.load(shard_key(cached)).hits == 1


def test_renamed_button_heals_and_the_flow_is_rewritten(tmp_path):
    cache, cached = seeded(tmp_path)
    key = shard_key(cached)
    driver = make_driver(tmp_path, settings_label="Preferences")
    seen_goals: list[str] = []

    class Spy(FakeModelProvider):
        def decide_step(self, obs, goal, history, *, timeout_s=30.0):
            seen_goals.append(goal)
            return super().decide_step(obs, goal, history, timeout_s=timeout_s)

    provider = Spy(steps=[tap("Preferences"), StepDecision(kind="done")])
    healer = healer_for(tmp_path, cached, cache, provider=provider)
    result = replay(tmp_path, cached, driver, healer=healer)
    healer.finish(result)

    assert result.status == "passed" and result.findings == []
    assert [s.status for s in result.steps] == ["passed", "skipped"]
    assert "healed" in result.steps[0].message
    assert "click:Preferences" in driver.actions_log and "click:Dark Mode" in driver.actions_log
    assert healer.healed
    assert seen_goals and seen_goals[0].startswith(GOAL) and "step 1" in seen_goals[0]

    document = json.loads(cache.flow_path(key).read_text())
    assert labels(document["steps"]) == ["Preferences", "Dark Mode"]
    flow = cache.load(key)
    assert flow.heals == 1 and flow.hits == 1 and flow.goal == GOAL

    # The rewritten flow replays on the changed app with no heal.
    [again] = cached_shards(config_for(tmp_path), [goal_shard()], cache=cache)
    rerun = replay(tmp_path, again, make_driver(tmp_path, settings_label="Preferences"), worker="w3")
    assert rerun.status == "passed"


def test_heal_keeps_the_steps_before_the_failed_one(tmp_path):
    cache, cached = seeded(tmp_path)
    driver = make_driver(tmp_path, toggle="Night Mode")
    provider = FakeModelProvider(steps=[tap("Night Mode"), StepDecision(kind="done")])
    healer = healer_for(tmp_path, cached, cache, provider=provider)
    result = replay(tmp_path, cached, driver, healer=healer)
    assert result.status == "passed"
    assert [s.status for s in result.steps] == ["passed", "passed"]
    assert labels(cache.load(shard_key(cached)).steps) == ["Settings", "Night Mode"]


def test_unhealable_step_drops_the_entry_and_the_failure_stands(tmp_path):
    cache, cached = seeded(tmp_path)
    driver = make_driver(tmp_path, settings_label="Preferences")
    provider = FakeModelProvider(steps=[StepDecision(kind="give_up")])
    healer = healer_for(tmp_path, cached, cache, provider=provider)
    result = replay(tmp_path, cached, driver, healer=healer)
    healer.finish(result)
    assert result.status == "failed"
    assert [f.kind for f in result.findings] == ["missing_control"]
    assert [s.status for s in result.steps] == ["failed", "skipped"]
    assert len(healer.attempts) == 1 and healer.attempts[0].dropped
    assert cache.load(shard_key(cached)) is None
    assert cached_shards(config_for(tmp_path), [goal_shard()], cache=cache)[0].kind == "exploratory"


def test_heal_is_capped_at_heal_max_steps(tmp_path):
    cache, cached = seeded(tmp_path)
    driver = make_driver(tmp_path, settings_label="Preferences")
    provider = FakeModelProvider(steps=[tap("Profile"), tap("Home")] * 10)
    healer = healer_for(tmp_path, cached, cache, provider=provider, heal_max_steps=2)
    result = replay(tmp_path, cached, driver, healer=healer)
    assert result.status == "failed"
    assert "max_steps" in healer.attempts[0].reason
    assert cache.load(shard_key(cached)) is None


def test_a_crash_is_a_finding_not_a_heal(tmp_path):
    cache, cached = seeded(tmp_path)
    driver = make_driver(tmp_path)
    driver.crash_labels.add("Settings")
    healer = healer_for(tmp_path, cached, cache, provider=FakeModelProvider())
    result = replay(tmp_path, cached, driver, healer=healer)
    assert result.status == "failed" and result.findings[0].kind == "crash"
    assert healer.attempts == []
    assert cache.load(shard_key(cached)) is not None


def test_heal_off_leaves_the_failure_and_the_entry(tmp_path):
    cache, cached = seeded(tmp_path)
    driver = make_driver(tmp_path, settings_label="Preferences")
    healer = healer_for(tmp_path, cached, cache, provider=FakeModelProvider(), heal=False)
    result = replay(tmp_path, cached, driver, healer=healer)
    assert result.status == "failed" and healer.attempts == []
    assert cache.load(shard_key(cached)) is not None


def test_healer_is_inactive_for_shards_that_are_not_cached(tmp_path):
    shard = Shard(id="s1", kind="scripted", name="save", actions=[Action(action="click", target=ElementQuery(label="X"))])
    healer = Healer.for_shard(shard, config_for(tmp_path), work_dir=tmp_path / "heal")
    assert not healer.active
    result = replay(tmp_path, shard, make_driver(tmp_path), healer=healer)
    healer.finish(result)
    assert result.status == "failed"


# The scripted hook ----------------------------------------------------------


def _broken_shard() -> Shard:
    return Shard(
        id="s1",
        kind="scripted",
        name="broken",
        actions=[
            Action(action="click", target=ElementQuery(label="Settings")),
            Action(action="click", target=ElementQuery(label="Missing")),
            Action(action="click", target=ElementQuery(label="About")),
        ],
    )


def _summary(result):
    return (
        result.status,
        [(s.index, s.action, s.status, s.message) for s in result.steps],
        [(f.id, f.kind, f.title, f.steps) for f in result.findings],
    )


def test_scripted_without_the_hook_is_unchanged(tmp_path):
    plain = replay(tmp_path / "a", _broken_shard(), make_driver(tmp_path / "a"))
    calls: list[StepFailure] = []

    def decline(failure: StepFailure):
        calls.append(failure)
        return None

    def broken(failure: StepFailure):
        raise RuntimeError("boom")

    declined = replay(tmp_path / "b", _broken_shard(), make_driver(tmp_path / "b"), healer=decline)
    raised = replay(tmp_path / "c", _broken_shard(), make_driver(tmp_path / "c"), healer=broken)
    assert _summary(plain) == _summary(declined) == _summary(raised)
    assert [s.status for s in plain.steps] == ["passed", "failed", "skipped"]
    assert [(c.index, c.description, type(c.error).__name__) for c in calls] == [
        (1, 'click "Missing"', "ElementNotFoundError")
    ]


def test_scripted_hook_note_ends_the_run_as_healed(tmp_path):
    result = replay(tmp_path, _broken_shard(), make_driver(tmp_path), healer=lambda failure: "fixed")
    assert result.status == "passed" and result.findings == []
    assert [(s.status, s.message) for s in result.steps] == [
        ("passed", 'click "Settings"'),
        ("passed", 'click "Missing": healed: fixed'),
        ("skipped", "replaced by the healed steps"),
    ]

"""WP-D3: device leases, a crawl split across devices, merged screen graphs."""

from __future__ import annotations

from pathlib import Path

import pytest

from swarmqa.config import validate_config
from swarmqa.devices.fake import FakeDevicePool
from swarmqa.driver.fake import FakeDriver
from swarmqa.errors import ConfigError
from swarmqa.explorer.agent_loop import AgentLoopSettings, run_agent_loop
from swarmqa.explorer.screen_graph import ScreenGraph
from swarmqa.models import Shard, UIElement
from swarmqa.orchestrator.campaign import run_campaign
from swarmqa.orchestrator.swarm import (
    CRAWL_TAG,
    LeasingDriverFactory,
    SharedCrawl,
    SwarmContext,
    SwarmSettings,
    expand_crawl,
)
from swarmqa.testing import make_app, sample_config


def screen(title: str | None, *controls: tuple[str, str]) -> list[UIElement]:
    children = [UIElement(role="navigationbar", label=title)] if title else []
    children += [UIElement(role=role, label=label, identifier=label.lower()) for role, label in controls]
    return [UIElement(role="window", label="Demo", children=children)]


HOME = screen(None, ("button", "Settings"), ("button", "Profile"))
SETTINGS = screen("Settings", ("button", "Home"), ("switch", "Dark Mode"), ("button", "About"))
ABOUT = screen("About", ("button", "Settings"))
PROFILE = screen("Profile", ("button", "Home"), ("textfield", "Name"), ("button", "Save"))
TRANSITIONS = {"Settings": SETTINGS, "About": ABOUT, "Profile": PROFILE, "Home": HOME}


class App(FakeDriver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transitions = dict(TRANSITIONS)

    def launch(self) -> None:
        super().launch()
        self.tree = list(HOME)


def ui_actions(driver: FakeDriver) -> list[str]:
    return [a for a in driver.actions_log if a.startswith(("click:", "type:"))]


def crawl(tmp_path, shared, worker: str, max_steps: int = 60):
    config = sample_config(make_app(tmp_path))
    work = tmp_path / "c" / "workers" / worker
    driver = App(config.app, work)
    shard = Shard(id=f"s-{worker}", kind="exploratory", name="crawl")
    result = run_agent_loop(
        shard,
        driver,
        config,
        settings=AgentLoopSettings(mode="crawl", max_steps=max_steps),
        work_dir=work,
        worker_id=worker,
        shared_crawl=shared,
    )
    return result, driver, ScreenGraph.load(work / "screen_graph.json")


def test_claims_go_to_the_first_worker():
    shared = SharedCrawl()
    assert shared.claim("s", "button|Save|", "w1")
    assert shared.claim("s", "button|Save|", "w1")
    assert not shared.claim("s", "button|Save|", "w2")


def test_a_second_worker_skips_what_the_first_already_tried(tmp_path):
    shared = SharedCrawl()
    _, first, _ = crawl(tmp_path, shared, "w1", max_steps=2)
    assert ui_actions(first) == ["click:Settings", "click:Home"]
    _, second, graph = crawl(tmp_path, shared, "w2")
    # w1 claimed Settings and, on the settings screen, Home: w2 starts with Profile.
    assert ui_actions(second)[0] == "click:Profile"
    assert graph.coverage()["untried"] == 0
    assert "click:Settings" not in ui_actions(second)[:2]


def test_imported_screens_and_routes_start_at_zero():
    shared = SharedCrawl()
    found = ScreenGraph()
    found.add_screen("home", [])
    found.add_screen("settings", [])
    found.add_edge("home", {"kind": "tap"}, "settings")
    shared.exchange(found, "w1")
    fresh = ScreenGraph()
    shared.exchange(fresh, "w2")
    assert fresh.nodes["settings"].visits == 0
    assert [edge.count for edge in fresh.edges.values()] == [0]


def test_expand_crawl_adds_one_group_once():
    settings = SwarmSettings(crawl=3)
    queue = expand_crawl([Shard(id="s-1-a", kind="scripted", name="a")], settings)
    crawls = [s for s in queue if CRAWL_TAG in s.tags]
    assert [s.id for s in crawls] == ["s-crawl-1", "s-crawl-2", "s-crawl-3"]
    assert all(s.goal == "" and "crawl_group:crawl" in s.tags for s in crawls)
    assert expand_crawl(queue, settings) == queue
    context = SwarmContext()
    assert context.crawl_for(crawls[0]) is context.crawl_for(crawls[2])
    assert context.crawl_for(queue[0]) is None


def swarm_config(tmp_path: Path, **swarm):
    config = sample_config(make_app(tmp_path))
    config.report_root = str(tmp_path / "reports")
    config.explorer.engine = "agent"
    config.explorer.max_steps = 60
    config.workers = 4
    config.swarm.settings = {"enabled": True, "pool": "fake", **swarm}
    return config


def test_a_split_crawl_leases_devices_and_merges_one_graph(tmp_path):
    config = swarm_config(tmp_path, crawl=3)
    pool = FakeDevicePool({"macos": 2})
    drivers: list[FakeDriver] = []

    def factory(target, work_dir):
        drivers.append(App(target, work_dir))
        return drivers[-1]

    result = run_campaign(config, [], driver_factory=factory, device_pool=pool)
    # The fake app's Dark Mode and Save do nothing: dead-tap findings, not errors.
    assert len(result.results) == 3 and all(r.status in ("passed", "failed") for r in result.results)
    titles = {f.title for r in result.results for f in r.findings}
    assert titles == {'Tapping switch "Dark Mode" did nothing', 'Tapping button "Save" did nothing'}
    assert len(pool.released) == 3 and pool._leased == {}
    merged = ScreenGraph.load(Path(result.report_dir) / "screen_graph.json")
    stats = merged.coverage()
    assert stats["screens"] == 4 and stats["untried"] == 0
    # Three devices share one frontier: together they do about one crawl's work, not three.
    _, solo, _ = crawl(tmp_path, None, "solo")
    total = sum(len(ui_actions(d)) for d in drivers)
    assert total < 2 * len(ui_actions(solo))


def test_slots_are_capped_by_pool_capacity_and_empty_pools_are_an_error(tmp_path, capsys):
    config = swarm_config(tmp_path)
    seen = []

    def executor(shard, worker_id, work_dir, cfg):
        from swarmqa.models import WorkerResult

        seen.append(worker_id)
        return WorkerResult(worker_id=worker_id, shard_id=shard.id, status="passed")

    queue = [Shard(id=f"s-{i}", kind="scripted", name=str(i)) for i in range(3)]
    run_campaign(config, queue, executor=executor, device_pool=FakeDevicePool({"macos": 2}))
    assert "swarm: 2 macos device(s)" in capsys.readouterr().err
    with pytest.raises(ConfigError, match="no macos devices"):
        run_campaign(config, queue, executor=executor, device_pool=FakeDevicePool({"ios": 2}))


def test_leasing_factory_pins_a_simulator_and_releases_once(tmp_path):
    config = sample_config(make_app(tmp_path))
    config.app.platform = "ios"
    pool = FakeDevicePool({"ios": 1})
    targets = []

    def base(target, work_dir):
        targets.append(target)
        return FakeDriver(target, work_dir)

    factory = LeasingDriverFactory(pool, "ios", config, base=base, timeout_s=0.1)
    driver = factory(config.app, tmp_path / "w")
    assert targets[0].simulator == driver.device.id and targets[0].simulators == [driver.device.id]
    driver.launch()
    assert driver.launched  # attributes reach the real driver
    driver.close()
    driver.close()
    assert pool.released == [(driver.device.id, True)]


def test_a_failing_driver_still_returns_the_lease(tmp_path):
    config = sample_config(make_app(tmp_path))
    pool = FakeDevicePool({"macos": 1})

    def base(target, work_dir):
        raise RuntimeError("no runner")

    with pytest.raises(RuntimeError):
        LeasingDriverFactory(pool, "macos", config, base=base)(config.app, tmp_path)
    assert len(pool.released) == 1


def test_swarm_settings_validation():
    config = sample_config()
    config.swarm.settings = {"crawl": 2, "pol": "ios"}
    assert "swarm.pol: unknown key" in validate_config(config)
    config.swarm.settings = {"crawl": 2}
    assert 'swarm.crawl: needs explorer.engine = "agent"' in validate_config(config)
    config.explorer.engine = "agent"
    config.local.isolation = "subprocess"
    assert 'swarm: needs local.isolation = "thread"' in validate_config(config)
    config.local.isolation = "thread"
    config.swarm.settings = {"enabled": True, "pool": "ios"}
    assert "swarm.pool: ios does not match app.platform = macos" in validate_config(config)
    config.swarm.settings = {"enabled": True, "erase_mode": "wipe", "devices": -1}
    errors = validate_config(config)
    assert "swarm.erase_mode: must be one of uninstall, erase" in errors and "swarm.devices: must be >= 0" in errors

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
    SwarmConfigError,
    LeasingDriverFactory,
    SharedCrawl,
    SwarmContext,
    SwarmSettings,
    build_once,
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
    config.findings.settings = {"confirm": False}  # these tests count crawl actions only
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


# Defaults, retries and one build ---------------------------------------------


@pytest.mark.real_pool_default
def test_more_ios_workers_than_listed_simulators_are_swarmed():
    config = sample_config()
    config.app.platform = "ios"
    config.workers = 3
    config.app.simulators = ["iPhone 17"]
    assert SwarmSettings.from_config(config).enabled
    config.app.simulators = ["A", "B", "C"]
    assert not SwarmSettings.from_config(config).enabled
    config.app.simulators = []
    config.swarm.settings = {"enabled": False}
    assert not SwarmSettings.from_config(config).enabled  # an explicit choice wins
    config.swarm.settings = {}
    config.local.isolation = "subprocess"
    assert not SwarmSettings.from_config(config).enabled  # leases need thread isolation
    config.local.isolation = "thread"
    config.workers = 1
    assert not SwarmSettings.from_config(config).enabled


def test_max_concurrent_boots_is_validated():
    assert SwarmSettings.from_mapping({"max_concurrent_boots": 2}).max_concurrent_boots == 2
    with pytest.raises(SwarmConfigError):
        SwarmSettings.from_mapping({"max_concurrent_boots": -1})


def _result(shard, worker_id, status="passed", error=None):
    from swarmqa.models import WorkerResult

    return WorkerResult(worker_id=worker_id, shard_id=shard.id, status=status, error=error)


def test_a_shard_whose_device_failed_runs_once_more(tmp_path, capsys):
    config = swarm_config(tmp_path, devices=1)
    pool = FakeDevicePool({"macos": 1})
    attempts = []

    def executor(shard, worker_id, work_dir, cfg):
        attempts.append(worker_id)
        if len(attempts) == 1:
            return _result(shard, worker_id, "error", "BackendUnavailable: Simulator did not boot")
        return _result(shard, worker_id)

    result = run_campaign(config, [Shard(id="s-1", kind="scripted", name="one")], executor=executor, device_pool=pool)
    assert [r.status for r in result.results] == ["passed"]
    assert len(attempts) == 2
    assert "running the shard again on a fresh lease" in capsys.readouterr().err


def test_an_app_error_or_a_second_device_failure_is_not_retried(tmp_path):
    config = swarm_config(tmp_path, devices=1)
    attempts = []

    def app_error(shard, worker_id, work_dir, cfg):
        attempts.append(worker_id)
        return _result(shard, worker_id, "error", "AQAError: runner internal")

    run_campaign(config, [Shard(id="s-1", kind="scripted", name="one")], executor=app_error,
                 device_pool=FakeDevicePool({"macos": 1}))
    assert len(attempts) == 1

    attempts.clear()

    def device_error(shard, worker_id, work_dir, cfg):
        attempts.append(worker_id)
        return _result(shard, worker_id, "error", "BackendUnavailable: gone")

    result = run_campaign(config, [Shard(id="s-1", kind="scripted", name="one")], executor=device_error,
                          device_pool=FakeDevicePool({"macos": 1}))
    assert len(attempts) == 2 and result.results[0].status == "error"


def test_the_app_is_built_once_for_every_device(tmp_path):
    app = make_app(tmp_path / "built")
    config = sample_config()
    config.app.path = None
    config.app.bundle_id = None
    config.app.build_command = "build-it"
    calls = []

    def runner(args, **kwargs):
        from subprocess import CompletedProcess

        calls.append(args)
        return CompletedProcess(args, 0, f"** BUILD SUCCEEDED **\n{app.path}\n", "")

    build_once(config, runner=runner)
    assert calls == [["/bin/sh", "-c", "build-it"]]
    assert config.app.path == app.path

"""WP-D3 swarm scheduler: [swarm] settings, device leases, a shared frontier, one campaign graph."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from swarmqa.backends.local import create_device_driver, device_driver_factory, device_env
from swarmqa.devices.fake import FakeDevicePool
from swarmqa.devices.protocol import Device, DeviceSetupError, DeviceUnavailable
from swarmqa.driver.fake import FakeDriver
from swarmqa.explorer.screen_graph import ScreenGraph, actionable_controls
from swarmqa.models import Shard, UIElement, WorkerResult
from swarmqa.orchestrator.campaign import run_campaign
from swarmqa.orchestrator.swarm import (
    CampaignGraph,
    FrontierBoard,
    Swarm,
    SwarmConfigError,
    SwarmSettings,
    build_artifact,
    create_pool,
    pool_errors,
)
from swarmqa.testing import make_app, sample_config, sample_tree, scripted_shard

# A four-screen app (the one in tests/test_agent_loop.py): Home has Settings
# and Profile; Settings has Dark Mode and About; Profile has Name and Save.
# Back buttons carry the previous screen's title as their label.


def _screen(title: str | None, *controls: UIElement) -> list[UIElement]:
    children: list[UIElement] = []
    if title is not None:
        back = "Settings" if title == "About" else "Home"
        children.append(
            UIElement(
                role="navigationbar",
                label=title,
                children=[UIElement(role="button", label=back, identifier="back")],
            )
        )
    else:
        children.append(UIElement(role="text", label="Welcome"))
    children.extend(controls)
    return [UIElement(role="window", label="Demo", children=children)]


def home():
    return _screen(
        None,
        UIElement(role="button", label="Settings", identifier="settings"),
        UIElement(role="button", label="Profile", identifier="profile"),
    )


def settings_screen():
    return _screen(
        "Settings",
        UIElement(role="switch", label="Dark Mode", identifier="dark"),
        UIElement(role="button", label="About", identifier="about"),
    )


def about():
    return _screen("About", UIElement(role="text", label="Version 1.0"))


def profile():
    return _screen(
        "Profile",
        UIElement(role="textfield", label="Name", identifier="name"),
        UIElement(role="button", label="Save", identifier="save"),
    )


ALL_CONTROLS = 9  # Settings, Profile | Home, Dark Mode, About | Settings | Home, Name, Save
ALL_PAIRS = {
    ("Home", "Settings"),
    ("Home", "Profile"),
    ("Settings", "Home"),
    ("Settings", "Dark Mode"),
    ("Settings", "About"),
    ("About", "Settings"),
    ("Profile", "Home"),
    ("Profile", "Name"),
    ("Profile", "Save"),
}


def _title(tree: list[UIElement]) -> str:
    for child in tree[0].children:
        if child.role == "navigationbar":
            return child.label or ""
    return "Home"


class AppFake(FakeDriver):
    """The four-screen app. Records (screen, label) per action; `delay_s` slows every action."""

    delay_s = 0.0

    def __init__(self, *args, device: Device | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.device = device
        self.touched: list[tuple[str, str]] = []
        self.set_tree(home())
        self.transitions = {"Settings": settings_screen(), "About": about(), "Profile": profile(), "Home": home()}

    def launch(self) -> None:
        super().launch()
        self.tree = home()

    def click(self, target) -> None:
        self.touched.append((_title(self.tree), target.label or ""))
        if self.delay_s:
            time.sleep(self.delay_s)
        super().click(target)

    def type_text(self, target, text) -> None:
        self.touched.append((_title(self.tree), target.label or ""))
        if self.delay_s:
            time.sleep(self.delay_s)
        super().type_text(target, text)


class CountingPool(FakeDevicePool):
    def __init__(self, capacity=None, *, fail: Exception | None = None):
        super().__init__(capacity)
        self.fail = fail
        self.acquired: list[str] = []
        self.closed = 0
        self.lock = threading.Lock()

    def acquire(self, platform, build, *, timeout_s=600.0):
        if self.fail is not None:
            raise self.fail
        device = super().acquire(platform, build, timeout_s=timeout_s)
        with self.lock:
            self.acquired.append(device.id)
        return device

    def close(self) -> None:
        self.closed += 1
        super().close()


def _agent_config(tmp_path: Path, *, workers: int = 2, swarm: dict | None = None):
    config = sample_config(make_app(tmp_path))
    config.report_root = str(tmp_path / "reports")
    config.workers = workers
    config.driver.kind = "fake"
    config.explorer.engine = "agent"
    config.explorer.max_steps = 60
    config.swarm.settings = dict(swarm or {})
    return config


def _crawl(shard_id: str) -> Shard:
    return Shard(id=shard_id, kind="exploratory", name=shard_id)


class Factory:
    """A driver factory that takes the leased device and keeps every driver it made."""

    def __init__(self, cls=AppFake, delay_s: float = 0.0):
        self.cls = cls
        self.delay_s = delay_s
        self.drivers: list[AppFake] = []
        self.lock = threading.Lock()

    def __call__(self, target, work_dir, device=None):
        driver = self.cls(target, work_dir, device=device)
        driver.delay_s = self.delay_s
        with self.lock:
            self.drivers.append(driver)
        return driver


def _passed(shard: Shard, worker_id: str) -> WorkerResult:
    return WorkerResult(
        worker_id=worker_id, shard_id=shard.id, shard_name=shard.name, shard_kind=shard.kind, status="passed"
    )


# Settings -----------------------------------------------------------------


def test_settings_default_to_no_pool_and_nothing_shared():
    settings = SwarmSettings.from_mapping(None)
    assert settings.pool == "none"
    assert settings.devices is None
    assert not settings.uses_pool and not settings.frontier_shared and not settings.graphs_merged
    assert not settings.active


def test_settings_turn_sharing_on_with_a_pool_unless_set_off():
    settings = SwarmSettings.from_mapping({"pool": "ios", "devices": 6, "golden": "iPhone 17"})
    assert settings.frontier_shared and settings.graphs_merged
    off = SwarmSettings.from_mapping({"pool": "ios", "share_frontier": False})
    assert not off.frontier_shared and off.graphs_merged
    no_pool = SwarmSettings.from_mapping({"merge_graphs": True})
    assert no_pool.graphs_merged and not no_pool.frontier_shared and no_pool.active


def test_settings_reject_unknown_keys_and_bad_values():
    with pytest.raises(SwarmConfigError) as caught:
        SwarmSettings.from_mapping(
            {
                "pool": "android",
                "devices": 0,
                "acquire_timeout_s": True,
                "share_frontier": "yes",
                "golden": "",
                "erase_mode": "wipe",
                "simulators": 3,
            }
        )
    errors = caught.value.errors
    assert "swarm.simulators: unknown key" in errors
    assert any(e.startswith("swarm.pool:") for e in errors)
    assert any(e.startswith("swarm.devices:") for e in errors)
    assert any(e.startswith("swarm.acquire_timeout_s:") for e in errors)
    assert any(e.startswith("swarm.share_frontier:") for e in errors)
    assert any(e.startswith("swarm.golden:") for e in errors)
    assert any(e.startswith("swarm.erase_mode:") for e in errors)


def test_settings_from_config_reads_the_raw_table(tmp_path):
    config = _agent_config(tmp_path, swarm={"pool": "auto", "acquire_timeout_s": 30})
    settings = SwarmSettings.from_config(config)
    assert settings.pool == "auto" and settings.acquire_timeout_s == 30.0


def test_create_pool_builds_the_named_pool(tmp_path):
    from swarmqa.devices.ios_pool import IOSSimulatorPool
    from swarmqa.devices.local_mac import LocalMacPool

    config = _agent_config(tmp_path)
    assert create_pool(config, SwarmSettings()) is None
    assert isinstance(create_pool(config, SwarmSettings(pool="auto")), LocalMacPool)
    with pytest.raises(SwarmConfigError):
        create_pool(config, SwarmSettings(pool="ios"))
    with pytest.raises(SwarmConfigError):
        create_pool(config, SwarmSettings(pool="tart"))
    config.app.platform = "ios"
    pool = create_pool(config, SwarmSettings(pool="auto", devices=3), runner=lambda *a, **k: None)
    assert isinstance(pool, IOSSimulatorPool)
    assert pool.device_type == "iPhone 17" and pool.golden is None
    golden = create_pool(config, SwarmSettings(pool="ios", golden="Golden 17"))
    assert golden.golden == "Golden 17"
    with pytest.raises(SwarmConfigError):
        create_pool(config, SwarmSettings(pool="local_mac"))


def test_pool_errors_explain_a_mismatch_without_building_a_pool(tmp_path):
    config = _agent_config(tmp_path)
    assert pool_errors(config, SwarmSettings()) == []
    assert pool_errors(config, SwarmSettings(pool="local_mac")) == []
    assert pool_errors(config, SwarmSettings(pool="ios")) == [
        'swarm.pool: "ios" needs app.platform = "ios", not \'macos\''
    ]
    assert pool_errors(config, SwarmSettings(pool="tart")) == ['swarm.image: required when swarm.pool = "tart"']
    assert pool_errors(config, SwarmSettings(pool="auto", image="base")) == []


def test_build_artifact_comes_from_app_path(tmp_path):
    config = _agent_config(tmp_path)
    build = build_artifact(config)
    assert build is not None
    assert build.app_path == config.app.path and build.bundle_id == "dev.swarmqa.sample"
    config.app.path = None
    assert build_artifact(config) is None


# Frontier board and campaign graph ------------------------------------------


def test_frontier_board_first_claimer_wins_and_settle_frees_untried():
    board = FrontierBoard()
    assert board.claim("w1", "home", "Settings") is True
    assert board.claim("w1", "home", "Settings") is True
    assert board.claim("w2", "home", "Settings") is False
    assert board.claim("w2", "home", "Profile") is True
    assert board.refused("w2") == {("home", "Settings")}
    assert board.claim("w1", "home", "About") is True
    freed = board.settle("w1", tried={("home", "Settings")})
    assert freed == {("home", "About")}
    assert board.claim("w3", "home", "About") is True
    assert board.claim("w3", "home", "Settings") is False
    assert board.settle("w2", None) == {("home", "Profile")}


def test_frontier_board_is_thread_safe():
    board = FrontierBoard()
    wins: list[str] = []
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        barrier.wait()
        for key in range(200):
            if board.claim(f"w{index}", "s", str(key)):
                wins.append(str(key))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(wins, key=int) == [str(key) for key in range(200)]


def test_campaign_graph_keeps_refused_controls_untried(tmp_path):
    graph = ScreenGraph()
    graph.add_screen("home", actionable_controls(home()))
    node = graph.nodes["home"]
    keys = [control.key for control in node.controls]
    for key in keys:
        graph.mark_tried("home", key)
    campaign = CampaignGraph.open(tmp_path)
    assert campaign.snapshot() is None
    campaign.merge_worker("w1", graph, untried={("home", keys[1])})
    stats = campaign.coverage()
    assert stats["tried"] == 1 and stats["controls"] == 2
    assert campaign.save() == tmp_path / "screen_graph.json"
    assert CampaignGraph.open(tmp_path).coverage()["tried"] == 1
    snapshot = campaign.snapshot()
    assert snapshot is not None and snapshot is not campaign.graph


def test_goal_shards_get_the_seed_graph_but_no_claim(tmp_path):
    config = _agent_config(tmp_path, swarm={"share_frontier": True, "merge_graphs": True})
    swarm = Swarm.open(config, tmp_path)
    graph = ScreenGraph()
    graph.add_screen("home", actionable_controls(home()))
    swarm.graph.merge_worker("w0", graph)
    goal = swarm.context(Shard(id="g", kind="exploratory", name="g", goal="Open About"), "w1")
    crawl = swarm.context(_crawl("c"), "w2")
    scripted = swarm.context(scripted_shard(), "w3")
    assert goal.seed_graph is not None and goal.claim is None
    assert crawl.seed_graph is not None and crawl.claim is not None
    assert scripted.seed_graph is None and scripted.claim is None


def test_subprocess_isolation_shares_nothing_in_memory(tmp_path):
    config = _agent_config(tmp_path, swarm={"share_frontier": True, "merge_graphs": True})
    config.local.isolation = "subprocess"
    swarm = Swarm.open(config, tmp_path)
    assert swarm.board is None and swarm.graph is None and not swarm.active


# Leased devices reach the driver ---------------------------------------------


def test_device_reaches_a_factory_that_asks_for_it(tmp_path):
    config = _agent_config(tmp_path)
    device = Device(id="UDID-1", platform="ios", kind="simulator")
    seen = []

    def wants(target, work_dir, *, device):
        seen.append(device)
        return "driver"

    def plain(target, work_dir):
        return "plain"

    assert device_driver_factory(config, device, wants)(config.app, tmp_path) == "driver"
    assert seen == [device]
    assert device_driver_factory(config, device, plain)(config.app, tmp_path) == "plain"


def test_default_factory_pins_ios_drivers_to_the_leased_udid(tmp_path):
    from swarmqa.driver.ios import IOSSimulatorDriver
    from swarmqa.driver.ios_runner import IOSRunnerDriver

    config = _agent_config(tmp_path)
    config.app.platform = "ios"
    device = Device(id="0A1B2C3D-0000-0000-0000-000000000001", platform="ios", kind="simulator")
    config.driver.kind = "auto"
    legacy = create_device_driver(config, config.app, tmp_path / "w1", device)
    assert isinstance(legacy, IOSSimulatorDriver) and legacy._udid_override == device.id
    config.driver.kind = "runner"
    runner = create_device_driver(config, config.app, tmp_path / "w2", device)
    assert isinstance(runner, IOSRunnerDriver) and runner._udid_override == device.id
    config.driver.kind = "fake"
    assert isinstance(create_device_driver(config, config.app, tmp_path / "w3", device), FakeDriver)
    vm = Device(id="aqa-vm-1", platform="macos", kind="vm", address="10.0.0.2")
    with pytest.raises(Exception, match="vm backend"):
        create_device_driver(config, config.app, tmp_path / "w4", vm)


def test_subprocess_workers_get_the_udid_in_their_env():
    assert device_env(Device(id="U1", platform="ios", kind="simulator")) == {"SWARMQA_SIMULATOR_UDID": "U1"}
    assert device_env(Device(id="host", platform="macos", kind="host")) == {}
    assert device_env(None) == {}


# Campaigns ----------------------------------------------------------------


def test_pool_none_is_unchanged(tmp_path, capsys):
    """No [swarm] table and an explicit pool = "none" run the same campaign:
    same progress lines, no lease step, no campaign graph, no extra summary."""

    def run(name: str, table: dict | None):
        config = _agent_config(tmp_path / name, workers=1, swarm=table)
        factory = Factory()
        result = run_campaign(config, [_crawl("c1")], driver_factory=lambda t, w: factory(t, w))
        err = capsys.readouterr().err
        return result, err

    first, err_a = run("a", None)
    second, err_b = run("b", {"pool": "none"})
    strip = lambda text, root: text.replace(root, "<root>").replace(Path(root).name, "<id>")
    assert strip(err_a, first.report_dir) == strip(err_b, second.report_dir)
    assert "step=lease" not in err_a
    for result in (first, second):
        report = Path(result.report_dir)
        assert not (report / "screen_graph.json").exists()
        summary = (report / "summary.md").read_text(encoding="utf-8")
        assert "## Screen graph" not in summary and "## Devices" not in summary


def test_slots_are_capped_by_pool_capacity(tmp_path):
    config = _agent_config(tmp_path, workers=4, swarm={"merge_graphs": False, "share_frontier": False})
    pool = CountingPool({"macos": 2})
    lock = threading.Lock()
    state = {"current": 0, "max": 0}

    def executor(shard, worker_id, work_dir, _config):
        with lock:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
        time.sleep(0.05)
        with lock:
            state["current"] -= 1
        return _passed(shard, worker_id)

    shards = [scripted_shard() for _ in range(5)]
    for index, shard in enumerate(shards):
        shard.id = f"s-{index}"
    result = run_campaign(config, shards, executor=executor, device_pool=pool)
    assert state["max"] == 2
    status = json.loads((Path(result.report_dir) / "status.json").read_text(encoding="utf-8"))
    assert status["workers_configured"] == 2
    assert len(pool.acquired) == 5
    summary = (Path(result.report_dir) / "summary.md").read_text(encoding="utf-8")
    assert "## Devices" in summary and "- leases: 5" in summary


def test_devices_setting_caps_slots_below_capacity(tmp_path):
    config = _agent_config(tmp_path, workers=4, swarm={"devices": 1})
    swarm = Swarm.open(config, tmp_path, pool=FakeDevicePool({"macos": 3}))
    assert swarm.slots(config.workers) == 1
    assert Swarm.open(config, tmp_path).slots(4) == 4  # no pool: config.workers


def test_each_shard_leases_and_releases_once_even_when_it_raises(tmp_path, capsys):
    config = _agent_config(tmp_path, workers=2)
    pool = CountingPool({"macos": 2})

    def executor(shard, worker_id, work_dir, _config):
        if shard.id == "s-boom":
            raise RuntimeError("worker exploded")
        return _passed(shard, worker_id)

    shards = [
        Shard(id="s-ok", kind="scripted", name="ok"),
        Shard(id="s-boom", kind="scripted", name="boom"),
        Shard(id="s-suite", kind="suite", name="suite", suite_command="true"),
    ]
    result = run_campaign(config, shards, executor=executor, device_pool=pool)
    by_id = {item.shard_id: item for item in result.results}
    assert by_id["s-ok"].status == "passed"
    assert by_id["s-boom"].status == "error" and "worker exploded" in by_id["s-boom"].error
    assert len(pool.acquired) == 2  # the suite shard needs no device
    assert sorted(device for device, _erase in pool.released) == sorted(pool.acquired)
    assert all(erase for _device, erase in pool.released)
    assert pool._leased == {}
    assert pool.closed == 1
    err = capsys.readouterr().err
    assert "shard=ok mode=scripted step=lease" in err


def test_erase_on_release_false_is_passed_to_the_pool(tmp_path):
    config = _agent_config(tmp_path, workers=1, swarm={"erase_on_release": False})
    pool = CountingPool({"macos": 1})
    run_campaign(config, [scripted_shard()], executor=lambda s, w, d, c: _passed(s, w), device_pool=pool)
    assert pool.released and all(erase is False for _device, erase in pool.released)


@pytest.mark.parametrize(
    "failure, words",
    [
        (DeviceUnavailable("no macos device free after 1s"), "no macos device available"),
        (DeviceSetupError("simctl install failed"), "device setup failed: simctl install failed"),
    ],
)
def test_lease_failures_become_error_results(tmp_path, failure, words):
    config = _agent_config(tmp_path, workers=1)
    pool = CountingPool({"macos": 1}, fail=failure)
    called = []
    result = run_campaign(
        config,
        [scripted_shard()],
        executor=lambda s, w, d, c: called.append(s) or _passed(s, w),
        device_pool=pool,
    )
    item = result.results[0]
    assert item.status == "error" and words in item.error
    assert called == []
    assert pool.closed == 1
    assert result.exit_code == 1


def test_a_pool_without_devices_for_the_platform_fails_fast(tmp_path):
    config = _agent_config(tmp_path, workers=1)
    pool = CountingPool({"ios": 2})
    started = time.monotonic()
    result = run_campaign(config, [scripted_shard()], executor=lambda s, w, d, c: _passed(s, w), device_pool=pool)
    assert time.monotonic() - started < 5
    assert result.results[0].status == "error" and "pool has none" in result.results[0].error


@pytest.mark.parametrize("workers", [2, 3])
def test_concurrent_crawl_workers_split_the_frontier(tmp_path, workers):
    config = _agent_config(tmp_path, workers=workers, swarm={"pool": "local_mac"})
    pool = CountingPool({"macos": workers})
    factory = Factory(delay_s=0.03)
    shards = [_crawl(f"crawl-{index}") for index in range(workers)]
    result = run_campaign(config, shards, driver_factory=factory, device_pool=pool)
    # "failed" is the functional check flagging the fake's dead Dark Mode and Save controls.
    assert all(item.status in {"passed", "failed"} for item in result.results)

    # Every driver got its own leased device.
    devices = [driver.device.id for driver in factory.drivers]
    assert len(set(devices)) == workers and set(devices) == set(pool.acquired)

    # No control was tried by two workers, and together they tried everything.
    owners: dict[tuple[str, str], set[int]] = {}
    for index, driver in enumerate(factory.drivers):
        for pair in driver.touched:
            owners.setdefault(pair, set()).add(index)
    assert set(owners) == ALL_PAIRS
    assert all(len(who) == 1 for who in owners.values()), owners
    assert sum(1 for driver in factory.drivers if driver.touched) >= 2

    graph = ScreenGraph.load(Path(result.report_dir) / "screen_graph.json")
    stats = graph.coverage()
    assert stats["screens"] == 4
    assert stats["controls"] == ALL_CONTROLS and stats["tried"] == ALL_CONTROLS
    assert stats["ratio"] == 1.0
    summary = (Path(result.report_dir) / "summary.md").read_text(encoding="utf-8")
    assert f"controls tried: {ALL_CONTROLS} of {ALL_CONTROLS} (100%)" in summary
    assert pool.closed == 1


def test_without_share_frontier_workers_crawl_the_same_controls(tmp_path):
    config = _agent_config(tmp_path, workers=2, swarm={"pool": "local_mac", "share_frontier": False})
    factory = Factory(delay_s=0.02)
    run_campaign(config, [_crawl("a"), _crawl("b")], driver_factory=factory, device_pool=CountingPool({"macos": 2}))
    first, second = (set(driver.touched) for driver in factory.drivers)
    assert first == second == ALL_PAIRS


def test_seed_graph_reaches_later_workers(tmp_path):
    config = _agent_config(tmp_path, workers=1, swarm={"pool": "local_mac"})
    factory = Factory()
    result = run_campaign(
        config, [_crawl("first"), _crawl("second")], driver_factory=factory, device_pool=CountingPool({"macos": 1})
    )
    first, second = factory.drivers
    assert set(first.touched) == ALL_PAIRS
    assert second.touched == []  # the seed already had every control tried
    graph = ScreenGraph.load(Path(result.report_dir) / "screen_graph.json")
    assert graph.coverage()["tried"] == ALL_CONTROLS


def test_finish_frees_claims_a_worker_never_tried(tmp_path):
    """A worker that ends (say, in error) after claiming controls it never
    tried hands them back, and its refusals stay untried in the campaign graph."""
    config = _agent_config(tmp_path, swarm={"share_frontier": True, "merge_graphs": True})
    swarm = Swarm.open(config, tmp_path)
    controls = actionable_controls(home())
    settings_key, profile_key = (control.key for control in controls)
    graph = ScreenGraph()
    graph.add_screen("home", controls)
    graph.mark_tried("home", settings_key)
    graph.mark_tried("home", profile_key)  # only because w2 holds it, see below
    work = tmp_path / "workers" / "w1"
    graph.save(work / "screen_graph.json")
    assert swarm.board.claim("w1", "home", settings_key)
    assert swarm.board.claim("w1", "settings", "button|Dark Mode|dark")  # claimed, never tried
    assert swarm.board.claim("w2", "home", profile_key)
    assert not swarm.board.claim("w1", "home", profile_key)
    swarm.finish("w1", work)
    assert swarm.board.claim("w3", "settings", "button|Dark Mode|dark") is True
    assert swarm.board.claim("w3", "home", settings_key) is False
    stats = swarm.graph.coverage()
    assert stats["tried"] == 1 and stats["controls"] == 2
    # w2 dies before writing a graph: all of its claims go back.
    swarm.finish("w2", tmp_path / "workers" / "w2")
    assert swarm.board.claim("w3", "home", profile_key) is True
    swarm.close()
    assert (tmp_path / "screen_graph.json").is_file()


def test_merge_graphs_without_a_pool(tmp_path):
    config = _agent_config(tmp_path, workers=1, swarm={"merge_graphs": True})
    factory = Factory()
    result = run_campaign(config, [_crawl("c1")], driver_factory=lambda t, w: factory(t, w))
    assert (Path(result.report_dir) / "screen_graph.json").is_file()
    assert factory.drivers[0].device is None


def test_injected_pool_is_closed_when_the_campaign_fails_to_schedule(tmp_path):
    config = _agent_config(tmp_path, workers=1)
    pool = CountingPool({"macos": 1})
    run_campaign(config, [], device_pool=pool)
    assert pool.closed == 1


# Speed --------------------------------------------------------------------


def _timed_campaign(tmp_path: Path, devices: int, shards: int, delay_s: float) -> float:
    config = _agent_config(tmp_path, workers=6, swarm={"share_frontier": False, "merge_graphs": False})
    config.explorer.engine = "heuristic"

    class Slow(FakeDriver):
        def __init__(self, target, work_dir, device=None):
            super().__init__(target, work_dir)
            self.set_tree(sample_tree())

        def click(self, target):
            time.sleep(delay_s)
            super().click(target)

    queue = []
    for index in range(shards):
        shard = scripted_shard()
        shard.id = f"s-{index}"
        shard.name = f"save-{index}"
        queue.append(shard)
    started = time.monotonic()
    result = run_campaign(config, queue, driver_factory=Slow, device_pool=FakeDevicePool({"macos": devices}))
    elapsed = time.monotonic() - started
    assert all(item.status == "passed" for item in result.results)
    return elapsed


def test_n_devices_finish_about_n_times_faster(tmp_path):
    """Six shards with one 0.2 s step each. Measured on an M-series Mac:
    1 device 1.33 s, 3 devices 0.52 s (2.6x), 6 devices 0.27 s (4.9x)."""
    _timed_campaign(tmp_path / "warm", devices=6, shards=1, delay_s=0.0)  # imports, first-run costs
    one = _timed_campaign(tmp_path / "one", devices=1, shards=6, delay_s=0.3)
    three = _timed_campaign(tmp_path / "three", devices=3, shards=6, delay_s=0.3)
    assert one >= 1.8
    # Ideal is 3x; leave room for a loaded CI host.
    assert one / three > 1.8, (one, three)


# Defaults, retries and one build -------------------------------------------------


@pytest.mark.real_pool_default
def test_more_ios_workers_than_listed_simulators_get_a_pool(tmp_path):
    config = _agent_config(tmp_path, workers=3)
    config.app.platform = "ios"
    config.app.simulators = ["iPhone 17"]
    assert SwarmSettings.from_config(config).pool == "auto"
    config.app.simulators = ["A", "B", "C"]
    assert SwarmSettings.from_config(config).pool == "none"
    config.app.simulators = []
    config.swarm.settings = {"pool": "none"}
    assert SwarmSettings.from_config(config).pool == "none"  # an explicit choice wins
    config.swarm.settings = {}
    config.workers = 1
    assert SwarmSettings.from_config(config).pool == "none"


def test_max_concurrent_boots_is_validated():
    assert SwarmSettings.from_mapping({"max_concurrent_boots": 2}).max_concurrent_boots == 2
    with pytest.raises(SwarmConfigError):
        SwarmSettings.from_mapping({"max_concurrent_boots": 0})


def test_a_shard_whose_device_failed_runs_once_more(tmp_path, capsys):
    config = _agent_config(tmp_path, workers=1)
    pool = CountingPool({"macos": 1})
    attempts = []

    def executor(shard, worker_id, work_dir, _config):
        attempts.append(shard.id)
        if len(attempts) == 1:
            return WorkerResult(worker_id=worker_id, shard_id=shard.id, shard_name=shard.name,
                                shard_kind=shard.kind, status="error",
                                error="BackendUnavailable: Simulator did not boot")
        return _passed(shard, worker_id)

    result = run_campaign(config, [scripted_shard()], executor=executor, device_pool=pool)
    assert result.results[0].status == "passed"
    assert len(attempts) == 2 and len(pool.acquired) == 2
    assert "running the shard again on a fresh lease" in capsys.readouterr().err


def test_an_app_error_is_not_retried(tmp_path):
    config = _agent_config(tmp_path, workers=1)
    pool = CountingPool({"macos": 1})
    attempts = []

    def executor(shard, worker_id, work_dir, _config):
        attempts.append(shard.id)
        return WorkerResult(worker_id=worker_id, shard_id=shard.id, shard_name=shard.name,
                            shard_kind=shard.kind, status="error", error="AQAError: runner internal")

    run_campaign(config, [scripted_shard()], executor=executor, device_pool=pool)
    assert len(attempts) == 1


def test_the_app_is_built_once_for_every_device(tmp_path):
    app = make_app(tmp_path / "built")
    config = _agent_config(tmp_path, workers=2)
    config.app.path = None
    config.app.bundle_id = None
    config.app.build_command = "build-it"
    calls = []

    def runner(args, **kwargs):
        from subprocess import CompletedProcess

        calls.append(args)
        return CompletedProcess(args, 0, f"** BUILD SUCCEEDED **\n{app.path}\n", "")

    swarm = Swarm.open(config, tmp_path, pool=FakeDevicePool({"macos": 2}), runner=runner)
    assert calls == [["/bin/sh", "-c", "build-it"]]
    assert config.app.path == app.path
    assert swarm.build is not None and swarm.build.app_path == app.path

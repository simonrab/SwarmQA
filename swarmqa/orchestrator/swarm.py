"""WP-D3: the swarm scheduler's parts.

With `[swarm] enabled = true` the campaign leases a device from a DevicePool
for every shard instead of sharing one configured simulator or the desktop:
worker slots are capped by the pool's capacity, the driver is built for the
leased device, and the lease is returned when the driver closes.

`[swarm] crawl = N` adds a crawl (an exploratory shard with no goal) split
across N devices. The N workers share one `SharedCrawl`: each publishes its
screen graph and claims a control before trying it, so no two devices try
the same control, and each can route to screens the others found.

After every campaign the worker screen graphs are merged into
`<campaign>/screen_graph.json`.

    [swarm]
    enabled = false
    pool = "auto"            # auto (from app.platform) | ios | macos | fake
    devices = 0              # cap on devices; 0 = the pool's capacity
    golden = ""              # ios: shut-down simulator (name or UDID) to clone
    device_type = ""         # ios: else create clones of this type (default "iPhone 17")
    runtime = ""             # ios: with device_type; default the newest
    erase_mode = "uninstall" # ios: uninstall | erase between leases
    keep_devices = true      # keep clones booted for the next campaign
    lease_timeout_s = 600
    crawl = 0                # crawl shards sharing one frontier; 0 = no crawl

Leases need thread isolation (`[local] isolation = "thread"`) on the local
backend: the pool and the shared crawl live in the campaign process.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from swarmqa.build.settings import _fill
from swarmqa.devices.protocol import Device, DevicePool
from swarmqa.errors import ConfigError
from swarmqa.explorer.screen_graph import Edge, ScreenGraph, ScreenNode
from swarmqa.models import CampaignConfig, Shard

POOLS = ("auto", "ios", "macos", "fake")
ERASE_MODES = ("uninstall", "erase")
CRAWL_TAG = "swarm-crawl"
GRAPH_FILE = "screen_graph.json"


class SwarmConfigError(ConfigError):
    """The `[swarm]` table has a bad key or value."""


@dataclass
class SwarmSettings:
    enabled: bool = False
    pool: str = "auto"
    devices: int = 0
    golden: str = ""
    device_type: str = ""
    runtime: str = ""
    erase_mode: str = "uninstall"
    keep_devices: bool = True
    lease_timeout_s: float = 600.0
    crawl: int = 0

    @classmethod
    def from_mapping(cls, data: dict[str, Any] | None) -> "SwarmSettings":
        errors: list[str] = []
        settings = _fill(cls(), dict(data or {}), "swarm", errors)
        if settings.pool not in POOLS:
            errors.append(f"swarm.pool: must be one of {', '.join(POOLS)}")
        if settings.erase_mode not in ERASE_MODES:
            errors.append(f"swarm.erase_mode: must be one of {', '.join(ERASE_MODES)}")
        for name in ("devices", "crawl"):
            value = getattr(settings, name)
            if isinstance(value, int) and value < 0:
                errors.append(f"swarm.{name}: must be >= 0")
        if isinstance(settings.lease_timeout_s, (int, float)) and settings.lease_timeout_s <= 0:
            errors.append("swarm.lease_timeout_s: must be > 0")
        if errors:
            raise SwarmConfigError(errors)
        return settings

    @classmethod
    def from_config(cls, config: CampaignConfig) -> "SwarmSettings":
        return cls.from_mapping(config.swarm.settings)

    def platform(self, config: CampaignConfig) -> str:
        return config.app.platform if self.pool in ("auto", "fake") else self.pool


def swarm_problems(settings: SwarmSettings, config: CampaignConfig) -> list[str]:
    """Settings that cannot work together with the rest of the config."""
    problems: list[str] = []
    if settings.crawl and config.explorer.engine != "agent":
        problems.append('swarm.crawl: needs explorer.engine = "agent"')
    if settings.enabled or settings.crawl:
        if config.backend != "local":
            problems.append("swarm: works with backend = local only")
        if config.local.isolation != "thread":
            problems.append('swarm: needs local.isolation = "thread"')
    if settings.enabled and settings.pool != "auto" and settings.pool != "fake" and settings.pool != config.app.platform:
        problems.append(f"swarm.pool: {settings.pool} does not match app.platform = {config.app.platform}")
    return problems


def build_pool(settings: SwarmSettings, config: CampaignConfig) -> DevicePool:
    """The DevicePool `[swarm]` describes."""
    if settings.pool == "fake":
        from swarmqa.devices.fake import FakeDevicePool

        return FakeDevicePool({config.app.platform: settings.devices or config.workers})
    if settings.platform(config) == "ios":
        from swarmqa.devices.ios_pool import IOSSimulatorPool

        return IOSSimulatorPool(
            golden=settings.golden or None,
            device_type=None if settings.golden else (settings.device_type or "iPhone 17"),
            runtime=settings.runtime or None,
            erase_mode=settings.erase_mode,  # type: ignore[arg-type]
        )
    from swarmqa.devices.local_mac import LocalMacPool

    return LocalMacPool()


def expand_crawl(queue: list[Shard], settings: SwarmSettings) -> list[Shard]:
    """`queue` plus `settings.crawl` crawl shards (no goal), all in one crawl group."""
    if settings.crawl <= 0 or any(CRAWL_TAG in shard.tags for shard in queue):
        return list(queue)
    count = settings.crawl
    crawl = [
        Shard(
            id=f"s-crawl-{index}",
            kind="exploratory",
            name=f"crawl {index}/{count}",
            tags=[CRAWL_TAG, "crawl_group:crawl"],
        )
        for index in range(1, count + 1)
    ]
    return [*queue, *crawl]


class SharedCrawl:
    """Claims and a merged screen graph shared by the workers of one crawl.

    `claim` hands each (screen, control) to the first worker that asks.
    `exchange` publishes a worker's graph and copies back what it lacks:
    screens and routes found by others (imported with zero visits and
    counts, so merged coverage is not inflated), and every control someone
    else has claimed or tried, marked tried.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.graph = ScreenGraph()
        self.claims: dict[tuple[str, str], str] = {}

    def claim(self, screen_id: str, key: str, worker_id: str) -> bool:
        with self._lock:
            owner = self.claims.setdefault((screen_id, key), worker_id)
            return owner == worker_id

    def exchange(self, local: ScreenGraph, worker_id: str) -> None:
        with self._lock:
            self._absorb(local)
            self._feed(local, worker_id)

    def _absorb(self, local: ScreenGraph) -> None:
        shared = self.graph
        if shared.start is None:
            shared.start = local.start
        for node in sorted(local.nodes.values(), key=lambda n: n.order):
            mine = shared.nodes.get(node.id)
            if mine is None:
                mine = ScreenNode(id=node.id, screenshot=node.screenshot, order=len(shared.nodes))
                shared.nodes[node.id] = mine
            known = {c.key for c in mine.controls}
            mine.controls.extend(c for c in node.controls if c.key not in known)
            mine.tried |= node.tried
        for key, edge in local.edges.items():
            if key not in shared.edges:
                shared.edges[key] = Edge(edge.source, dict(edge.action), edge.target, 0)

    def _feed(self, local: ScreenGraph, worker_id: str) -> None:
        for node in sorted(self.graph.nodes.values(), key=lambda n: n.order):
            mine = local.nodes.get(node.id)
            if mine is None:
                mine = ScreenNode(id=node.id, screenshot=node.screenshot, order=len(local.nodes), visits=0)
                local.nodes[node.id] = mine
            known = {c.key for c in mine.controls}
            mine.controls.extend(replace(c) for c in node.controls if c.key not in known)
            mine.tried |= node.tried
        for (screen_id, key), owner in self.claims.items():
            if owner != worker_id and screen_id in local.nodes:
                local.nodes[screen_id].tried.add(key)
        for key, edge in self.graph.edges.items():
            if key not in local.edges:
                local.edges[key] = Edge(edge.source, dict(edge.action), edge.target, 0)


class SwarmContext:
    """What the local backend needs from the swarm: shared crawls by group."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._crawls: dict[str, SharedCrawl] = {}

    def crawl_for(self, shard: Shard) -> SharedCrawl | None:
        group = next((tag.split(":", 1)[1] for tag in shard.tags if tag.startswith("crawl_group:")), None)
        if group is None:
            return None
        with self._lock:
            return self._crawls.setdefault(group, SharedCrawl())


class LeasedDriver:
    """A driver bound to a leased device; closing it returns the lease (once)."""

    def __init__(self, driver: Any, pool: DevicePool, device: Device):
        self._driver = driver
        self._pool = pool
        self.device = device
        self._released = False
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._driver, name)

    def close(self) -> None:
        try:
            close = getattr(self._driver, "close", None)
            if close is not None:
                close()
        finally:
            with self._lock:
                released, self._released = self._released, True
            if not released:
                self._pool.release(self.device)


class LeasingDriverFactory:
    """`driver_factory(target, work_dir)` that leases a device per driver.

    A leased simulator is handed to the driver as its UDID (and as
    `app.simulator` for injected factories); the pool holds that
    simulator's host-wide lock, so the driver must not take it again.
    """

    def __init__(
        self,
        pool: DevicePool,
        platform: str,
        config: CampaignConfig,
        *,
        base: Callable | None = None,
        timeout_s: float = 600.0,
    ):
        self.pool = pool
        self.platform = platform
        self.config = config
        self.base = base
        self.timeout_s = timeout_s

    def __call__(self, target, work_dir: Path):
        device = self.pool.acquire(self.platform, None, timeout_s=self.timeout_s)  # type: ignore[arg-type]
        try:
            udid = device.id if device.kind == "simulator" else None
            leased = replace(target, simulator=udid, simulators=[udid]) if udid else target
            if self.base is not None:
                driver = self.base(leased, work_dir)
            else:
                from swarmqa.driver import create_driver

                kind = None if self.config.driver.kind == "auto" else self.config.driver.kind
                driver = create_driver(leased, work_dir, kind=kind, video_mode=self.config.video.mode, udid=udid)
        except BaseException:
            self.pool.release(device)
            raise
        return LeasedDriver(driver, self.pool, device)


def merge_worker_graphs(root: Path) -> dict[str, Any] | None:
    """Merge `workers/*/screen_graph.json` into `<root>/screen_graph.json`; return its coverage."""
    paths = sorted(Path(root).glob(f"workers/*/{GRAPH_FILE}"))
    if not paths:
        return None
    merged = ScreenGraph()
    for path in paths:
        try:
            merged.merge(ScreenGraph.load(path))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"warning: could not merge {path}: {exc}", file=sys.stderr)
    merged.save(Path(root) / GRAPH_FILE)
    return merged.coverage()

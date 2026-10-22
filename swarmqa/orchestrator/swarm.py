"""WP-D3 — swarm scheduler: device pools, a shared crawl frontier, one campaign graph.

    [swarm]
    pool = "ios"              # "none" (default), "ios", "tart", "local_mac" or "auto"
    devices = 6               # devices to run at once (default: the pool's capacity)
    golden = "iPhone 17"      # iOS: shut-down simulator to clone (name or UDID)
    device_type = "iPhone 17" # iOS: `simctl create` device type when there is no golden
    runtime = "iOS-26-5"      # iOS: runtime for `simctl create`
    erase_mode = "uninstall"  # iOS: "uninstall" (fast) or "erase" (cold boot per lease)
    image = "macos-base"      # tart: VM image to clone
    acquire_timeout_s = 600   # how long a shard waits for a free device
    erase_on_release = true   # wipe app state when a lease ends
    share_frontier = true     # crawl workers split untried controls (default: on with a pool)
    merge_graphs = true       # merge screen graphs and seed later workers (default: on with a pool)

With `pool = "none"` and neither `share_frontier` nor `merge_graphs` set, the
campaign runs exactly as it did before the swarm existed. Unknown keys raise
`SwarmConfigError`. See docs/swarm.md.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Mapping

from swarmqa.devices.protocol import (
    Device,
    DevicePool,
    DeviceSetupError,
    DeviceUnavailable,
    lease,
)
from swarmqa.errors import ConfigError
from swarmqa.explorer.screen_graph import ScreenGraph
from swarmqa.models import BuildArtifact, CampaignConfig, Shard, WorkerResult

POOLS = ("none", "ios", "tart", "local_mac", "auto")
ERASE_MODES = ("uninstall", "erase")
DEVICE_KINDS = frozenset({"scripted", "exploratory", "visual"})
GRAPH_FILE = "screen_graph.json"
LEASE_STEP = "lease"

Pair = tuple[str, str]


class SwarmConfigError(ConfigError):
    """The `[swarm]` table has a bad key or value."""


@dataclass
class SwarmSettings:
    pool: str = "none"
    devices: int | None = None
    golden: str | None = None
    device_type: str | None = None
    runtime: str | None = None
    erase_mode: str = "uninstall"
    image: str | None = None
    acquire_timeout_s: float = 600.0
    # Simulators booting at once; None means 1 under 16 GB of RAM, else 2.
    max_concurrent_boots: int | None = None
    erase_on_release: bool = True
    # None means "on when a pool is configured".
    share_frontier: bool | None = None
    merge_graphs: bool | None = None

    @property
    def uses_pool(self) -> bool:
        return self.pool != "none"

    @property
    def frontier_shared(self) -> bool:
        return self.uses_pool if self.share_frontier is None else self.share_frontier

    @property
    def graphs_merged(self) -> bool:
        return self.uses_pool if self.merge_graphs is None else self.merge_graphs

    @property
    def active(self) -> bool:
        return self.uses_pool or self.frontier_shared or self.graphs_merged

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "SwarmSettings":
        data = dict(data or {})
        known = {item.name for item in fields(cls)}
        errors = [f"swarm.{key}: unknown key" for key in sorted(set(data) - known)]
        settings = cls()
        for key, value in data.items():
            if key not in known:
                continue
            name = f"swarm.{key}"
            if key == "pool":
                if value not in POOLS:
                    errors.append(f"{name}: must be one of {', '.join(POOLS)}")
                    continue
            elif key == "erase_mode":
                if value not in ERASE_MODES:
                    errors.append(f"{name}: must be one of {', '.join(ERASE_MODES)}")
                    continue
            elif key in {"devices", "max_concurrent_boots"}:
                if type(value) is not int or value < 1:
                    errors.append(f"{name}: must be an integer of at least 1")
                    continue
            elif key == "acquire_timeout_s":
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                    errors.append(f"{name}: must be a number greater than 0")
                    continue
                value = float(value)
            elif key in {"erase_on_release", "share_frontier", "merge_graphs"}:
                if not isinstance(value, bool):
                    errors.append(f"{name}: must be true or false")
                    continue
            elif not isinstance(value, str) or not value.strip():
                errors.append(f"{name}: must be a non-empty string")
                continue
            setattr(settings, key, value)
        if errors:
            raise SwarmConfigError(errors)
        return settings

    @classmethod
    def from_config(cls, config: Any) -> "SwarmSettings":
        swarm = getattr(config, "swarm", None)
        table = getattr(swarm, "settings", None) or {}
        settings = cls.from_mapping(table)
        if "pool" not in table and _wants_ios_pool(config):
            # More iOS workers than listed simulators: clone the simulators
            # rather than make the workers share one.
            settings.pool = "auto"
        return settings


def resolve_pool_kind(config: CampaignConfig, settings: SwarmSettings) -> str:
    """The concrete pool for `settings.pool`; "auto" picks one from `app.platform`."""
    kind = settings.pool
    if kind == "auto":
        if config.app.platform == "ios":
            return "ios"
        return "tart" if settings.image else "local_mac"
    return kind


def pool_errors(config: CampaignConfig, settings: SwarmSettings) -> list[str]:
    """Why the configured pool cannot serve this campaign; [] when it can (or there is none)."""
    kind = resolve_pool_kind(config, settings)
    platform = config.app.platform
    if kind == "none":
        return []
    if kind == "ios":
        if platform != "ios":
            return [f"swarm.pool: \"ios\" needs app.platform = \"ios\", not {platform!r}"]
        return []
    if platform != "macos":
        return [f"swarm.pool: {kind!r} needs app.platform = \"macos\", not {platform!r}"]
    if kind == "tart" and not settings.image:
        return ["swarm.image: required when swarm.pool = \"tart\""]
    return []


def create_pool(
    config: CampaignConfig,
    settings: SwarmSettings,
    *,
    runner: Any = None,
) -> DevicePool | None:
    """Build the pool `settings` names, or None for `pool = "none"`.

    Constructing a pool is cheap: simulators and VMs are cloned and booted on
    the first lease. Raises `SwarmConfigError` when the pool cannot serve
    `app.platform` or is missing a required value (see `pool_errors`).
    """
    errors = pool_errors(config, settings)
    if errors:
        raise SwarmConfigError(errors)
    kind = resolve_pool_kind(config, settings)
    if kind == "none":
        return None
    if kind == "ios":
        from swarmqa.devices.capacity import CapacityPolicy
        from swarmqa.devices.ios_pool import IOSSimulatorPool

        golden = settings.golden
        device_type = settings.device_type
        if not golden and not device_type:
            golden, device_type = _default_ios_source(config)
        return IOSSimulatorPool(
            golden=golden,
            device_type=device_type,
            runtime=settings.runtime,
            runner=runner,
            policy=CapacityPolicy(max_simulators=settings.devices),
            erase_mode=settings.erase_mode,  # type: ignore[arg-type]
            max_concurrent_boots=settings.max_concurrent_boots,
        )
    if kind == "local_mac":
        from swarmqa.devices.local_mac import LocalMacPool

        return LocalMacPool(runner=runner)
    if kind == "tart":
        from swarmqa.devices.capacity import CapacityPolicy
        from swarmqa.devices.tart_pool import TartVMPool

        return TartVMPool(image=settings.image, runner=runner, policy=CapacityPolicy(max_vms=settings.devices))
    raise SwarmConfigError([f"swarm.pool: unknown pool {kind!r}"])


def _wants_ios_pool(config: Any) -> bool:
    """A local iOS campaign with more workers than `app.simulators` names."""
    app = getattr(config, "app", None)
    if app is None or getattr(app, "platform", None) != "ios" or getattr(config, "backend", "local") != "local":
        return False
    workers = int(getattr(config, "workers", 1) or 1)
    listed = [name for name in (getattr(app, "simulators", None) or []) if str(name).strip()]
    return workers > 1 and len(listed) < workers


def _default_ios_source(config: CampaignConfig) -> tuple[str | None, str | None]:
    """With no golden or device_type, clone `app.simulator` if it is a UDID, else create one of that type."""
    from swarmqa.driver.ios import _DEFAULT_DEVICE, _is_udid

    name = (config.app.simulator or "").strip() or _DEFAULT_DEVICE
    if _is_udid(name):
        return name, None
    return None, name


def build_once(config: CampaignConfig, *, runner: Any = None) -> None:
    """Run `app.build_command` and point `app.path` (and `app.bundle_id`) at the `.app` it reports.

    Raises `DeviceSetupError` when the build fails or reports no app, so the
    campaign stops before leasing devices for an app that does not exist.
    """
    from swarmqa.devices import commands
    from swarmqa.driver.ios import _discover_app_path
    from swarmqa.driver.runner_client import read_info_plist

    print("building the app once for every device: app.build_command", file=sys.stderr, flush=True)
    result = commands.run(
        runner if runner is not None else commands.default_runner,
        ["/bin/sh", "-c", str(config.app.build_command)],
        timeout=1800,
    )
    if not result.ok:
        raise DeviceSetupError(f"app.build_command failed: {result.detail()}")
    found = _discover_app_path(result.stdout + "\n" + result.stderr)
    if not found or not Path(found).is_dir():
        raise DeviceSetupError("app.build_command finished but did not report a .app path; set app.path")
    config.app.path = str(found)
    if not config.app.bundle_id:
        bundle_id = str(read_info_plist(Path(found)).get("CFBundleIdentifier") or "").strip()
        config.app.bundle_id = bundle_id or None


def build_artifact(config: CampaignConfig) -> BuildArtifact | None:
    """The build to install on each leased device: `app.path`, or None when unset."""
    if not config.app.path:
        return None
    return BuildArtifact(
        platform=config.app.platform,
        app_path=str(config.app.path),
        bundle_id=config.app.bundle_id or "",
    )


class FrontierBoard:
    """Campaign-wide claims on untried controls, so crawl workers split the frontier.

    The first worker to claim `(screen_id, control_key)` owns it; everyone
    else is refused. When a worker finishes, `settle` keeps the claims it
    actually tried and frees the rest, so a later worker can take them.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owners: dict[Pair, str] = {}
        self._refused: dict[str, set[Pair]] = {}
        self.granted: list[tuple[str, str, str]] = []

    def claim(self, worker_id: str, screen_id: str, control_key: str) -> bool:
        pair = (screen_id, control_key)
        with self._lock:
            owner = self._owners.get(pair)
            if owner is None:
                self._owners[pair] = worker_id
                self.granted.append((worker_id, screen_id, control_key))
                return True
            if owner == worker_id:
                return True
            self._refused.setdefault(worker_id, set()).add(pair)
            return False

    def for_worker(self, worker_id: str) -> Callable[[str, str], bool]:
        def claim(screen_id: str, control_key: str) -> bool:
            return self.claim(worker_id, screen_id, control_key)

        return claim

    def owned(self, worker_id: str) -> set[Pair]:
        with self._lock:
            return {pair for pair, owner in self._owners.items() if owner == worker_id}

    def refused(self, worker_id: str) -> set[Pair]:
        with self._lock:
            return set(self._refused.get(worker_id, ()))

    def settle(self, worker_id: str, tried: set[Pair] | None) -> set[Pair]:
        """Free the worker's claims it never tried (all of them when `tried` is None).

        Returns the freed pairs.
        """
        with self._lock:
            freed = {
                pair
                for pair, owner in self._owners.items()
                if owner == worker_id and (tried is None or pair not in tried)
            }
            for pair in freed:
                self._owners.pop(pair, None)
            return freed


def tried_pairs(graph: ScreenGraph) -> set[Pair]:
    return {(node.id, key) for node in graph.nodes.values() for key in node.tried}


class CampaignGraph:
    """The campaign's merged screen graph, saved to `<campaign>/screen_graph.json`."""

    def __init__(self, path: Path, graph: ScreenGraph | None = None):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.graph = graph or ScreenGraph()
        self.merged_workers: list[str] = []

    @classmethod
    def open(cls, root: Path) -> "CampaignGraph":
        path = Path(root) / GRAPH_FILE
        graph = None
        if path.is_file():
            try:
                graph = ScreenGraph.load(path)
            except (OSError, TypeError, ValueError, KeyError):
                graph = None
        return cls(path, graph)

    def snapshot(self) -> ScreenGraph | None:
        """A copy for a new worker's `seed_graph`, or None while nothing is mapped."""
        with self._lock:
            if not self.graph.nodes:
                return None
            return ScreenGraph.from_dict(self.graph.to_dict())

    def merge_worker(self, worker_id: str, graph: ScreenGraph, *, untried: set[Pair] = frozenset()) -> None:
        """Merge one worker's graph. `untried` are pairs it marked tried only
        because another worker held the claim; they stay untried here."""
        for screen_id, key in untried:
            node = graph.nodes.get(screen_id)
            if node is not None:
                node.tried.discard(key)
        with self._lock:
            self.graph.merge(graph)
            self.merged_workers.append(worker_id)

    def coverage(self) -> dict[str, Any]:
        with self._lock:
            return self.graph.coverage()

    def save(self) -> Path | None:
        with self._lock:
            if not self.graph.nodes:
                return None
            data = self.graph.to_dict()
        from swarmqa.serialize import dump_json

        dump_json(data, self.path)
        return self.path


@dataclass
class ShardContext:
    """What the swarm hands one shard: its leased device and crawl hooks."""

    device: Device | None = None
    seed_graph: ScreenGraph | None = None
    claim: Callable[[str, str], bool] | None = None


@dataclass
class Swarm:
    """One campaign's pool, frontier board and merged graph.

    `run` wraps a backend's `run_shard`: it leases a device for interactive
    shards, builds the shard's context, and turns lease failures into error
    results. `finish` folds a finished worker back into the campaign.
    """

    config: CampaignConfig
    settings: SwarmSettings
    root: Path
    pool: DevicePool | None = None
    in_process: bool = True
    board: FrontierBoard | None = None
    graph: CampaignGraph | None = None
    build: BuildArtifact | None = None
    devices_leased: list[str] = field(default_factory=list)
    _capacity: int | None = None
    _closed: bool = False

    @classmethod
    def open(
        cls,
        config: CampaignConfig,
        root: Path,
        *,
        settings: SwarmSettings | None = None,
        pool: DevicePool | None = None,
        runner: Any = None,
    ) -> "Swarm":
        settings = settings or SwarmSettings.from_config(config)
        in_process = config.backend == "local" and config.local.isolation != "subprocess"
        if pool is None and settings.uses_pool and config.backend == "local":
            pool = create_pool(config, settings, runner=runner)
        elif pool is None and settings.uses_pool:
            print(
                f"warning: swarm.pool = {settings.pool!r} is used only by the local backend; "
                f"the {config.backend} backend brings its own machines",
                file=sys.stderr,
                flush=True,
            )
        swarm = cls(config=config, settings=settings, root=Path(root), pool=pool, in_process=in_process)
        if in_process and settings.frontier_shared:
            swarm.board = FrontierBoard()
        if in_process and settings.graphs_merged:
            swarm.graph = CampaignGraph.open(swarm.root)
        if pool is not None and not config.app.path and (config.app.build_command or "").strip():
            # Build once for every device instead of once per worker.
            build_once(config, runner=runner)
        swarm.build = build_artifact(config)
        return swarm

    @property
    def active(self) -> bool:
        return self.pool is not None or self.board is not None or self.graph is not None

    @property
    def platform(self) -> str:
        return self.config.app.platform

    def capacity(self) -> int:
        """Devices the pool runs at once, capped by `devices`; 0 when it cannot serve the platform."""
        if self.pool is None:
            return 0
        if self._capacity is None:
            try:
                capacity = int(self.pool.capacity(self.platform))  # type: ignore[arg-type]
            except Exception as exc:
                print(f"warning: reading the device pool's capacity failed: {exc}", file=sys.stderr, flush=True)
                capacity = 1
            if self.settings.devices is not None:
                capacity = min(capacity, self.settings.devices)
            self._capacity = max(0, capacity)
        return self._capacity

    def slots(self, workers: int) -> int:
        """Worker slots: `workers`, capped by the devices the pool can run at once."""
        slots = max(1, int(workers))
        if self.pool is None:
            return slots
        return max(1, min(slots, self.capacity()))

    def context(self, shard: Shard, worker_id: str, device: Device | None = None) -> ShardContext:
        context = ShardContext(device=device)
        if shard.kind != "exploratory" or not self.in_process:
            return context
        if self.graph is not None:
            context.seed_graph = self.graph.snapshot()
        if self.board is not None and _is_crawl(shard):
            context.claim = self.board.for_worker(worker_id)
        return context

    def run(
        self,
        run_shard: Callable[..., WorkerResult],
        shard: Shard,
        worker_id: str,
        *,
        on_step: Callable[[str, str, str, str], None] | None = None,
        make_error: Callable[[Shard, str, str], WorkerResult],
    ) -> WorkerResult:
        """Run one shard, leased on a device when the pool covers its kind.

        A shard whose device failed under it (an error result starting with
        `BackendUnavailable`) runs once more on a fresh lease: the device was
        at fault, not the app.
        """
        if self.pool is None or shard.kind not in DEVICE_KINDS:
            return run_shard(shard, worker_id, context=self.context(shard, worker_id))
        if self.capacity() <= 0:
            return make_error(shard, worker_id, f"no {self.platform} device available: the pool has none")
        result = self._run_leased(run_shard, shard, worker_id, on_step=on_step, make_error=make_error)
        if result.status == "error" and (result.error or "").startswith("BackendUnavailable"):
            print(
                f"warning: {worker_id}: the device failed ({result.error.splitlines()[0]}); "
                "running the shard again on a fresh lease",
                file=sys.stderr,
                flush=True,
            )
            result = self._run_leased(run_shard, shard, worker_id, on_step=on_step, make_error=make_error, erase=True)
        return result

    def _run_leased(
        self,
        run_shard: Callable[..., WorkerResult],
        shard: Shard,
        worker_id: str,
        *,
        on_step: Callable[[str, str, str, str], None] | None,
        make_error: Callable[[Shard, str, str], WorkerResult],
        erase: bool | None = None,
    ) -> WorkerResult:
        if on_step is not None:
            on_step(worker_id, shard.name, shard.kind, LEASE_STEP)
        held: list[Device] = []
        result: WorkerResult | None = None
        try:
            with lease(
                self.pool,
                self.platform,  # type: ignore[arg-type]
                self.build,
                timeout_s=self.settings.acquire_timeout_s,
                erase=self.settings.erase_on_release if erase is None else erase,
            ) as device:
                held.append(device)
                self.devices_leased.append(device.id)
                result = run_shard(shard, worker_id, context=self.context(shard, worker_id, device))
        except DeviceSetupError as exc:
            if not held:
                return make_error(shard, worker_id, f"device setup failed: {exc}")
            raise
        except DeviceUnavailable as exc:
            if not held:
                return make_error(shard, worker_id, f"no {self.platform} device available: {exc}")
            raise
        except Exception as exc:
            if result is None:
                raise
            # The shard ran; only the release failed. Keep the result.
            print(f"warning: releasing device for {worker_id} failed: {exc}", file=sys.stderr, flush=True)
        assert result is not None
        return result

    def finish(self, worker_id: str, work_dir: Path) -> None:
        """Settle a finished worker's claims and merge its screen graph."""
        graph = _load_graph(Path(work_dir) / GRAPH_FILE)
        refused: set[Pair] = set()
        if self.board is not None:
            refused = self.board.refused(worker_id)
            self.board.settle(worker_id, tried_pairs(graph) if graph is not None else None)
        if self.graph is not None and graph is not None:
            self.graph.merge_worker(worker_id, graph, untried=refused)
            try:
                self.graph.save()
            except OSError as exc:
                print(f"warning: saving the campaign screen graph failed: {exc}", file=sys.stderr, flush=True)

    def summary_lines(self) -> list[str]:
        """Markdown for summary.md, or [] when there is nothing to report."""
        lines: list[str] = []
        if self.pool is not None:
            lines += [
                "## Devices",
                "",
                f"- pool: {resolve_pool_kind(self.config, self.settings) if self.settings.uses_pool else 'custom'}",
                f"- leases: {len(self.devices_leased)}",
                f"- distinct devices: {len(set(self.devices_leased))}",
                "",
            ]
        if self.graph is not None and self.graph.merged_workers:
            stats = self.graph.coverage()
            lines += [
                "## Screen graph",
                "",
                f"- screens: {stats['screens']}",
                f"- controls tried: {stats['tried']} of {stats['controls']} ({stats['ratio']:.0%})",
                f"- frontier screens: {stats['frontier']}",
                f"- merged from: {', '.join(self.graph.merged_workers)}",
                f"- file: `{GRAPH_FILE}`",
                "",
            ]
        return lines

    def __enter__(self) -> "Swarm":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the pool (releasing every lease) and save the merged graph. Idempotent."""
        if self.graph is not None and self.graph.merged_workers:
            try:
                self.graph.save()
            except OSError as exc:
                print(f"warning: saving the campaign screen graph failed: {exc}", file=sys.stderr, flush=True)
        if self._closed:
            return
        self._closed = True
        if self.pool is not None:
            try:
                self.pool.close()
            except Exception as exc:
                print(f"warning: closing the device pool failed: {exc}", file=sys.stderr, flush=True)


def _is_crawl(shard: Shard) -> bool:
    # The agent loop crawls when the shard has no goal (AgentLoopSettings.mode
    # is not set from config).
    return not (shard.goal or "").strip()


def _load_graph(path: Path) -> ScreenGraph | None:
    if not path.is_file():
        return None
    try:
        return ScreenGraph.load(path)
    except (OSError, TypeError, ValueError, KeyError):
        return None

"""C9b — pluggable cloud backend and the local cost simulator.

See docs/CONTRACTS.md section C9 and docs/backends.md.

A future paid Mac host implements `RunnerBackend` and registers itself with
`register_cloud_adapter`. `create_cloud_backend` selects it from
`config.cloud.adapter`. Callers such as the orchestrator stay unchanged.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable

from swarmqa.errors import ChunkNotReady
from swarmqa.models import CampaignConfig, Shard, WorkerResult
from swarmqa.spend import SpendMeter

CloudFactory = Callable[[CampaignConfig], Any]
ShardExecutor = Callable[[Shard, str], WorkerResult]

_ADAPTERS: dict[str, CloudFactory] = {}


def register_cloud_adapter(name: str, factory: CloudFactory) -> None:
    """Publish a cloud adapter under `config.cloud.adapter`.

    The factory returns an object with `RunnerBackend`'s methods:
    `cost_per_worker_minute`, `run_shard`, and `cancel`.
    """
    if not name or name in {"simulator", "sim"}:
        raise ValueError(f"cloud adapter name {name!r} is reserved")
    _ADAPTERS[name] = factory


def create_cloud_backend(config: CampaignConfig):
    if config.cloud.adapter in {"simulator", "sim", ""}:
        return SimulatedCloudBackend(config)
    factory = _ADAPTERS.get(config.cloud.adapter)
    if factory is not None:
        return factory(config)
    raise ChunkNotReady(
        "C9",
        f"cloud adapter {config.cloud.adapter} is not registered. "
        "A paid Mac host implements RunnerBackend and is selected via cloud.adapter.",
    )


def plan_affordable(
    shards: list[Shard],
    meter: SpendMeter,
    rate: float,
    minutes: float,
) -> tuple[list[Shard], str | None]:
    """Return the shards that fit under the cap, and a stop reason.

    Each shard is priced at `rate * minutes` before it starts. Pass
    `config.cloud.estimated_shard_minutes` (default 1) so the pre-check
    matches the orchestrator's estimate. A shard that fits is reserved with
    `SpendMeter.add`. The first shard that does not fit stops scheduling:
    later shards stay unscheduled and the stop reason is `spend_cap`.
    `meter.cap is None` allows every shard.
    """
    cost = float(rate) * float(minutes)
    runnable: list[Shard] = []
    stop_reason: str | None = None
    for shard in shards:
        decision = meter.can_start(cost)
        if not decision.allowed:
            stop_reason = decision.stop_reason or "spend_cap"
            break
        meter.add(cost)
        runnable.append(shard)
    return runnable, stop_reason


class SimulatedCloudBackend:
    """Metered adapter that runs shards through an executor or LocalBackend."""

    name = "cloud"

    def __init__(
        self,
        config: CampaignConfig,
        *,
        executor: ShardExecutor | None = None,
    ):
        self.config = config
        self.executor = executor
        self.cancelled: set[str] = set()

    def cost_per_worker_minute(self) -> float:
        return float(self.config.cloud.cost_per_worker_minute)

    def run_shard(self, shard: Shard, worker_id: str) -> WorkerResult:
        """Run one shard and set `estimated_cost` from the measured minutes.

        The pre-check price is `plan_affordable`'s `rate * minutes` (at least
        one `estimated_shard_minutes`). This method bills the minutes that
        actually elapsed, or the executor's `worker_minutes` when that is
        larger. An executor failure becomes status `error` for this shard.
        """
        started = time.perf_counter()
        started_at = _now()
        try:
            produced = self._execute(shard, worker_id)
        except ChunkNotReady:
            raise
        except Exception as exc:
            produced = _error_result(shard, worker_id, exc, started_at=started_at)
        if not isinstance(produced, WorkerResult):
            produced = WorkerResult(
                worker_id=worker_id,
                shard_id=shard.id,
                status="passed",
                started_at=started_at,
                backend="cloud",
                shard_name=shard.name,
                shard_kind=shard.kind,
            )
        elapsed = max(0.0, (time.perf_counter() - started) / 60.0)
        measured = max(elapsed, float(produced.worker_minutes or 0))
        produced.worker_id = produced.worker_id or worker_id
        produced.shard_id = produced.shard_id or shard.id
        produced.shard_name = produced.shard_name or shard.name
        produced.shard_kind = produced.shard_kind or shard.kind
        produced.worker_minutes = measured
        produced.estimated_cost = self.cost_per_worker_minute() * measured
        produced.backend = "cloud"
        produced.started_at = produced.started_at or started_at
        produced.finished_at = produced.finished_at or _now()
        return produced

    def cancel(self, worker_id: str) -> None:
        """Record and forward cancel when overrun policy is cancel.

        Drain does not stop the worker. A local backend that is still a stub
        is skipped after the id is recorded.
        """
        if not _policy_cancels(self.config):
            return
        self.cancelled.add(worker_id)
        from swarmqa.backends.local import LocalBackend

        try:
            LocalBackend(self.config).cancel(worker_id)
        except ChunkNotReady:
            return

    def _execute(self, shard: Shard, worker_id: str) -> WorkerResult:
        if self.executor is not None:
            return self.executor(shard, worker_id)
        from swarmqa.backends.local import LocalBackend

        try:
            return LocalBackend(self.config).run_shard(shard, worker_id)
        except ChunkNotReady as exc:
            raise ChunkNotReady(
                "C9",
                "LocalBackend.run_shard is not available. "
                "Pass executor= to SimulatedCloudBackend until the local backend lands.",
            ) from exc


def _policy_cancels(config: CampaignConfig) -> bool:
    return config.spend.overrun == "cancel" or config.budgets.on_budget == "cancel"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_result(
    shard: Shard,
    worker_id: str,
    exc: BaseException,
    *,
    started_at: str,
) -> WorkerResult:
    return WorkerResult(
        worker_id=worker_id,
        shard_id=shard.id,
        status="error",
        error=str(exc),
        started_at=started_at,
        finished_at=_now(),
        backend="cloud",
        shard_name=shard.name,
        shard_kind=shard.kind,
    )

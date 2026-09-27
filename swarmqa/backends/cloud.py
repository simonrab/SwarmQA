"""C9b — pluggable cloud backend and the local cost simulator.

See docs/CONTRACTS.md section C9.
"""

from __future__ import annotations

from swarmqa.errors import ChunkNotReady
from swarmqa.models import CampaignConfig, Shard, WorkerResult


class SimulatedCloudBackend:
    """Metered adapter that runs shards locally and reports a configured rate."""

    name = "cloud"

    def __init__(self, config: CampaignConfig):
        self.config = config

    def cost_per_worker_minute(self) -> float:
        return float(self.config.cloud.cost_per_worker_minute)

    def run_shard(self, shard: Shard, worker_id: str) -> WorkerResult:
        raise ChunkNotReady("C9", "SimulatedCloudBackend.run_shard")

    def cancel(self, worker_id: str) -> None:
        raise ChunkNotReady("C9", "SimulatedCloudBackend.cancel")


def create_cloud_backend(config: CampaignConfig):
    if config.cloud.adapter in {"simulator", "sim", ""}:
        return SimulatedCloudBackend(config)
    raise ChunkNotReady("C9", f"cloud adapter {config.cloud.adapter}")

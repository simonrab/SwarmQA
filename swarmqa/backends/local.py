"""C8 — local worker backend.

See docs/CONTRACTS.md section C8.
"""

from __future__ import annotations

from swarmqa.errors import ChunkNotReady
from swarmqa.models import CampaignConfig, Shard, WorkerResult


class LocalBackend:
    name = "local"

    def __init__(self, config: CampaignConfig):
        self.config = config

    def cost_per_worker_minute(self) -> float:
        return 0.0

    def run_shard(self, shard: Shard, worker_id: str) -> WorkerResult:
        raise ChunkNotReady("C8", "LocalBackend.run_shard")

    def cancel(self, worker_id: str) -> None:
        raise ChunkNotReady("C8", "LocalBackend.cancel")

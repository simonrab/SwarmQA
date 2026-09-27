"""C9a — Tart VM backend.

See docs/CONTRACTS.md section C9.
"""

from __future__ import annotations

from swarmqa.errors import ChunkNotReady
from swarmqa.models import CampaignConfig, Shard, WorkerResult


class TartBackend:
    name = "vm"

    def __init__(self, config: CampaignConfig, runner=None):
        self.config = config
        self.runner = runner

    def cost_per_worker_minute(self) -> float:
        return float(self.config.vm.cost_per_worker_minute)

    def run_shard(self, shard: Shard, worker_id: str) -> WorkerResult:
        raise ChunkNotReady("C9", "TartBackend.run_shard")

    def cancel(self, worker_id: str) -> None:
        raise ChunkNotReady("C9", "TartBackend.cancel")

    def ensure_available(self) -> None:
        """Raise BackendUnavailable with guidance to use backend=local."""
        raise ChunkNotReady("C9", "TartBackend.ensure_available")

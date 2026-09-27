"""Backend protocol shared by local, Tart, and cloud adapters."""

from __future__ import annotations

from typing import Protocol

from swarmqa.models import Shard, WorkerResult


class RunnerBackend(Protocol):
    name: str

    def cost_per_worker_minute(self) -> float:
        """Estimated currency units per worker-minute. Zero means unmetered."""

    def run_shard(self, shard: Shard, worker_id: str) -> WorkerResult:
        """Execute one shard. Raise if the worker process itself crashes."""

    def cancel(self, worker_id: str) -> None:
        """Stop an in-flight shard when overrun policy is cancel."""

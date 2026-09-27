"""C5 — goal-directed exploration inside step and time budgets.

See docs/CONTRACTS.md section C5.
"""

from __future__ import annotations

from pathlib import Path

from swarmqa.errors import ChunkNotReady
from swarmqa.models import CampaignConfig, Shard, WorkerResult


def run_exploratory(
    shard: Shard,
    driver,
    config: CampaignConfig,
    *,
    worker_id: str,
    work_dir: Path,
) -> WorkerResult:
    """Hunt within explorer.max_steps and explorer.max_time_s."""
    raise ChunkNotReady("C5", "swarmqa.explorer.exploratory.run_exploratory")

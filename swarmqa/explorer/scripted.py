"""C4 — execute a scripted shard against one driver.

See docs/CONTRACTS.md section C4.
"""

from __future__ import annotations

from pathlib import Path

from swarmqa.errors import ChunkNotReady
from swarmqa.models import CampaignConfig, Shard, WorkerResult


def run_scripted(
    shard: Shard,
    driver,
    config: CampaignConfig,
    *,
    worker_id: str,
    work_dir: Path,
) -> WorkerResult:
    """Run shard.actions. Record step pass/fail and findings on failure."""
    raise ChunkNotReady("C4", "swarmqa.explorer.scripted.run_scripted")

"""C8 — schedule shards across workers and merge one campaign report.

See docs/CONTRACTS.md section C8.
"""

from __future__ import annotations

from typing import Callable

from swarmqa.errors import ChunkNotReady
from swarmqa.models import CampaignConfig, CampaignResult, RunOptions, Shard


def run_campaign(
    config: CampaignConfig,
    queue: list[Shard],
    *,
    options: RunOptions | None = None,
    driver_factory: Callable | None = None,
) -> CampaignResult:
    """Run up to config.workers sessions. Merge artifacts. Survive a worker crash."""
    raise ChunkNotReady("C8", "swarmqa.orchestrator.campaign.run_campaign")

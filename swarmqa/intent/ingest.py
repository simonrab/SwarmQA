"""C3 — turn NL markdown, JSON flows, and suite commands into shards.

See docs/CONTRACTS.md section C3. Keep `build_queue`'s signature.
"""

from __future__ import annotations

from swarmqa.errors import ChunkNotReady
from swarmqa.models import CampaignConfig, Shard


def build_queue(config: CampaignConfig) -> list[Shard]:
    """Return shard units for one campaign.

    Raise IntentError when the intent set is empty or paths are missing.
    Mix markdown, JSON flows, and config.suite.command in one queue.
    """
    raise ChunkNotReady("C3", "swarmqa.intent.ingest.build_queue")

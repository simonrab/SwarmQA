"""C8 — live campaign status file.

See docs/CONTRACTS.md section C8.
"""

from __future__ import annotations

from pathlib import Path

from swarmqa.errors import ChunkNotReady
from swarmqa.models import CampaignStatus


def write_status(status: CampaignStatus, report_dir: Path) -> Path:
    """Write report_dir/status.json."""
    raise ChunkNotReady("C8", "swarmqa.orchestrator.status.write_status")


def read_status(report_root: Path, campaign_id: str | None = None) -> str:
    """Return a human-readable status block for `aqa status`."""
    raise ChunkNotReady("C8", "swarmqa.orchestrator.status.read_status")

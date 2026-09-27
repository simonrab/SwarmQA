"""C4 — GitHub and Linear issue creation.

Always write local ticket files even when trackers are disabled.
See docs/CONTRACTS.md section C4.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from swarmqa.errors import ChunkNotReady
from swarmqa.models import CampaignConfig, Finding, IssueRef


def create_issues(
    findings: list[Finding],
    config: CampaignConfig,
    campaign_dir: Path,
    *,
    runner: Callable | None = None,
    http_post: Callable | None = None,
) -> list[IssueRef]:
    """Create tracker issues when enabled. Include local IssueRefs always."""
    raise ChunkNotReady("C4", "swarmqa.reporter.issues.create_issues")

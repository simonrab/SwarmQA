"""C7 — one campaign-level fix loop.

See docs/CONTRACTS.md section C7. Workers must not call this themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from swarmqa.errors import ChunkNotReady
from swarmqa.models import CampaignConfig, CampaignResult, FixLoopResult, FixProposal


def run_fix_loop(
    result: CampaignResult,
    config: CampaignConfig,
    *,
    repo: Path,
    fixer: Callable[..., FixProposal] | None = None,
    retest: Callable[..., CampaignResult] | None = None,
    gh_runner: Callable | None = None,
) -> FixLoopResult:
    """HITL writes one draft and returns. Autonomous iterates until green or a cap."""
    raise ChunkNotReady("C7", "swarmqa.prloop.loop.run_fix_loop")

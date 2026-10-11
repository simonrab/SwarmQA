"""Findings people can trust (WP-F2): confirmation re-runs and grouping.

Contract (lead-owned signature; WP-F2 owns the body and the rest of this package):

`trust_findings(findings, results, campaign_dir, config, *, driver_factory=None)`
is called by the orchestrator after within-run dedup and before the evidence
pipeline writes `findings.json`. It returns the findings to report, and keeps
each `WorkerResult.findings` list consistent with them (the summary and the
exit code read those lists). It sets `Finding.confirmation`,
`confirm_attempts`, `confirm_hits` and `occurrences` (see `swarmqa.models`).
It must never raise: on any internal error it returns `findings` unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from swarmqa.models import CampaignConfig, Finding, WorkerResult


def trust_findings(
    findings: list[Finding],
    results: list[WorkerResult],
    campaign_dir: Path,
    config: CampaignConfig,
    *,
    driver_factory: Callable | None = None,
) -> list[Finding]:
    return findings

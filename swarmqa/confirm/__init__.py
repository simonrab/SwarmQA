"""Findings people can trust (WP-F2): confirmation re-runs and grouping.

`trust_findings(findings, results, campaign_dir, config, *, driver_factory=None)`
is called by the orchestrator after within-run dedup and before the evidence
pipeline writes `findings.json`. It returns the findings to report, and keeps
each `WorkerResult.findings` list consistent with them (the summary and the
exit code read those lists). It sets `Finding.confirmation`,
`confirm_attempts`, `confirm_hits` and `occurrences` (see `swarmqa.models`).
It must never raise: on any internal error it returns `findings` unchanged.

Order: group first (fewer replays), then re-run the representatives. A
failed shard whose every finding did not reproduce is marked passed, so it
no longer fails the run. See docs/trust.md.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Callable

from swarmqa.confirm.settings import FindingsSettings
from swarmqa.models import CampaignConfig, Finding, WorkerResult


def trust_findings(
    findings: list[Finding],
    results: list[WorkerResult],
    campaign_dir: Path,
    config: CampaignConfig,
    *,
    driver_factory: Callable | None = None,
) -> list[Finding]:
    """Group repeats, re-run what is left, and return the findings to report."""
    saved = None
    try:
        saved = _snapshot(findings, results)
        return _trust(list(findings), results, Path(campaign_dir), config, driver_factory)
    except Exception as exc:  # noqa: BLE001 - trust is best effort; never fail the campaign
        if saved is not None:
            _restore(saved)
        print(f"warning: finding confirmation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return findings


def _trust(findings, results, campaign_dir: Path, config, driver_factory) -> list[Finding]:
    from swarmqa.confirm.group import group_findings
    from swarmqa.confirm.rerun import confirm_findings

    settings = FindingsSettings.from_config(config)
    original = {id(result): [(item, item.advisory) for item in result.findings] for result in results}
    folded: dict[int, Finding] = {}
    if settings.group:
        findings, folded = group_findings(findings, settings.group_min)
    reason = _cannot_confirm(config, driver_factory) if settings.confirm else None
    if settings.confirm and reason is None:
        confirm_findings(findings, campaign_dir, config, settings, driver_factory)
    elif settings.confirm:
        print(f"note: findings not re-run: {reason}", file=sys.stderr)
    downgraded = {id(item) for item in findings if item.confirmation == "not_reproduced"}
    if settings.drop_not_reproduced:
        findings = [item for item in findings if id(item) not in downgraded]
    kept = {id(item) for item in findings}
    removed = set(folded) | (downgraded - kept)
    for result in results:
        result.findings = [item for item in result.findings if id(item) not in removed]
        _settle_status(result, original.get(id(result), []), folded, downgraded)
    return findings


def _cannot_confirm(config: CampaignConfig, driver_factory: Callable | None) -> str | None:
    if driver_factory is None and config.backend != "local":
        return f"re-runs need the local backend (backend is {config.backend})"
    return None


def _settle_status(result: WorkerResult, original, folded: dict[int, Finding], downgraded: set[int]) -> None:
    """A failed shard whose findings all failed to reproduce no longer fails."""
    if result.status != "failed" or not original or any(advisory for _item, advisory in original):
        return
    if all(id(folded.get(id(item), item)) in downgraded for item, _advisory in original):
        result.status = "passed"


def _snapshot(findings: list[Finding], results: list[WorkerResult]):
    every = {id(item): item for item in findings}
    every.update({id(item): item for result in results for item in result.findings})
    return (
        [(item, copy.deepcopy(item.__dict__)) for item in every.values()],
        [(result, list(result.findings), result.status) for result in results],
    )


def _restore(saved) -> None:
    items, results = saved
    for item, state in items:
        item.__dict__.clear()
        item.__dict__.update(state)
    for result, findings, status in results:
        result.findings = findings
        result.status = status

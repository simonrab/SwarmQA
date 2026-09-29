"""Turn a campaign's raw findings into the final evidence set.

`finalize_findings` runs, in order: within-run dedup, repro files, frames
and video clips, the source map, the cross-run state store, and then
rewrites `findings/<id>.md` and writes `findings.json`. Dedup is in place:
the returned findings are the first input finding of each fingerprint,
enriched, so a `CampaignResult` that already holds them shows the evidence.
See docs/evidence.md.
"""

from __future__ import annotations

from pathlib import Path

from swarmqa.models import CampaignConfig, Finding
from swarmqa.report.clips import collect_frames, trim_clip
from swarmqa.report.dedup import dedup_findings
from swarmqa.report.findings_json import write_findings_json
from swarmqa.report.paths import Runner
from swarmqa.report.repro import ensure_repro
from swarmqa.report.source_map import map_finding
from swarmqa.report.state import mark_seen
from swarmqa.reporter.findings import write_finding


def finalize_findings(
    campaign_dir: Path,
    campaign_id: str,
    findings: list[Finding],
    *,
    config: CampaignConfig,
    repo: Path | None = None,
    sha: str | None = None,
    runner: Runner | None = None,
    state_root: Path | None = None,
    full_run: bool = True,
    ffmpeg: str | None = None,
) -> list[Finding]:
    """Dedup and enrich `findings`, write their files, and return them.

    `repo` is the target app's checkout: it turns on the source map and is
    the default `state_root` for `.aqa/state/findings.json`. With neither,
    the cross-run store is skipped. `full_run=False` (a partial or stopped
    campaign) keeps missing fingerprints from being marked fixed. `runner`
    runs `git` and `ffmpeg`; `ffmpeg` is the binary (None: look on PATH,
    "": no clips). Evidence steps are best effort: a failure is noted in
    `environment["evidence_errors"]` and the rest still runs. `config` is
    accepted for future knobs and is not read yet.
    """
    campaign_dir = Path(campaign_dir)
    merged = dedup_findings(findings, in_place=True)
    for finding in merged:
        _safely(finding, "repro", lambda f=finding: ensure_repro(campaign_dir, f))
        _safely(finding, "frames", lambda f=finding: collect_frames(campaign_dir, f))
        _safely(finding, "clip", lambda f=finding: trim_clip(campaign_dir, f, runner=runner, ffmpeg=ffmpeg))
        if repo is not None:
            _safely(finding, "source_map", lambda f=finding: map_finding(f, Path(repo), campaign_dir=campaign_dir, runner=runner))
    root = state_root if state_root is not None else repo
    if root is not None:
        try:
            mark_seen(merged, campaign_id, root=Path(root), full_run=full_run)
        except OSError as exc:
            for finding in merged:
                _note(finding, f"state: {exc}")
    for finding in merged:
        write_finding(finding, campaign_dir)
    write_findings_json(campaign_dir, campaign_id, merged, sha=sha)
    return merged


def _safely(finding: Finding, step: str, action) -> None:
    try:
        action()
    except Exception as exc:  # noqa: BLE001 - evidence is best effort
        _note(finding, f"{step}: {type(exc).__name__}: {exc}")


def _note(finding: Finding, message: str) -> None:
    current = finding.environment.get("evidence_errors", "")
    finding.environment["evidence_errors"] = f"{current}; {message}" if current else message

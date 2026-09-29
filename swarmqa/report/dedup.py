"""Merge findings that share a fingerprint.

Parallel workers keep every evidence link and every worker id. The first
title, severity, and narrative stay; later duplicates contribute media.
With `in_place=True` the first finding of each fingerprint is updated and
returned instead of a copy, so callers holding it see the merge.
"""

from __future__ import annotations

import copy

from swarmqa.models import Finding


def dedup_findings(findings: list[Finding], *, in_place: bool = False) -> list[Finding]:
    merged: dict[str, Finding] = {}
    order: list[str] = []
    for finding in findings:
        key = finding.fingerprint
        current = merged.get(key)
        if current is None:
            merged[key] = finding if in_place else _clone(finding)
            order.append(key)
            continue
        if finding is current:
            continue
        for worker_id in finding.worker_ids or [finding.worker_id]:
            if worker_id and worker_id not in current.worker_ids:
                current.worker_ids.append(worker_id)
        for shot in finding.screenshots:
            if shot not in current.screenshots:
                current.screenshots.append(shot)
        if finding.video and finding.video != current.video:
            extra = current.environment.setdefault("extra_videos", "")
            videos = [part for part in extra.split(",") if part]
            if finding.video not in videos and finding.video != current.video:
                videos.append(finding.video)
            current.environment["extra_videos"] = ",".join(videos)
        if finding.replay_json and not current.replay_json:
            current.replay_json = finding.replay_json
        for frame in finding.evidence.frames:
            if frame not in current.evidence.frames:
                current.evidence.frames.append(frame)
        if finding.evidence.video_clip and not current.evidence.video_clip:
            current.evidence.video_clip = finding.evidence.video_clip
        if finding.repro and not current.repro:
            current.repro = finding.repro
        for source in finding.suspected_sources:
            if source not in current.suspected_sources:
                current.suspected_sources.append(source)
        # A duplicate confirmed without a model makes the merged finding firm.
        if not finding.advisory:
            current.advisory = False
        current.confidence = max(current.confidence, finding.confidence)
        if finding.details and finding.details not in current.details:
            current.details = (current.details + "\n" + finding.details).strip()
    return [merged[key] for key in order]


def _clone(finding: Finding) -> Finding:
    return copy.deepcopy(finding)

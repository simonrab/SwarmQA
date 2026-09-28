"""Merge findings that share a fingerprint.

Parallel workers keep every evidence link and every worker id. The first
title, severity, and narrative stay; later duplicates contribute media.
"""

from __future__ import annotations

from swarmqa.models import Finding


def dedup_findings(findings: list[Finding]) -> list[Finding]:
    merged: dict[str, Finding] = {}
    order: list[str] = []
    for finding in findings:
        key = finding.fingerprint
        current = merged.get(key)
        if current is None:
            cloned = _clone(finding)
            merged[key] = cloned
            order.append(key)
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
        if finding.details and finding.details not in current.details:
            current.details = (current.details + "\n" + finding.details).strip()
    return [merged[key] for key in order]


def _clone(finding: Finding) -> Finding:
    return Finding(
        id=finding.id,
        title=finding.title,
        severity=finding.severity,
        kind=finding.kind,
        steps=list(finding.steps),
        fingerprint=finding.fingerprint,
        worker_id=finding.worker_id,
        backend=finding.backend,
        shard_id=finding.shard_id,
        screenshots=list(finding.screenshots),
        video=finding.video,
        replay_json=finding.replay_json,
        environment=dict(finding.environment),
        details=finding.details,
        worker_ids=list(finding.worker_ids),
    )

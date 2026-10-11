"""Fold findings that are the same problem in different places into one.

Two findings match when they have the same `kind`, `category` and
`advisory`, and the same title once element names are taken out: quoted
names (`button "Share"`), screen points (`at (120, 40)`), and, for layout
findings without quotes, the text after the first colon. Crashes and launch
failures never fold: a crash in another place is usually another bug.
"""

from __future__ import annotations

import re

from swarmqa.models import Finding

NEVER_GROUP = {"crash", "launch"}
MAX_SCREENSHOTS = 5
_QUOTED = re.compile(r'"[^"]*"')
_POINT = re.compile(r"\bat \(-?\d+(?:\.\d+)?, -?\d+(?:\.\d+)?\)")
_SEVERITY = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def group_key(finding: Finding) -> tuple[str, str, bool, str] | None:
    """The key findings fold on, or None when this finding never folds."""
    if finding.kind in NEVER_GROUP:
        return None
    return (finding.kind, str(finding.category), bool(finding.advisory), _shape(finding))


def _shape(finding: Finding) -> str:
    title = finding.title
    shaped = _POINT.sub("at (…)", _QUOTED.sub('"…"', title))
    if shaped == title and finding.kind == "visual" and ": " in title:
        shaped = title.split(": ", 1)[0] + ": …"
    return shaped


def place(finding: Finding) -> str:
    """A short name for where this finding was seen: its element names, else its screen point."""
    names = _QUOTED.findall(finding.title)
    if names:
        return " / ".join(names)
    point = _POINT.search(finding.title)
    if point:
        return point.group(0)
    if finding.kind == "visual" and ": " in finding.title:
        return finding.title.split(": ", 1)[1]
    env = finding.environment or {}
    return env.get("target_label") or env.get("target_identifier") or finding.shard_id or finding.id


def rank(finding: Finding) -> tuple:
    """Sort key: most severe, then firm before advisory, then most confident."""
    return (_SEVERITY.get(finding.severity, 9), bool(finding.advisory), -float(finding.confidence or 0))


def group_findings(findings: list[Finding], group_min: int = 2) -> tuple[list[Finding], dict[int, Finding]]:
    """Fold matching findings. Returns the kept findings (in input order) and a
    map from `id()` of each folded-away finding to its representative."""
    buckets: dict[tuple, list[Finding]] = {}
    for finding in findings:
        key = group_key(finding)
        if key is not None:
            buckets.setdefault(key, []).append(finding)
    folded: dict[int, Finding] = {}
    for members in buckets.values():
        if len(members) < max(2, group_min):
            continue
        ordered = sorted(members, key=lambda item: (rank(item), item.fingerprint, item.id))
        head, rest = ordered[0], ordered[1:]
        _merge(head, rest)
        folded.update({id(item): head for item in rest})
    return [item for item in findings if id(item) not in folded], folded


def _merge(head: Finding, rest: list[Finding]) -> None:
    head.severity = min((item.severity for item in [head, *rest]), key=lambda value: _SEVERITY.get(value, 9))
    head.confidence = max(float(item.confidence or 0) for item in [head, *rest])
    for item in rest:
        head.occurrences.extend([place(item), *item.occurrences])
        head.worker_ids.extend(worker for worker in item.worker_ids if worker not in head.worker_ids)
        for shot in item.screenshots:
            if len(head.screenshots) >= MAX_SCREENSHOTS:
                break
            if shot not in head.screenshots:
                head.screenshots.append(shot)
    head.title = f"{head.title} (+{len(head.occurrences)} more)"
    shown = ", ".join(head.occurrences[:10]) + (", …" if len(head.occurrences) > 10 else "")
    note = f"Same problem in {len(head.occurrences) + 1} places. Also seen at: {shown}."
    head.details = f"{head.details}\n\n{note}" if head.details else note

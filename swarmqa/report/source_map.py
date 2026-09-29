"""Map a finding to suspected source files in the target app's repo.

Searches with `git grep -n -I -F --full-name` for the element's accessibility
identifier as a quoted string literal (`"home.settings"`), then for its exact
label (`"Refresh"`). Quoting matches Swift and Objective-C literals,
storyboard and xib attributes, and `.strings` keys while skipping partial
words. Hits in `.swift`, `.m`, `.storyboard`, `.xib`, and `.strings` files
rank first; at most 5 `path:line` entries go to `Finding.suspected_sources`.

The identifier and label come from `environment["target_identifier"]` and
`environment["target_label"]`, falling back to the target of the last
targeted step in the finding's replay flow.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from swarmqa.models import Finding
from swarmqa.report.paths import Runner, default_runner, resolve

MAX_SOURCES = 5
PREFERRED_SUFFIXES = (".swift", ".m", ".storyboard", ".xib", ".strings")
_HIT = re.compile(r"^(?P<path>.+?):(?P<line>\d+):")


def finding_targets(finding: Finding, campaign_dir: Path | None = None) -> tuple[str, str]:
    """`(identifier, label)` for the element the finding is about; empty strings when unknown."""
    environment = finding.environment or {}
    identifier = environment.get("target_identifier", "") or ""
    label = environment.get("target_label", "") or ""
    if (identifier or label) or campaign_dir is None or not finding.replay_json:
        return identifier, label
    try:
        document = json.loads(resolve(campaign_dir, finding.replay_json).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", ""
    for step in reversed(document.get("steps", [])):
        target = step.get("target") if isinstance(step, dict) else None
        if isinstance(target, dict) and (target.get("identifier") or target.get("label")):
            return str(target.get("identifier") or ""), str(target.get("label") or "")
    return "", ""


def git_grep(repo: Path, needle: str, *, runner: Runner | None = None) -> list[str]:
    """`path:line` hits for a fixed string; empty when not a repo or nothing matched."""
    args = ["git", "-C", str(repo), "grep", "-n", "-I", "-F", "--full-name", "-e", needle]
    try:
        completed = (runner or default_runner)(args)
    except (OSError, ValueError):
        return []
    if getattr(completed, "returncode", 1) != 0:
        return []
    hits: list[str] = []
    for line in (getattr(completed, "stdout", "") or "").splitlines():
        match = _HIT.match(line)
        if match:
            hits.append(f"{match.group('path')}:{match.group('line')}")
    return hits


def rank(hits: list[str]) -> list[str]:
    """Preferred file types first, in `PREFERRED_SUFFIXES` order; stable otherwise."""

    def weight(hit: str) -> int:
        path = hit.rsplit(":", 1)[0].lower()
        for index, suffix in enumerate(PREFERRED_SUFFIXES):
            if path.endswith(suffix):
                return index
        return len(PREFERRED_SUFFIXES)

    return sorted(hits, key=weight)


def suspected_sources(
    repo: Path,
    identifier: str,
    label: str,
    *,
    runner: Runner | None = None,
    limit: int = MAX_SOURCES,
) -> list[str]:
    """Identifier hits first, then label hits, de-duplicated and capped at `limit`."""
    found: list[str] = []
    for needle in (identifier, label):
        needle = (needle or "").strip()
        if len(needle) < 2:
            continue
        for hit in rank(git_grep(repo, f'"{needle}"', runner=runner)):
            if hit not in found:
                found.append(hit)
        if len(found) >= limit:
            break
    return found[:limit]


def map_finding(
    finding: Finding,
    repo: Path,
    *,
    campaign_dir: Path | None = None,
    runner: Runner | None = None,
) -> list[str]:
    """Fill `finding.suspected_sources` (keeping existing entries first) and return it."""
    identifier, label = finding_targets(finding, campaign_dir)
    if not identifier and not label:
        return finding.suspected_sources
    for hit in suspected_sources(repo, identifier, label, runner=runner):
        if hit not in finding.suspected_sources and len(finding.suspected_sources) < MAX_SOURCES:
            finding.suspected_sources.append(hit)
    return finding.suspected_sources

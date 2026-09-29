"""C4 — write per-finding artifacts and render issue bodies.

See docs/CONTRACTS.md section C4.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from swarmqa.models import Finding
from swarmqa.serialize import dump_json

_PLACEHOLDER = re.compile(r"\{\{([^{}]+)\}\}")


def fingerprint_for(kind: str, title: str, target: str = "") -> str:
    """Stable 16-hex fingerprint for dedup. See CONTRACTS."""
    payload = f"{kind}\n{title.strip().lower()}\n{target.strip().lower()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def write_finding(finding: Finding, campaign_dir: Path) -> Path:
    """Write findings/<id>.md and return that path.

    The built-in template is followed by a Triage section with the v2
    fields: category, confidence, advisory, repro, clip, frames, and
    suspected sources.
    """
    destination = Path(campaign_dir) / "findings" / f"{finding.id}.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    text = render_issue(default_template(), finding)
    if not text.endswith("\n"):
        text += "\n"
    text += "\n" + render_triage(finding)
    destination.write_text(text, encoding="utf-8")
    return destination


def write_replay(finding_id: str, campaign_dir: Path, steps: list[dict]) -> Path:
    """Write findings/<id>.replay.json as a version-1 flow."""
    destination = Path(campaign_dir) / "findings" / f"{finding_id}.replay.json"
    document = {
        "version": 1,
        "name": finding_id or "replay",
        "steps": [_drop_none(step) for step in steps],
    }
    dump_json(document, destination)
    return destination


def render_issue(template: str, finding: Finding) -> str:
    """Fill {{placeholders}} from the finding. Leave unknown tokens unchanged."""
    values = _placeholder_values(finding)

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token in values:
            return values[token]
        return match.group(0)

    return _PLACEHOLDER.sub(replace, template)


def render_triage(finding: Finding) -> str:
    """Markdown block with the findings-v2 triage fields."""
    evidence = finding.evidence
    lines = [
        "## Triage",
        "",
        f"Category: {finding.category or ''}",
        f"Confidence: {finding.confidence:.2f}",
        f"Advisory: {'yes (model-only)' if finding.advisory else 'no'}",
        f"Repro: {finding.repro or 'none'}",
        f"Clip: {evidence.video_clip or 'none'}",
    ]
    if finding.environment.get("seen_before") == "true":
        lines.append(f"Seen before: yes, first seen {finding.environment.get('first_seen', '')}".rstrip())
    lines.extend(["", "Frames:", ""])
    lines.extend([f"- {frame}" for frame in evidence.frames] or ["none"])
    lines.extend(["", "Suspected sources:", ""])
    lines.extend([f"- {source}" for source in finding.suspected_sources] or ["none"])
    return "\n".join(lines) + "\n"


def default_template() -> str:
    """Return the built-in issue template text."""
    path = Path(__file__).resolve().parents[1] / "templates" / "issue.md"
    return path.read_text(encoding="utf-8")


def _placeholder_values(finding: Finding) -> dict[str, str]:
    environment = finding.environment or {}
    return {
        "title": finding.title or "",
        "severity": finding.severity or "",
        "kind": finding.kind or "",
        "steps": "\n".join(finding.steps or []),
        "video": finding.video or "",
        "screenshots": "\n".join(finding.screenshots or []),
        "replay_json": finding.replay_json or "",
        "environment": "\n".join(f"{key}: {value}" for key, value in environment.items()),
        "worker_id": finding.worker_id or "",
        "backend": finding.backend or "",
        "build_id": "" if environment.get("version") in (None, "") else str(environment.get("version")),
        "fingerprint": finding.fingerprint or "",
        "details": finding.details or "",
        "category": finding.category or "",
        "confidence": f"{finding.confidence:.2f}",
        "advisory": "true" if finding.advisory else "false",
        "repro": finding.repro or "",
        "video_clip": finding.evidence.video_clip or "",
        "frames": "\n".join(finding.evidence.frames or []),
        "suspected_sources": "\n".join(finding.suspected_sources or []),
    }


def _drop_none(value):
    if isinstance(value, dict):
        return {key: _drop_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_drop_none(item) for item in value]
    return value

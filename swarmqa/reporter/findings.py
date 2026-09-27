"""C4 — write per-finding artifacts and render issue bodies.

See docs/CONTRACTS.md section C4.
"""

from __future__ import annotations

from pathlib import Path

from swarmqa.errors import ChunkNotReady
from swarmqa.models import Finding


def fingerprint_for(kind: str, title: str, target: str = "") -> str:
    """Stable 16-hex fingerprint for dedup. See CONTRACTS."""
    raise ChunkNotReady("C4", "swarmqa.reporter.findings.fingerprint_for")


def write_finding(finding: Finding, campaign_dir: Path) -> Path:
    """Write findings/<id>.md and return that path."""
    raise ChunkNotReady("C4", "swarmqa.reporter.findings.write_finding")


def write_replay(finding_id: str, campaign_dir: Path, steps: list[dict]) -> Path:
    """Write findings/<id>.replay.json."""
    raise ChunkNotReady("C4", "swarmqa.reporter.findings.write_replay")


def render_issue(template: str, finding: Finding) -> str:
    """Fill {{placeholders}} from the finding. Leave unknown tokens unchanged."""
    raise ChunkNotReady("C4", "swarmqa.reporter.findings.render_issue")


def default_template() -> str:
    """Return the built-in issue template text."""
    raise ChunkNotReady("C4", "swarmqa.reporter.findings.default_template")

"""C6 — compare a screenshot to a baseline.

See docs/CONTRACTS.md section C6.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from swarmqa.errors import ChunkNotReady


@dataclass
class DiffResult:
    name: str
    passed: bool
    score: float
    diff_path: Path | None
    message: str = ""


def compare_screenshot(
    current: Path,
    baseline: Path,
    diff_out: Path,
    *,
    threshold: float,
) -> DiffResult:
    """Compare images. Write a highlighted diff. Never modify `baseline`."""
    raise ChunkNotReady("C6", "swarmqa.visual.diff.compare_screenshot")

"""C6 — explicit baseline promotion.

See docs/CONTRACTS.md section C6.
"""

from __future__ import annotations

from pathlib import Path

from swarmqa.errors import ChunkNotReady


def update_baselines(source_dir: Path, baseline_dir: Path) -> list[Path]:
    """Copy PNG files from source_dir into baseline_dir. Return written paths."""
    raise ChunkNotReady("C6", "swarmqa.visual.baseline.update_baselines")

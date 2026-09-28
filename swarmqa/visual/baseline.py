"""C6 — explicit baseline promotion.

See docs/CONTRACTS.md section C6 and docs/visual.md.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def update_baselines(source_dir: Path, baseline_dir: Path) -> list[Path]:
    """Copy PNG files from source_dir into baseline_dir. Return written paths."""
    source = Path(source_dir)
    destination = Path(baseline_dir)
    if not source.is_dir():
        raise FileNotFoundError(f"source directory not found: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for source_path in sorted(path for path in source.glob("*.png") if path.is_file()):
        target = destination / source_path.name
        if source_path.resolve() != target.resolve():
            shutil.copyfile(source_path, target)
        written.append(target)
    return written

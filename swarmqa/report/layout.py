"""Stable campaign directory layout.

Every worker and the merged report use these paths. Writers add files inside
the directories; they do not invent sibling folders.
"""

from __future__ import annotations

from pathlib import Path


LAYOUT_DIRS = ("workers", "findings", "media", "raw", "pr")


def campaign_dir(report_root: Path, campaign_id: str) -> Path:
    return report_root / campaign_id


def ensure_campaign_layout(root: Path) -> dict[str, Path]:
    """Create `summary` siblings and the standard subdirectories.

    Returns absolute paths keyed by `root`, `summary_md`, `summary_json`,
    `status_json`, and each layout directory name.
    """
    root.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {
        "root": root,
        "summary_md": root / "summary.md",
        "summary_json": root / "summary.json",
        "status_json": root / "status.json",
    }
    for name in LAYOUT_DIRS:
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        paths[name] = path
    return paths


def worker_dir(root: Path, worker_id: str) -> Path:
    path = root / "workers" / worker_id
    path.mkdir(parents=True, exist_ok=True)
    return path
